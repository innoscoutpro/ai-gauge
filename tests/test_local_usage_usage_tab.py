import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QMenu

from aigauge import app as app_module
from aigauge.config import Config
from aigauge.local_usage.claude_logs import ClaudeMessage
from aigauge.local_usage.codex_logs import CodexQuotaReading
from aigauge.local_usage.rates import load_rate_table, summarize_costs
from aigauge.local_usage.service import LocalUsageService
from aigauge.local_usage.store import ModelUsage
from aigauge.local_usage.summaries import WindowSummary, save_summary
from aigauge.local_usage.tokens import TokenCounts
from aigauge.local_usage.usage_tab import (
    RANGES,
    UNAVAILABLE,
    UsageCostTab,
    _Bar,
    display_model_name,
    format_tokens,
    unpriced_note,
)
from aigauge.local_usage.windows import (
    claude_windows_from_snapshot,
    codex_windows_from_readings,
    current_windows,
)
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
from aigauge.ratio_dialog import RatioHistoryDialog

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
UTC = timezone.utc
NOW = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


def _local_naive(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None)


def _claude_snapshot(session_pct=20.0, weekly_pct=10.0, fable_pct=None) -> UsageSnapshot:
    metrics = [
        UsageMetric(
            "Session", session_pct,
            resets_at=_local_naive(datetime(2026, 9, 10, 16, 0, tzinfo=UTC)),
            window=timedelta(hours=5),
        ),
        UsageMetric(
            "Weekly", weekly_pct,
            resets_at=_local_naive(datetime(2026, 9, 14, 0, 0, tzinfo=UTC)),
            window=timedelta(days=7),
        ),
    ]
    if fable_pct is not None:
        metrics.append(
            UsageMetric("Fable", fable_pct,
                        resets_at=_local_naive(datetime(2026, 9, 14, 0, 0, tzinfo=UTC)))
        )
    return UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.OK,
        metrics=metrics,
        fetched_at=_local_naive(datetime(2026, 9, 10, 15, 55, tzinfo=UTC)),
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


def _tab(qtbot, service, account_id="claude", snapshot=None):
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


def test_dialog_opens_on_usage_tab_by_default(qtbot, service):
    usage_first = RatioHistoryDialog(
        "claude", "Claude", [], current_estimate=None, usage_tab=_tab(qtbot, service)
    )
    ratio_first = RatioHistoryDialog(
        "claude", "Claude", [], current_estimate=None,
        usage_tab=_tab(qtbot, service), open_usage_tab=False,
    )
    qtbot.addWidget(ratio_first)
    qtbot.addWidget(usage_first)

    assert [ratio_first.tabs.tabText(i) for i in range(2)] == ["Session vs weekly", "Usage and cost"]
    assert ratio_first.tabs.currentIndex() == 0
    assert usage_first.tabs.currentIndex() == 1


def test_empty_state_shows_not_available_rather_than_zero(qtbot, service):
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert tab.updated_label.text() == "Not imported yet"
    assert tab.card_text("Session", "cost") == UNAVAILABLE
    assert tab.card_text("Weekly", "quota") == UNAVAILABLE


def test_cards_show_cost_allowance_and_cost_per_percent(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    # opus $0.025106 + sonnet $0.00342
    assert tab.card_text("Session", "cost") == "$0.03"
    assert tab.card_text("Session", "quota") == "20% of allowance used"
    assert tab.card_text("Weekly", "per_point") == "≈ $0.00 per 1%"
    assert tab.card_text("Session", "title").startswith("This session · ")
    assert tab.cards["Fable"].isHidden()


def test_cards_reserve_height_for_an_optional_note(qtbot, service):
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())
    session = tab.cards["Session"]
    weekly = tab.cards["Weekly"]

    session.set_values("This session", UNAVAILABLE, UNAVAILABLE, "")
    weekly.set_values("This week", "$12.71", "15% of allowance used", "A note")
    tab.resize(900, 600)
    tab.show()
    qtbot.waitUntil(lambda: tab.card_row._columns == 2)

    assert not session.labels["per_point"].isHidden()
    assert session.height() == weekly.height()


def test_limit_reached_card_drops_cost_per_percent(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot(weekly_pct=100.0))

    assert tab.card_text("Weekly", "quota") == "100% · limit reached"
    assert tab.card_text("Weekly", "per_point") == "No cost per 1% at the limit"
    assert "extra usage" in tab.cards["Weekly"].labels["quota"].toolTip()


def test_fable_card_counts_only_fable_models(qtbot, service):
    service.run_sync()
    with service.store.transaction() as conn:
        service.store.upsert_claude_messages(conn, [ClaudeMessage(
            "msg_F", "req_F", datetime(2026, 9, 10, 12, 30, tzinfo=UTC), "claude-fable-5-1",
            TokenCounts(output=1_000_000), "standard", "standard", "2.1.268", False)])
    tab = _tab(qtbot, service, snapshot=_claude_snapshot(fable_pct=50.0))

    assert not tab.cards["Fable"].isHidden()
    assert tab.card_text("Fable", "cost") == "$50.00"
    assert tab.card_text("Fable", "quota") == "50% of Fable allowance used"
    assert tab.card_text("Fable", "per_point") == "≈ $1.00 per 1%"
    assert tab.card_text("Weekly", "cost") == "$50.03"


def test_model_table_leads_with_cost_and_share(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    rows = _model_rows(tab)
    assert [r[0] for r in rows] == ["● Opus 5", "● Sonnet 5", "Total"]
    assert tab.model_table.item(0, 0).toolTip() == "claude-opus-5"
    assert [tab.model_table.horizontalHeaderItem(i).text() for i in range(6)] == [
        "Model", "Est. cost", "Share of cost", "Output", "Share of output", "Msgs",
    ]
    assert not tab.model_table.horizontalHeader().stretchLastSection()
    # opus 936 of 1136 output tokens
    assert rows[0][4] == "82%"
    assert rows[0][5] == "3"
    assert rows[-1][5] == "4"
    share = tab.model_table.cellWidget(0, 2)
    assert isinstance(share, _Bar) and share.label.endswith("%")


def test_token_details_are_hidden_until_asked(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert tab.model_table.isColumnHidden(7)
    tab.token_details_cb.setChecked(True)
    assert not tab.model_table.isColumnHidden(7)


def test_range_selector_defaults_to_this_week_and_covers_all_ranges(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert tab.range_combo.currentText() == "This week"
    assert [tab.range_combo.itemText(i) for i in range(tab.range_combo.count())] == [
        label for _key, label in RANGES
    ]
    for key, _label in RANGES:
        _select_range(tab, key)
        assert _model_rows(tab)[0][0] == "● Opus 5"


def test_view_and_range_choices_are_remembered(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    tab.show_view("day")
    _select_range(tab, "30d")

    reopened = _tab(qtbot, service, snapshot=_claude_snapshot())
    assert reopened.current_view() == "day"
    assert reopened.range_combo.currentData() == "30d"
    saved = Config.load().local_usage
    assert saved.details_view == "day"
    assert saved.details_range == "30d"


def test_unpriced_models_use_clear_names_and_keep_total_readable(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, account_id="codex")
    _select_range(tab, "30d")

    rows = _model_rows(tab)
    assert [r[0] for r in rows][-2:] == ["● Unidentified", "Total"]
    assert rows[-2][1] == "price unavailable"
    assert "+ unpriced" not in rows[-1][1]
    assert rows[-1][1].startswith("$")
    total_cost = tab.model_table.item(tab.model_table.rowCount() - 1, 1)
    assert total_cost.toolTip().startswith("Excludes unidentified usage (")


def test_internal_codex_review_model_has_a_friendly_exclusion_note():
    rates = load_rate_table(override_path=Path("does-not-exist.json"))
    summary = summarize_costs(
        [ModelUsage("codex-auto-review", "", TokenCounts(output=500), 2)],
        rates,
    )

    assert display_model_name("codex-auto-review") == "Automatic review"
    assert unpriced_note(summary) == "Excludes automatic review (100% of tokens)"


@pytest.mark.parametrize(
    ("model", "expected"),
    (
        ("claude-opus-5", "Opus 5"),
        ("claude-fable-5-1", "Fable 5.1"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-3-5-sonnet-20241022", "Sonnet 3.5"),
        ("claude-opus-5-latest", "Opus 5"),
        ("claude-future-6", "claude-future-6"),
        ("other-model-20251001", "other-model-20251001"),
    ),
)
def test_claude_model_display_names_are_conservative(model, expected):
    assert display_model_name(model) == expected


def test_clicking_a_day_shows_its_models_with_a_removable_chip(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())
    tab.show_view("day")
    assert tab.daily_table.rowCount() == 1
    assert "Opus 5" in tab.day_legend.text()
    assert "Opus 5 — claude-opus-5" in tab.day_legend.toolTip()

    tab._on_day_clicked(0, 0)

    assert tab.current_view() == "model"
    assert not tab.day_chip.isHidden()
    assert _model_rows(tab)[0][0] == "● Opus 5"
    tab.day_chip.click()
    assert tab.day_chip.isHidden()


def test_views_switch_with_their_own_controls(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert [b.text() for b in tab.view_buttons.values()] == ["By model", "By day", "Trend"]
    tab.show_view("trend")
    assert tab.range_combo.isHidden()
    assert not tab._trend_metric_bar.isHidden()


def test_tables_fit_their_rows_instead_of_scrolling(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    table = tab.model_table
    rows_height = sum(table.rowHeight(r) for r in range(table.rowCount()))
    assert table.height() >= rows_height


def test_not_recognized_state_is_shown(qtbot, service):
    service.run_sync()
    service.store.set_meta("recognized:claude", False)
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert "weren't recognized" in tab.status_label.text()
    assert tab.card_text("Session", "cost") == UNAVAILABLE


def test_importing_marks_numbers_partial(qtbot, service, monkeypatch):
    service.run_sync()
    monkeypatch.setattr(service, "is_running", lambda: True)
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())

    assert "partial" in tab.status_label.text()
    assert tab.card_text("Session", "cost").endswith("(partial)")
    assert tab.importing_label.text().startswith("Importing")


def test_dialog_renders_at_small_size(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot(fable_pct=40.0))
    dialog = RatioHistoryDialog(
        "claude", "Claude", [], current_estimate=None, usage_tab=tab, open_usage_tab=True
    )
    qtbot.addWidget(dialog)
    dialog.resize(360, 320)
    dialog.show()

    assert dialog.width() >= 360


def test_claude_window_starts_at_resets_minus_window():
    windows = claude_windows_from_snapshot(_claude_snapshot(fable_pct=30.0))

    assert windows["Session"].start == datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    assert windows["Weekly"].start == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    assert windows["Fable"].start == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    assert windows["Fable"].model_filter == "fable"
    assert windows["Session"].model_filter is None


def test_claude_current_windows_fall_back_to_persisted_open_summaries(service):
    session_reset = NOW + timedelta(hours=1)
    weekly_reset = NOW + timedelta(hours=4)
    for metric, reset, pct, window in (
        ("Session", session_reset, 8.0, timedelta(hours=5)),
        ("Weekly", weekly_reset, 100.0, timedelta(days=7)),
    ):
        save_summary(
            service.store,
            WindowSummary(
                account_id="claude",
                metric=metric,
                window_start=reset - window,
                resets_at=reset,
                last_reading_at=NOW - timedelta(minutes=5),
                last_pct=pct,
                usage=[],
                origin="live",
                closed=False,
            ),
        )
    fable_reset = _local_naive(weekly_reset)
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric(
                "Session",
                8.0,
                resets_at=None,
                note="Paused until your week resets",
                window=timedelta(hours=5),
            ),
            UsageMetric("Fable", 76.0, resets_at=fable_reset, window=timedelta(days=7)),
        ],
        fetched_at=_local_naive(NOW),
    )

    windows = current_windows(
        "claude", snapshot, service.store, NOW, account_id="claude"
    )

    assert windows["Session"].pct == 8.0
    assert windows["Session"].resets_at == session_reset
    assert windows["Weekly"].pct == 100.0
    assert windows["Weekly"].resets_at == weekly_reset
    assert windows["Fable"].pct == 76.0


def test_codex_windows_come_from_log_readings():
    resets = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
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


def test_format_tokens_reads_large_counts():
    assert format_tokens(412) == "412"
    assert format_tokens(9_100) == "9.1K"
    assert format_tokens(151_000) == "151K"
    assert format_tokens(18_200_000) == "18.2M"
    assert format_tokens(6_887_700_000) == "6.9B"


def test_details_menu_opens_the_usage_tab(qtbot):
    opened = []
    stub = SimpleNamespace(
        _config=Config(),
        _details_menu=QMenu(),
        open_ratio_history=lambda account_id, **kwargs: opened.append((account_id, kwargs)),
    )

    app_module.App._populate_details_menu(stub)
    actions = stub._details_menu.actions()
    actions[0].trigger()

    assert [a.text() for a in actions] == ["Claude", "Codex"]
    assert opened == [("claude", {"show_usage": True})]


def test_short_views_start_at_the_top_after_a_tall_one(qtbot, service):
    service.run_sync()
    tab = _tab(qtbot, service, snapshot=_claude_snapshot())
    tab.resize(700, 600)
    tab.show()
    tab.show_view("trend")
    qtbot.wait(10)

    tab.show_view("day")
    qtbot.wait(10)

    day_page = tab.pages["day"]
    assert tab.pages["trend"].isHidden()
    assert tab.daily_table.y() < 80
    assert day_page.height() < tab.daily_table.height() + 120
