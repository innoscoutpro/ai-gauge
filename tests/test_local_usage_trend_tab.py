import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aigauge.config import Config
from aigauge.local_usage.rates import load_rate_table
from aigauge.local_usage.service import LocalUsageService
from aigauge.local_usage.store import ModelUsage
from aigauge.local_usage.summaries import FLAG_LIMIT_REACHED, WindowSummary, save_summary
from aigauge.local_usage.tokens import TokenCounts
from aigauge.local_usage.trend import build_trend
from aigauge.local_usage.usage_tab import UsageCostTab, trend_summary_text, window_label

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


def _save(service, index, pct, flags=(), model="claude-sonnet-5", metric="Session"):
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
            usage=[ModelUsage(model, "", TokenCounts(output=1_000_000), 10)],
            origin="live",
            flags=set(flags),
            period_id="p1",
            closed=True,
        ),
    )


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


def test_headline_answers_in_plain_words(qtbot, service):
    for index, pct in enumerate((20, 40, 20, 10), start=1):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    assert tab.trend_headline_label.text() == (
        "Latest session: 1% of the limit covered about $1.00 of usage"
    )
    detail = tab.trend_detail_label.text()
    assert "100% more than usual (typical $0.50, from 3 earlier sessions)" in detail
    assert "Output per 1%: 100K, 100% more than usual" in detail
    assert tab.trend_warning_label.isHidden()


def test_about_the_same_within_ten_percent(qtbot, service):
    for index, pct in enumerate((20, 20, 20, 19), start=1):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    assert tab.trend_detail_label.text().startswith("About the same as usual (typical $0.50")


def test_compared_table_is_short_with_vs_typical(qtbot, service):
    for index, pct in enumerate((20, 40, 20, 10), start=1):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    table = tab.trend_table
    assert [table.horizontalHeaderItem(i).text() for i in range(5)] == [
        "Window", "Used", "Cost per 1%", "Output per 1%", "vs typical",
    ]
    assert _column(table, 1) == ["10%", "20%", "40%", "20%"]
    assert _column(table, 4)[0] == "+100%"
    assert table.isColumnHidden(5)
    tab.trend_details_cb.setChecked(True)
    assert not tab.trend_table.isColumnHidden(5)


def test_skipped_windows_fold_into_one_line(qtbot, service):
    _save(service, 1, 20)
    _save(service, 2, 100, flags=[FLAG_LIMIT_REACHED])
    _save(service, 3, 5)

    tab = _tab(qtbot, service)

    assert tab.trend_headline_label.text() == "Needs 3 earlier completed sessions to compare; 0 so far."
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


def test_weeks_toggle_without_windows(qtbot, service):
    _save(service, 1, 20)
    tab = _tab(qtbot, service)

    tab.set_trend_metric("Weekly")

    assert tab.trend_metric_buttons["Weekly"].isChecked()
    assert tab.trend_headline_label.text() == "No completed weeks to compare yet."
    assert tab.trend_table.item(0, 0).text() == "No compared weeks yet."


def test_mix_warning_when_the_latest_window_used_other_models(qtbot, service):
    for index in range(1, 4):
        _save(service, index, 20)
    _save(service, 4, 20, model="claude-opus-5")

    tab = _tab(qtbot, service)

    assert not tab.trend_warning_label.isHidden()
    assert "Mostly claude-opus-5 in the latest window" in tab.trend_warning_label.text()


def test_chart_gets_compared_windows_and_typical_line(qtbot, service):
    for index, pct in enumerate((20, 40, 20, 10), start=1):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    assert len(tab.trend_chart._points) == 4
    assert tab.trend_chart._typical == pytest.approx(0.5)
    tab.trend_chart.resize(500, 170)
    assert not tab.trend_chart.grab().isNull()


def test_trend_summary_text_without_windows():
    report = build_trend([], "Session", load_rate_table(override_path=Path("none.json")))

    assert trend_summary_text(report, "Session") == "No completed sessions to compare yet."


def test_window_label_formats_session_and_week():
    start = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)

    assert " to " in window_label(start, start + timedelta(hours=5))
    assert window_label(start, start + timedelta(days=7)).count(" ") == 4


def test_marking_a_limit_change_saves_it_and_restarts_the_baseline(qtbot, service):
    for index in range(1, 5):
        _save(service, index, 20)
    for index in range(40, 44):
        _save(service, index, 20)
    tab = _tab(qtbot, service)
    assert not tab.limit_changes_btn.isHidden()

    tab.set_limit_changes([datetime(2026, 9, 5, 12, tzinfo=UTC).astimezone().date()])

    saved = service.config.local_usage.claude.limit_changes
    assert saved
    assert Config.load().local_usage.claude.limit_changes == saved
    assert "since the limit change on Sep 05" in tab.trend_detail_label.text()
    assert "before a limit change 4" in tab.trend_skipped_btn.text()
