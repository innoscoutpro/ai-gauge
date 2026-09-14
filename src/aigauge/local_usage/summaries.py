"""Window summaries: the usage and quota figure for each allowance window.

Summaries are saved during a window, not only at rollover, so history
survives app shutdowns and Claude Code's log cleanup. Each one keeps its tokens
by model, price variant and category; costs are applied when it is read.

- Claude: live summaries come from AI Gauge's in-flight periods
  (``history.py``), are updated after each import and finalized when the
  period closes. Closed periods in ``history.jsonl`` whose whole window is
  covered by imported logs are backfilled. Old ``history.jsonl`` timestamps are
  naive local time; a window that spans a daylight saving change is marked
  time uncertain.
- Codex: windows come from the quota readings Codex writes into its logs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..history import PeriodRecord
from .assignment import assigned_account, comparison_period_id
from .codex_logs import CodexQuotaReading
from .store import CLAUDE, CODEX, ModelUsage, UsageStore, from_db_time, to_db_time
from .tokens import CATEGORIES, TokenCounts
from .windows import CODEX_LIMIT_ID, DEFAULT_WINDOWS, metric_for_minutes

ORIGIN_LIVE = "live"
ORIGIN_BACKFILL = "backfill"
FLAG_INCOMPLETE = "incomplete"
FLAG_TIME_UNCERTAIN = "time_uncertain"
FLAG_LIMIT_REACHED = "limit_reached"

# Rendered reset times drift by minutes between readings; a real rollover moves
# resets_at by at least the 5-hour session length. Mirrors history.py.
_SAME_WINDOW = timedelta(hours=2)
# Closed Codex windows older than this are not recomputed on every import.
_RECOMPUTE_RECENT = timedelta(days=8)
# Codex writes a usage delta and its quota reading on the same line, with the
# same timestamp; the window end is inclusive of that reading.
_INCLUSIVE = timedelta(microseconds=1)


@dataclass
class WindowSummary:
    account_id: str
    metric: str
    window_start: datetime
    resets_at: datetime
    last_reading_at: datetime | None
    last_pct: float | None
    usage: list[ModelUsage]
    origin: str
    flags: set[str] = field(default_factory=set)
    period_id: str = ""
    closed: bool = False
    updated_at: datetime | None = None

    @property
    def limit_id(self) -> str:
        """Codex limit for non-default limits, encoded in the metric name."""
        if self.metric.endswith(")") and " (" in self.metric:
            return self.metric.rsplit(" (", 1)[1][:-1]
        return CODEX_LIMIT_ID

    @property
    def base_metric(self) -> str:
        return self.metric.rsplit(" (", 1)[0] if self.metric.endswith(")") else self.metric

    def to_row(self) -> dict:
        return {
            "account_id": self.account_id,
            "metric": self.metric,
            "window_start": to_db_time(self.window_start),
            "resets_at": to_db_time(self.resets_at),
            "last_reading_at": to_db_time(self.last_reading_at) if self.last_reading_at else None,
            "last_pct": self.last_pct,
            "usage_json": usage_to_json(self.usage),
            "origin": self.origin,
            "flags": json.dumps(sorted(self.flags)),
            "period_id": self.period_id,
            "closed": self.closed,
            "updated_at": to_db_time(self.updated_at or datetime.now(timezone.utc)),
        }

    @classmethod
    def from_row(cls, row: dict) -> WindowSummary | None:
        start = from_db_time(row.get("window_start"))
        resets = from_db_time(row.get("resets_at"))
        if start is None or resets is None:
            return None
        try:
            flags = set(json.loads(row.get("flags") or "[]"))
        except ValueError:
            flags = set()
        return cls(
            account_id=row["account_id"],
            metric=row["metric"],
            window_start=start,
            resets_at=resets,
            last_reading_at=from_db_time(row.get("last_reading_at")),
            last_pct=row.get("last_pct"),
            usage=usage_from_json(row.get("usage_json")),
            origin=row.get("origin") or ORIGIN_LIVE,
            flags=flags,
            period_id=row.get("period_id") or "",
            closed=bool(row.get("closed")),
            updated_at=from_db_time(row.get("updated_at")),
        )


def usage_to_json(rows: Iterable[ModelUsage]) -> str:
    models: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        entry = models.setdefault(row.model, {}).setdefault(row.variant, {})
        for name in CATEGORIES:
            entry[name] = entry.get(name, 0) + getattr(row.tokens, name)
        entry["messages"] = entry.get("messages", 0) + row.messages
    return json.dumps({"models": models}, sort_keys=True)


def usage_from_json(text: object) -> list[ModelUsage]:
    try:
        data = json.loads(text) if isinstance(text, str) else {}
    except ValueError:
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return []
    out = []
    for model, variants in models.items():
        if not isinstance(variants, dict):
            continue
        for variant, counts in variants.items():
            if not isinstance(counts, dict):
                continue
            messages = counts.get("messages")
            out.append(
                ModelUsage(
                    model,
                    variant,
                    TokenCounts.from_dict(counts),
                    int(messages) if isinstance(messages, (int, float)) else 0,
                )
            )
    return out


def load_summaries(store: UsageStore, account_id: str, metric: str | None = None) -> list[WindowSummary]:
    out = []
    for row in store.window_summaries(account_id, metric):
        summary = WindowSummary.from_row(row)
        if summary is not None:
            out.append(summary)
    return out


def _find_same_window(
    existing: Iterable[WindowSummary], metric: str, resets_at: datetime
) -> WindowSummary | None:
    for summary in existing:
        if summary.metric == metric and abs(summary.resets_at - resets_at) < _SAME_WINDOW:
            return summary
    return None


def save_summary(store: UsageStore, summary: WindowSummary) -> WindowSummary:
    """Insert or update, matching an existing row for the same window."""
    existing = _find_same_window(
        load_summaries(store, summary.account_id, summary.metric),
        summary.metric,
        summary.resets_at,
    )
    if existing is not None:
        if existing.origin == ORIGIN_LIVE:
            summary.origin = ORIGIN_LIVE
        if existing.resets_at != summary.resets_at:
            store.delete_window_summary(
                existing.account_id, existing.metric, to_db_time(existing.resets_at)
            )
    store.upsert_window_summary(summary.to_row())
    return summary


# ---- Claude ----


def local_offset(naive_local: datetime) -> timedelta | None:
    """UTC offset of a naive local time under this machine's timezone rules."""
    return naive_local.astimezone().utcoffset()


def _parse_app_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def claude_summary(
    store: UsageStore,
    record: PeriodRecord,
    *,
    period_id: str,
    closed: bool,
    origin: str,
    now: datetime,
) -> WindowSummary | None:
    window = DEFAULT_WINDOWS.get(record.label)
    resets_local = _parse_app_time(record.resets_at)
    last_local = _parse_app_time(record.last_seen_at)
    if window is None or resets_local is None or last_local is None:
        return None
    resets = _as_utc(resets_local)
    last = _as_utc(last_local)
    start = resets - window
    end = min(last, resets)
    flags: set[str] = set()
    if resets_local.tzinfo is None and local_offset(resets_local - window) != local_offset(resets_local):
        flags.add(FLAG_TIME_UNCERTAIN)
    if not store.covers(CLAUDE, start, end):
        flags.add(FLAG_INCOMPLETE)
    if record.peak_pct >= 100:
        flags.add(FLAG_LIMIT_REACHED)
    return WindowSummary(
        account_id=record.provider,
        metric=record.label,
        window_start=start,
        resets_at=resets,
        last_reading_at=end,
        last_pct=float(record.peak_pct),
        usage=store.usage_by_model(CLAUDE, start, end),
        origin=origin,
        flags=flags,
        period_id=period_id,
        closed=closed,
        updated_at=now,
    )


def update_claude(
    store: UsageStore,
    config: Config,
    account_id: str,
    *,
    current: Iterable[PeriodRecord],
    closed: Iterable[PeriodRecord],
    history: Iterable[PeriodRecord],
    now: datetime,
) -> None:
    period_id = comparison_period_id(config, CLAUDE)
    for record in closed:
        if record.provider == account_id:
            summary = claude_summary(
                store, record, period_id=period_id, closed=True, origin=ORIGIN_LIVE, now=now
            )
            if summary is not None:
                save_summary(store, summary)
    for record in current:
        if record.provider == account_id:
            summary = claude_summary(
                store, record, period_id=period_id, closed=False, origin=ORIGIN_LIVE, now=now
            )
            if summary is not None:
                save_summary(store, summary)

    existing = load_summaries(store, account_id)
    for record in history:
        if record.provider != account_id or record.label not in DEFAULT_WINDOWS:
            continue
        resets_local = _parse_app_time(record.resets_at)
        if resets_local is None:
            continue
        if _find_same_window(existing, record.label, _as_utc(resets_local)) is not None:
            continue
        summary = claude_summary(
            store, record, period_id=period_id, closed=True, origin=ORIGIN_BACKFILL, now=now
        )
        # Only windows the imported logs fully cover are backfilled.
        if summary is None or FLAG_INCOMPLETE in summary.flags:
            continue
        save_summary(store, summary)
        existing.append(summary)


# ---- Codex ----


def _codex_metric(limit_id: str, minutes: int) -> str:
    base = metric_for_minutes(minutes) or f"{minutes} min"
    return base if limit_id == CODEX_LIMIT_ID else f"{base} ({limit_id})"


def _cluster_readings(readings: Iterable[CodexQuotaReading]) -> list[list[CodexQuotaReading]]:
    groups: dict[tuple[str, int], list[CodexQuotaReading]] = {}
    for reading in readings:
        if reading.resets_at is None or reading.window_minutes is None:
            continue
        groups.setdefault((reading.limit_id, reading.window_minutes), []).append(reading)
    clusters: list[list[CodexQuotaReading]] = []
    for group in groups.values():
        group.sort(key=lambda r: (r.resets_at, r.timestamp))
        current: list[CodexQuotaReading] = []
        for reading in group:
            if current and reading.resets_at - current[-1].resets_at >= _SAME_WINDOW:
                clusters.append(current)
                current = []
            current.append(reading)
        if current:
            clusters.append(current)
    return clusters


def update_codex(store: UsageStore, config: Config, account_id: str, *, now: datetime) -> None:
    base_period = comparison_period_id(config, CODEX)
    existing = load_summaries(store, account_id)
    for cluster in _cluster_readings(store.quota_readings()):
        in_window = [r for r in cluster if r.timestamp <= r.resets_at]
        if not in_window:
            continue
        last = max(in_window, key=lambda r: r.timestamp)
        resets = last.resets_at
        minutes = last.window_minutes
        metric = _codex_metric(last.limit_id, minutes)
        closed = resets <= now
        previous = _find_same_window(existing, metric, resets)
        if (
            previous is not None
            and previous.closed
            and closed
            and resets < now - _RECOMPUTE_RECENT
        ):
            continue
        start = resets - timedelta(minutes=minutes)
        flags: set[str] = set()
        if not store.covers(CODEX, start, last.timestamp):
            flags.add(FLAG_INCOMPLETE)
        if last.used_percent >= 100:
            flags.add(FLAG_LIMIT_REACHED)
        summary = WindowSummary(
            account_id=account_id,
            metric=metric,
            window_start=start,
            resets_at=resets,
            last_reading_at=last.timestamp,
            last_pct=last.used_percent,
            usage=store.usage_by_model(CODEX, start, last.timestamp + _INCLUSIVE),
            origin=previous.origin if previous else (ORIGIN_BACKFILL if closed else ORIGIN_LIVE),
            flags=flags,
            # A plan change (for example Plus to Pro) starts a new comparison period.
            period_id=f"{base_period}|{last.plan_type or ''}",
            closed=closed,
            updated_at=now,
        )
        save_summary(store, summary)
        if previous is None:
            existing.append(summary)


def update_all(
    store: UsageStore,
    config: Config,
    *,
    claude_current: Iterable[PeriodRecord] = (),
    claude_closed: Iterable[PeriodRecord] = (),
    history: Iterable[PeriodRecord] = (),
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    claude_account = assigned_account(config, CLAUDE)
    if claude_account is not None:
        update_claude(
            store,
            config,
            claude_account,
            current=claude_current,
            closed=claude_closed,
            history=history,
            now=now,
        )
    codex_account = assigned_account(config, CODEX)
    if codex_account is not None:
        update_codex(store, config, codex_account, now=now)
