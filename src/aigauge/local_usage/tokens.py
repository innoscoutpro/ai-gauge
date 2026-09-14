from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

# Token categories shared by both providers. `input` is always *uncached*
# input, so the categories never overlap and each can be priced on its own.
# `reasoning` is informational: providers bill it as part of `output`, so it is
# never priced separately.
CATEGORIES = (
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
)


@dataclass
class TokenCounts:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0

    def add(self, other: TokenCounts) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def total_input(self) -> int:
        """All prompt-side tokens: uncached, cache reads and cache writes."""
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def is_zero(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> TokenCounts:
        if not isinstance(data, dict):
            return cls()
        out = cls()
        for name in CATEGORIES:
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(out, name, int(value))
        return out


def as_int(value: object) -> int:
    """Coerce a log token field to a non-negative int; anything else is 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def parse_log_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp from a CLI log into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # CLI logs write UTC; a missing offset is read as UTC rather than local.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
