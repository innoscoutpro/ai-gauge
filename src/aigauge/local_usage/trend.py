"""Allowance trend: does the same work use more or less of the allowance?

For each completed, comparable window:

    dollars per point        = window cost / percent at last reading
    output tokens per point  = window output tokens / percent at last reading
    change vs baseline       = current / median(baseline windows) - 1

The baseline is the median of at least three earlier comparable completed
windows of the same metric. Figures across several windows divide total cost
by total points; per-window ratios are never averaged.

This is informational only: it never raises an alert and never feeds the
quota percentages, the session ratio or the MCP guard.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..ratio import MAX_COUNTABLE_PCT, MIN_COUNTABLE_PCT
from .rates import CostSummary, RateTable, summarize_costs
from .summaries import (
    FLAG_INCOMPLETE,
    FLAG_LIMIT_REACHED,
    FLAG_TIME_UNCERTAIN,
    WindowSummary,
)

BASELINE_MIN_WINDOWS = 3
BASELINE_MAX_WINDOWS = 8

COUNTED = "counted"
REASON_NO_READING = "no quota reading"
REASON_LOW = f"under {MIN_COUNTABLE_PCT:.0f}% used"
REASON_LIMIT = "limit reached (extra usage possible)"
REASON_INCOMPLETE = "logs missing for part of the window"
REASON_TIME = "clock changed during the window"
REASON_PERIOD = "different account, folder or plan"
REASON_UNPRICED = "some models have no price"


@dataclass
class TrendRow:
    summary: WindowSummary
    cost: CostSummary
    pct: float | None
    dollars_per_point: float | None
    output_per_point: float | None
    cache_share: float | None
    top_model: str | None
    reason: str  # COUNTED or why the window is left out
    dollars_reason: str  # COUNTED, or why only the dollar figure is left out

    @property
    def counted(self) -> bool:
        return self.reason == COUNTED

    @property
    def dollars_counted(self) -> bool:
        return self.counted and self.dollars_reason == COUNTED


@dataclass
class Baseline:
    windows: int
    median: float | None
    low: float | None
    high: float | None


@dataclass
class TrendReport:
    metric: str
    rows: list[TrendRow]  # newest first
    current: TrendRow | None
    dollars_baseline: Baseline
    output_baseline: Baseline
    dollars_change: float | None
    output_change: float | None
    pooled_dollars_per_point: float | None
    pooled_output_per_point: float | None

    @property
    def collecting(self) -> bool:
        return self.output_baseline.windows < BASELINE_MIN_WINDOWS


def cache_share(cost: CostSummary) -> float | None:
    tokens = cost.tokens
    total = tokens.total_input()
    return tokens.cache_read / total if total else None


def top_model(cost: CostSummary) -> str | None:
    if not cost.rows:
        return None
    priced = [r for r in cost.rows if r.cost is not None]
    if priced:
        return max(priced, key=lambda r: r.cost).model
    return max(cost.rows, key=lambda r: r.tokens.output).model


def _exclusion(summary: WindowSummary, period_id: str | None) -> str:
    pct = summary.last_pct
    if pct is None:
        return REASON_NO_READING
    if FLAG_LIMIT_REACHED in summary.flags or pct > MAX_COUNTABLE_PCT:
        return REASON_LIMIT
    if pct < MIN_COUNTABLE_PCT:
        return REASON_LOW
    if FLAG_INCOMPLETE in summary.flags:
        return REASON_INCOMPLETE
    if FLAG_TIME_UNCERTAIN in summary.flags:
        return REASON_TIME
    if period_id is not None and summary.period_id != period_id:
        return REASON_PERIOD
    return COUNTED


def build_row(summary: WindowSummary, rates: RateTable, period_id: str | None) -> TrendRow:
    cost = summarize_costs(summary.usage, rates)
    pct = summary.last_pct
    reason = _exclusion(summary, period_id)
    usable_pct = pct if pct is not None and pct > 0 else None
    dollars_reason = COUNTED if not cost.has_unpriced else REASON_UNPRICED
    return TrendRow(
        summary=summary,
        cost=cost,
        pct=pct,
        dollars_per_point=(
            cost.priced_cost / usable_pct
            if usable_pct is not None and dollars_reason == COUNTED
            else None
        ),
        output_per_point=cost.tokens.output / usable_pct if usable_pct is not None else None,
        cache_share=cache_share(cost),
        top_model=top_model(cost),
        reason=reason,
        dollars_reason=dollars_reason,
    )


def _baseline(values: list[float]) -> Baseline:
    if not values:
        return Baseline(0, None, None, None)
    return Baseline(len(values), statistics.median(values), min(values), max(values))


def _change(current: float | None, baseline: Baseline) -> float | None:
    if (
        current is None
        or baseline.median is None
        or baseline.median <= 0
        or baseline.windows < BASELINE_MIN_WINDOWS
    ):
        return None
    return current / baseline.median - 1


def build_trend(summaries: list[WindowSummary], metric: str, rates: RateTable) -> TrendReport:
    completed = sorted(
        (s for s in summaries if s.closed and s.metric == metric),
        key=lambda s: s.resets_at,
        reverse=True,
    )
    # The newest completed window defines the comparison period in force.
    period_id = completed[0].period_id if completed else None
    rows = [build_row(summary, rates, period_id) for summary in completed]
    counted = [row for row in rows if row.counted]
    current = counted[0] if counted else None
    earlier = counted[1 : 1 + BASELINE_MAX_WINDOWS]

    output_baseline = _baseline([r.output_per_point for r in earlier if r.output_per_point is not None])
    dollars_baseline = _baseline(
        [r.dollars_per_point for r in earlier if r.dollars_counted and r.dollars_per_point is not None]
    )

    priced = [r for r in counted if r.dollars_counted]
    points_priced = sum(r.pct or 0 for r in priced)
    points_all = sum(r.pct or 0 for r in counted)
    return TrendReport(
        metric=metric,
        rows=rows,
        current=current,
        dollars_baseline=dollars_baseline,
        output_baseline=output_baseline,
        dollars_change=_change(
            current.dollars_per_point if current and current.dollars_counted else None,
            dollars_baseline,
        ),
        output_change=_change(current.output_per_point if current else None, output_baseline),
        pooled_dollars_per_point=(
            sum(r.cost.priced_cost for r in priced) / points_priced if points_priced else None
        ),
        pooled_output_per_point=(
            sum(r.cost.tokens.output for r in counted) / points_all if points_all else None
        ),
    )
