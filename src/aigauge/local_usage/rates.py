"""API price table and pricing on read.

The bundled ``rates.json`` is versioned and records a source and date for each
model. An optional ``app_data_dir()/rates.override.json`` with the same shape
adds or replaces whole model entries without waiting for a release.

A model with no entry is unpriced: its tokens still show, its cost is None, and
it is never priced as another model or as zero.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ..config import app_data_dir
from .store import ModelUsage
from .tokens import TokenCounts

log = logging.getLogger("aigauge.local_usage.rates")

OVERRIDE_FILENAME = "rates.override.json"
PRICED_CATEGORIES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class PriceSet:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float

    def cost(self, tokens: TokenCounts) -> float:
        return (
            tokens.input * self.input
            + tokens.output * self.output
            + tokens.cache_read * self.cache_read
            + tokens.cache_write_5m * self.cache_write_5m
            + tokens.cache_write_1h * self.cache_write_1h
        ) / 1_000_000

    @classmethod
    def parse(cls, data: object) -> PriceSet:
        if not isinstance(data, dict):
            raise ValueError("price set must be an object")
        values = {}
        for name in PRICED_CATEGORIES:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"missing or invalid {name!r}")
            values[name] = float(value)
        return cls(**values)


@dataclass(frozen=True)
class ModelRates:
    model: str
    base: PriceSet
    variants: dict[str, PriceSet]
    source: str
    as_of: str

    def price_set(self, variant: str) -> PriceSet | None:
        if not variant:
            return self.base
        if variant in self.variants:
            return self.variants[variant]
        parts = variant.split("+")
        if "long" in parts and not any("long" in key.split("+") for key in self.variants):
            # No long-context surcharge for this model.
            return self.price_set("+".join(p for p in parts if p != "long"))
        return None


@dataclass
class RateTable:
    version: str
    currency: str
    models: dict[str, ModelRates]
    override_path: Path | None = None
    override_models: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def rates_for(self, model: str) -> ModelRates | None:
        found = self.models.get(model)
        if found is None:
            found = self.models.get(_DATE_SUFFIX.sub("", model))
        return found

    def cost(self, model: str, variant: str, tokens: TokenCounts) -> float | None:
        rates = self.rates_for(model)
        if rates is None:
            return None
        price = rates.price_set(variant)
        return None if price is None else price.cost(tokens)

    def describe(self) -> str:
        text = f"Rate table {self.version} ({self.currency})"
        if self.override_models:
            text += f" + {len(self.override_models)} override(s)"
        return text


def _parse_models(data: object, errors: list[str], origin: str) -> dict[str, ModelRates]:
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        errors.append(f"{origin}: no models object")
        return {}
    out = {}
    for name, entry in models.items():
        try:
            if not isinstance(entry, dict):
                raise ValueError("entry must be an object")
            variants_raw = entry.get("variants") or {}
            if not isinstance(variants_raw, dict):
                raise ValueError("variants must be an object")
            out[name] = ModelRates(
                model=name,
                base=PriceSet.parse(entry.get("rates")),
                variants={key: PriceSet.parse(value) for key, value in variants_raw.items()},
                source=str(entry.get("source") or ""),
                as_of=str(entry.get("as_of") or ""),
            )
        except ValueError as exc:
            errors.append(f"{origin}: {name}: {exc}")
    return out


def bundled_rates_text() -> str:
    return resources.files("aigauge.local_usage").joinpath("rates.json").read_text(encoding="utf-8")


def default_override_path() -> Path:
    return app_data_dir() / OVERRIDE_FILENAME


def load_rate_table(override_path: Path | None = None, bundled_text: str | None = None) -> RateTable:
    errors: list[str] = []
    try:
        bundled = json.loads(bundled_text if bundled_text is not None else bundled_rates_text())
    except (OSError, ValueError) as exc:
        log.error("bundled rate table unreadable: %s", exc)
        bundled = {}
        errors.append(f"bundled: {exc}")
    table = RateTable(
        version=str(bundled.get("version") or "unknown") if isinstance(bundled, dict) else "unknown",
        currency=str(bundled.get("currency") or "USD") if isinstance(bundled, dict) else "USD",
        models=_parse_models(bundled, errors, "bundled"),
        errors=errors,
    )
    path = override_path if override_path is not None else default_override_path()
    table.override_path = path
    if path.is_file():
        try:
            override = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"override: {exc}")
        else:
            models = _parse_models(override, errors, "override")
            table.models.update(models)
            table.override_models = set(models)
    for error in errors:
        log.warning("rate table: %s", error)
    return table


# ---- aggregation for display ----


@dataclass
class ModelCostRow:
    model: str
    tokens: TokenCounts
    messages: int
    cost: float | None  # None when nothing in the row could be priced
    has_unpriced: bool  # some of the row's usage had no price

    @property
    def priced(self) -> bool:
        return self.cost is not None and not self.has_unpriced


@dataclass
class CostSummary:
    rows: list[ModelCostRow]
    tokens: TokenCounts
    messages: int
    priced_cost: float
    has_unpriced: bool

    def share(self, row: ModelCostRow) -> float | None:
        if row.cost is None or self.priced_cost <= 0:
            return None
        return row.cost / self.priced_cost


def summarize_costs(usage: list[ModelUsage], table: RateTable) -> CostSummary:
    """Group usage by model and price each price bucket on read."""
    by_model: dict[str, ModelCostRow] = {}
    for item in usage:
        row = by_model.get(item.model)
        if row is None:
            row = ModelCostRow(item.model, TokenCounts(), 0, None, False)
            by_model[item.model] = row
        row.tokens.add(item.tokens)
        row.messages += item.messages
        cost = table.cost(item.model, item.variant, item.tokens)
        if cost is None:
            if not item.tokens.is_zero():
                row.has_unpriced = True
        else:
            row.cost = (row.cost or 0.0) + cost
    rows = sorted(
        by_model.values(),
        key=lambda r: (r.cost is None, -(r.cost or 0.0), r.model),
    )
    total_tokens = TokenCounts()
    for row in rows:
        total_tokens.add(row.tokens)
    return CostSummary(
        rows=rows,
        tokens=total_tokens,
        messages=sum(r.messages for r in rows),
        priced_cost=sum(r.cost or 0.0 for r in rows),
        has_unpriced=any(r.has_unpriced for r in rows),
    )


def format_cost(value: float | None) -> str:
    if value is None:
        return "no price"
    if value >= 100:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def format_total_cost(summary: CostSummary) -> str:
    if not summary.rows:
        return "$0.00"
    if summary.has_unpriced and summary.priced_cost == 0:
        return "no price"
    text = format_cost(summary.priced_cost)
    return f"{text} + unpriced" if summary.has_unpriced else text
