import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aigauge.local_usage import importer as importer_module
from aigauge.local_usage.importer import ImportSource, LogImporter
from aigauge.local_usage.store import UsageStore, local_date
from aigauge.local_usage.tokens import TokenCounts

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
WIDE_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
WIDE_END = datetime(2030, 1, 1, tzinfo=timezone.utc)
FILE_TIME = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def logs(tmp_path):
    claude = tmp_path / "logs" / "claude" / "projects"
    codex = tmp_path / "logs" / "codex"
    shutil.copytree(FIXTURES / "claude" / "projects", claude)
    shutil.copytree(FIXTURES / "codex" / "sessions", codex / "sessions")
    for path in (tmp_path / "logs").rglob("*.jsonl"):
        os.utime(path, (FILE_TIME, FILE_TIME))
    return claude, codex


def _sources(logs):
    claude, codex = logs
    return [
        ImportSource("claude", (claude,)),
        ImportSource("codex", (codex / "sessions", codex / "archived_sessions")),
    ]


def _totals(store, provider):
    out = {}
    for row in store.usage_by_model(provider, WIDE_START, WIDE_END):
        counts, messages = out.setdefault(row.model, (TokenCounts(), 0))
        counts.add(row.tokens)
        out[row.model] = (counts, messages + row.messages)
    return out


def _store(tmp_path, name="local_usage.sqlite"):
    return UsageStore(tmp_path / "db" / name)


def test_import_matches_hand_worked_totals(tmp_path, logs):
    store = _store(tmp_path)
    results = LogImporter(store).run(_sources(logs))

    claude = _totals(store, "claude")
    assert claude["claude-opus-5"] == (
        TokenCounts(input=8, output=936, cache_read=607, cache_write_5m=90, cache_write_1h=80),
        3,
    )
    assert claude["claude-sonnet-5"][1] == 1
    codex = _totals(store, "codex")
    assert codex["gpt-5.5"][0] == TokenCounts(
        input=1500, output=600, cache_read=3500, reasoning=150
    )
    assert codex["gpt-5.6-sol"][0].output == 340
    assert [r.provider for r in results] == ["claude", "codex"]
    assert all(not r.cancelled and not r.not_recognized for r in results)


def test_daily_totals_are_written_for_imported_days(tmp_path, logs):
    store = _store(tmp_path)
    LogImporter(store).run(_sources(logs))

    day = local_date(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
    daily = store.daily_totals("claude", day - timedelta(days=1), day + timedelta(days=1))
    output = sum(row.tokens.output for rows in daily.values() for row in rows)
    assert output == 1136


def test_appended_lines_are_read_incrementally(tmp_path, logs):
    claude, _codex = logs
    store = _store(tmp_path)
    LogImporter(store).run(_sources(logs)[:1])
    session = claude / "c--proj" / "sess-a.jsonl"
    size_before = session.stat().st_size

    # Finish the half-written last line, then append one more message.
    with session.open("ab") as fh:
        fh.write(b'ens":44,"service_tier":"standard"}},"requestId":"req_T","type":"assistant",'
                 b'"timestamp":"2026-09-10T12:09:00.000Z","version":"2.1.268"}\n')
    results = LogImporter(store).run(_sources(logs)[:1])

    messages = {row.model: row.messages for row in store.usage_by_model("claude", WIDE_START, WIDE_END)}
    assert messages["claude-opus-5"] == 4
    assert _totals(store, "claude")["claude-opus-5"][0].output == 980
    # Only the tail of the file was read on the second pass.
    assert 0 < results[0].bytes_read < size_before


def test_half_written_line_is_not_marked_read(tmp_path, logs):
    claude, _codex = logs
    store = _store(tmp_path)
    LogImporter(store).run(_sources(logs)[:1])

    session = claude / "c--proj" / "sess-a.jsonl"
    record = store.get_source_file("claude", os.path.normcase(os.path.realpath(session)))
    assert record.read_offset < session.stat().st_size
    assert session.read_bytes()[: record.read_offset].endswith(b"\n")


def test_truncated_codex_file_is_reread_without_double_counting(tmp_path, logs):
    _claude, codex = logs
    store = _store(tmp_path)
    sources = _sources(logs)[1:]
    LogImporter(store).run(sources)
    rollout = next((codex / "sessions").rglob("*sess-1.jsonl"))
    lines = rollout.read_bytes().splitlines(keepends=True)
    rollout.write_bytes(b"".join(lines[:6]))

    LogImporter(store).run(sources)

    codex_totals = _totals(store, "codex")
    assert "gpt-5.6-sol" not in codex_totals
    assert codex_totals["gpt-5.5"][0] == TokenCounts(input=1000, output=400, cache_read=2000, reasoning=100)


def test_rewritten_file_of_same_size_is_fully_reread(tmp_path, logs):
    _claude, codex = logs
    store = _store(tmp_path)
    sources = _sources(logs)[1:]
    LogImporter(store).run(sources)
    rollout = next((codex / "sessions").rglob("*sess-1.jsonl"))
    content = rollout.read_bytes()
    rollout.write_bytes(content.replace(b'"model":"gpt-5.6-sol"', b'"model":"gpt-5.6-xyz"'))

    LogImporter(store).run(sources)

    models = set(_totals(store, "codex"))
    assert "gpt-5.6-xyz" in models
    assert "gpt-5.6-sol" not in models


def test_two_roots_reaching_the_same_file_count_it_once(tmp_path, logs):
    claude, _codex = logs
    store = _store(tmp_path)
    LogImporter(store).run([ImportSource("claude", (claude, claude / "c--proj" / ".."))])

    assert store.source_file_count("claude") == 2
    assert _totals(store, "claude")["claude-opus-5"][1] == 3


def test_cancelled_import_resumes_to_same_totals(tmp_path, logs, monkeypatch):
    monkeypatch.setattr(importer_module, "COMMIT_EVERY_BYTES", 300)
    reference = _store(tmp_path, "reference.sqlite")
    LogImporter(reference).run(_sources(logs))

    store = _store(tmp_path)
    cancel = threading.Event()

    def progress(done, total):
        if done > 0:
            cancel.set()

    first = LogImporter(store, cancel=cancel, progress=progress).run(_sources(logs))
    assert first[0].cancelled
    assert _totals(store, "claude") != _totals(reference, "claude")

    LogImporter(store).run(_sources(logs))

    assert _totals(store, "claude") == _totals(reference, "claude")
    assert _totals(store, "codex") == _totals(reference, "codex")


def test_unrecognized_format_is_reported_not_zero(tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    (root / "s.jsonl").write_text(
        '{"type":"assistant","message":{"model":"claude-opus-5","tokens":{"in":1}},'
        '"timestamp":"2026-09-10T12:00:00Z"}\n',
        encoding="utf-8",
    )
    store = _store(tmp_path)

    results = LogImporter(store).run([ImportSource("claude", (tmp_path / "projects",))])

    assert results[0].not_recognized
    assert store.get_meta("recognized:claude") is False


def test_start_from_skips_older_events(tmp_path, logs):
    store = _store(tmp_path)
    cutoff = datetime(2026, 9, 10, 12, 4, tzinfo=timezone.utc)
    claude, _codex = logs

    LogImporter(store).run([ImportSource("claude", (claude,), start_from=cutoff)])

    assert set(_totals(store, "claude")) == {"claude-opus-5", "claude-sonnet-5"}
    assert _totals(store, "claude")["claude-opus-5"][1] == 1  # only msg_D at 12:08


def test_coverage_starts_at_oldest_log_file(tmp_path, logs):
    store = _store(tmp_path)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)

    LogImporter(store, now=lambda: now).run(_sources(logs))

    start = datetime.fromtimestamp(FILE_TIME, tz=timezone.utc)
    assert store.covers("claude", start, now)
    assert not store.covers("claude", start - timedelta(minutes=1), now)


def test_prune_keeps_daily_totals_and_blocks_reimport(tmp_path, logs):
    store = _store(tmp_path)
    LogImporter(store).run(_sources(logs))
    day = local_date(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))

    deleted = store.prune(now=datetime(2027, 1, 1, tzinfo=timezone.utc))

    assert deleted > 0
    assert store.row_count("claude") == 0
    assert store.daily_totals("claude", day, day)
    store.reset_progress("claude")
    LogImporter(store).run(_sources(logs))
    assert store.row_count("claude") == 0
    assert sum(r.tokens.output for r in store.daily_totals("claude", day, day)[day]) == 1136
