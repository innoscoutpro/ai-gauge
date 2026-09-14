"""Allowance trend: does the same work use more or less of the allowance?

For each completed, comparable window:

    dollars per point        = window cost / percent at last reading
    output tokens per point  = window output tokens / percent at last reading

The recent figure is the median of the last few compared windows (one by
default; the Trend view uses five sessions, because single sessions swing
widely). It is compared with the median of the comparable windows before them:

    change vs baseline       = recent / median(baseline windows) - 1

A baseline needs at least three windows. Figures across several windows divide
total cost by total points; per-window ratios are never averaged.

When the user marks a date a provider changed its limits, only windows after
the latest change are compared, and a window that spans a change is skipped.

This is informational only: it never raises an alert and never feeds the
quota percentages, the session ratio or the MCP guard.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

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
# Readings are whole percents, so a session that ended at a few percent has a
# large rounding error in its per-1% figures. Weekly windows keep the lower floor.
TREND_MIN_PCT = {"Session": 10.0}
# A window whose unpriced usage is at most this share of its tokens still gets
# a cost per 1%, from its priced models, marked as such.
UNPRICED_TOLERANCE = 0.10
# A cache share this many points away from the baseline's is worth a warning.
MIX_CACHE_POINTS = 0.15

COUNTED = "counted"
REASON_NO_READING = "no quota reading"
REASON_LOW = "too little of the limit used to compare"
REASON_LIMIT = "limit reached (extra usage possible)"
REASON_INCOMPLETE = "logs missing for part of the window"
REASON_TIME = "clock changed during the window"
REASON_PERIOD = "different account, folder or plan"
REASON_SPANS_CHANGE = "spans a limit change"
REASON_BEFORE_CHANGE = "before the latest limit change"
REASON_UNPRICED = "some models have no price"
NOTE_PARTIAL_PRICE = "cost excludes unpriced models"


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
    dollars_note: str = ""  # set when the dollar figure leaves out a little unpriced usage

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
    current: TrendRow | None  # the newest compared window
    dollars_baseline: Baseline
    output_baseline: Baseline
    dollars_change: float | None
    output_change: float | None
    pooled_dollars_per_point: float | None
    pooled_output_per_point: float | None
    limit_change: datetime | None = None  # the change the baseline restarts from
    baseline_rows: list[TrendRow] = field(default_factory=list)
    recent_rows: list[TrendRow] = field(default_factory=list)
    recent_dollars_per_point: float | None = None
    recent_output_per_point: float | None = None
    recent_windows: int = 1
    compared_windows: int = 0

    @property
    def collecting(self) -> bool:
        return self.output_baseline.windows < BASELINE_MIN_WINDOWS


def min_trend_pct(metric: str) -> float:
    return TREND_MIN_PCT.get(metric, MIN_COUNTABLE_PCT)


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


def _median(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def _exclusion(summary: WindowSummary, period_id: str | None) -> str:
    pct = summary.last_pct
    if pct is None:
        return REASON_NO_READING
    if FLAG_LIMIT_REACHED in summary.flags or pct > MAX_COUNTABLE_PCT:
        return REASON_LIMIT
    if pct < min_trend_pct(summary.base_metric):
        return REASON_LOW
    if FLAG_INCOMPLETE in summary.flags:
        return REASON_INCOMPLETE
    if FLAG_TIME_UNCERTAIN in summary.flags:
        return REASON_TIME
    if period_id is not None and summary.period_id != period_id:
        return REASON_PERIOD
    return COUNTED


def _segment(changes: list[datetime], when: datetime) -> int:
    return sum(1 for change in changes if change <= when)


def build_row(
    summary: WindowSummary,
    rates: RateTable,
    period_id: str | None,
    changes: list[datetime] = (),
    current_segment: int = 0,
) -> TrendRow:
    cost = summarize_costs(summary.usage, rates)
    pct = summary.last_pct
    reason = _exclusion(summary, period_id)
    if reason == COUNTED and changes:
        if any(summary.window_start < change < summary.resets_at for change in changes):
            reason = REASON_SPANS_CHANGE
        elif _segment(changes, summary.window_start) != current_segment:
            reason = REASON_BEFORE_CHANGE
    usable_pct = pct if pct is not None and pct > 0 else None
    note = ""
    if not cost.has_unpriced:
        dollars_reason = COUNTED
    elif cost.unpriced_share <= UNPRICED_TOLERANCE and cost.priced_cost > 0:
        dollars_reason = COUNTED
        note = NOTE_PARTIAL_PRICE
    else:
        dollars_reason = REASON_UNPRICED
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
        dollars_note=note,
    )


def _baseline(values: list[float]) -> Baseline:
    if not values:
        return Baseline(0, None, None, None)
    return Baseline(len(values), statistics.median(values), min(values), max(values))


def _change(recent: float | None, baseline: Baseline) -> float | None:
    if (
        recent is None
        or baseline.median is None
        or baseline.median <= 0
        or baseline.windows < BASELINE_MIN_WINDOWS
    ):
        return None
    return recent / baseline.median - 1


def build_trend(
    summaries: list[WindowSummary],
    metric: str,
    rates: RateTable,
    limit_changes: Iterable[datetime] = (),
    recent_windows: int = 1,
    baseline_windows: int = BASELINE_MAX_WINDOWS,
) -> TrendReport:
    completed = sorted(
        (s for s in summaries if s.closed and s.metric == metric),
        key=lambda s: s.resets_at,
        reverse=True,
    )
    changes = sorted(limit_changes)
    # The newest completed window defines the comparison period in force, and
    # the limit changes before its end define the segment being compared.
    period_id = completed[0].period_id if completed else None
    current_segment = _segment(changes, completed[0].resets_at) if completed else len(changes)
    rows = [build_row(s, rates, period_id, changes, current_segment) for s in completed]
    counted = [row for row in rows if row.counted]
    recent_windows = max(1, recent_windows)
    recent = counted[:recent_windows]
    earlier = counted[recent_windows : recent_windows + baseline_windows]

    output_baseline = _baseline([r.output_per_point for r in earlier if r.output_per_point is not None])
    dollars_baseline = _baseline(
        [r.dollars_per_point for r in earlier if r.dollars_counted and r.dollars_per_point is not None]
    )
    recent_dollars = _median(r.dollars_per_point for r in recent if r.dollars_counted)
    recent_output = _median(r.output_per_point for r in recent)

    priced = [r for r in counted if r.dollars_counted]
    points_priced = sum(r.pct or 0 for r in priced)
    points_all = sum(r.pct or 0 for r in counted)
    return TrendReport(
        metric=metric,
        rows=rows,
        current=counted[0] if counted else None,
        dollars_baseline=dollars_baseline,
        output_baseline=output_baseline,
        dollars_change=_change(recent_dollars, dollars_baseline),
        output_change=_change(recent_output, output_baseline),
        pooled_dollars_per_point=(
            sum(r.cost.priced_cost for r in priced) / points_priced if points_priced else None
        ),
        pooled_output_per_point=(
            sum(r.cost.tokens.output for r in counted) / points_all if points_all else None
        ),
        limit_change=changes[current_segment - 1] if current_segment else None,
        baseline_rows=earlier,
        recent_rows=recent,
        recent_dollars_per_point=recent_dollars,
        recent_output_per_point=recent_output,
        recent_windows=recent_windows,
        compared_windows=len(counted),
    )


def mix_warnings(report: TrendReport) -> list[str]:
    """Plain warnings when the recent windows' mix differs from the baseline's.

    A different model or cache mix moves cost per 1% without any change to
    the allowance, so it is called out next to the verdict.
    """
    recent = report.recent_rows
    baseline = report.baseline_rows
    if not recent or len(baseline) < BASELINE_MIN_WINDOWS:
        return []

    def most_common_model(rows: list[TrendRow]) -> str | None:
        counts = Counter(row.top_model for row in rows if row.top_model)
        return counts.most_common(1)[0][0] if counts else None

    out = []
    recent_top = most_common_model(recent)
    usual_top = most_common_model(baseline)
    if recent_top and usual_top and recent_top != usual_top:
        out.append(
            f"Mostly {recent_top} in the recent windows, unlike the usual {usual_top}. "
            "A different model mix changes cost per 1%."
        )
    recent_cache = _median(row.cache_share for row in recent)
    usual_cache = _median(row.cache_share for row in baseline)
    if (
        recent_cache is not None
        and usual_cache is not None
        and abs(recent_cache - usual_cache) >= MIX_CACHE_POINTS
    ):
        out.append(
            f"Cache reads were {recent_cache:.0%} of input in the recent windows, against a "
            f"usual {usual_cache:.0%}. That changes cost per 1% too."
        )
    return out
