"""SQLite storage for local usage.

One database, ``app_data_dir()/local_usage.sqlite``. Timestamps are stored as
UTC ISO strings with a fixed format, so string comparison orders them.

Costs are never stored: every row and summary keeps tokens by model, price
variant and category, and prices are applied when the data is read.

Raw message and usage rows are kept for ``RETENTION_DAYS``. Daily per-model
totals are recomputed after each import for the days it touched and kept
indefinitely, so pruning raw rows never loses the daily or per-model history.
Events older than the prune watermark are never imported again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .claude_logs import ClaudeMessage
from .codex_logs import CodexQuotaReading, CodexUsage
from .tokens import CATEGORIES, TokenCounts, price_variant

log = logging.getLogger("aigauge.local_usage.store")

SCHEMA_VERSION = 1
DB_FILENAME = "local_usage.sqlite"
RETENTION_DAYS = 90
CLAUDE = "claude"
CODEX = "codex"
PROVIDERS = (CLAUDE, CODEX)

_TOKEN_COLS = ", ".join(CATEGORIES)
_TOKEN_SUMS = ", ".join(f"SUM({c})" for c in CATEGORIES)
_TOKEN_PLACEHOLDERS = ", ".join("?" for _ in CATEGORIES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    read_offset INTEGER NOT NULL,
    tail_hash TEXT,
    parser_state TEXT,
    last_import TEXT,
    UNIQUE (provider, path)
);
CREATE TABLE IF NOT EXISTS claude_messages (
    message_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    variant TEXT NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL,
    reasoning INTEGER NOT NULL,
    speed TEXT,
    service_tier TEXT,
    cli_version TEXT,
    is_sidechain INTEGER NOT NULL,
    PRIMARY KEY (message_id, request_id)
);
CREATE INDEX IF NOT EXISTS claude_messages_ts ON claude_messages (timestamp);
CREATE TABLE IF NOT EXISTS codex_usage (
    source_id INTEGER NOT NULL,
    line_offset INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    variant TEXT NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL,
    reasoning INTEGER NOT NULL,
    PRIMARY KEY (source_id, line_offset)
);
CREATE INDEX IF NOT EXISTS codex_usage_ts ON codex_usage (timestamp);
CREATE TABLE IF NOT EXISTS codex_quota_readings (
    source_id INTEGER NOT NULL,
    line_offset INTEGER NOT NULL,
    slot TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    limit_id TEXT NOT NULL,
    used_percent REAL NOT NULL,
    window_minutes INTEGER,
    resets_at TEXT,
    plan_type TEXT,
    PRIMARY KEY (source_id, line_offset, slot)
);
CREATE INDEX IF NOT EXISTS codex_quota_ts ON codex_quota_readings (timestamp);
CREATE TABLE IF NOT EXISTS window_summaries (
    account_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_start TEXT NOT NULL,
    resets_at TEXT NOT NULL,
    last_reading_at TEXT,
    last_pct REAL,
    usage_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    flags TEXT NOT NULL,
    period_id TEXT NOT NULL,
    closed INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, metric, resets_at)
);
CREATE TABLE IF NOT EXISTS daily_model_totals (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    variant TEXT NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL,
    reasoning INTEGER NOT NULL,
    messages INTEGER NOT NULL,
    PRIMARY KEY (day, provider, model, variant)
);
CREATE TABLE IF NOT EXISTS coverage (
    provider TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL
);
"""


def to_db_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive values in this app are local time
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_db_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_date(dt: datetime) -> date:
    return dt.astimezone().date()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start and end of a local calendar day."""
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


@dataclass
class SourceFileRecord:
    id: int
    provider: str
    path: str
    size: int
    mtime: float
    read_offset: int
    tail_hash: str | None
    parser_state: str | None
    last_import: str | None


@dataclass
class ModelUsage:
    model: str
    variant: str
    tokens: TokenCounts
    messages: int


def _usage_from_row(row: sqlite3.Row | tuple, start: int = 2) -> tuple[TokenCounts, int]:
    values = list(row[start : start + len(CATEGORIES)])
    tokens = TokenCounts(*(int(v or 0) for v in values))
    messages = int(row[start + len(CATEGORIES)] or 0)
    return tokens, messages


class UsageStore:
    """Thread-safe access to the local usage database.

    Each thread gets its own connection; WAL mode lets the dialog read while
    the import worker writes.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        conn = self._conn()
        with conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

    # ---- connections ----

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path), timeout=10, isolation_level=None, check_same_thread=False
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    @contextmanager
    def transaction(self):
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def close_thread_connection(self) -> None:
        """Close only the calling thread's connection (used by worker threads)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def schema_version(self) -> int:
        row = self._conn().execute("SELECT version FROM schema_version").fetchone()
        return int(row[0]) if row else 0

    # ---- meta ----

    def get_meta(self, key: str, default=None):
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set_meta(self, key: str, value, conn: sqlite3.Connection | None = None) -> None:
        (conn or self._conn()).execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def watermark(self) -> datetime | None:
        return from_db_time(self.get_meta("prune_watermark"))

    # ---- source files ----

    def get_source_file(self, provider: str, path: str) -> SourceFileRecord | None:
        row = self._conn().execute(
            "SELECT id, provider, path, size, mtime, read_offset, tail_hash, "
            "parser_state, last_import FROM source_files WHERE provider = ? AND path = ?",
            (provider, path),
        ).fetchone()
        return SourceFileRecord(*row) if row else None

    def save_source_file(
        self,
        conn: sqlite3.Connection,
        provider: str,
        path: str,
        size: int,
        mtime: float,
        read_offset: int,
        tail_hash: str | None,
        parser_state: str | None,
        last_import: datetime,
    ) -> int:
        conn.execute(
            "INSERT INTO source_files (provider, path, size, mtime, read_offset, tail_hash, "
            "parser_state, last_import) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, path) DO UPDATE SET size = excluded.size, "
            "mtime = excluded.mtime, read_offset = excluded.read_offset, "
            "tail_hash = excluded.tail_hash, parser_state = excluded.parser_state, "
            "last_import = excluded.last_import",
            (provider, path, size, mtime, read_offset, tail_hash, parser_state,
             to_db_time(last_import)),
        )
        row = conn.execute(
            "SELECT id FROM source_files WHERE provider = ? AND path = ?", (provider, path)
        ).fetchone()
        return int(row[0])

    def reset_progress(self, provider: str) -> None:
        """Make the next import re-read every file for a provider.

        Ids are kept so a Codex re-read replaces the rows it wrote before.
        """
        self._conn().execute(
            "UPDATE source_files SET read_offset = 0, tail_hash = NULL, parser_state = NULL "
            "WHERE provider = ?",
            (provider,),
        )

    def delete_codex_rows_for_source(self, conn: sqlite3.Connection, source_id: int) -> set[date]:
        days = {
            local_date(ts)
            for (value,) in conn.execute(
                "SELECT timestamp FROM codex_usage WHERE source_id = ?", (source_id,)
            )
            if (ts := from_db_time(value)) is not None
        }
        conn.execute("DELETE FROM codex_usage WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM codex_quota_readings WHERE source_id = ?", (source_id,))
        return days

    def source_file_count(self, provider: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM source_files WHERE provider = ?", (provider,)
        ).fetchone()
        return int(row[0])

    def last_import(self, provider: str) -> datetime | None:
        row = self._conn().execute(
            "SELECT MAX(last_import) FROM source_files WHERE provider = ?", (provider,)
        ).fetchone()
        return from_db_time(row[0]) if row else None

    # ---- raw usage ----

    def upsert_claude_messages(
        self, conn: sqlite3.Connection, messages: Iterable[ClaudeMessage]
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO claude_messages (message_id, request_id, timestamp, "
            f"model, variant, {_TOKEN_COLS}, speed, service_tier, cli_version, is_sidechain) "
            f"VALUES (?, ?, ?, ?, ?, {_TOKEN_PLACEHOLDERS}, ?, ?, ?, ?)",
            [
                (
                    m.message_id,
                    m.request_id,
                    to_db_time(m.timestamp),
                    m.model,
                    price_variant(CLAUDE, m.tokens, m.speed),
                    *(getattr(m.tokens, c) for c in CATEGORIES),
                    m.speed,
                    m.service_tier,
                    m.cli_version,
                    int(m.is_sidechain),
                )
                for m in messages
            ],
        )

    def insert_codex_usage(
        self, conn: sqlite3.Connection, source_id: int, rows: Iterable[tuple[int, CodexUsage]]
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO codex_usage (source_id, line_offset, session_id, "
            f"timestamp, model, variant, {_TOKEN_COLS}) "
            f"VALUES (?, ?, ?, ?, ?, ?, {_TOKEN_PLACEHOLDERS})",
            [
                (
                    source_id,
                    offset,
                    u.session_id,
                    to_db_time(u.timestamp),
                    u.model,
                    price_variant(CODEX, u.tokens),
                    *(getattr(u.tokens, c) for c in CATEGORIES),
                )
                for offset, u in rows
            ],
        )

    def insert_codex_readings(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        rows: Iterable[tuple[int, CodexQuotaReading]],
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO codex_quota_readings (source_id, line_offset, slot, "
            "timestamp, limit_id, used_percent, window_minutes, resets_at, plan_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    source_id,
                    offset,
                    r.slot,
                    to_db_time(r.timestamp),
                    r.limit_id,
                    r.used_percent,
                    r.window_minutes,
                    to_db_time(r.resets_at) if r.resets_at else None,
                    r.plan_type,
                )
                for offset, r in rows
            ],
        )

    def quota_readings(
        self, limit_id: str | None = None, since: datetime | None = None
    ) -> list[CodexQuotaReading]:
        sql = (
            "SELECT timestamp, limit_id, slot, used_percent, window_minutes, resets_at, "
            "plan_type FROM codex_quota_readings WHERE 1 = 1"
        )
        params: list = []
        if limit_id is not None:
            sql += " AND limit_id = ?"
            params.append(limit_id)
        if since is not None:
            sql += " AND timestamp >= ?"
            params.append(to_db_time(since))
        sql += " ORDER BY timestamp"
        out = []
        for ts, lim, slot, pct, minutes, resets, plan in self._conn().execute(sql, params):
            parsed = from_db_time(ts)
            if parsed is None:
                continue
            out.append(
                CodexQuotaReading(
                    timestamp=parsed,
                    limit_id=lim,
                    slot=slot,
                    used_percent=float(pct),
                    window_minutes=minutes,
                    resets_at=from_db_time(resets),
                    plan_type=plan,
                )
            )
        return out

    def _table(self, provider: str) -> str:
        if provider == CLAUDE:
            return "claude_messages"
        if provider == CODEX:
            return "codex_usage"
        raise ValueError(f"unknown provider {provider!r}")

    def usage_by_model(
        self, provider: str, start: datetime, end: datetime
    ) -> list[ModelUsage]:
        """Usage from raw rows with start <= timestamp < end."""
        table = self._table(provider)
        rows = self._conn().execute(
            f"SELECT model, variant, {_TOKEN_SUMS}, COUNT(*) FROM {table} "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY model, variant",
            (to_db_time(start), to_db_time(end)),
        ).fetchall()
        out = []
        for row in rows:
            tokens, messages = _usage_from_row(row)
            out.append(ModelUsage(row[0], row[1], tokens, messages))
        return out

    def sidechain_output(self, start: datetime, end: datetime) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT model, SUM(output) FROM claude_messages WHERE is_sidechain = 1 "
            "AND timestamp >= ? AND timestamp < ? GROUP BY model",
            (to_db_time(start), to_db_time(end)),
        ).fetchall()
        return {model: int(total or 0) for model, total in rows}

    def earliest_event(self, provider: str) -> datetime | None:
        row = self._conn().execute(
            f"SELECT MIN(timestamp) FROM {self._table(provider)}"
        ).fetchone()
        return from_db_time(row[0]) if row else None

    def row_count(self, provider: str) -> int:
        row = self._conn().execute(f"SELECT COUNT(*) FROM {self._table(provider)}").fetchone()
        return int(row[0])

    # ---- daily totals ----

    def recompute_daily(
        self, conn: sqlite3.Connection, provider: str, days: Iterable[date]
    ) -> None:
        watermark = self.watermark()
        floor = local_date(watermark) if watermark else None
        table = self._table(provider)
        for day in sorted(set(days)):
            if floor is not None and day < floor:
                continue  # raw rows are gone; the saved totals are final
            start, end = local_day_bounds(day)
            rows = conn.execute(
                f"SELECT model, variant, {_TOKEN_SUMS}, COUNT(*) FROM {table} "
                "WHERE timestamp >= ? AND timestamp < ? GROUP BY model, variant",
                (to_db_time(start), to_db_time(end)),
            ).fetchall()
            conn.execute(
                "DELETE FROM daily_model_totals WHERE day = ? AND provider = ?",
                (day.isoformat(), provider),
            )
            conn.executemany(
                "INSERT INTO daily_model_totals (day, provider, model, variant, "
                f"{_TOKEN_COLS}, messages) VALUES (?, ?, ?, ?, {_TOKEN_PLACEHOLDERS}, ?)",
                [(day.isoformat(), provider, *row) for row in rows],
            )

    def daily_totals(
        self, provider: str, first_day: date, last_day: date
    ) -> dict[date, list[ModelUsage]]:
        rows = self._conn().execute(
            f"SELECT day, model, variant, {_TOKEN_COLS}, messages FROM daily_model_totals "
            "WHERE provider = ? AND day >= ? AND day <= ? ORDER BY day",
            (provider, first_day.isoformat(), last_day.isoformat()),
        ).fetchall()
        out: dict[date, list[ModelUsage]] = {}
        for row in rows:
            tokens, messages = _usage_from_row(row, start=3)
            out.setdefault(date.fromisoformat(row[0]), []).append(
                ModelUsage(row[1], row[2], tokens, messages)
            )
        return out

    # ---- coverage ----

    def add_coverage(self, provider: str, start: datetime, end: datetime) -> None:
        """Record that every event in [start, end] has been imported, merging spans."""
        if end <= start:
            return
        spans = self.coverage(provider) + [(start, end)]
        spans.sort()
        merged: list[tuple[datetime, datetime]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        with self.transaction() as conn:
            conn.execute("DELETE FROM coverage WHERE provider = ?", (provider,))
            conn.executemany(
                "INSERT INTO coverage (provider, start, end) VALUES (?, ?, ?)",
                [(provider, to_db_time(s), to_db_time(e)) for s, e in merged],
            )

    def coverage(self, provider: str) -> list[tuple[datetime, datetime]]:
        out = []
        for s, e in self._conn().execute(
            "SELECT start, end FROM coverage WHERE provider = ? ORDER BY start", (provider,)
        ):
            start, end = from_db_time(s), from_db_time(e)
            if start and end:
                out.append((start, end))
        return out

    def covers(self, provider: str, start: datetime, end: datetime) -> bool:
        return any(s <= start and end <= e for s, e in self.coverage(provider))

    # ---- retention ----

    def prune(self, now: datetime | None = None, retention_days: int = RETENTION_DAYS) -> int:
        """Delete raw rows older than the retention window. Returns rows deleted.

        Daily totals for those days were already computed at import time, and
        window summaries hold their own copies of the usage they cover.
        """
        now = now or datetime.now(timezone.utc)
        cutoff_day = local_date(now) - timedelta(days=retention_days)
        cutoff, _ = local_day_bounds(cutoff_day)
        current = self.watermark()
        if current is not None and cutoff <= current:
            return 0
        stamp = to_db_time(cutoff)
        deleted = 0
        with self.transaction() as conn:
            for table in ("claude_messages", "codex_usage", "codex_quota_readings"):
                cur = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (stamp,))
                deleted += cur.rowcount or 0
            self.set_meta("prune_watermark", stamp, conn)
        if deleted:
            log.info("pruned %d local usage rows older than %s", deleted, stamp)
        return deleted

    # ---- window summaries ----

    def upsert_window_summary(self, summary: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO window_summaries (account_id, metric, window_start, "
                "resets_at, last_reading_at, last_pct, usage_json, origin, flags, period_id, "
                "closed, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary["account_id"],
                    summary["metric"],
                    summary["window_start"],
                    summary["resets_at"],
                    summary.get("last_reading_at"),
                    summary.get("last_pct"),
                    summary["usage_json"],
                    summary["origin"],
                    summary["flags"],
                    summary["period_id"],
                    int(summary["closed"]),
                    summary["updated_at"],
                ),
            )

    def delete_window_summary(self, account_id: str, metric: str, resets_at: str) -> None:
        self._conn().execute(
            "DELETE FROM window_summaries WHERE account_id = ? AND metric = ? AND resets_at = ?",
            (account_id, metric, resets_at),
        )

    def window_summaries(self, account_id: str, metric: str | None = None) -> list[dict]:
        sql = (
            "SELECT account_id, metric, window_start, resets_at, last_reading_at, last_pct, "
            "usage_json, origin, flags, period_id, closed, updated_at FROM window_summaries "
            "WHERE account_id = ?"
        )
        params: list = [account_id]
        if metric is not None:
            sql += " AND metric = ?"
            params.append(metric)
        sql += " ORDER BY resets_at"
        keys = (
            "account_id", "metric", "window_start", "resets_at", "last_reading_at",
            "last_pct", "usage_json", "origin", "flags", "period_id", "closed", "updated_at",
        )
        return [dict(zip(keys, row)) for row in self._conn().execute(sql, params)]
