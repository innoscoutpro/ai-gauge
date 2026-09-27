from types import SimpleNamespace

from aigauge import app as app_module
from aigauge import config as config_module
from aigauge.app import App, _enabled_providers
from aigauge.config import ColorThresholds, Config
from aigauge.gauge import thresholds_for_provider
from aigauge.mcp_server import _configured_account_ids
from aigauge.menubar import PROVIDER_LABELS
from aigauge.settings_dialog import SettingsDialog
from aigauge.widget import PROVIDER_ORDER


def test_antigravity_config_defaults_and_legacy_json(tmp_path, monkeypatch):
    config = Config()
    assert config.providers.antigravity is False
    assert config.antigravity.colors == ColorThresholds()
    assert config.antigravity.cli_path is None
    assert config.antigravity.show_gemini is True
    assert config.antigravity.show_third_party is True
    config_file = tmp_path / "config.json"
    config_file.write_text('{"providers": {"claude": false}}', encoding="utf-8")
    monkeypatch.setattr(config_module, "config_path", lambda: config_file)
    legacy = Config.load()
    assert legacy.providers.antigravity is False
    assert legacy.antigravity.cli_path is None


def test_antigravity_threshold_label_and_order():
    config = Config()
    colors = ColorThresholds(green_color="#123456")
    config.antigravity.colors = colors
    assert thresholds_for_provider(config, "antigravity") == colors
    assert PROVIDER_LABELS["antigravity"] == "Ag"
    assert PROVIDER_ORDER[-1] == "antigravity"


def test_mcp_account_ids_follow_antigravity_toggle():
    config = Config()
    config.providers.antigravity = True
    assert "antigravity" in _configured_account_ids(config)
    config.providers.antigravity = False
    assert "antigravity" not in _configured_account_ids(config)


def test_app_builds_antigravity_provider_with_config_and_tile(monkeypatch):
    config = Config()
    config.providers.claude = False
    config.providers.codex = False
    config.providers.copilot = False
    config.providers.antigravity = True
    config.antigravity.cli_path = "C:/tools/agy.exe"
    config.antigravity.show_gemini = False
    config.antigravity.show_third_party = True
    created = []
    ensured = []

    def fake_provider(**kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(app_module, "AntigravityProvider", fake_provider)
    app = App.__new__(App)
    app._config = config
    app._providers = {}
    app._snapshots = {}
    app._widget = SimpleNamespace(
        _tiles={},
        ensure_tile=lambda account_id, label: ensured.append((account_id, label)),
        remove_tile=lambda account_id: None,
    )

    assert "antigravity" in _enabled_providers(config)
    app._build_providers()

    assert created == [{
        "cli_path": "C:/tools/agy.exe",
        "show_gemini": False,
        "show_third_party": True,
    }]
    assert "antigravity" in app._providers
    assert ensured == [("antigravity", "Antigravity")]


def test_settings_dialog_shows_and_saves_antigravity_options(qtbot, monkeypatch):
    monkeypatch.setattr("aigauge.settings_dialog.resolve_cli", lambda _: "agy-test")
    monkeypatch.setattr("aigauge.settings_dialog.set_start_at_login", lambda _: None)
    config = Config()
    monkeypatch.setattr(Config, "save", lambda self: None)
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    index = next(
        i for i in range(dialog.tabs.count()) if dialog.tabs.tabText(i) == "Antigravity"
    )
    assert dialog.antigravity_cli_path_label.text() == "agy-test"
    dialog.antigravity_show_gemini_cb.setChecked(False)
    dialog.apply_to(config)

    assert config.antigravity.show_gemini is False
    dialog.show_provider("antigravity")
    assert dialog.tabs.currentIndex() == index


