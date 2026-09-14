"""Log format versions the parsers have been checked against.

Both CLI log formats are undocumented. Versions outside these ranges still
import; Settings shows them as an informational note so a silent format change
is easier to spot.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Claude Code: the sample logs plus the real logs surveyed on 2026-09-14.
# Codex: the cli_version values in session_meta surveyed on the same day.
TESTED_VERSION_RANGES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "claude": ((2, 1, 231), (2, 1, 270)),
    "codex": ((0, 119, 0), (0, 154, 0)),
}

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def version_tuple(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def untested_versions(provider: str, versions: Iterable[str]) -> list[str]:
    bounds = TESTED_VERSION_RANGES.get(provider)
    if bounds is None:
        return []
    low, high = bounds
    out = []
    for version in versions:
        parsed = version_tuple(version)
        if parsed is None or not (low <= parsed <= high):
            out.append(version)
    return sorted(set(out))
