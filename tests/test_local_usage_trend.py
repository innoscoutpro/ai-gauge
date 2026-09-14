from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aigauge.local_usage.rates import load_rate_table
from aigauge.local_usage.store import ModelUsage
from aigauge.local_usage.summaries import (
    FLAG_INCOMPLETE,
    FLAG_LIMIT_REACHED,
    FLAG_TIME_UNCERTAIN,
    WindowSummary,
)
from aigauge.local_usage.tokens import TokenCounts
from aigauge.local_usage.trend import (
    COUNTED,
    NOTE_PARTIAL_PRICE,
    REASON_BEFORE_CHANGE,
    REASON_INCOMPLETE,
    REASON_LIMIT,
    REASON_LOW,
    REASON_PERIOD,
    REASON_SPANS_CHANGE,
    REASON_TIME,
    REASON_UNPRICED,
    build_trend,
)

UTC = timezone.utc
MILLION = 1_000_000


@pytest.fixture
def rates():
    return load_rate_table(override_path=Path("does-not-exist.json"))


def _window(index, pct, output_millions=1.0, *, flags=(), period="p1", model="claude-sonnet-5",
            closed=True, metric="Session", cache_read=0):
    resets = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=5 * index)
    return WindowSummary(
        account_id="claude",
        metric=metric,
        window_start=resets - timedelta(hours=5),
        resets_at=resets,
        last_reading_at=resets - timedelta(minutes=5),
        last_pct=pct,
        usage=[ModelUsage(model, "", TokenCounts(output=int(output_millions * MILLION),
                                                 cache_read=cache_read), 10)],
        origin="live",
        flags=set(flags),
        period_id=period,
        closed=closed,
    )


def test_collecting_until_three_earlier_windows(rates):
    report = build_trend([_window(1, 10), _window(2, 10), _window(3, 10)], "Session", rates)

    assert report.collecting
    assert report.output_baseline.windows == 2
    assert report.dollars_change is None


def test_change_is_current_over_median_of_earlier_windows(rates):
    # sonnet output $10 per million: 1M output at 20% is $0.50 per point
    windows = [_window(1, 20), _window(2, 40), _window(3, 20), _window(4, 10)]

    report = build_trend(windows, "Session", rates)

    assert report.current.summary is windows[3]
    assert report.current.dollars_per_point == pytest.approx(1.0)
    # earlier: $0.50, $0.25, $0.50 -> median $0.50
    assert report.dollars_baseline.median == pytest.approx(0.5)
    assert (report.dollars_baseline.low, report.dollars_baseline.high) == (0.25, 0.5)
    assert report.dollars_change == pytest.approx(1.0)
    assert report.output_change == pytest.approx(1.0)


def test_pooled_figures_divide_totals_rather_than_average_ratios(rates):
    windows = [_window(1, 10, 1.0), _window(2, 40, 1.0)]

    report = build_trend(windows, "Session", rates)

    # average of ratios would be (1.00 + 0.25) / 2 = 0.625
    assert report.pooled_dollars_per_point == pytest.approx(20.0 / 50)
    assert report.pooled_output_per_point == pytest.approx(2 * MILLION / 50)


@pytest.mark.parametrize(
    ("window", "reason"),
    [
        (_window(1, 1.0), REASON_LOW),
        (_window(1, 99.5), REASON_LIMIT),
        (_window(1, 100.0, flags=[FLAG_LIMIT_REACHED]), REASON_LIMIT),
        (_window(1, 30, flags=[FLAG_INCOMPLETE]), REASON_INCOMPLETE),
        (_window(1, 30, flags=[FLAG_TIME_UNCERTAIN]), REASON_TIME),
    ],
)
def test_windows_are_left_out_with_their_reason(rates, window, reason):
    report = build_trend([window, _window(0, 20)], "Session", rates)

    newest = report.rows[0]
    assert newest.summary is window
    assert newest.reason == reason
    assert report.current.summary is not window


def test_other_comparison_period_is_left_out(rates):
    report = build_trend([_window(1, 20, period="old"), _window(2, 20, period="new")], "Session", rates)

    assert [r.reason for r in report.rows] == [COUNTED, REASON_PERIOD]


def test_unpriced_usage_excludes_only_the_dollar_figure(rates):
    report = build_trend([_window(1, 20, model="mystery-model")], "Session", rates)

    row = report.rows[0]
    assert row.counted
    assert row.dollars_reason == REASON_UNPRICED
    assert row.dollars_per_point is None
    assert row.output_per_point == pytest.approx(MILLION / 20)
    assert report.pooled_dollars_per_point is None


def test_rate_table_changes_do_not_change_exclusions(rates, tmp_path):
    windows = [_window(i, 10 + i) for i in range(5)] + [_window(9, 1.0)]
    cheaper = tmp_path / "rates.override.json"
    cheaper.write_text(
        '{"models": {"claude-sonnet-5": {"rates": {"input": 1, "output": 1, '
        '"cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}}}}',
        encoding="utf-8",
    )

    before = build_trend(windows, "Session", rates)
    after = build_trend(windows, "Session", load_rate_table(override_path=cheaper))

    assert [r.reason for r in before.rows] == [r.reason for r in after.rows]
    assert before.current.dollars_per_point != after.current.dollars_per_point


def test_open_windows_and_other_metrics_are_ignored(rates):
    report = build_trend(
        [_window(1, 20, closed=False), _window(2, 20, metric="Weekly")], "Session", rates
    )

    assert report.rows == []
    assert report.current is None


def test_cache_share_and_top_model_are_reported(rates):
    window = _window(1, 20, cache_read=3 * MILLION)
    window.usage.append(ModelUsage("claude-opus-5", "", TokenCounts(input=MILLION, output=MILLION), 1))

    row = build_trend([window], "Session", rates).rows[0]

    assert row.cache_share == pytest.approx(0.75)
    assert row.top_model == "claude-opus-5"


def test_session_windows_need_ten_percent_but_weekly_windows_do_not(rates):
    report_session = build_trend([_window(1, 8)], "Session", rates)
    report_weekly = build_trend([_window(1, 8, metric="Weekly")], "Weekly", rates)

    assert report_session.rows[0].reason == REASON_LOW
    assert report_weekly.rows[0].counted


def test_small_unpriced_share_still_gets_a_cost_per_point(rates):
    window = _window(1, 20)
    window.usage.append(ModelUsage("codex-auto-review", "", TokenCounts(output=50_000), 3))

    row = build_trend([window], "Session", rates).rows[0]

    assert row.dollars_counted
    assert row.dollars_note == NOTE_PARTIAL_PRICE
    assert row.dollars_per_point == pytest.approx(10.0 / 20)


def test_limit_change_restarts_the_baseline_and_skips_spanning_windows(rates):
    windows = [_window(i, 20) for i in range(1, 7)]
    change = windows[3].window_start + timedelta(hours=1)  # inside window 4

    report = build_trend(windows, "Session", rates, [change])

    reasons = {r.summary.resets_at: r.reason for r in report.rows}
    assert reasons[windows[5].resets_at] == COUNTED
    assert reasons[windows[4].resets_at] == COUNTED
    assert reasons[windows[3].resets_at] == REASON_SPANS_CHANGE
    assert reasons[windows[2].resets_at] == REASON_BEFORE_CHANGE
    assert report.limit_change == change
    assert report.collecting
