from typing import Any, cast

from aigauge.webview.scraper import HeadlessScraper
from PyQt6.QtCore import QUrl


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
