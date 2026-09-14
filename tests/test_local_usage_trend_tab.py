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


def _save(service, index, pct, flags=()):
    resets = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=5 * index)
    save_summary(
        service.store,
        WindowSummary(
            account_id="claude",
            metric="Session",
            window_start=resets - timedelta(hours=5),
            resets_at=resets,
            last_reading_at=resets - timedelta(minutes=5),
            last_pct=pct,
            usage=[ModelUsage("claude-sonnet-5", "", TokenCounts(output=1_000_000), 10)],
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
    return tab


def _column(table, col):
    return [table.item(r, col).text() for r in range(table.rowCount())]


def test_trend_page_shows_change_against_baseline(qtbot, service):
    for index, pct in enumerate((10, 20, 10, 5), start=1):
        _save(service, index, pct)

    tab = _tab(qtbot, service)

    assert tab.lower_tabs.tabText(1) == "Allowance trend"
    text = tab.trend_summary_label.text()
    assert "Cost per 1%: $2.00 in the latest window, +100% vs the typical $1.00" in text
    assert "3 earlier windows ranged $0.50 to $1.00" in text
    assert "Output per 1%: 200K, +100% vs the typical 100K." in text
    assert _column(tab.trend_table, 8) == ["included"] * 4


def test_trend_page_collects_before_baseline_and_shows_reasons(qtbot, service):
    _save(service, 1, 20)
    _save(service, 2, 100, flags=[FLAG_LIMIT_REACHED])

    tab = _tab(qtbot, service)

    assert tab.trend_summary_label.text() == "Needs 3 earlier completed windows to compare; 0 so far."
    assert _column(tab.trend_table, 8) == ["skipped: limit reached (extra usage possible)", "included"]


def test_trend_page_for_metric_without_windows(qtbot, service):
    _save(service, 1, 20)
    tab = _tab(qtbot, service)

    tab.trend_metric_combo.setCurrentIndex(tab.trend_metric_combo.findData("Weekly"))

    assert tab.trend_summary_label.text() == "No completed windows to compare yet."
    assert tab.trend_table.item(0, 0).text() == "No completed windows yet."


def test_trend_summary_text_without_windows():
    report = build_trend([], "Session", load_rate_table(override_path=Path("none.json")))

    assert trend_summary_text(report) == "No completed windows to compare yet."


def test_window_label_formats_session_and_week():
    start = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)

    assert " to " in window_label(start, start + timedelta(hours=5))
    assert window_label(start, start + timedelta(days=7)).count(" ") == 4
