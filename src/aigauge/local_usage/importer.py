"""Incremental import of CLI log files into the usage store.

For each file the store records the path, size, modification time, the byte
position read up to, and a hash of the bytes just before that position. When a
file grows, reading resumes from that position. When it shrinks or the hash no
longer matches, the whole file is read again; Claude rows are upserts and Codex
rows for the file are replaced, so a re-read never double counts. A half-written
last line is left unread until it is complete.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import claude_logs, codex_logs
from .claude_logs import ClaudeParseStats
from .codex_logs import CodexParser, CodexParserState, CodexParseStats
from .store import CLAUDE, CODEX, UsageStore, local_date

log = logging.getLogger("aigauge.local_usage.importer")

_TAIL_BYTES = 256
# Commit progress (and check for cancel) after roughly this many bytes, so an
# interrupted import of a large file resumes close to where it stopped.
COMMIT_EVERY_BYTES = 8 * 1024 * 1024

ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class LogFile:
    path: Path
    key: str  # normalized resolved path, the identity stored in source_files
    size: int
    mtime: float


@dataclass
class ProviderScan:
    provider: str
    roots: list[Path]
    files: list[LogFile]

    @property
    def roots_found(self) -> bool:
        return any(root.is_dir() for root in self.roots)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def oldest_mtime(self) -> datetime | None:
        if not self.files:
            return None
        return datetime.fromtimestamp(min(f.mtime for f in self.files), tz=timezone.utc)


@dataclass(frozen=True)
class ImportSource:
    provider: str
    roots: tuple[Path, ...]
    start_from: datetime | None = None


@dataclass
class ProviderImportResult:
    provider: str
    files_seen: int = 0
    files_read: int = 0
    bytes_read: int = 0
    candidates: int = 0
    parsed: int = 0
    versions: Counter = field(default_factory=Counter)
    cancelled: bool = False
    errors: int = 0

    @property
    def not_recognized(self) -> bool:
        return self.candidates > 0 and self.parsed == 0


def scan_provider(provider: str, roots: Iterable[Path]) -> ProviderScan:
    """List log files under the roots without opening any of them."""
    root_list = list(roots)
    seen: set[str] = set()
    files: list[LogFile] = []
    for root in root_list:
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(dirpath) / name
                try:
                    key = os.path.normcase(os.path.realpath(path))
                    if key in seen:
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                seen.add(key)
                files.append(LogFile(path, key, stat.st_size, stat.st_mtime))
    files.sort(key=lambda f: (f.mtime, f.key))
    return ProviderScan(provider, root_list, files)


def provider_roots(provider: str, override: str | None) -> tuple[Path, ...]:
    if provider == CLAUDE:
        if override:
            return (Path(override).expanduser(),)
        return tuple(claude_logs.default_roots())
    root = Path(override).expanduser() if override else codex_logs.default_root()
    return tuple(codex_logs.log_dirs(root))


def _tail_hash(tail: bytes) -> str:
    return hashlib.sha1(tail).hexdigest()


def _read_tail(path: Path, offset: int) -> bytes:
    start = max(0, offset - _TAIL_BYTES)
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(offset - start)


class LogImporter:
    def __init__(
        self,
        store: UsageStore,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._store = store
        self._cancel = cancel or threading.Event()
        self._progress = progress
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._done = 0
        self._total = 0

    def run(self, sources: Iterable[ImportSource]) -> list[ProviderImportResult]:
        started = self._now()
        scans = [(source, scan_provider(source.provider, source.roots)) for source in sources]
        plan: list[tuple[ImportSource, ProviderScan, list[tuple[LogFile, int]]]] = []
        self._done = 0
        self._total = 0
        for source, scan in scans:
            pending = []
            for lf in scan.files:
                resume = self._resume_offset(source.provider, lf)
                if resume is None or resume < lf.size:
                    pending.append((lf, resume or 0))
                    self._total += lf.size - (resume or 0)
            plan.append((source, scan, pending))
        self._report()

        results = []
        for source, scan, pending in plan:
            result = ProviderImportResult(source.provider, files_seen=len(scan.files))
            results.append(result)
            if self._cancel.is_set():
                result.cancelled = True
                continue
            for lf, _resume in pending:
                try:
                    self._import_file(source, lf, result)
                except OSError:
                    log.warning("could not read %s log %s", source.provider, lf.path)
                    result.errors += 1
                if result.cancelled or self._cancel.is_set():
                    result.cancelled = True
                    break
            self._finish_provider(source, scan, result, started)
        return results

    # ---- internals ----

    def _report(self) -> None:
        if self._progress is not None:
            self._progress(self._done, self._total)

    def _resume_offset(self, provider: str, lf: LogFile) -> int | None:
        """Where reading this file would start, or None for a full re-read."""
        rec = self._store.get_source_file(provider, lf.key)
        if rec is None or rec.read_offset == 0 or rec.tail_hash is None:
            return None
        if lf.size < rec.read_offset:
            return None
        if lf.size == rec.read_offset and lf.mtime != rec.mtime:
            # Same length but written since: a rewrite in place, not an append.
            return None
        try:
            tail = _read_tail(lf.path, rec.read_offset)
        except OSError:
            return None
        if _tail_hash(tail) != rec.tail_hash:
            return None
        return rec.read_offset

    def _min_timestamp(self, source: ImportSource) -> datetime | None:
        bounds = [b for b in (source.start_from, self._store.watermark()) if b is not None]
        return max(bounds) if bounds else None

    def _import_file(self, source: ImportSource, lf: LogFile, result: ProviderImportResult) -> None:
        store = self._store
        provider = source.provider
        rec = store.get_source_file(provider, lf.key)
        resume = self._resume_offset(provider, lf)
        start = resume or 0
        min_ts = self._min_timestamp(source)
        days: set[date] = set()

        if resume is None:
            tail = b""
            state = None
            if rec is not None and provider == CODEX:
                with store.transaction() as conn:
                    days |= store.delete_codex_rows_for_source(conn, rec.id)
                    store.recompute_daily(conn, provider, days)
                days.clear()
        else:
            tail = _read_tail(lf.path, start)
            state = rec.parser_state if rec else None

        source_id = rec.id if rec is not None else None
        claude_stats = ClaudeParseStats()
        codex_stats = CodexParseStats()
        codex_parser = (
            CodexParser(lf.path.stem, CodexParserState.from_json(state), codex_stats)
            if provider == CODEX
            else None
        )
        claude_batch: dict[tuple[str, str], claude_logs.ClaudeMessage] = {}
        usage_batch: list = []
        reading_batch: list = []
        pos = start
        committed = start
        since_commit = 0

        def flush() -> None:
            nonlocal source_id, committed, since_commit
            with store.transaction() as conn:
                source_id = store.save_source_file(
                    conn,
                    provider,
                    lf.key,
                    lf.size,
                    lf.mtime,
                    pos,
                    _tail_hash(tail),
                    codex_parser.state.to_json() if codex_parser else None,
                    self._now(),
                )
                if claude_batch:
                    store.upsert_claude_messages(conn, claude_batch.values())
                if usage_batch:
                    store.insert_codex_usage(conn, source_id, usage_batch)
                if reading_batch:
                    store.insert_codex_readings(conn, source_id, reading_batch)
                store.recompute_daily(conn, provider, days)
            claude_batch.clear()
            usage_batch.clear()
            reading_batch.clear()
            days.clear()
            self._done += pos - committed
            committed = pos
            since_commit = 0
            self._report()

        with lf.path.open("rb") as fh:
            fh.seek(start)
            for line in fh:
                if not line.endswith(b"\n"):
                    break  # half-written; picked up on a later pass
                line_start = pos
                pos += len(line)
                since_commit += len(line)
                tail = (tail + line)[-_TAIL_BYTES:]
                if codex_parser is not None:
                    usage, readings = codex_parser.feed(line)
                    if usage is not None and (min_ts is None or usage.timestamp >= min_ts):
                        usage_batch.append((line_start, usage))
                        days.add(local_date(usage.timestamp))
                    for reading in readings:
                        if min_ts is None or reading.timestamp >= min_ts:
                            reading_batch.append((line_start, reading))
                else:
                    message = claude_logs.parse_line(line, claude_stats)
                    if message is not None and (min_ts is None or message.timestamp >= min_ts):
                        claude_batch.pop(message.key, None)
                        claude_batch[message.key] = message
                        days.add(local_date(message.timestamp))
                if since_commit >= COMMIT_EVERY_BYTES:
                    flush()
                    if self._cancel.is_set():
                        result.cancelled = True
                        break
        if since_commit or rec is None or pos != (rec.read_offset if rec else -1):
            flush()
        # Count the unread remainder (a half-written line) as done for progress.
        self._done += max(0, lf.size - committed)
        self._report()

        result.files_read += 1
        result.bytes_read += pos - start
        if codex_parser is not None:
            result.candidates += codex_stats.candidate_lines
            result.parsed += codex_stats.usage_events + codex_stats.quota_readings
            result.versions.update(codex_stats.formats)
        else:
            result.candidates += claude_stats.candidate_lines
            result.parsed += claude_stats.parsed_messages
            result.versions.update(claude_stats.versions)

    def _finish_provider(
        self,
        source: ImportSource,
        scan: ProviderScan,
        result: ProviderImportResult,
        started: datetime,
    ) -> None:
        store = self._store
        provider = source.provider
        versions = Counter(store.get_meta(f"versions:{provider}", {}))
        versions.update(result.versions)
        store.set_meta(f"versions:{provider}", dict(versions))
        if result.candidates:
            store.set_meta(f"recognized:{provider}", not result.not_recognized)
        if result.cancelled or result.errors:
            return
        # Every event at or after the oldest remaining file's mtime is still in
        # the logs: cleanup removes whole files by age, and an event is always
        # older than the file it was written to.
        oldest = scan.oldest_mtime
        start = max(b for b in (oldest, source.start_from) if b is not None) if (
            oldest or source.start_from
        ) else None
        if start is not None:
            store.add_coverage(provider, start, started)
        elif scan.roots_found:
            store.add_coverage(provider, started, started)
