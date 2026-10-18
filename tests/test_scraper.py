from typing import Any, cast

from aigauge.webview.scraper import HeadlessScraper, _prepare_provider_view
from PyQt6.QtCore import QUrl, Qt


class _LoginPage:
    def url(self):
        return QUrl("https://claude.ai/login")

    def title(self):
        return "Claude"


class _FailedLoginScrape:
    _provider = "claude"
    _finished = False
    _max_progress = 100
    _last_load_status = "LoadStartedStatus"
    _last_load_is_error_page = False
    _page: Any = _LoginPage()

    def __init__(self):
        self.result = None

    def _finish(self, payload, error):
        self.result = (payload, error)


def test_failed_load_on_claude_login_reports_auth_instead_of_transport_failure():
    scrape = _FailedLoginScrape()
    HeadlessScraper._on_load_finished(cast(Any, scrape), False)
    assert scrape.result == ({"logged_out": True, "url": "https://claude.ai/login"}, "")


def test_failed_load_on_sign_in_page_at_new_route_reports_auth():
    scrape = _FailedLoginScrape()
    scrape._page = type("Page", (), {
        "url": lambda self: QUrl("https://claude.ai/new#settings/usage"),
        "title": lambda self: "Sign in - Claude",
    })()
    HeadlessScraper._on_load_finished(cast(Any, scrape), False)
    assert scrape.result == ({"logged_out": True, "url": "https://claude.ai/new"}, "")


def test_failed_load_on_other_page_remains_transport_failure():
    scrape = _FailedLoginScrape()
    scrape._page = type("Page", (), {
        "url": lambda self: QUrl("https://claude.ai/new"),
        "title": lambda self: "Claude",
    })()
    HeadlessScraper._on_load_finished(cast(Any, scrape), False)
    assert scrape.result == (None, "page failed to load")


def test_extractor_retry_limit_is_retryable_transport_error():
    assert "extractor retry limit exceeded" in HeadlessScraper._RETRYABLE_ERRORS


class _View:
    def __init__(self):
        self.attributes: list[tuple[object, bool]] = []
        self.shown = False

    def setAttribute(self, attribute, enabled):
        self.attributes.append((attribute, enabled))

    def show(self):
        self.shown = True


def test_minimax_provider_view_is_visible_to_chromium_without_being_shown_on_desktop():
    view = _View()

    _prepare_provider_view(cast(Any, view), "minimax")

    assert view.attributes == [(Qt.WidgetAttribute.WA_DontShowOnScreen, True)]
    assert view.shown is True


def test_minimax_team_provider_view_is_visible_to_chromium_without_being_shown_on_desktop():
    view = _View()

    _prepare_provider_view(cast(Any, view), "minimax-team")

    assert view.attributes == [(Qt.WidgetAttribute.WA_DontShowOnScreen, True)]
    assert view.shown is True


def test_other_provider_views_remain_hidden():
    view = _View()

    _prepare_provider_view(cast(Any, view), "claude")

    assert view.attributes == []
    assert view.shown is False
