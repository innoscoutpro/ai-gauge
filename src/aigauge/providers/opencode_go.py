from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Callable

import requests

from ..config import Config, get_opencode_go_key
from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from .base import Provider

OPENCODE_GO_USAGE_API = "https://opencode.ai/zen/go/v1/usage"

log = logging.getLogger("aigauge.providers.opencode_go")

_METRICS = (
    ("rolling", "Rolling", timedelta(hours=5)),
    ("weekly", "Weekly", timedelta(days=7)),
    ("monthly", "Monthly", timedelta(days=30)),
)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _fetch_usage(api_key: str) -> dict[str, Any]:
    response = requests.get(
        OPENCODE_GO_USAGE_API,
        headers=_headers(api_key),
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OpenCode returned an unexpected response.")
    log.debug(
        "provider api diagnosis provider=opencode_go "
        "classification=usage_ok status=%s payload_keys=%s",
        response.status_code,
        sorted(payload),
    )
    return payload


def _parse_reset_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing reset time")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        # The rest of AI Gauge uses local, timezone-naive datetimes for display,
        # scheduling, and history. Preserve the instant while matching that model.
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _build_snapshot(
    payload: dict[str, Any],
    *,
    account_id: str = "opencode_go",
) -> UsageSnapshot:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("OpenCode response is missing usage data.")

    metrics: list[UsageMetric] = []
    for key, label, window in _METRICS:
        row = usage.get(key)
        if not isinstance(row, dict):
            raise ValueError(f"OpenCode response is missing {label.lower()} usage.")
        try:
            percent = float(row["percent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"OpenCode returned an invalid {label.lower()} percentage."
            ) from exc
        if not math.isfinite(percent):
            raise ValueError(
                f"OpenCode returned an invalid {label.lower()} percentage."
            )
        status = str(row.get("status") or "").lower()
        if status not in ("ok", "rate-limited"):
            raise ValueError(
                f"OpenCode returned an unknown {label.lower()} usage status."
            )
        if status == "rate-limited":
            percent = max(100.0, percent)
        metrics.append(
            UsageMetric(
                label=label,
                percent_used=max(0.0, min(100.0, percent)),
                resets_at=_parse_reset_at(row.get("resetsAt")),
                window=window,
                note="Rate limit reached." if status == "rate-limited" else None,
            )
        )

    return UsageSnapshot(
        provider=account_id,
        status=SnapshotStatus.OK,
        metrics=metrics,
        raw=payload,
    )


class OpenCodeGoProvider(Provider):
    name = "opencode_go"
    display_name = "OpenCode"

    def __init__(
        self,
        config: Config,
        parent=None,
        account_id: str = "opencode_go",
        pool=None,
    ):
        # Keep config and parent in the signature for compatibility with the
        # other account providers and existing app construction.
        self._config = config
        self._account_id = account_id
        self._pool = pool

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        api_key = get_opencode_go_key(self._account_id)
        if not api_key:
            on_done(
                UsageSnapshot(
                    provider=self._account_id,
                    status=SnapshotStatus.AUTH_REQUIRED,
                    error="Add an OpenCode Go API key in Settings.",
                )
            )
            return

        def work() -> UsageSnapshot:
            try:
                return _build_snapshot(
                    _fetch_usage(api_key),
                    account_id=self._account_id,
                )
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                log.warning(
                    "provider api diagnosis provider=%s "
                    "classification=usage_http_error status=%s",
                    self._account_id,
                    status,
                )
                if status == 401:
                    return UsageSnapshot(
                        provider=self._account_id,
                        status=SnapshotStatus.AUTH_REQUIRED,
                        error="OpenCode rejected the API key. Update it in Settings.",
                    )
                if status == 403:
                    return UsageSnapshot(
                        provider=self._account_id,
                        status=SnapshotStatus.ERROR,
                        error="This API key does not have an active OpenCode Go subscription.",
                    )
                return UsageSnapshot(
                    provider=self._account_id,
                    status=SnapshotStatus.ERROR,
                    error=f"OpenCode usage API returned HTTP {status}.",
                )
            except requests.RequestException as exc:
                return UsageSnapshot(
                    provider=self._account_id,
                    status=SnapshotStatus.ERROR,
                    error=f"OpenCode usage request failed: {exc}",
                )
            except (TypeError, ValueError) as exc:
                log.warning(
                    "provider api diagnosis provider=%s "
                    "classification=unexpected_response error=%s",
                    self._account_id,
                    exc,
                )
                return UsageSnapshot(
                    provider=self._account_id,
                    status=SnapshotStatus.ERROR,
                    error=str(exc),
                )

        self._run_async(work, on_done)
