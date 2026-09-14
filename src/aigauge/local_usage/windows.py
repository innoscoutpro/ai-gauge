"""Allowance windows for local usage.

A window's usage is the activity from window start to the last quota reading,
and its quota figure is the percent at that reading.

- Claude: start is ``resets_at - window`` (5 hours for Session, 7 days for
  Weekly), taken from AI Gauge's own readings.
- Codex: the window comes from the ``resets_at`` and ``window_minutes`` that
  Codex writes into its logs; the reading is the last one at or before the time
  measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import SnapshotStatus, UsageSnapshot
from .codex_logs import CodexQuotaReading
from .store import CLAUDE, CODEX, UsageStore

SESSION = "Session"
WEEKLY = "Weekly"
FABLE = "Fable"
DEFAULT_WINDOWS = {SESSION: timedelta(hours=5), WEEKLY: timedelta(days=7)}
# Limits that apply to one model family: metric label -> (default window, the
# fragment a model name must contain to count toward it). Display only; window
# summaries and trends use DEFAULT_WINDOWS.
MODEL_LIMITS = {FABLE: (timedelta(days=7), "fable")}
CODEX_LIMIT_ID = "codex"
_METRIC_BY_MINUTES = {300: SESSION, 10080: WEEKLY}


@dataclass
class WindowSpec:
    metric: str
    start: datetime  # UTC
    resets_at: datetime  # UTC
    last_reading_at: datetime | None
    pct: float | None
    plan_type: str | None = None
    # Only models whose name contains this count toward the window's limit.
    model_filter: str | None = None


def to_utc(dt: datetime) -> datetime:
    """App timestamps are naive local time; log timestamps are aware."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def metric_for_minutes(minutes: int | None) -> str | None:
    return _METRIC_BY_MINUTES.get(minutes) if minutes is not None else None


def claude_windows_from_snapshot(snapshot: UsageSnapshot | None) -> dict[str, WindowSpec]:
    if snapshot is None or snapshot.status != SnapshotStatus.OK:
        return {}
    out = {}
    fetched = to_utc(snapshot.fetched_at)
    for metric in snapshot.metrics:
        if metric.resets_at is None:
            continue
        if metric.label in DEFAULT_WINDOWS:
            default, model_filter = DEFAULT_WINDOWS[metric.label], None
        elif metric.label in MODEL_LIMITS:
            default, model_filter = MODEL_LIMITS[metric.label]
        else:
            continue
        resets = to_utc(metric.resets_at)
        window = metric.window or default
        out[metric.label] = WindowSpec(
            metric=metric.label,
            start=resets - window,
            resets_at=resets,
            last_reading_at=fetched,
            pct=metric.percent_used,
            model_filter=model_filter,
        )
    return out


def codex_windows_from_readings(
    readings: list[CodexQuotaReading], now: datetime
) -> dict[str, WindowSpec]:
    """The current window per metric, from the latest reading that is still open."""
    out: dict[str, WindowSpec] = {}
    for reading in sorted(readings, key=lambda r: r.timestamp):
        metric = metric_for_minutes(reading.window_minutes)
        if (
            metric is None
            or reading.limit_id != CODEX_LIMIT_ID
            or reading.resets_at is None
            or reading.resets_at <= now
            or reading.timestamp > now
        ):
            continue
        out[metric] = WindowSpec(
            metric=metric,
            start=reading.resets_at - timedelta(minutes=reading.window_minutes),
            resets_at=reading.resets_at,
            last_reading_at=reading.timestamp,
            pct=reading.used_percent,
            plan_type=reading.plan_type,
        )
    return out


def current_windows(
    provider: str,
    snapshot: UsageSnapshot | None,
    store: UsageStore,
    now: datetime,
) -> dict[str, WindowSpec]:
    if provider == CLAUDE:
        return claude_windows_from_snapshot(snapshot)
    if provider == CODEX:
        readings = store.quota_readings(CODEX_LIMIT_ID, since=now - timedelta(days=8))
        windows = codex_windows_from_readings(readings, now)
        if windows:
            return windows
        # No log readings yet (for example, straight after enabling): fall back
        # to the website reading so the current window still shows.
        return claude_windows_from_snapshot(snapshot)
    return {}
