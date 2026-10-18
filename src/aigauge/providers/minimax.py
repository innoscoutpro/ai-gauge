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

# Extract only the plan-limit portion needed for the tile. Do not retain a full
# authenticated page body: it can contain account and usage-history data.
EXTRACTOR_JS = r"""
(() => {
  const text = ((document.body && document.body.innerText) || '')
    .replace(/\s+/g, ' ').trim();
  const start = text.search(/\bMy usage\b/i);
  const end = start < 0 ? -1 : text.slice(start).search(/\bCredit balance\b/i);
  const usageText = start < 0 ? '' : text.slice(start, end < 0 ? undefined : start + end);
  const path = location.pathname.toLowerCase();
  return {
    url: location.href.split('?')[0].split('#')[0],
    usage_text: usageText.slice(0, 600),
    logged_out: /\/(?:login|sign-in|signin)(?:\/|$)/.test(path) ||
      (/sign in|log in/i.test(text) && !/\bMy usage\b/i.test(text)),
  };
})();
"""


def _safe_raw(payload: dict[str, Any]) -> dict[str, object]:
    """Keep diagnostics limited to URL and auth state, never rendered text."""
    return {
        "url": str(payload.get("url") or ""),
        "logged_out": bool(payload.get("logged_out")),
    }


def _is_usage_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "platform.minimax.io"
        and parsed.path.rstrip("/") == "/console/usage"
        and parsed.port is None
    )


def _build_snapshot(
    payload: dict[str, Any], *, account_id: str = "minimax"
) -> UsageSnapshot:
    raw = _safe_raw(payload)
    url = str(raw["url"])
    parsed = urlparse(url)
    if payload.get("logged_out") or (
        parsed.netloc == "platform.minimax.io"
        and re.search(r"/(?:login|sign-in|signin)(?:/|$)", parsed.path, re.I)
    ):
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in to MiniMax.",
            raw=raw,
        )
    if not _is_usage_url(url):
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="MiniMax redirected away from its plan usage page.",
            raw=raw,
        )
    usage = re.sub(r"\s+", " ", str(payload.get("usage_text") or "")).strip()
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
            raw=raw,
        )
    reset = _parse_reset_text(rolling.group(1))
    used = float(rolling.group(3))
    if reset is None or not 0 <= used <= 100:
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="MiniMax returned an invalid 5-hour limit or reset time.",
            raw=raw,
        )
    return UsageSnapshot(
        provider=account_id,
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric(
                label="5h", percent_used=used, resets_at=reset, window=timedelta(hours=5)
            ),
            UsageMetric(
                label="Weekly",
                percent_used=None,
                reset_label="Unlimited",
                note="MiniMax Coding Plan weekly limit is unlimited.",
            ),
        ],
        raw=raw,
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
