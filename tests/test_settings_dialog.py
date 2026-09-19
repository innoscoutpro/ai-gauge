from PyQt6.QtWidgets import QPushButton

from aigauge import settings_dialog
from aigauge.config import BrowserAccount, ColorThresholds, Config
from aigauge.settings_dialog import SettingsDialog


def _button(dialog: SettingsDialog, name: str) -> QPushButton:
    button = dialog.findChild(QPushButton, name)
    assert button is not None
    return button


def test_brand_header_showcases_app_identity(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    assert not dialog.brand_icon.pixmap().isNull()
    assert dialog.brand_icon.width() == 56
    assert dialog.brand_icon.height() == 56
    assert dialog.brand_title.text() == "AI Gauge"
    assert "usage at a glance" in dialog.brand_subtitle.text()
    assert settings_dialog.__version__ in dialog.brand_subtitle.text()


def test_sign_in_button_emits_sign_in_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.sign_in_clicked) as signal:
        _button(dialog, "claude_signin_btn").click()

    assert signal.args == ["claude"]


def test_paste_cookie_button_emits_paste_cookie_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.paste_cookie_clicked) as signal:
        _button(dialog, "codex_paste_cookie_btn").click()

    assert signal.args == ["codex"]


def test_clear_sign_in_button_emits_account_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.clear_sign_in_clicked) as signal:
        _button(dialog, "claude_clear_signin_btn").click()

    assert signal.args == ["claude"]


def test_claude_open_usage_button_launches_browser(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    _button(dialog, "claude_open_usage_btn").click()

    assert opened == [settings_dialog.CLAUDE_USAGE_URL]


def test_codex_open_usage_button_launches_browser(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    _button(dialog, "codex_open_usage_btn").click()

    assert opened == [settings_dialog.CODEX_USAGE_URL]


def test_mcp_integration_defaults_off_and_gates_policies(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    assert not dialog.mcp_enabled_cb.isChecked()
    enabled, threshold = dialog.mcp_policy_controls["codex"]
    assert not enabled.isEnabled()
    assert not threshold.isEnabled()

    dialog.mcp_enabled_cb.setChecked(True)
    enabled.setChecked(True)
    dialog.apply_to(config)

    assert config.mcp_enabled is True
    assert config.mcp_pause_policies["codex"] == 90




def test_opencode_go_uses_masked_api_key_field_instead_of_browser_buttons(
    qtbot, monkeypatch
):
    monkeypatch.setattr(settings_dialog, "get_opencode_go_key", lambda account_id: None)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    assert row.api_key_edit is not None
    assert row.api_key_edit.echoMode() == row.api_key_edit.EchoMode.Password
    assert dialog.findChild(QPushButton, "opencode_go_signin_btn") is None
    assert dialog.findChild(QPushButton, "opencode_go_paste_cookie_btn") is None


def test_opencode_go_accept_saves_key_to_account_secret(qtbot, monkeypatch):
    secrets = {}
    monkeypatch.setattr(
        settings_dialog,
        "get_opencode_go_key",
        lambda account_id: secrets.get(account_id),
    )
    monkeypatch.setattr(
        settings_dialog,
        "set_opencode_go_key",
        lambda account_id, value: (
            secrets.__setitem__(account_id, value)
            if value
            else secrets.pop(account_id, None)
        ),
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    row.api_key_edit.setText("oc_sk_test")

    dialog._accept()  # noqa: SLF001

    assert secrets == {"opencode_go": "oc_sk_test"}


def test_opencode_go_existing_key_can_be_cleared(qtbot, monkeypatch):
    secrets = {"opencode_go": "saved-key"}
    monkeypatch.setattr(
        settings_dialog,
        "get_opencode_go_key",
        lambda account_id: secrets.get(account_id),
    )
    monkeypatch.setattr(
        settings_dialog,
        "set_opencode_go_key",
        lambda account_id, value: (
            secrets.__setitem__(account_id, value)
            if value
            else secrets.pop(account_id, None)
        ),
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    assert row.clear_api_key_cb is not None
    assert not row.clear_api_key_cb.isHidden()
    row.clear_api_key_cb.setChecked(True)

    dialog._accept()  # noqa: SLF001

    assert secrets == {}


def test_opencode_go_key_draft_survives_adding_an_account(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "get_opencode_go_key", lambda account_id: None)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    first_row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    first_row.api_key_edit.setText("oc_sk_unsaved")

    dialog._add_browser_account("opencode_go")  # noqa: SLF001

    rebuilt_first_row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    assert rebuilt_first_row.api_key_edit.text() == "oc_sk_unsaved"


def test_removing_opencode_account_clears_its_key_on_accept(qtbot, monkeypatch):
    secrets = {"opencode_go": "saved-key"}
    monkeypatch.setattr(
        settings_dialog,
        "get_opencode_go_key",
        lambda account_id: secrets.get(account_id),
    )
    monkeypatch.setattr(
        settings_dialog,
        "set_opencode_go_key",
        lambda account_id, value: (
            secrets.__setitem__(account_id, value)
            if value
            else secrets.pop(account_id, None)
        ),
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    dialog._remove_browser_account("opencode_go")  # noqa: SLF001
    dialog._accept()  # noqa: SLF001

    assert secrets == {}


def test_opencode_go_settings_apply_multiple_accounts(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(settings_dialog, "get_opencode_go_key", lambda account_id: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.opencode_go_cb.setChecked(True)
    first_row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id == "opencode_go"
    )
    dialog._add_browser_account("opencode_go")  # noqa: SLF001
    second_row = next(
        row
        for row in dialog._browser_account_rows  # noqa: SLF001
        if row.account_id.startswith("opencode_go-")
    )
    second_row.name_edit.setText("Work")
    second_row.colors = ColorThresholds(
        green_max=20,
        yellow_max=50,
        orange_max=80,
    )
    dialog.apply_to(config)

    accounts = [a for a in config.browser_accounts if a.kind == "opencode_go"]
    assert config.providers.opencode_go is True
    assert [a.name for a in accounts] == [None, "Work"]
    assert accounts[1].colors.green_max == 20

def test_add_codex_account_creates_named_secondary_row(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._add_browser_account("codex")  # noqa: SLF001
    dialog.apply_to(config)

    codex_accounts = [a for a in config.browser_accounts if a.kind == "codex"]
    assert len(codex_accounts) == 2
    assert codex_accounts[1].name == "Account 2"
    assert codex_accounts[1].enabled is True


def test_color_thresholds_apply_per_account_and_copilot(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    codex_row = next(
        row for row in dialog._browser_account_rows if row.account_id == "codex"  # noqa: SLF001
    )
    codex_row.colors = ColorThresholds(
        green_max=20, yellow_max=50, orange_max=80
    )
    dialog.copilot_colors.colors = ColorThresholds(
        green_max=25, yellow_max=55, orange_max=85
    )
    dialog.apply_to(config)

    codex = next(a for a in config.browser_accounts if a.id == "codex")
    assert codex.colors == ColorThresholds(
        green_max=20, yellow_max=50, orange_max=80
    )
    assert config.copilot.colors == ColorThresholds(
        green_max=25, yellow_max=55, orange_max=85
    )


def test_color_threshold_dialog_resolves_visible_arrow_assets(qtbot):
    dialog = settings_dialog._ColorThresholdDialog(ColorThresholds())  # noqa: SLF001
    qtbot.addWidget(dialog)

    stylesheet = dialog.styleSheet()
    assert "__UP_ARROW__" not in stylesheet
    assert "__DOWN_ARROW__" not in stylesheet
    assert "QSpinBox::up-arrow" in stylesheet
    assert "QSpinBox::down-arrow" in stylesheet


def test_color_editor_updates_live_ranges_and_all_four_colors(qtbot):
    editor = settings_dialog._ColorThresholdEditor(ColorThresholds())  # noqa: SLF001
    qtbot.addWidget(editor)

    editor.green.setValue(25)
    editor.yellow.setValue(55)
    editor.orange.setValue(85)
    editor._colors["red"] = "#123456"  # noqa: SLF001
    editor._refresh_band_buttons()  # noqa: SLF001

    assert editor._color_buttons["green"].text() == "Green · 0–25%"  # noqa: SLF001
    assert editor._color_buttons["yellow"].text() == "Yellow · 26–55%"  # noqa: SLF001
    assert editor._color_buttons["orange"].text() == "Orange · 56–85%"  # noqa: SLF001
    assert editor._color_buttons["red"].text() == "Red · 86%+"  # noqa: SLF001
    assert editor.value().red_color == "#123456"

    editor.reset_defaults()
    assert editor.value() == ColorThresholds()


def test_remove_secondary_account_clears_session(qtbot, monkeypatch):
    removed = []
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog,
        "clear_browser_session",
        lambda account_id: removed.append(account_id),
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._add_browser_account("claude")  # noqa: SLF001
    account_id = dialog._browser_accounts[-1].id  # noqa: SLF001
    dialog._remove_browser_account(account_id)  # noqa: SLF001
    dialog.apply_to(config)

    assert removed == [account_id]


def test_remove_original_codex_account_and_add_one_back(qtbot, monkeypatch):
    removed = []
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog,
        "clear_browser_session",
        lambda account_id: removed.append(account_id),
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._remove_browser_account("codex")  # noqa: SLF001
    dialog.apply_to(config)

    assert [a for a in config.browser_accounts if a.kind == "codex"] == []
    assert removed == ["codex"]

    replacement = SettingsDialog(config)
    qtbot.addWidget(replacement)
    replacement._add_browser_account("codex")  # noqa: SLF001
    replacement.apply_to(config)

    codex_accounts = [a for a in config.browser_accounts if a.kind == "codex"]
    assert len(codex_accounts) == 1
    assert codex_accounts[0].name == "Account 1"

def test_fade_when_inactive_setting_applies(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    assert not dialog.fade_when_inactive_cb.isChecked()
    assert not dialog.opacity_slider.isEnabled()

    dialog.fade_when_inactive_cb.setChecked(True)
    dialog.opacity_slider.setValue(62)
    dialog.apply_to(config)

    assert config.window.fade_when_inactive is True
    assert config.window.opacity == 0.62


def test_square_corners_setting_applies(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    assert not dialog.square_corners_cb.isChecked()

    dialog.square_corners_cb.setChecked(True)
    dialog.apply_to(config)

    assert config.window.square_corners is True


def test_sign_in_browser_setting_applies(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.set_sign_in_browser("brave")
    dialog.apply_to(config)

    assert config.sign_in_browser == "brave"

def test_fable_toggle_is_per_claude_account(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config(
        browser_accounts=[
            BrowserAccount(id="claude", kind="claude"),
            BrowserAccount(id="claude-work", kind="claude", name="Work"),
            BrowserAccount(id="codex", kind="codex"),
        ]
    )
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    rows = {row.account_id: row for row in dialog._browser_account_rows}  # noqa: SLF001
    assert rows["codex"].fable_cb is None, "only Claude accounts offer the toggle"
    assert rows["claude"].fable_cb.isChecked(), "on by default"

    rows["claude-work"].fable_cb.setChecked(False)
    dialog.apply_to(config)

    saved = {a.id: a.show_fable for a in config.browser_accounts}
    assert saved["claude"] is True
    assert saved["claude-work"] is False, "each account is independent"


def test_fable_toggle_reflects_saved_account_state(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config(
        browser_accounts=[
            BrowserAccount(id="claude", kind="claude", show_fable=False),
        ]
    )
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    row = dialog._browser_account_rows[0]  # noqa: SLF001
    assert not row.fable_cb.isChecked()


def test_clear_saved_pat_checkbox_removes_existing_pat(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        settings_dialog, "get_github_pat", lambda: None if calls else "saved"
    )
    monkeypatch.setattr(
        settings_dialog, "set_github_pat", lambda value: calls.append(value)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    dialog.clear_pat_cb.setChecked(True)

    dialog._accept()  # noqa: SLF001

    assert calls == [None]


def test_mcp_policy_for_removed_account_is_not_saved(qtbot, monkeypatch):
    """The MCP tab's rows are built once, before an account can be removed.

    Without a save-side filter, apply_to() would persist a pause policy for an
    account that no longer exists — inert at guard time, but it accumulates in
    config and reappears with a stale label next time Settings opens.
    """
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(settings_dialog, "clear_browser_session", lambda account_id: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.mcp_enabled_cb.setChecked(True)
    for account_id in ("codex", "claude"):
        dialog.mcp_policy_controls[account_id][0].setChecked(True)

    # Drop the Codex account the way the Accounts tab does.
    dialog._browser_account_rows = [
        row for row in dialog._browser_account_rows if row.account_id != "codex"
    ]
    dialog.apply_to(config)

    assert "claude" in config.mcp_pause_policies
    assert "codex" not in config.mcp_pause_policies
