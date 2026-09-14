import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QTabWidget

from aigauge import app as app_module
from aigauge.config import BrowserAccount, Config, app_data_dir
from aigauge.local_usage import enable_dialog
from aigauge.local_usage.enable_dialog import backfill_prompt_text
from aigauge.local_usage.formats import untested_versions
from aigauge.local_usage.importer import scan_provider
from aigauge.local_usage.settings_panel import LocalUsagePanel
from aigauge.settings_dialog import SettingsDialog

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"


def _panel(qtbot, config: Config) -> LocalUsagePanel:
    panel = LocalUsagePanel(config, config.browser_accounts)
    qtbot.addWidget(panel)
    return panel


def test_tracking_is_off_by_default_and_reads_nothing(qtbot, monkeypatch):
    def fail_scan(*_args, **_kwargs):
        raise AssertionError("log folders must not be listed while tracking is off")

    monkeypatch.setattr("aigauge.local_usage.settings_panel.scan_provider", fail_scan)
    panel = _panel(qtbot, Config())

    assert not panel.enabled_cb.isChecked()
    assert panel.status_text("claude") == "Off. No log files are read."
    assert not panel.import_btn.isEnabled()


def test_settings_dialog_has_local_usage_tab(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    tabs = dialog.findChild(QTabWidget)
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert "Local usage" in names


def test_single_account_is_preselected_and_saved_by_id(qtbot):
    config = Config()
    panel = _panel(qtbot, config)
    panel.enabled_cb.setChecked(True)

    panel.apply_to(config)

    assert config.local_usage.enabled
    assert config.local_usage.claude.account_id == "claude"
    assert config.local_usage.codex.account_id == "codex"


def test_several_accounts_require_a_choice(qtbot):
    config = Config()
    config.browser_accounts.append(BrowserAccount(id="claude-2", kind="claude", name="Work"))
    panel = _panel(qtbot, config)
    panel.enabled_cb.setChecked(True)

    panel.apply_to(config)
    assert config.local_usage.claude.account_id is None

    combo = panel.controls["claude"].account_combo
    combo.setCurrentIndex(combo.findData("claude-2"))
    panel.apply_to(config)
    assert config.local_usage.claude.account_id == "claude-2"


def test_removed_account_shows_paused_status(qtbot, tmp_path):
    config = Config()
    config.local_usage.enabled = True
    config.local_usage.claude.account_id = "gone"
    config.local_usage.claude.log_root = str(tmp_path)
    config.browser_accounts.append(BrowserAccount(id="claude-2", kind="claude"))
    panel = _panel(qtbot, config)

    assert "Paused" in panel.status_text("claude")


def test_backfill_prompt_describes_logs_from_listings(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "claude" / "projects", root)
    scans = [scan_provider("claude", [root]), scan_provider("codex", [tmp_path / "none"])]

    text = backfill_prompt_text(scans)

    assert "Claude logs go back to" in text
    assert "No Codex logs were found yet." in text
    assert "background" in text


def test_untested_versions_are_reported():
    assert untested_versions("claude", ["2.1.265", "2.1.300", "junk"]) == ["2.1.300", "junk"]
    assert untested_versions("codex", ["0.154.0-alpha.6.2"]) == []


def _app_stub(config: Config):
    return SimpleNamespace(_config=config, _local_usage=None, _widget=None)


def test_app_creates_nothing_while_tracking_is_off():
    stub = _app_stub(Config())

    app_module.App._sync_local_usage(stub)

    assert stub._local_usage is None
    assert not (app_data_dir() / "local_usage.sqlite").exists()


def test_turning_tracking_on_asks_about_history(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "claude" / "projects", root)
    config = Config()
    old = config.local_usage.model_copy(deep=True)
    config.local_usage.enabled = True
    config.local_usage.claude.account_id = "claude"
    config.local_usage.claude.log_root = str(root)
    config.local_usage.codex.enabled = False
    asked = []
    requested = []
    monkeypatch.setattr(
        enable_dialog, "ask_backfill",
        lambda parent, scans: asked.append([s.provider for s in scans]) or enable_dialog.START_FROM_NOW,
    )
    monkeypatch.setattr(
        "aigauge.local_usage.service.LocalUsageService.request_import",
        lambda self, kind="incremental": requested.append(kind),
    )
    stub = _app_stub(config)

    app_module.App._sync_local_usage(stub, old)
    try:
        assert asked == [["claude"]]
        assert config.local_usage.claude.start_from is not None
        assert requested == ["incremental"]
        assert (app_data_dir() / "local_usage.sqlite").exists()

        # Turning it off stops tracking and keeps the saved data.
        config.local_usage.enabled = False
        stub._local_usage.deleteLater = lambda: None
        app_module.App._sync_local_usage(stub, config.local_usage.model_copy(deep=True))
        assert stub._local_usage is None
        assert (app_data_dir() / "local_usage.sqlite").exists()
    finally:
        if stub._local_usage is not None:
            stub._local_usage.shutdown()
