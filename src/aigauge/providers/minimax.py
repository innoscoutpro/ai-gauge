from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject

from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._scrape_runner import ScrapeRunner
from .base import Provider
from .codex import _parse_reset_text

MINIMAX_USAGE_URL = "https://platform.minimax.io/console/usage"
log = logging.getLogger("aigauge.providers.minimax")

# Read visible text only. The authenticated page has an explicit "My usage"
# section; API credits and subscription analytics below it are not plan limits.
EXTRACTOR_JS = r"""
(() => {
  const bodyText = ((document.body && document.body.innerText) || '')
    .replace(/\s+/g, ' ').trim();
  const path = location.pathname.toLowerCase();
  return {
    url: location.href,
    title: document.title,
    body_text: bodyText.slice(0, 8000),
    logged_out: /\/(?:login|sign-in|signin)(?:\/|$)/.test(path) ||
      (/sign in|log in/i.test(bodyText) && !/\bMy usage\b/i.test(bodyText)),
  };
})();
"""


def _is_usage_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "platform.minimax.io"
        and parsed.path.rstrip("/") == "/console/usage"
    )


def _build_snapshot(
    payload: dict[str, Any], *, account_id: str = "minimax"
) -> UsageSnapshot:
    url = str(payload.get("url") or "")
    if payload.get("logged_out") or (
        urlparse(url).netloc == "platform.minimax.io"
        and re.search(r"/(?:login|sign-in|signin)(?:/|$)", urlparse(url).path, re.I)
    ):
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in to MiniMax.",
            raw=payload,
        )
    if not _is_usage_url(url):
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="MiniMax redirected away from its plan usage page.",
            raw=payload,
        )
    text = re.sub(r"\s+", " ", str(payload.get("body_text") or "")).strip()
    section = re.search(r"\bMy usage\b(.*?)(?:\bCredit balance\b|$)", text, re.I)
    usage = section.group(1) if section else ""
    rolling = re.search(
        r"\b5h limit\b\s+Resets?\s+in\s+(.{1,60}?)\s+"
        r"Total quota\s+(\d+(?:\.\d+)?)\s*%\s+"
        r"Used\s+(\d+(?:\.\d+)?)\s*%",
        usage,
        re.I,
    )
    unlimited = re.search(r"\bWeekly limit\b.{0,32}\bUnlimited\b", usage, re.I)
    if not rolling or not unlimited:
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="Could not read MiniMax Coding Plan limits (layout may have changed).",
            raw=payload,
        )
    reset = _parse_reset_text(rolling.group(1))
    used = float(rolling.group(3))
    if reset is None or not 0 <= used <= 100:
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="MiniMax returned an invalid 5-hour limit or reset time.",
            raw=payload,
        )
    return UsageSnapshot(
        provider=account_id,
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric(
                label="5h", percent_used=used, resets_at=reset,
                window=timedelta(hours=5),
            ),
            UsageMetric(
                label="Weekly", percent_used=None,
                reset_label="Unlimited", note="MiniMax Coding Plan weekly limit is unlimited.",
            ),
        ],
        raw=payload,
    )


class MiniMaxProvider(Provider):
    name = "minimax"
    display_name = "MiniMax"

    def __init__(self, parent: QObject | None = None, account_id: str = "minimax"):
        self._parent = parent
        self._account_id = account_id
        self._runner: ScrapeRunner | None = None

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        self._runner = ScrapeRunner(
            account_id=self._account_id,
            url=MINIMAX_USAGE_URL,
            extractor_js=EXTRACTOR_JS,
            build=lambda payload: _build_snapshot(payload, account_id=self._account_id),
            log=log,
            wait_ms=7000,
            transport_max_attempts=2,
            build_max_attempts=2,
            parent=self._parent,
        )
        self._runner.run(on_done)
