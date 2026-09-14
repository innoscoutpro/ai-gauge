"""Codex CLI log parsing.

Codex writes rollout files under ``$CODEX_HOME/sessions/**`` (and
``archived_sessions``). ``event_msg`` lines whose payload type is
``token_count`` carry a running total for the session in
``info.total_token_usage`` and, usually, the account's ``rate_limits``.

Usage is the change in the running total between events: a repeated total adds
nothing, and a total that drops starts a new baseline rather than recording
negative usage. The model comes from the most recent ``turn_context`` event; a
token event that cannot be tied to one is recorded as ``unknown``.

The published protocol makes ``info``, ``rate_limits``, ``window_minutes`` and
``resets_at`` optional, so every one of them may be missing.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .tokens import TokenCounts, as_int, parse_log_timestamp

UNKNOWN_MODEL = "unknown"

_RAW_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


@dataclass(frozen=True)
class CodexUsage:
    timestamp: datetime
    session_id: str
    model: str
    tokens: TokenCounts


@dataclass(frozen=True)
class CodexQuotaReading:
    timestamp: datetime
    limit_id: str
    slot: str  # "primary" or "secondary"
    used_percent: float
    window_minutes: int | None
    resets_at: datetime | None
    plan_type: str | None


@dataclass
class CodexParserState:
    """Per-file state carried between incremental reads of the same file."""

    session_id: str | None = None
    model: str | None = None
    last_total: dict[str, int] | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": self.session_id,
                "model": self.model,
                "last_total": self.last_total,
            }
        )

    @classmethod
    def from_json(cls, text: str | None) -> CodexParserState:
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        last_total = data.get("last_total")
        return cls(
            session_id=data.get("session_id") if isinstance(data.get("session_id"), str) else None,
            model=data.get("model") if isinstance(data.get("model"), str) else None,
            last_total=(
                {k: as_int(last_total.get(k)) for k in _RAW_FIELDS}
                if isinstance(last_total, dict)
                else None
            ),
        )


@dataclass
class CodexParseStats:
    candidate_lines: int = 0  # token_count events, parsed or not
    usage_events: int = 0
    quota_readings: int = 0
    formats: Counter = field(default_factory=Counter)  # cli_version values seen

    def add(self, other: CodexParseStats) -> None:
        self.candidate_lines += other.candidate_lines
        self.usage_events += other.usage_events
        self.quota_readings += other.quota_readings
        self.formats.update(other.formats)


def normalize_usage(raw: Mapping[str, int]) -> TokenCounts:
    """Map Codex's token fields onto the shared, non-overlapping categories.

    Codex mirrors the OpenAI Responses API: ``input_tokens`` includes cached
    input and ``output_tokens`` includes reasoning. Cache writes are treated
    the same way as cached input, as part of ``input_tokens``.
    """
    cached = as_int(raw.get("cached_input_tokens"))
    cache_write = as_int(raw.get("cache_write_input_tokens"))
    uncached = max(0, as_int(raw.get("input_tokens")) - cached - cache_write)
    return TokenCounts(
        input=uncached,
        output=as_int(raw.get("output_tokens")),
        cache_read=cached,
        cache_write_5m=cache_write,
        cache_write_1h=0,
        reasoning=as_int(raw.get("reasoning_output_tokens")),
    )


def _raw_total(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    if not any(key in value for key in _RAW_FIELDS):
        return None
    return {key: as_int(value.get(key)) for key in _RAW_FIELDS}


def _epoch_to_utc(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class CodexParser:
    """Feeds one rollout file's lines in order and emits usage and readings."""

    def __init__(
        self,
        fallback_session_id: str,
        state: CodexParserState | None = None,
        stats: CodexParseStats | None = None,
    ):
        self.state = state or CodexParserState()
        if not self.state.session_id:
            self.state.session_id = fallback_session_id
        self.stats = stats or CodexParseStats()

    def feed(
        self, line: bytes | str
    ) -> tuple[CodexUsage | None, list[CodexQuotaReading]]:
        raw = line if isinstance(line, bytes) else line.encode("utf-8")
        if (
            b"token_count" not in raw
            and b"turn_context" not in raw
            and b"session_meta" not in raw
        ):
            return None, []
        is_candidate = b'"token_count"' in raw
        if is_candidate:
            self.stats.candidate_lines += 1
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None, []
        if not isinstance(entry, dict):
            return None, []
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None, []
        kind = entry.get("type")
        if kind == "session_meta":
            session_id = payload.get("id") or payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.state.session_id = session_id
            version = payload.get("cli_version")
            if isinstance(version, str):
                self.stats.formats[version] += 1
            return None, []
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.state.model = model
            return None, []
        if kind != "event_msg" or payload.get("type") != "token_count":
            return None, []
        timestamp = parse_log_timestamp(entry.get("timestamp"))
        if timestamp is None:
            return None, []
        usage = self._usage(timestamp, payload.get("info"))
        readings = self._readings(timestamp, payload.get("rate_limits"))
        return usage, readings

    def _usage(self, timestamp: datetime, info: object) -> CodexUsage | None:
        if not isinstance(info, dict):
            return None
        total = _raw_total(info.get("total_token_usage"))
        if total is None:
            return None
        self.stats.usage_events += 1
        last = self.state.last_total
        self.state.last_total = total
        if last is None:
            delta = total
        elif any(total[key] < last[key] for key in _RAW_FIELDS):
            # The running total went backwards: start a new baseline and record
            # nothing, rather than a negative or double-counted amount.
            return None
        else:
            delta = {key: total[key] - last[key] for key in _RAW_FIELDS}
        if not any(delta.values()):
            return None
        return CodexUsage(
            timestamp=timestamp,
            session_id=self.state.session_id or "",
            model=self.state.model or UNKNOWN_MODEL,
            tokens=normalize_usage(delta),
        )

    def _readings(self, timestamp: datetime, limits: object) -> list[CodexQuotaReading]:
        if not isinstance(limits, dict):
            return []
        limit_id = limits.get("limit_id")
        if not isinstance(limit_id, str) or not limit_id:
            limit_id = "codex"
        plan_type = limits.get("plan_type")
        out: list[CodexQuotaReading] = []
        for slot in ("primary", "secondary"):
            window = limits.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                continue
            minutes = window.get("window_minutes")
            out.append(
                CodexQuotaReading(
                    timestamp=timestamp,
                    limit_id=limit_id,
                    slot=slot,
                    used_percent=float(used),
                    window_minutes=(
                        int(minutes)
                        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
                        else None
                    ),
                    resets_at=_epoch_to_utc(window.get("resets_at")),
                    plan_type=plan_type if isinstance(plan_type, str) else None,
                )
            )
        self.stats.quota_readings += len(out)
        return out


def default_root(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    configured = env.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else home / ".codex"


def log_dirs(root: Path) -> list[Path]:
    return [root / "sessions", root / "archived_sessions"]
