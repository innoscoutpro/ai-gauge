import json
import shutil
import subprocess

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QPushButton

from aigauge.webview import login_window
from aigauge.webview.login_window import (
    LoginWindow,
    VERIFY_TARGETS,
    _host_allowed,
    _is_google_host,
    _safe_url_for_log,
)


def _run_target_js(tmp_path, provider: str, state: dict) -> bool:
    if shutil.which("node") is None:
        pytest.skip("Node.js not available")
    harness = tmp_path / "verify-harness.js"
    script = tmp_path / "verify-target.js"
    payload = tmp_path / "verify-state.json"
    harness.write_text(
        """
const fs = require('fs');
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
global.document = {body: {innerText: state.body}, title: state.title};
global.location = state.location;
const result = eval(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(result));
""",
        encoding="utf-8",
    )
    script.write_text(VERIFY_TARGETS[provider][1], encoding="utf-8")
    payload.write_text(json.dumps(state), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(payload), str(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(json.loads(completed.stdout))


def test_google_hosts_are_detected_and_allowlisted():
    assert _is_google_host("accounts.google.com")
    assert _is_google_host("google.com")
    assert _host_allowed("accounts.google.com")
    assert _host_allowed("accounts.youtube.com")


def test_logged_blocked_url_drops_query_and_fragment():
    url = QUrl(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        "login_hint=person@example.com#frag"
    )

    assert _safe_url_for_log(url) == "https://accounts.google.com/o/oauth2/v2/auth"


def test_codex_verification_accepts_weekly_only_usage_page():
    _, check_js = VERIFY_TARGETS["codex"]

    success_check = check_js.split("return true", maxsplit=1)[0]
    assert "/Weekly usage limit/i.test(text)" in success_check
    assert "/5 hour usage limit/i.test(text)" not in success_check
    assert (
        """querySelectorAll('button,a,[role="tab"],[role="button"]')""" in check_js
    )
    assert ",div,span,p" not in check_js


def test_claude_verification_accepts_authenticated_home_shell(tmp_path):
    state = {
        "body": (
            "Home Code New Projects Artifacts Scheduled Customize Chats and tasks "
            "How can I help you today? John Max"
        ),
        "title": "New chat - Claude",
        "location": {
            "hostname": "claude.ai",
            "pathname": "/new",
            "hash": "#settings/usage",
        },
    }

    assert _run_target_js(tmp_path, "claude", state)


def test_claude_verification_rejects_login_ui_on_authenticated_route(tmp_path):
    state = {
        "body": "Log in to Claude Continue with Google Create an account",
        "title": "Claude",
        "location": {
            "hostname": "claude.ai",
            "pathname": "/new",
            "hash": "#settings/usage",
        },
    }

    assert not _run_target_js(tmp_path, "claude", state)


def test_claude_verification_rejects_login_page(tmp_path):
    state = {
        "body": "Log in to Claude",
        "title": "Claude",
        "location": {
            "hostname": "claude.ai",
            "pathname": "/login",
            "hash": "",
        },
    }

    assert not _run_target_js(tmp_path, "claude", state)


def test_claude_verification_rejects_usage_text_on_another_host(tmp_path):
    state = {
        "body": "Plan usage limits Current session All models",
        "title": "Claude",
        "location": {
            "hostname": "example.com",
            "pathname": "/new",
            "hash": "#settings/usage",
        },
    }

    assert not _run_target_js(tmp_path, "claude", state)


def test_external_failure_returns_to_choice_without_opening_embedded():
    calls = []

    class Status:
        def setText(self, value):  # noqa: N802 - Qt-shaped test double
            calls.append(("status", value))

        def setStyleSheet(self, value):  # noqa: N802 - Qt-shaped test double
            calls.append(("style", value))

    class Dialog:
        _closing = False
        _status = Status()

        def _show_browser_choice(self, message):
            calls.append(("choice", message))

    LoginWindow._on_external_failed(Dialog(), "browser closed")

    assert ("choice", "browser closed") in calls
    assert all(name != "embedded" for name, _value in calls)


def test_visible_page_owns_the_dialog_default_action(qtbot):
    continue_btn = QPushButton("Continue")
    verify_btn = QPushButton("I'm signed in")
    qtbot.addWidget(continue_btn)
    qtbot.addWidget(verify_btn)

    class Dialog:
        _continue_btn = continue_btn
        _verify_btn = verify_btn

    dialog = Dialog()

    LoginWindow._set_default_action(dialog, continue_btn)
    assert continue_btn.isDefault()
    assert not verify_btn.isDefault()

    LoginWindow._set_default_action(dialog, verify_btn)
    assert not continue_btn.isDefault()
    assert verify_btn.isDefault()

    LoginWindow._set_default_action(dialog, None)
    assert not continue_btn.isDefault()
    assert not verify_btn.isDefault()


def test_embedded_auth_cookie_schedules_automatic_verification(monkeypatch):
    timers = []

    class Status:
        def setText(self, value):  # noqa: N802 - Qt-shaped test double
            return None

        def setStyleSheet(self, value):  # noqa: N802 - Qt-shaped test double
            return None

    class Cookie:
        @staticmethod
        def name():
            return b"sessionKey"

        @staticmethod
        def domain():
            return ".claude.ai"

    class Dialog:
        _closing = False
        _embedded_active = True
        _provider = "claude"
        _embedded_auth_seen = False
        _session_may_have_changed = False
        _embedded_verify_scheduled = False
        _verifying = False
        _embedded_status = Status()

        @staticmethod
        def _set_label_status(label, text, color):
            label.setText(text)
            label.setStyleSheet(color)

        def _verify_after_embedded_cookie(self):
            return None

    monkeypatch.setattr(
        login_window.QTimer,
        "singleShot",
        lambda delay, callback: timers.append((delay, callback)),
    )
    dialog = Dialog()

    LoginWindow._on_embedded_cookie_added(dialog, Cookie())

    assert dialog._embedded_auth_seen is True
    assert dialog._session_may_have_changed is True
    assert dialog._embedded_verify_scheduled is True
    assert timers[0][0] == 1000


def test_authenticated_embedded_shell_closes_without_manual_verification(monkeypatch):
    timers = []
    accepted = []

    class Status:
        def setText(self, value):  # noqa: N802 - Qt-shaped test double
            return None

        def setStyleSheet(self, value):  # noqa: N802 - Qt-shaped test double
            return None

    class Dialog:
        _closing = False
        _embedded_active = True
        _embedded_auth_seen = False
        _session_may_have_changed = False
        _embedded_verify_scheduled = False
        _verifying = False
        _embedded_status = Status()

        @staticmethod
        def _set_label_status(label, text, color):
            label.setText(text)
            label.setStyleSheet(color)

        def _accept_embedded_session(self):
            accepted.append(True)

    monkeypatch.setattr(
        login_window.QTimer,
        "singleShot",
        lambda delay, callback: timers.append((delay, callback)),
    )
    dialog = Dialog()

    LoginWindow._on_embedded_shell_result(dialog, True)

    assert dialog._embedded_auth_seen is True
    assert dialog._session_may_have_changed is True
    assert dialog._embedded_verify_scheduled is True
    assert timers[0][0] == 250
    timers[0][1]()
    assert accepted == [True]


def test_stopping_external_login_waits_for_worker_cleanup():
    calls = []

    class Worker:
        def stop(self):
            calls.append("stop")

        def isRunning(self):  # noqa: N802 - Qt-shaped test double
            calls.append("is_running")
            return True

        def wait(self):
            calls.append("wait")
            return True

    class Dialog:
        _external_worker = Worker()

    dialog = Dialog()

    LoginWindow._stop_external_login(dialog)

    assert calls == ["stop", "is_running", "wait"]
    assert dialog._external_worker is None


def test_closed_dialog_does_not_start_external_login():
    class Dialog:
        _closing = True
        _external_worker = None

    dialog = Dialog()

    LoginWindow._start_external_login(dialog)

    assert dialog._external_worker is None
