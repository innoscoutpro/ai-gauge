from datetime import datetime, timedelta, timezone

import pytest

from aigauge.config import Config
from aigauge.history import PeriodRecord
from aigauge.local_usage import summaries
from aigauge.local_usage.claude_logs import ClaudeMessage
from aigauge.local_usage.codex_logs import CodexQuotaReading, CodexUsage
from aigauge.local_usage.store import UsageStore
from aigauge.local_usage.summaries import (
    FLAG_INCOMPLETE,
    FLAG_LIMIT_REACHED,
    FLAG_TIME_UNCERTAIN,
    ORIGIN_BACKFILL,
    ORIGIN_LIVE,
    load_summaries,
    update_all,
    usage_from_json,
    usage_to_json,
)
from aigauge.local_usage.tokens import TokenCounts

UTC = timezone.utc


def _local(dt: datetime) -> str:
    return dt.astimezone().replace(tzinfo=None).isoformat()


@pytest.fixture
def store(tmp_path):
    s = UsageStore(tmp_path / "local_usage.sqlite")
    yield s
    s.close()


@pytest.fixture
def config():
    c = Config()
    c.local_usage.enabled = True
    c.local_usage.claude.account_id = "claude"
    c.local_usage.codex.account_id = "codex"
    return c


def _claude_message(store, msg_id, when, output, model="claude-opus-5"):
    with store.transaction() as conn:
        store.upsert_claude_messages(
            conn,
            [ClaudeMessage(msg_id, "req", when, model, TokenCounts(output=output),
                           "standard", "standard", "2.1.268", False)],
        )


def _record(label, resets, last_seen, pct, account="claude"):
    return PeriodRecord(account, label, _local(resets), _local(last_seen - timedelta(hours=1)),
                        _local(last_seen), pct)


def test_usage_json_round_trip():
    from aigauge.local_usage.store import ModelUsage

    rows = [ModelUsage("gpt-5.5", "long", TokenCounts(input=5, output=7), 2),
            ModelUsage("gpt-5.5", "", TokenCounts(cache_read=9), 1)]

    assert sorted(usage_from_json(usage_to_json(rows)), key=lambda r: r.variant) == sorted(
        rows, key=lambda r: r.variant
    )


def test_claude_live_summary_counts_window_start_to_last_reading(store, config):
    resets = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    _claude_message(store, "before", datetime(2026, 9, 10, 10, 30, tzinfo=UTC), 5000)
    _claude_message(store, "inside", datetime(2026, 9, 10, 12, 0, tzinfo=UTC), 1000)
    _claude_message(store, "after-reading", datetime(2026, 9, 10, 15, 58, tzinfo=UTC), 700)
    store.add_coverage("claude", datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    record = _record("Session", resets, datetime(2026, 9, 10, 15, 55, tzinfo=UTC), 20)

    update_all(store, config, claude_current=[record], now=datetime(2026, 9, 10, 15, 56, tzinfo=UTC))

    [summary] = load_summaries(store, "claude")
    assert summary.window_start == datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    assert sum(u.tokens.output for u in summary.usage) == 1000
    assert (summary.last_pct, summary.closed, summary.origin) == (20.0, False, ORIGIN_LIVE)
    assert summary.flags == set()


def test_claude_jittered_resets_update_the_same_window_then_finalize(store, config):
    resets = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    store.add_coverage("claude", datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    first = _record("Session", resets, datetime(2026, 9, 10, 13, 0, tzinfo=UTC), 10)
    later = _record("Session", resets + timedelta(minutes=4), datetime(2026, 9, 10, 15, 0, tzinfo=UTC), 25)

    update_all(store, config, claude_current=[first])
    update_all(store, config, claude_current=[later])
    update_all(store, config, claude_closed=[later])

    [summary] = load_summaries(store, "claude")
    assert summary.last_pct == 25.0
    assert summary.closed


def test_claude_backfill_only_covered_closed_periods(store, config):
    store.add_coverage("claude", datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    covered = _record("Session", datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
                      datetime(2026, 9, 10, 15, 50, tzinfo=UTC), 40)
    uncovered = _record("Session", datetime(2026, 8, 1, 16, 0, tzinfo=UTC),
                        datetime(2026, 8, 1, 15, 50, tzinfo=UTC), 40)
    other_account = _record("Session", datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
                            datetime(2026, 9, 10, 20, 50, tzinfo=UTC), 40, account="claude-2")

    update_all(store, config, history=[covered, uncovered, other_account])

    [summary] = load_summaries(store, "claude")
    assert summary.origin == ORIGIN_BACKFILL
    assert summary.closed
    assert load_summaries(store, "claude-2") == []


def test_live_summary_is_not_replaced_by_backfill(store, config):
    store.add_coverage("claude", datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    record = _record("Session", datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
                     datetime(2026, 9, 10, 15, 50, tzinfo=UTC), 40)

    update_all(store, config, claude_closed=[record])
    update_all(store, config, history=[record])

    [summary] = load_summaries(store, "claude")
    assert summary.origin == ORIGIN_LIVE


def test_window_before_coverage_is_incomplete(store, config):
    store.add_coverage("claude", datetime(2026, 9, 10, 12, 0, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    record = _record("Session", datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
                     datetime(2026, 9, 10, 15, 50, tzinfo=UTC), 40)

    update_all(store, config, claude_current=[record])

    assert FLAG_INCOMPLETE in load_summaries(store, "claude")[0].flags


def test_window_spanning_a_clock_change_is_time_uncertain(store, config, monkeypatch):
    store.add_coverage("claude", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    resets = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    record = _record("Session", resets, datetime(2026, 9, 10, 15, 50, tzinfo=UTC), 40)
    resets_local = datetime.fromisoformat(record.resets_at)
    monkeypatch.setattr(
        summaries, "local_offset",
        lambda naive: timedelta(hours=1) if naive == resets_local else timedelta(0),
    )

    update_all(store, config, claude_current=[record])

    assert FLAG_TIME_UNCERTAIN in load_summaries(store, "claude")[0].flags


def test_limit_reached_is_flagged(store, config):
    store.add_coverage("claude", datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    record = _record("Weekly", datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
                     datetime(2026, 9, 10, 15, 50, tzinfo=UTC), 100)

    update_all(store, config, claude_current=[record])

    assert FLAG_LIMIT_REACHED in load_summaries(store, "claude", "Weekly")[0].flags


def _codex_rows(store, usage, readings):
    with store.transaction() as conn:
        store.insert_codex_usage(conn, 1, list(enumerate(usage)))
        store.insert_codex_readings(conn, 1, list(enumerate(readings)))


def test_codex_summaries_come_from_log_readings(store, config):
    resets = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
    at = lambda h, m: datetime(2026, 9, 10, h, m, tzinfo=UTC)  # noqa: E731
    _codex_rows(
        store,
        [
            CodexUsage(at(13, 0), "s", "gpt-5.5", TokenCounts(output=900)),  # before window
            CodexUsage(at(14, 30), "s", "gpt-5.5", TokenCounts(output=100)),
            CodexUsage(at(15, 0), "s", "gpt-5.5", TokenCounts(output=50)),  # same line as reading
            CodexUsage(at(15, 30), "s", "gpt-5.5", TokenCounts(output=70)),  # after last reading
        ],
        [
            CodexQuotaReading(at(14, 30), "codex", "primary", 5.0, 300, resets, "team"),
            CodexQuotaReading(at(15, 0), "codex", "primary", 8.0, 300, resets + timedelta(seconds=2), "team"),
            CodexQuotaReading(at(15, 0), "premium", "primary", 30.0, 10080, resets + timedelta(days=3), "team"),
        ],
    )
    store.add_coverage("codex", datetime(2026, 9, 9, tzinfo=UTC), at(16, 0))

    update_all(store, config, now=at(16, 0))

    session = load_summaries(store, "codex", "Session")
    assert len(session) == 1
    assert session[0].last_pct == 8.0
    assert sum(u.tokens.output for u in session[0].usage) == 150
    assert not session[0].closed
    assert session[0].period_id.endswith("|team")
    premium = load_summaries(store, "codex", "Weekly (premium)")
    assert premium[0].limit_id == "premium"
    assert premium[0].base_metric == "Weekly"

    update_all(store, config, now=at(20, 0))
    assert load_summaries(store, "codex", "Session")[0].closed


def test_codex_plan_change_starts_new_comparison_period(store, config):
    at = lambda d, h: datetime(2026, 9, d, h, 0, tzinfo=UTC)  # noqa: E731
    _codex_rows(
        store,
        [],
        [
            CodexQuotaReading(at(10, 10), "codex", "primary", 20.0, 300, at(10, 12), "plus"),
            CodexQuotaReading(at(11, 10), "codex", "primary", 20.0, 300, at(11, 12), "pro"),
        ],
    )

    update_all(store, config, now=at(12, 0))

    periods = {s.period_id for s in load_summaries(store, "codex")}
    assert len(periods) == 2


def test_nothing_is_summarized_without_an_assigned_account(store):
    update_all(store, Config(), now=datetime(2026, 9, 12, tzinfo=UTC))

    assert load_summaries(store, "claude") == []
    assert load_summaries(store, "codex") == []
