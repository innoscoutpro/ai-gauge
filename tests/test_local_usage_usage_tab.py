import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QMenu

from aigauge import app as app_module
from aigauge.config import Config
from aigauge.local_usage.codex_logs import CodexQuotaReading
from aigauge.local_usage.rates import load_rate_table
from aigauge.local_usage.service import LocalUsageService
from aigauge.local_usage.usage_tab import (
    RANGES,
    UNAVAILABLE,
    UsageCostTab,
    format_tokens,
    share_bar,
)
from aigauge.local_usage.windows import (
    claude_windows_from_snapshot,
    codex_windows_from_readings,
)
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
from aigauge.ratio_dialog import RatioHistoryDialog

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
NOW = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)


def _local_naive(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None)


def _claude_snapshot(session_pct=20.0, weekly_pct=10.0) -> UsageSnapshot:
    return UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric(
                "Session", session_pct,
                resets_at=_local_naive(datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)),
                window=timedelta(hours=5),
            ),
            UsageMetric(
                "Weekly", weekly_pct,
                resets_at=_local_naive(datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)),
                window=timedelta(days=7),
            ),
        ],
        fetched_at=_local_naive(datetime(2026, 9, 10, 15, 55, tzinfo=timezone.utc)),
    )


@pytest.fixture
def service(tmp_path):
    claude = tmp_path / "logs" / "claude" / "projects"
    codex = tmp_path / "logs" / "codex"
    shutil.copytree(FIXTURES / "claude" / "projects", claude)
    shutil.copytree(FIXTURES / "codex" / "sessions", codex / "sessions")
    config = Config()
    config.local_usage.enabled = True
    config.local_usage.claude.account_id = "claude"
    config.local_usage.claude.log_root = str(claude)
    config.local_usage.codex.account_id = "codex"
    config.local_usage.codex.log_root = str(codex)
    svc = LocalUsageService(config, base_dir=tmp_path / "data")
    yield svc
    svc.shutdown()


def _tab(qtbot, service, account_id="claude", snapshot=None, tmp_path=None):
    tab = UsageCostTab(
        service,
        account_id,
        "Claude",
        snapshot,
        rate_table=load_rate_table(override_path=Path("does-not-exist.json")),
        now=lambda: NOW,
    )
    qtbot.addWidget(tab)
    return tab


def _select_range(tab, key):
    tab.range_combo.setCurrentIndex(tab.range_combo.findData(key))


def _model_rows(tab):
    table = tab.model_table
    return [
        [table.item(r, c).text() if table.item(r, c) else "" for c in range(table.columnCount())]
        for r in range(table.rowCount())
    ]


def test_dialog_without_usage_tab_is_unchanged(qtbot):
    dialog = RatioHistoryDialog("claude", "Claude", [], current_estimate=None)
    qtbot.addWidget(dialog)

    assert dialog.tabs is None
    assert "session vs weekly" in dialog.windowTitle()


def test_dialog_with_usage_tab_opens_on_ratio_first(qtbot, service):
    tab = _tab(qtbot, service)
    dialog = RatioHistoryDialog("claude", "Claude", [], current_estimate=None, usage_tab=tab)
    qtbot.addWidget(dialog)

    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Session vs weekly", "Usage and cost",
    ]
    assert dialog.tabs.currentIndex() == 0


def test_empty_state_shows_unavailable_not_zero(qtbot, service):
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert tab.updated_label.text() == "Not imported yet"
    assert tab.window_cell_text("Session", "cost") == UNAVAILABLE
    assert tab.window_cell_text("Weekly", "quota") == UNAVAILABLE


def test_current_windows_show_cost_quota_and_per_point(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    # opus $0.025106 + sonnet $0.00342
    assert tab.window_cell_text("Session", "cost") == "$0.03"
    assert tab.window_cell_text("Session", "quota") == "20%"
    assert tab.window_cell_text("Session", "output_per_point") == "57"
    assert tab.window_cell_text("Weekly", "per_point") == "$0.00"


def test_limit_reached_is_labelled(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot(session_pct=100.0))

    assert "extra usage possible" in tab.window_cell_text("Session", "quota")


def test_model_table_lists_models_sorted_by_cost_with_total(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    rows = _model_rows(tab)
    assert [r[0] for r in rows] == ["claude-opus-5", "claude-sonnet-5", "Total"]
    assert rows[0][1] == "3"
    assert rows[-1][1] == "4"


def test_range_selector_covers_all_ranges(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert [tab.range_combo.itemText(i) for i in range(tab.range_combo.count())] == [
        label for _key, label in RANGES
    ]
    for key, _label in RANGES:
        _select_range(tab, key)
        assert _model_rows(tab)[0][0] == "claude-opus-5"


def test_unpriced_models_show_no_price_and_sort_last(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, account_id="codex")
    _select_range(tab, "30d")

    rows = _model_rows(tab)
    names = [r[0] for r in rows]
    assert names[-2:] == ["unknown", "Total"]
    assert rows[-2][10] == "no price"
    assert rows[-1][10].endswith("+ unpriced")


def test_clicking_a_day_filters_the_model_table(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert tab.daily_table.rowCount() == 1
    tab._on_day_clicked(0, 0)

    assert tab.range_combo.currentText().startswith("Day: ")
    assert _model_rows(tab)[0][0] == "claude-opus-5"
    _select_range(tab, "today")
    assert tab.range_combo.findData("day") == -1


def test_not_recognized_state_is_shown(qtbot, service):
    service.run_sync()
    service.store.set_meta("recognized:claude", False)
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert "not recognized" in tab.status_label.text()
    assert tab.window_cell_text("Session", "cost") == UNAVAILABLE


def test_importing_marks_numbers_partial(qtbot, service, monkeypatch):
    service.run_sync()
    monkeypatch.setattr(service, "is_running", lambda: True)
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert "partial" in tab.status_label.text()
    assert tab.window_cell_text("Session", "cost").endswith("(partial)")


def test_dialog_renders_at_small_size(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())
    dialog = RatioHistoryDialog("claude", "Claude", [], current_estimate=None, usage_tab=tab)
    qtbot.addWidget(dialog)
    dialog.tabs.setCurrentIndex(1)
    dialog.resize(360, 320)
    dialog.show()

    assert dialog.width() >= 360


def test_claude_window_starts_at_resets_minus_window():
    windows = claude_windows_from_snapshot(_claude_snapshot())

    assert windows["Session"].start == datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
    assert windows["Weekly"].start == datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    assert windows["Session"].pct == 20.0


def test_codex_windows_come_from_log_readings():
    resets = datetime(2026, 9, 10, 19, 0, tzinfo=timezone.utc)
    readings = [
        CodexQuotaReading(NOW - timedelta(hours=1), "codex", "primary", 4.0, 300, resets, "team"),
        CodexQuotaReading(NOW - timedelta(minutes=5), "codex", "primary", 7.0, 300, resets, "team"),
        CodexQuotaReading(NOW - timedelta(minutes=5), "premium", "primary", 50.0, 300, resets, "team"),
        CodexQuotaReading(NOW - timedelta(hours=6), "codex", "primary", 90.0, 300,
                          NOW - timedelta(hours=1), "team"),
    ]

    windows = codex_windows_from_readings(readings, NOW)

    assert windows["Session"].pct == 7.0
    assert windows["Session"].start == resets - timedelta(hours=5)
    assert "Weekly" not in windows


def test_formatting_helpers():
    assert format_tokens(412) == "412"
    assert format_tokens(9_100) == "9.1K"
    assert format_tokens(151_000) == "151K"
    assert format_tokens(18_200_000) == "18.2M"
    assert share_bar(None) == "n/a"
    assert share_bar(0.5).startswith("50% █████")


def test_details_menu_lists_claude_and_codex_accounts(qtbot):
    opened = []
    stub = SimpleNamespace(
        _config=Config(),
        _details_menu=QMenu(),
        open_ratio_history=lambda account_id: opened.append(account_id),
    )

    app_module.App._populate_details_menu(stub)
    actions = stub._details_menu.actions()
    actions[0].trigger()

    assert [a.text() for a in actions] == ["Claude", "Codex"]
    assert opened == ["claude"]
