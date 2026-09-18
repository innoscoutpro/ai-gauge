import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aigauge.config import Config
from aigauge.local_usage.rates import load_rate_table
from aigauge.local_usage.service import LocalUsageService
from aigauge.local_usage.store import ModelUsage
from aigauge.local_usage.summaries import (
    FLAG_LIMIT_REACHED,
    WindowSummary,
    load_summaries,
    save_summary,
)
from aigauge.local_usage.tokens import TokenCounts
from aigauge.local_usage.trend import build_trend
from aigauge.local_usage.usage_tab import (
    UsageCostTab,
    rolling_average_points,
    trend_summary_text,
    window_label,
)

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def service(tmp_path):
    claude = tmp_path / "logs" / "claude" / "projects"
    shutil.copytree(FIXTURES / "claude" / "projects", claude)
    config = Config()
    config.local_usage.enabled = True
    config.local_usage.claude.account_id = "claude"
    config.local_usage.claude.log_root = str(claude)
    config.local_usage.codex.enabled = False
    svc = LocalUsageService(config, base_dir=tmp_path / "data")
    svc.run_sync()
    yield svc
    svc.shutdown()


def _save(
    service,
    index,
    pct,
    flags=(),
    model="claude-sonnet-5",
    metric="Session",
    output=1_000_000,
):
    resets = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=5 * index)
    save_summary(
        service.store,
        WindowSummary(
            account_id="claude",
            metric=metric,
            window_start=resets - timedelta(hours=5),
            resets_at=resets,
            last_reading_at=resets - timedelta(minutes=5),
            last_pct=pct,
            usage=[ModelUsage(model, "", TokenCounts(output=output), 10)],
            origin="live",
            flags=set(flags),
            period_id="p1",
            closed=True,
        ),
    )


def _baseline_then_recent(service, recent_pct, recent_model="claude-sonnet-5", start=1):
    """Three baseline sessions at 20% ($0.50 per 1%), then five recent ones."""
    for index in range(start, start + 3):
        _save(service, index, 20)
    for index in range(start + 3, start + 8):
        _save(service, index, recent_pct, model=recent_model)


def _tab(qtbot, service):
    tab = UsageCostTab(
        service, "claude", "Claude",
        rate_table=load_rate_table(override_path=Path("does-not-exist.json")),
        now=lambda: NOW,
    )
    qtbot.addWidget(tab)
    tab.show_view("trend")
    return tab


def _column(table, col):
    return [table.item(r, col).text() for r in range(table.rowCount())]


def test_default_summary_is_a_compact_recent_ballpark(qtbot, service):
    _baseline_then_recent(service, recent_pct=10)

    tab = _tab(qtbot, service)

    assert tab.trend_headline_label.text() == (
        "Recent ballpark · 5 sessions    ~$1.0 API-equiv. / 1%"
        "    ·    ~100K output / 1%"
    )
    assert not tab.trend_headline_label.isHidden()
    assert tab.trend_answer.isHidden()
    assert [metric.title for metric in tab.trend_comparison.metrics] == [
        "API-equiv. / 1%", "Output / 1%",
    ]
    assert tab.trend_comparison.metrics[0].change == pytest.approx(1.0)
    detail = tab.trend_detail_label.text()
    assert "100% more than usual (earlier average $0.50, from 3 sessions)" in detail
    assert "Output per 1%: 100K, 100% more than usual" in detail
    assert tab.trend_warning_label.isHidden()


def test_large_session_has_proportional_weight_in_the_answer(qtbot, service):
    for index in range(1, 4):
        _save(service, index, 20)
    for index, pct in enumerate((20, 20, 20, 20, 80), start=4):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    assert "~$0.31 API-equiv. / 1%" in tab.trend_headline_label.text()
    assert tab.trend_detail_label.text().startswith(
        "38% less than usual (earlier average $0.50"
    )


def test_default_table_keeps_comparison_column_out_of_the_way(qtbot, service):
    _baseline_then_recent(service, recent_pct=10)

    tab = _tab(qtbot, service)

    table = tab.trend_table
    assert [table.horizontalHeaderItem(i).text() for i in range(5)] == [
        "Window", "Used", "Cost per 1%", "Output per 1%", "vs baseline",
    ]
    assert _column(table, 1) == ["10%"] * 5 + ["20%"] * 3
    assert _column(table, 4)[0] == "+100%"
    assert table.isColumnHidden(4)
    assert table.isColumnHidden(5)
    tab.trend_details_cb.setChecked(True)
    assert not tab.trend_table.isColumnHidden(5)


def test_skipped_windows_fold_into_one_line(qtbot, service):
    _save(service, 1, 20)
    _save(service, 2, 100, flags=[FLAG_LIMIT_REACHED])
    _save(service, 3, 5)

    tab = _tab(qtbot, service)

    assert tab.trend_headline_label.text() == (
        "Recent ballpark · latest session    ~$0.50 API-equiv. / 1%"
        "    ·    ~50K output / 1%"
    )
    assert tab.trend_skipped_btn.text() == (
        "▸ 2 windows not compared (too little used 1 · limit reached 1)"
    )
    assert _column(tab.trend_table, 1) == ["20%"]
    assert tab.trend_skipped_table.isHidden()
    tab.trend_skipped_btn.click()
    assert not tab.trend_skipped_table.isHidden()
    assert _column(tab.trend_skipped_table, 2) == [
        "too little of the limit used to compare",
        "limit reached (extra usage possible)",
    ]


def test_only_recent_windows_until_show_all(qtbot, service):
    for index in range(1, 26):
        _save(service, index, 20)

    tab = _tab(qtbot, service)

    assert tab.trend_table.rowCount() == 20
    assert tab.trend_show_all_btn.text() == "Show all 25"
    tab.trend_show_all_btn.click()
    assert tab.trend_table.rowCount() == 25


def test_weeks_compare_the_latest_week(qtbot, service):
    for index, pct in enumerate((20, 20, 20, 10), start=1):
        _save(service, index, pct, metric="Weekly")
    tab = _tab(qtbot, service)

    tab.set_trend_metric("Weekly")

    assert tab.trend_metric_buttons["Weekly"].isChecked()
    assert tab.trend_headline_label.text() == (
        "Recent ballpark · latest week    ~$1.0 API-equiv. / 1%"
        "    ·    ~100K output / 1%"
    )


def test_weeks_toggle_without_windows(qtbot, service):
    _save(service, 1, 20)
    tab = _tab(qtbot, service)

    tab.set_trend_metric("Weekly")

    assert tab.trend_headline_label.text() == "No completed weeks to summarize yet."
    assert tab.trend_table.item(0, 0).text() == "No compared weeks yet."


def test_mix_warning_when_recent_windows_used_other_models(qtbot, service):
    _baseline_then_recent(service, recent_pct=20, recent_model="claude-opus-5")

    tab = _tab(qtbot, service)

    assert tab.trend_answer.isHidden()
    assert tab.compare_change_btn.isHidden()
    assert tab.trend_warning_label.text() == "Usage mix changed ⓘ"
    assert "Mostly claude-opus-5 in the recent windows" in tab.trend_warning_label.toolTip()


def test_default_chart_focuses_on_the_rolling_trend_without_comparison_lines(qtbot, service):
    _baseline_then_recent(service, recent_pct=10)

    tab = _tab(qtbot, service)

    assert len(tab.trend_chart._points) == 8
    assert tab.trend_chart._typical is None
    assert tab.trend_chart._average_lines == []
    assert tab.trend_chart._focus_trend
    tab.trend_chart.resize(500, 170)
    assert not tab.trend_chart.grab().isNull()


def test_default_chart_clips_raw_outlier_to_keep_trend_readable(qtbot, service):
    for index, pct in enumerate((20, 20, 20, 20, 20, 20, 20, 10), start=1):
        _save(service, index, pct)
    tab = _tab(qtbot, service)

    tab.trend_chart.resize(500, 170)
    assert not tab.trend_chart.grab().isNull()

    assert tab.trend_chart._display_high is not None
    assert tab.trend_chart._display_high < max(point[1] for point in tab.trend_chart._points)


def test_opposing_metrics_collapse_explanation_into_mixed_signal_tooltip(qtbot, service):
    for index in range(1, 4):
        _save(service, index, 20, model="claude-opus-5")
    for index in range(40, 45):
        _save(service, index, 20, model="claude-sonnet-5", output=2_000_000)

    tab = _tab(qtbot, service)
    tab.set_limit_changes([datetime(2026, 9, 5, 12, tzinfo=UTC).astimezone().date()])
    tab.compare_change_btn.click()

    assert tab.trend_comparison.metrics[0].change < 0
    assert tab.trend_comparison.metrics[1].change > 0
    assert tab.trend_warning_label.text() == "Mixed signal ⓘ"
    assert "opposite directions" in tab.trend_warning_label.toolTip()


def test_trend_summary_text_without_windows():
    report = build_trend([], "Session", load_rate_table(override_path=Path("none.json")))

    assert trend_summary_text(report, "Session") == "No completed sessions to compare yet."


def test_window_label_formats_session_and_week():
    start = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)

    assert " to " in window_label(start, start + timedelta(hours=5))
    assert window_label(start, start + timedelta(days=7)).count(" ") == 4


def test_marking_a_limit_change_keeps_and_compares_older_data(qtbot, service):
    for index in range(1, 5):
        _save(service, index, 20)
    _baseline_then_recent(service, recent_pct=20, start=40)
    tab = _tab(qtbot, service)
    assert not tab.limit_changes_btn.isHidden()

    tab.set_limit_changes([datetime(2026, 9, 5, 12, tzinfo=UTC).astimezone().date()])

    saved = service.config.local_usage.claude.limit_changes
    assert saved
    assert Config.load().local_usage.claude.limit_changes == saved
    assert tab.compare_change_btn.text() == "Compare Sep 05"
    assert not tab.compare_change_btn.isHidden()
    assert tab.trend_answer.isHidden()
    assert "Recent ballpark" in tab.trend_headline_label.text()
    assert tab.trend_chart._average_lines == []

    tab.compare_change_btn.click()

    assert not tab.trend_answer.isHidden()
    assert tab.trend_headline_label.isHidden()
    assert not tab.trend_table.isColumnHidden(4)
    assert {point[3] for point in tab.trend_chart._points} == {0, 1}
    assert len(tab.trend_chart._rolling) == 2
    assert {line[3] for line in tab.trend_chart._average_lines} == {
        "before avg", "recent avg",
    }
    assert not tab.trend_chart._focus_trend
    assert tab.trend_skipped_btn.isHidden()


def test_rolling_average_is_weighted_and_does_not_cross_a_change(service):
    for index, pct in enumerate((10, 40, 20, 20), start=1):
        _save(service, index, pct)
    summaries = load_summaries(service.store, "claude", "Session")
    change = datetime(2026, 9, 1, 10, tzinfo=UTC)
    report = build_trend(
        summaries,
        "Session",
        load_rate_table(override_path=Path("does-not-exist.json")),
        [change],
    )

    segments = rolling_average_points(report.rows, 2, dollars=True)

    assert len(segments) == 2
    assert [value for _when, value in segments[0]] == pytest.approx([1.0, 0.4])
    assert [value for _when, value in segments[1]] == pytest.approx([0.5, 0.5])
