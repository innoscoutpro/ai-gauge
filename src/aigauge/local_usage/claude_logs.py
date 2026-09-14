"""Claude Code log parsing.

Claude Code writes one JSON object per line under ``<config>/projects/**``.
Each ``type: "assistant"`` line carries ``message.usage``. A single API message
can be written several times while it streams: in CLI 2.1.260 to 2.1.267 the
earlier copies hold placeholder ``output_tokens``, so when a
``(message.id, requestId)`` key repeats, the later line replaces the earlier.
Subagent logs (``subagents/`` directories, ``isSidechain: true``) count against
the same quota and are included.

Only token counts, the model, times, the CLI version and the sidechain flag are
kept. Prompt text, tool output and project paths are never read into results.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tokens import TokenCounts, as_int, parse_log_timestamp

SYNTHETIC_MODEL = "<synthetic>"

# Cheap substring checks run before json.loads: usage lines are a small share
# of a multi-gigabyte log, and most other lines are large message bodies.
_USAGE_MARKER = b'"usage"'
_ASSISTANT_MARKER = b'"assistant"'
_ASSISTANT_TYPE_MARKERS = (b'"type":"assistant"', b'"type": "assistant"')


@dataclass(frozen=True)
class ClaudeMessage:
    message_id: str
    request_id: str
    timestamp: datetime
    model: str
    tokens: TokenCounts
    speed: str | None
    service_tier: str | None
    cli_version: str | None
    is_sidechain: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.message_id, self.request_id)


@dataclass
class ClaudeParseStats:
    # Lines that look like assistant entries, parsed or not. Used to tell
    # "logs found, usage not recognized" apart from "no usage yet".
    candidate_lines: int = 0
    parsed_messages: int = 0
    synthetic_skipped: int = 0
    versions: Counter = field(default_factory=Counter)

    def add(self, other: ClaudeParseStats) -> None:
        self.candidate_lines += other.candidate_lines
        self.parsed_messages += other.parsed_messages
        self.synthetic_skipped += other.synthetic_skipped
        self.versions.update(other.versions)


def _as_bytes(line: bytes | str) -> bytes:
    return line if isinstance(line, bytes) else line.encode("utf-8")


def is_candidate_line(line: bytes | str) -> bool:
    raw = _as_bytes(line)
    return any(marker in raw for marker in _ASSISTANT_TYPE_MARKERS)


def parse_line(line: bytes | str, stats: ClaudeParseStats | None = None) -> ClaudeMessage | None:
    """Parse one log line. Returns None for anything that is not billable usage."""
    raw = _as_bytes(line)
    if stats is not None and is_candidate_line(raw):
        stats.candidate_lines += 1
    if _USAGE_MARKER not in raw or _ASSISTANT_MARKER not in raw:
        return None
    try:
        entry = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    model = message.get("model")
    if model == SYNTHETIC_MODEL:
        if stats is not None:
            stats.synthetic_skipped += 1
        return None
    message_id = message.get("id")
    timestamp = parse_log_timestamp(entry.get("timestamp"))
    if not isinstance(message_id, str) or not message_id or timestamp is None:
        return None
    request_id = entry.get("requestId")
    if not isinstance(request_id, str):
        request_id = ""

    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        write_5m = as_int(cache_creation.get("ephemeral_5m_input_tokens"))
        write_1h = as_int(cache_creation.get("ephemeral_1h_input_tokens"))
    else:
        # Older logs only carry the combined figure. The API's default cache
        # lifetime is five minutes, so that is where the combined count goes.
        write_5m = as_int(usage.get("cache_creation_input_tokens"))
        write_1h = 0

    details = usage.get("output_tokens_details")
    reasoning = as_int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    tokens = TokenCounts(
        input=as_int(usage.get("input_tokens")),
        output=as_int(usage.get("output_tokens")),
        cache_read=as_int(usage.get("cache_read_input_tokens")),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        reasoning=reasoning,
    )
    version = entry.get("version")
    speed = usage.get("speed")
    tier = usage.get("service_tier")
    parsed = ClaudeMessage(
        message_id=message_id,
        request_id=request_id,
        timestamp=timestamp,
        model=model if isinstance(model, str) and model else "unknown",
        tokens=tokens,
        speed=speed if isinstance(speed, str) else None,
        service_tier=tier if isinstance(tier, str) else None,
        cli_version=version if isinstance(version, str) else None,
        is_sidechain=bool(entry.get("isSidechain")),
    )
    if stats is not None:
        stats.parsed_messages += 1
        if parsed.cli_version:
            stats.versions[parsed.cli_version] += 1
    return parsed


def parse_lines(
    lines: Iterable[bytes | str],
    stats: ClaudeParseStats | None = None,
) -> list[ClaudeMessage]:
    """Parse lines in file order, keeping the last line for each message key."""
    by_key: dict[tuple[str, str], ClaudeMessage] = {}
    for line in lines:
        message = parse_line(line, stats)
        if message is None:
            continue
        # Re-inserting moves the key to the end, so output follows the order
        # in which each message was last written.
        by_key.pop(message.key, None)
        by_key[message.key] = message
    return list(by_key.values())


def default_roots(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Folders Claude Code writes project logs to, most specific first."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    roots: list[Path] = []
    configured = env.get("CLAUDE_CONFIG_DIR", "")
    for part in configured.split(os.pathsep) if configured else []:
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser() / "projects")
    roots.append(home / ".claude" / "projects")
    roots.append(home / ".config" / "claude" / "projects")
    return _unique_paths(roots)


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out
