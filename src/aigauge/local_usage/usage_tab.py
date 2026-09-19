"""The "Usage and cost" tab of the details dialog.

Top to bottom: a card for each current window (session, week, and a
model-specific limit such as Fable), one view at a time (by model, by day or
trend), and a footer that stays in view. The tab scrolls as a single area;
tables grow to fit their rows instead of scrolling inside it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..models import UsageSnapshot
from ..ratio import MIN_COUNTABLE_PCT
from .limit_changes_dialog import LimitChangesDialog
from .rates import (
    CostSummary,
    RateTable,
    format_cost,
    format_total_cost,
    load_rate_table,
    summarize_costs,
)
from .settings_panel import SETTINGS_DISCLOSURE, relative_time
from .store import CLAUDE, ModelUsage, local_date, local_day_bounds
from .summaries import ORIGIN_BACKFILL, load_summaries
from .trend import (
    BASELINE_MIN_WINDOWS,
    COUNTED,
    REASON_INCOMPLETE,
    REASON_IN_PROGRESS,
    REASON_LIMIT,
    REASON_LOW,
    REASON_NO_READING,
    REASON_PERIOD,
    REASON_SPANS_CHANGE,
    REASON_TIME,
    UNPRICED_TOLERANCE,
    TrendReport,
    TrendRow,
    build_trend,
    mix_warnings,
)
from .windows import FABLE, SESSION, WEEKLY, WindowSpec, current_windows

RANGE_WEEK = "week"
RANGE_SESSION = "session"
RANGE_TODAY = "today"
RANGE_7D = "7d"
RANGE_30D = "30d"
# "This week" first, so the table matches the week card by default.
RANGES = (
    (RANGE_WEEK, "This week"),
    (RANGE_SESSION, "This session"),
    (RANGE_TODAY, "Today"),
    (RANGE_7D, "Last 7 days"),
    (RANGE_30D, "Last 30 days"),
)
VIEW_MODEL = "model"
VIEW_DAY = "day"
VIEW_TREND = "trend"
VIEWS = ((VIEW_MODEL, "By model"), (VIEW_DAY, "By day"), (VIEW_TREND, "Trend"))
DAILY_DAYS = 30
UNAVAILABLE = "Not available yet"
FOOTER_TEXT = "This computer's logs only · API-equivalent estimate"
CARD_TITLES = {SESSION: "This session", WEEKLY: "This week", FABLE: "Fable this week"}
LIMIT_TIP = (
    "Usage past the limit may be billed as extra usage, so cost per 1% isn't "
    "meaningful for this window."
)

TEXT = "#e5e7eb"
MUTED = "#9ca3af"
DIM = "#6b7280"
AMBER = "#fbbf24"
RED = "#f87171"
MODEL_COLORS = (
    "#60a5fa", "#34d399", "#f472b6", "#f59e0b",
    "#a78bfa", "#f87171", "#22d3ee", "#a3e635",
)
MODEL_DISPLAY_NAMES = {
    "codex-auto-review": "Automatic review",
    "unknown": "Unidentified",
}
_CLAUDE_FAMILIES = {"opus", "sonnet", "haiku", "fable", "mythos"}
_MODEL_DATE_SUFFIX = re.compile(r"-20\d{6}$")
UNPRICED_DISPLAY_NAMES = {
    "codex-auto-review": "automatic review",
    "unknown": "unidentified usage",
}

# key, header, shown only with "Token details"
MODEL_COLUMNS = (
    ("model", "Model", False),
    ("cost", "Est. cost", False),
    ("share", "Share of cost", False),
    ("volume", "Token volume", False),
    ("average_rate", "Avg. $/MTok", False),
    ("messages", "Msgs", False),
    ("output", "Output", True),
    ("input", "Input", True),
    ("cache_read", "Cache read", True),
    ("cache_write", "Cache write", True),
    ("reasoning", "Reasoning", True),
    ("sidechain", "Subagent %", True),
)
_COLUMN_INDEX = {key: i for i, (key, _title, _details) in enumerate(MODEL_COLUMNS)}
# Cost and usage columns always show; the comparison column is opt-in and the
# remaining mix/source columns appear with "Details".
TREND_COLUMNS = (
    "Window", "Used", "Est. cost", "Cost per 1%", "Output per 1%", "vs baseline",
    "Cache share", "Top model", "Source",
)
TREND_BASIC_COLUMNS = 6
TREND_RECENT_ROWS = 20
TREND_CHART_POINTS = 60
TREND_UNITS = {SESSION: ("session", "sessions"), WEEKLY: ("week", "weeks")}
# Pool recent usage across several windows so a short session does not carry
# the same weight as a large one. A week is already an aggregate.
TREND_RECENT = {SESSION: 5, WEEKLY: 1}
TREND_BASELINE = {SESSION: 20, WEEKLY: 8}
TREND_ROLLING = {SESSION: 5, WEEKLY: 4}
# About the same as usual when within this share of the typical value.
SAME_AS_USUAL = 0.10
SHORT_REASONS = {
    REASON_NO_READING: "no reading",
    REASON_LOW: "too little used",
    REASON_LIMIT: "limit reached",
    REASON_INCOMPLETE: "logs missing",
    REASON_TIME: "clock change",
    REASON_PERIOD: "different account or plan",
    REASON_SPANS_CHANGE: "spans a limit change",
}
TREND_CONTEXT_REASONS = {REASON_LIMIT, REASON_IN_PROGRESS}
TREND_DEFINITION = (
    "Cost per 1% is what a window's usage would cost at API prices, divided by how "
    "much of the limit it used. Higher means the limit covered more. Dots are individual "
    "windows; capped and in-progress dots are context only. The solid line is a "
    "usage-weighted rolling average of completed comparable windows. This is a rough signal: "
    "model choice and cache use can move it even when the provider's limit is unchanged."
)


def display_model_name(model: str) -> str:
    explicit = MODEL_DISPLAY_NAMES.get(model)
    if explicit is not None:
        return explicit

    # Claude has used both family-first IDs (claude-sonnet-4-5) and
    # version-first IDs (claude-3-5-sonnet). Recognize only that stable shape,
    # remove a terminal YYYYMMDD release tag, and leave anything unfamiliar
    # untouched rather than guessing at a misleading friendly name.
    if not model.startswith("claude-"):
        return model
    body = _MODEL_DATE_SUFFIX.sub("", model.removeprefix("claude-"))
    parts = body.split("-")
    families = [part for part in parts if part in _CLAUDE_FAMILIES]
    if len(families) != 1:
        return model
    family = families[0]
    remainder = [part for part in parts if part != family]
    if remainder and remainder[-1] == "latest":
        remainder.pop()
    if not remainder or any(not part.isdigit() for part in remainder):
        return model
    return f"{family.title()} {'.'.join(remainder)}"


def unpriced_note(summary: CostSummary) -> str:
    rows = [row for row in summary.rows if row.has_unpriced]
    names = [UNPRICED_DISPLAY_NAMES.get(row.model, row.model) for row in rows]
    if not names:
        return ""
    if len(names) == 1:
        subject = names[0]
    elif len(names) == 2:
        subject = f"{names[0]} and {names[1]}"
    else:
        subject = f"{len(names)} models without prices"
    return f"Excludes {subject} ({summary.unpriced_share * 100:.0f}% of tokens)"

_SEGMENT_STYLE = (
    "QPushButton { background:#111827; color:#9ca3af; border:1px solid #374151; "
    "border-radius:0; padding:4px 14px; min-height:20px; }"
    "QPushButton:checked { background:#374151; color:#f9fafb; }"
    "QPushButton:hover { color:#f3f4f6; }"
)
_CHIP_STYLE = (
    "QPushButton { background:#1e3a8a; color:#dbeafe; border:1px solid #1d4ed8; "
    "border-radius:10px; padding:2px 10px; min-height:18px; }"
)
_COMPARE_STYLE = (
    "QPushButton { background:transparent; color:#93c5fd; border:1px solid #3b82f6; "
    "border-radius:10px; padding:2px 10px; min-height:18px; }"
    "QPushButton:checked { background:#1e3a8a; color:#eff6ff; }"
    "QPushButton:hover { color:#f3f4f6; }"
)


# ---- formatting ----


def format_tokens(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    if value < 1000:
        return f"{value:.0f}"
    if value < 10_000:
        return f"{value / 1000:.1f}K"
    if value < 1_000_000:
        return f"{value / 1000:.0f}K"
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1_000_000_000:.1f}B"


def token_volume(tokens: TokenCounts) -> int:
    """Non-overlapping prompt-side and output tokens recorded in the logs."""
    return tokens.total_input() + tokens.output


def average_cost_per_million(cost: float | None, tokens: TokenCounts) -> float | None:
    """Blended API-equivalent dollars per million recorded tokens."""
    total = token_volume(tokens)
    if cost is None or total <= 0:
        return None
    return cost * 1_000_000 / total


def token_volume_tooltip(
    tokens: TokenCounts, *, cost: float | None = None, exclusion: str = ""
) -> str:
    """Explain total token volume and the categories that make it up."""
    grouped = {
        "Cache reads": tokens.cache_read,
        "Cache writes": tokens.cache_write_5m + tokens.cache_write_1h,
        "Output": tokens.output,
        "Uncached input": tokens.input,
    }
    total = sum(grouped.values())
    lines = [
        f"Total token volume: {format_tokens(total)}",
        "Sum across requests; repeated cache reads count each time they are used.",
    ]
    if total > 0:
        for label, count in grouped.items():
            share = 100 * count / total
            share_text = "<1%" if 0 < share < 1 else f"{share:.0f}%"
            lines.append(f"{label}: {format_tokens(count)} ({share_text})")
        if cost is not None:
            lines.append(f"Blended API rate: {format_cost(cost * 1_000_000 / total)} per 1M tokens")
    lines.append("Reasoning tokens are already included in output and are not counted twice.")
    if exclusion:
        lines.append(exclusion)
    return "\n".join(lines)


def average_rate_tooltip(
    tokens: TokenCounts, *, cost: float | None = None, exclusion: str = ""
) -> str:
    """Explain why the blended per-token rate varies between rows."""
    prefix = (
        "Estimated cost divided by total token volume. The average varies with "
        "the token mix."
    )
    return f"{prefix}\n{token_volume_tooltip(tokens, cost=cost, exclusion=exclusion)}"


def format_ballpark_cost(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    return f"~${value:.1f}" if value >= 1 else f"~${value:.2f}"


def format_time_left(resets_at: datetime, now: datetime) -> str:
    seconds = int((resets_at - now).total_seconds())
    if seconds <= 0:
        return "resetting"
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h left"
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h {rem // 60}m left" if hours else f"{rem // 60}m left"


def window_label(start: datetime, end: datetime) -> str:
    start_local, end_local = start.astimezone(), end.astimezone()
    if (end - start) <= timedelta(hours=12):
        return f"{start_local:%b %d %H:%M} to {end_local:%H:%M}"
    return f"{start_local:%b %d} to {end_local:%b %d}"


def trend_window_label(row: TrendRow) -> str:
    text = window_label(row.summary.window_start, row.summary.resets_at)
    if row.reason == REASON_IN_PROGRESS:
        return f"{text} · in progress"
    if row.reason == REASON_LIMIT:
        return f"{text} · limit reached"
    return text


def trend_context_tooltip(row: TrendRow) -> str:
    if row.reason == REASON_IN_PROGRESS:
        return (
            "Current partial window. Shown for context but excluded from comparisons "
            "and rolling averages."
        )
    if row.reason == REASON_LIMIT:
        return (
            "This window reached the usage limit. It is shown for context but excluded "
            "from comparisons and rolling averages because extra usage may be billed separately."
        )
    return ""


def _signed_percent(change: float) -> str:
    return f"{change * 100:+.0f}%"


def _verdict(change: float, reference: str = "usual") -> str:
    if abs(change) <= SAME_AS_USUAL:
        return f"About the same as {reference}"
    return f"{abs(change) * 100:.0f}% {'more' if change > 0 else 'less'} than {reference}"


def trend_summary_lines(report: TrendReport, metric: str = SESSION) -> list[str]:
    """The answer at the top of the Trend view, in plain words, headline first."""
    unit, units = TREND_UNITS.get(metric, ("window", "windows"))
    change_day = report.limit_change.astimezone().strftime("%b %d") if report.limit_change else None
    if report.current is None:
        if change_day:
            return [f"No completed {units} since the limit change on {change_day} yet."]
        return [f"No completed {units} to compare yet."]
    if report.collecting:
        needed = report.recent_windows + BASELINE_MIN_WINDOWS
        message = f"Needs {needed} completed {units} to compare; {report.compared_windows} so far."
        if change_day:
            return [f"Since the limit change on {change_day}: {message.lower()}"]
        return [message]
    recent_count = len(report.recent_rows)
    lead = f"Last {recent_count} {units}" if recent_count > 1 else f"Latest {unit}"
    if report.baseline_before_change and change_day:
        lead = f"Since {change_day}, {lead.lower()}"
    reference = "before" if report.baseline_before_change else "usual"
    baseline_label = "before-change average" if report.baseline_before_change else "earlier average"
    baseline_suffix = f" before {change_day}" if report.baseline_before_change and change_day else ""
    dollars = report.dollars_baseline
    output = report.output_baseline
    lines = []
    if report.dollars_change is not None and report.recent_dollars_per_point is not None:
        lines.append(
            f"{lead} averaged {format_cost(report.recent_dollars_per_point)} of usage "
            "per 1% of the limit"
        )
        lines.append(
            f"{_verdict(report.dollars_change, reference)} "
            f"({baseline_label} {format_cost(dollars.average)}, from "
            f"{dollars.windows} {units}{baseline_suffix})"
        )
        if report.output_change is not None and report.recent_output_per_point is not None:
            lines.append(
                f"Output per 1%: {format_tokens(report.recent_output_per_point)}, "
                f"{_verdict(report.output_change, reference).lower()}"
            )
    elif report.output_change is not None and report.recent_output_per_point is not None:
        lines.append(
            f"{lead} averaged {format_tokens(report.recent_output_per_point)} output tokens "
            "per 1% of the limit"
        )
        lines.append(
            f"{_verdict(report.output_change, reference)} "
            f"({baseline_label} {format_tokens(output.average)}, from "
            f"{output.windows} {units}{baseline_suffix})"
        )
        lines.append("Cost per 1% isn't compared: too few windows have a price for every model.")
    else:
        lines.append(f"{lead}: not enough comparable figures yet.")
    return lines


def trend_summary_text(report: TrendReport, metric: str = SESSION) -> str:
    return "\n".join(trend_summary_lines(report, metric))


def trend_recent_text(report: TrendReport, metric: str = SESSION) -> str:
    """Compact everyday readout; detailed comparison is an optional mode."""
    unit, units = TREND_UNITS.get(metric, ("window", "windows"))
    count = len(report.recent_rows)
    if not count:
        partial = next(
            (row for row in report.rows if row.reason == REASON_IN_PROGRESS), None
        )
        if partial is not None:
            values = []
            if partial.pct is not None:
                values.append(f"{partial.pct:.0f}% used")
            if partial.cost.rows:
                values.append(f"{format_total_cost(partial.cost)} API-equiv.")
            suffix = "    ·    " + "    ·    ".join(values) if values else ""
            return f"Current {unit} in progress{suffix}"
        return f"No completed {units} to summarize yet."
    window_text = f"{count} {units}" if count > 1 else f"latest {unit}"
    values = []
    if report.recent_dollars_per_point is not None:
        values.append(f"{format_ballpark_cost(report.recent_dollars_per_point)} API-equiv. / 1%")
    if report.recent_output_per_point is not None:
        values.append(f"~{format_tokens(report.recent_output_per_point)} output / 1%")
    if not values:
        return f"Recent ballpark · {window_text}"
    return f"Recent ballpark · {window_text}    " + "    ·    ".join(values)


def _label(text: str = "", color: str = TEXT, size: int = 11, bold: bool = False) -> QLabel:
    label = QLabel(text)
    weight = "font-weight:700;" if bold else ""
    label.setStyleSheet(f"color:{color}; font-size:{size}px; {weight}")
    return label


# ---- widgets ----


class _Bar(QWidget):
    """Coloured bar segments with an optional label on the right."""

    def __init__(self, segments: list[tuple[float, str]], label: str = "", parent=None):
        super().__init__(parent)
        self.segments = segments
        self.label = label
        self.setMinimumHeight(20)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(6, 0, -6, 0)
        text_width = 40.0 if self.label else 0.0
        bar_width = max(0.0, rect.width() - text_width)
        top = rect.center().y() - 5
        x = rect.left()
        for fraction, color in self.segments:
            width = bar_width * max(0.0, min(1.0, fraction))
            if width <= 0:
                continue
            painter.fillRect(QRectF(x, top, width, 10), QColor(color))
            x += width
        if self.label:
            painter.setPen(QColor(TEXT))
            painter.drawText(
                QRectF(rect.right() - text_width, rect.top(), text_width, rect.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                self.label,
            )
        painter.end()


@dataclass(frozen=True)
class ComparisonMetric:
    title: str
    before: float
    recent: float
    before_text: str
    recent_text: str
    change: float


class _TrendComparison(QWidget):
    """Compact zero-based before/recent bars for the two ballpark metrics."""

    ROW_HEIGHT = 46

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.metrics: list[ComparisonMetric] = []
        self.reference_label = "Earlier"
        self.setMinimumHeight(1)

    def set_data(
        self,
        metrics: list[ComparisonMetric],
        tooltip: str = "",
        reference_label: str = "Earlier",
    ) -> None:
        self.metrics = list(metrics)
        self.reference_label = reference_label
        self.setFixedHeight(max(1, self.ROW_HEIGHT * len(self.metrics)))
        self.setToolTip(tooltip)
        self.setHidden(not self.metrics)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        if not self.metrics:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 2.0
        title_width = min(155.0, self.width() * 0.25)
        delta_width = 64.0
        bars_left = margin + title_width
        bars_width = max(80.0, self.width() - bars_left - delta_width - margin)
        tag_width = 48.0
        value_width = 60.0
        track_left = bars_left + tag_width
        track_width = max(20.0, bars_width - tag_width - value_width - 8.0)
        value_left = track_left + track_width + 6.0
        align_left = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        align_right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        for index, metric in enumerate(self.metrics):
            top = index * self.ROW_HEIGHT
            maximum = max(metric.before, metric.recent, 1e-9)
            title_font = painter.font()
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor(TEXT))
            painter.drawText(
                QRectF(margin, top, title_width - 8, self.ROW_HEIGHT),
                align_left,
                metric.title,
            )
            painter.setFont(font)

            for row, (tag, value, text, color) in enumerate(
                (
                    (self.reference_label, metric.before, metric.before_text, "#94a3b8"),
                    ("Recent", metric.recent, metric.recent_text, "#60a5fa"),
                )
            ):
                y = top + 7 + row * 19
                painter.setPen(QColor(MUTED if row == 0 else "#bfdbfe"))
                painter.drawText(QRectF(bars_left, y - 5, tag_width - 4, 14), align_left, tag)
                track = QRectF(track_left, y, track_width, 7)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#1f2937"))
                painter.drawRoundedRect(track, 3, 3)
                fill = QRectF(track_left, y, track_width * value / maximum, 7)
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(fill, 3, 3)
                painter.setPen(QColor(TEXT))
                painter.drawText(QRectF(value_left, y - 5, value_width, 14), align_right, text)

            painter.setPen(QColor(TEXT))
            painter.drawText(
                QRectF(self.width() - delta_width, top, delta_width - margin, self.ROW_HEIGHT),
                align_right,
                _signed_percent(metric.change),
            )
        painter.end()


def rolling_average_points(
    rows: list[TrendRow], window: int, *, dollars: bool
) -> list[list[tuple[datetime, float]]]:
    """Usage-weighted rolling averages, split at every marked limit change."""
    segments: dict[int, list[TrendRow]] = {}
    for row in sorted(rows, key=lambda item: item.summary.resets_at):
        if not row.counted or not row.pct:
            continue
        if dollars and not row.dollars_counted:
            continue
        segments.setdefault(row.segment, []).append(row)

    result = []
    for segment_rows in segments.values():
        points = []
        for index, row in enumerate(segment_rows):
            bucket = segment_rows[max(0, index - window + 1) : index + 1]
            total_pct = sum(item.pct or 0 for item in bucket)
            if dollars:
                total_usage = sum(item.cost.priced_cost for item in bucket)
            else:
                total_usage = sum(item.cost.tokens.output for item in bucket)
            if total_pct:
                points.append((row.summary.resets_at, total_usage / total_pct))
        if points:
            result.append(points)
    return result


class _TrendChart(QWidget):
    """Raw allowance windows with a usage-weighted rolling trend."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(170)
        self.setMouseTracking(True)
        self._points: list[tuple[datetime, float, str, int]] = []
        self._rolling: list[list[tuple[datetime, float]]] = []
        self._average_lines: list[tuple[datetime, datetime, float, str, str]] = []
        self._current_segment = 0
        self._focus_trend = False
        self._display_high: float | None = None
        self._typical: float | None = None
        self._changes: list[datetime] = []
        self._format: Callable[[float], str] = format_cost
        self._title = ""
        self._screen: list[tuple[QPointF, str]] = []

    def set_data(
        self,
        points,
        typical,
        changes,
        value_format,
        title,
        *,
        rolling=(),
        average_lines=(),
        current_segment=0,
        focus_trend=False,
    ) -> None:
        self._points = list(points)
        self._rolling = [list(segment) for segment in rolling]
        self._average_lines = list(average_lines)
        self._current_segment = current_segment
        self._focus_trend = focus_trend
        self._display_high = None
        self._typical = typical
        self._changes = list(changes)
        self._format = value_format
        self._title = title
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        left, right, top, bottom = 56.0, 16.0, 20.0, 20.0
        plot = QRectF(
            left, top, max(1.0, self.width() - left - right), max(1.0, self.height() - top - bottom)
        )
        align_left = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        align_right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        painter.setPen(QColor(MUTED))
        painter.drawText(QRectF(0, 0, self.width(), top - 4), align_left, self._title)
        self._screen = []
        if len(self._points) < 2:
            painter.setPen(QColor(DIM))
            painter.drawText(plot, int(Qt.AlignmentFlag.AlignCenter),
                             "Not enough compared windows to chart yet.")
            painter.end()
            return
        values = [value for _when, value, _label, _segment in self._points]
        trend_values = [value for segment in self._rolling for _when, value in segment]
        trend_values.extend(
            value for _start, _end, value, _label, _color in self._average_lines
        )
        if self._typical:
            trend_values.append(self._typical)
        scale_values = trend_values if self._focus_trend and trend_values else values + trend_values
        high = max(scale_values) * 1.15 or 1.0
        self._display_high = high
        start, end = self._points[0][0], self._points[-1][0]
        span = (end - start).total_seconds() or 1.0

        def x_at(when: datetime) -> float:
            fraction = (when - start).total_seconds() / span
            return plot.left() + plot.width() * min(1.0, max(0.0, fraction))

        def y_at(value: float) -> float:
            return plot.bottom() - plot.height() * (value / high)

        painter.setPen(QPen(QColor("#374151"), 1))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        painter.setPen(QColor(DIM))
        painter.drawText(QRectF(0, plot.top() - 6, left - 8, 12), align_right, self._format(high))
        painter.drawText(QRectF(0, plot.bottom() - 6, left - 8, 12), align_right, self._format(0))
        painter.drawText(QRectF(plot.left(), plot.bottom() + 3, 120, bottom - 3), align_left,
                         start.astimezone().strftime("%b %d"))
        painter.drawText(QRectF(plot.right() - 120, plot.bottom() + 3, 120, bottom - 3), align_right,
                         end.astimezone().strftime("%b %d"))

        for change in self._changes:
            if change <= start or change >= end:
                continue
            x = x_at(change)
            painter.setPen(QPen(QColor(AMBER), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor(AMBER))
            label = f"limits changed {change.astimezone():%b %d}"
            if x > plot.right() - 150:
                painter.drawText(QRectF(x - 154, plot.top(), 150, 12), align_right, label)
            else:
                painter.drawText(QRectF(x + 4, plot.top(), 150, 12), align_left, label)

        if self._typical:
            y = y_at(self._typical)
            painter.setPen(QPen(QColor(MUTED), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(MUTED))
            painter.drawText(
                QRectF(plot.right() - 90, y - 14, 90, 12), align_right, "earlier avg"
            )

        for line_start, line_end, value, label, color in self._average_lines:
            y = y_at(value)
            x1, x2 = x_at(line_start), x_at(line_end)
            painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x1, y), QPointF(x2, y))
            painter.setPen(QColor(color))
            painter.drawText(QRectF(x1 + 3, y - 14, max(75.0, x2 - x1 - 6), 12), align_left, label)

        for segment in self._rolling:
            if len(segment) < 2:
                continue
            is_current = any(
                point_segment == self._current_segment and point_when == segment[-1][0]
                for point_when, _value, _label, point_segment in self._points
            )
            painter.setPen(QPen(QColor("#60a5fa" if is_current else "#94a3b8"), 2))
            for first, second in zip(segment, segment[1:]):
                painter.drawLine(
                    QPointF(x_at(first[0]), y_at(first[1])),
                    QPointF(x_at(second[0]), y_at(second[1])),
                )

        painter.setPen(Qt.PenStyle.NoPen)
        last = len(self._points) - 1
        for index, (when, value, label, segment) in enumerate(self._points):
            clipped = value > high
            point = QPointF(x_at(when), plot.top() + 2 if clipped else y_at(value))
            if index == last:
                color = "#f9fafb"
            elif segment == self._current_segment:
                color = "#60a5fa"
            else:
                color = "#94a3b8"
            painter.setBrush(QColor(color))
            radius = 4.5 if index == last else 3.0
            if clipped:
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(point.x(), plot.top()),
                            QPointF(point.x() - 4.5, plot.top() + 8),
                            QPointF(point.x() + 4.5, plot.top() + 8),
                        ]
                    )
                )
            else:
                painter.drawEllipse(point, radius, radius)
            suffix = " (above displayed range)" if clipped else ""
            self._screen.append(
                (point, f"{label}: {self._format(value)} per 1%{suffix}")
            )
        painter.end()

    def mouseMoveEvent(self, event):  # noqa: N802
        position = event.position()
        best = None
        for point, text in self._screen:
            distance = (point.x() - position.x()) ** 2 + (point.y() - position.y()) ** 2
            if distance <= 144 and (best is None or distance < best[0]):
                best = (distance, text)
        if best is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), best[1], self)


class _WindowCard(QFrame):
    def __init__(self, metric: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.metric = metric
        self.setObjectName("usage_card")
        self.setStyleSheet(
            "QFrame#usage_card { background:#111827; border:1px solid #374151; "
            "border-radius:6px; }"
        )
        # Let the grid stretch shorter cards to the tallest card in each row.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(170)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        self.labels = {
            "title": _label(CARD_TITLES[metric], MUTED, 11),
            "cost": _label("", TEXT, 22, bold=True),
            "quota": _label("", TEXT, 12),
            "per_point": _label("", MUTED, 11),
        }
        for key, label in self.labels.items():
            label.setObjectName(f"usage_{metric.lower()}_{key}")
            layout.addWidget(label)

    def set_values(
        self, title: str, cost: str, quota: str, per_point: str, *, limit: bool = False
    ) -> None:
        self.labels["title"].setText(title)
        self.labels["cost"].setText(cost)
        self.labels["quota"].setText(quota)
        self.labels["quota"].setStyleSheet(
            f"color:{RED if limit else TEXT}; font-size:12px; {'font-weight:700;' if limit else ''}"
        )
        self.labels["quota"].setToolTip(LIMIT_TIP if limit else "")
        self.labels["per_point"].setText(per_point)
        self.labels["per_point"].setToolTip(LIMIT_TIP if limit else "")
        # Reserve the optional note's line so neighboring cards stay aligned.
        self.labels["per_point"].setHidden(False)


class _CardRow(QWidget):
    """Cards side by side, wrapping to more rows when the tab is narrow."""

    _MIN_CARD = 200

    def __init__(self, cards: list[_WindowCard], parent: QWidget | None = None):
        super().__init__(parent)
        self._all = cards
        self._visible = list(cards)
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self._relayout(force=True)

    def set_visible_cards(self, cards: list[_WindowCard]) -> None:
        if cards == self._visible:
            return
        self._visible = cards
        self._relayout(force=True)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self, force: bool = False) -> None:
        count = max(1, len(self._visible))
        width = self.width()
        columns = count if width <= 0 else max(1, min(count, width // self._MIN_CARD))
        if columns == self._columns and not force:
            return
        self._columns = columns
        for card in self._all:
            self._grid.removeWidget(card)
            card.setHidden(card not in self._visible)
        for index, card in enumerate(self._visible):
            self._grid.addWidget(card, index // columns, index % columns)
        for column in range(len(self._all)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)


def _table(columns: list[str] | tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(28)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return table


def _fit(table: QTableWidget, fixed: dict[int, int] | None = None) -> None:
    """Size a table to its rows, so only the tab itself scrolls."""
    header = table.horizontalHeader()
    fixed = fixed or {}
    for column, width in fixed.items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    frame = 2 * table.frameWidth()
    width = frame
    for column in range(table.columnCount()):
        if table.isColumnHidden(column):
            continue
        width += fixed.get(
            column, max(table.sizeHintForColumn(column), header.sectionSizeHint(column))
        )
    height = frame + header.sizeHint().height()
    height += sum(table.rowHeight(row) for row in range(table.rowCount()))
    table.setMinimumWidth(width)
    table.setFixedHeight(height)


def _item(text: str, *, right: bool = True, color: str | None = None, bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    item.setTextAlignment(
        (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
        | Qt.AlignmentFlag.AlignVCenter
    )
    if color:
        item.setForeground(QColor(color))
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def _placeholder(table: QTableWidget, text: str) -> None:
    table.clearSpans()
    table.setRowCount(1)
    table.setItem(0, 0, _item(text, right=False, color=DIM))
    if table.columnCount() > 1:
        table.setSpan(0, 0, 1, table.columnCount())


# ---- the tab ----


class UsageCostTab(QWidget):
    def __init__(
        self,
        service,
        account_id: str,
        display_name: str,
        snapshot: UsageSnapshot | None = None,
        rate_table: RateTable | None = None,
        now: Callable[[], datetime] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._service = service
        self._account_id = account_id
        self._display_name = display_name
        self._provider = service.provider_for_account(account_id) or CLAUDE
        self._snapshot = snapshot
        self._rates = rate_table or load_rate_table()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._day_filter: date | None = None
        self._daily_days: list[date] = []
        self._model_colors: dict[str, str] = {}
        self._view = self._service.config.local_usage.details_view
        self._trend_metric = SESSION
        self._trend_show_all = False
        self._trend_skipped_open = False
        self._trend_compare_change = False
        self._trend_compare_available = False
        self._connected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(12)

        self.status_label = _label("", AMBER, 11)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.cards = {metric: _WindowCard(metric) for metric in (SESSION, WEEKLY, FABLE)}
        self.card_row = _CardRow(list(self.cards.values()))
        layout.addWidget(self.card_row)

        layout.addLayout(self._build_view_bar())
        # Pages sit in the layout and only the current one is shown: a hidden
        # widget takes no space, whereas a stacked widget is as tall as its
        # tallest page and pads short views with empty space.
        self.pages = {
            VIEW_MODEL: self._build_model_page(),
            VIEW_DAY: self._build_day_page(),
            VIEW_TREND: self._build_trend_page(),
        }
        for page in self.pages.values():
            page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            layout.addWidget(page)
        layout.addStretch(1)

        root.addLayout(self._build_footer())

        self._connect()
        self.show_view(self._view)
        self.refresh()

    # ---- building ----

    def _build_view_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(0)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self.view_buttons: dict[str, QPushButton] = {}
        for key, label in VIEWS:
            button = QPushButton(label)
            button.setObjectName(f"usage_view_{key}")
            button.setCheckable(True)
            button.setStyleSheet(_SEGMENT_STYLE)
            button.clicked.connect(lambda _checked=False, key=key: self.show_view(key))
            self._view_group.addButton(button)
            self.view_buttons[key] = button
            bar.addWidget(button)
        bar.addSpacing(12)
        self.day_chip = QPushButton("")
        self.day_chip.setObjectName("usage_day_chip")
        self.day_chip.setStyleSheet(_CHIP_STYLE)
        self.day_chip.setToolTip("Show the whole range again")
        self.day_chip.clicked.connect(self._clear_day_filter)
        self.day_chip.setHidden(True)
        bar.addWidget(self.day_chip)
        bar.addStretch(1)
        self.range_combo = QComboBox()
        self.range_combo.setObjectName("usage_range_combo")
        for key, label in RANGES:
            self.range_combo.addItem(label, key)
        saved_range = self._service.config.local_usage.details_range
        saved_index = self.range_combo.findData(saved_range)
        self.range_combo.setCurrentIndex(max(0, saved_index))
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        bar.addWidget(self.range_combo)
        self._trend_metric_bar = QWidget()
        metric_row = QHBoxLayout(self._trend_metric_bar)
        metric_row.setContentsMargins(0, 0, 0, 0)
        metric_row.setSpacing(0)
        self._trend_metric_group = QButtonGroup(self)
        self._trend_metric_group.setExclusive(True)
        self.trend_metric_buttons: dict[str, QPushButton] = {}
        for metric, label in ((SESSION, "Sessions"), (WEEKLY, "Weeks")):
            button = QPushButton(label)
            button.setObjectName(f"usage_trend_{metric.lower()}")
            button.setCheckable(True)
            button.setStyleSheet(_SEGMENT_STYLE)
            button.clicked.connect(lambda _checked=False, metric=metric: self.set_trend_metric(metric))
            self._trend_metric_group.addButton(button)
            self.trend_metric_buttons[metric] = button
            metric_row.addWidget(button)
        self.trend_metric_buttons[self._trend_metric].setChecked(True)
        bar.addWidget(self._trend_metric_bar)
        bar.addSpacing(6)
        self.compare_change_btn = QPushButton("Compare change")
        self.compare_change_btn.setObjectName("usage_compare_change_btn")
        self.compare_change_btn.setCheckable(True)
        self.compare_change_btn.setStyleSheet(_COMPARE_STYLE)
        self.compare_change_btn.setToolTip("Show the before/after comparison for the marked change")
        self.compare_change_btn.toggled.connect(self._set_compare_change)
        self.compare_change_btn.setHidden(True)
        bar.addWidget(self.compare_change_btn)
        bar.addSpacing(6)
        self.limit_changes_btn = QPushButton("Limits changed…")
        self.limit_changes_btn.setObjectName("usage_limit_changes_btn")
        self.limit_changes_btn.setToolTip(
            "Mark when the provider changed its limits to compare usage before and after"
        )
        self.limit_changes_btn.clicked.connect(self._edit_limit_changes)
        bar.addWidget(self.limit_changes_btn)
        return bar

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.model_table = _table([title for _key, title, _details in MODEL_COLUMNS])
        self.model_table.setObjectName("usage_model_table")
        # Keep the compact numeric columns together; unused width belongs
        # after the table rather than inside the final Messages column.
        self.model_table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.model_table)
        row = QHBoxLayout()
        row.addStretch(1)
        self.token_details_cb = QCheckBox("Token details")
        self.token_details_cb.setToolTip(
            "Show estimated cost, raw token categories, reasoning and subagent columns"
        )
        self.token_details_cb.toggled.connect(self._on_token_details)
        row.addWidget(self.token_details_cb)
        layout.addLayout(row)
        return page

    def _build_day_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.day_legend = _label("", MUTED, 11)
        self.day_legend.setTextFormat(Qt.TextFormat.RichText)
        self.day_legend.setWordWrap(True)
        self.day_legend.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.day_legend)
        self.daily_table = _table(["Day", "Est. cost", "Msgs", "Cost by model"])
        self.daily_table.setObjectName("usage_daily_table")
        self.daily_table.cellClicked.connect(self._on_day_clicked)
        self.daily_table.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.daily_table)
        layout.addWidget(_label("Click a day to see its models.", DIM, 10))
        return page

    def _build_trend_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.trend_headline_label = _label("", TEXT, 12, bold=True)
        self.trend_headline_label.setWordWrap(True)
        layout.addWidget(self.trend_headline_label)

        box = QFrame()
        self.trend_answer = box
        box.setObjectName("trend_answer")
        box.setStyleSheet(
            "QFrame#trend_answer { background:#111827; border:1px solid #374151; border-radius:6px; }"
        )
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(12, 10, 12, 10)
        box_layout.setSpacing(4)
        self.trend_comparison = _TrendComparison()
        box_layout.addWidget(self.trend_comparison)
        self.trend_detail_label = _label("", MUTED, 10)
        self.trend_warning_label = _label("", AMBER, 11)
        for label in (self.trend_detail_label, self.trend_warning_label):
            label.setWordWrap(True)
            box_layout.addWidget(label)
        box.setHidden(True)
        layout.addWidget(box)

        self.trend_chart = _TrendChart()
        self.trend_chart.setToolTip(TREND_DEFINITION)
        layout.addWidget(self.trend_chart)

        self.trend_table = _table(TREND_COLUMNS)
        self.trend_table.setObjectName("usage_trend_table")
        layout.addWidget(self.trend_table)
        row = QHBoxLayout()
        self.trend_show_all_btn = QToolButton()
        self.trend_show_all_btn.clicked.connect(self._toggle_trend_show_all)
        row.addWidget(self.trend_show_all_btn)
        row.addStretch(1)
        self.trend_details_cb = QCheckBox("Details")
        self.trend_details_cb.setToolTip("Show cost, cache share, top model and source for each window")
        self.trend_details_cb.toggled.connect(lambda _on: self.refresh())
        row.addWidget(self.trend_details_cb)
        layout.addLayout(row)

        self.trend_skipped_btn = QToolButton()
        self.trend_skipped_btn.setStyleSheet(
            "QToolButton { background:transparent; border:none; color:#9ca3af; padding:2px 0; }"
            "QToolButton:hover { color:#f3f4f6; }"
        )
        self.trend_skipped_btn.clicked.connect(self._toggle_trend_skipped)
        layout.addWidget(self.trend_skipped_btn)
        self.trend_skipped_table = _table(("Window", "Used", "Why it isn't compared"))
        self.trend_skipped_table.setObjectName("usage_trend_skipped_table")
        self.trend_skipped_table.setHidden(True)
        layout.addWidget(self.trend_skipped_table)
        return page

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 4, 0)
        footer.setSpacing(6)
        self.updated_label = _label("", DIM, 10)
        footer.addWidget(self.updated_label)
        footer.addWidget(_label("·", DIM, 10))
        footer.addWidget(_label(FOOTER_TEXT, DIM, 10))
        info = QToolButton()
        info.setText("i")
        info.setToolTip("About these numbers")
        info.clicked.connect(self._show_disclosure)
        footer.addWidget(info)
        footer.addStretch(1)
        self.importing_label = _label("", AMBER, 10)
        footer.addWidget(self.importing_label)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.setToolTip("Stop the import. It picks up where it left off next time.")
        self.cancel_btn.clicked.connect(self._service.cancel)
        footer.addWidget(self.cancel_btn)
        return footer

    def _connect(self) -> None:
        self._service.import_finished.connect(self._on_import_finished)
        self._service.progress_changed.connect(self._on_progress)
        self._service.import_started.connect(self._on_import_started)
        self._connected = True

    def detach(self) -> None:
        """Stop listening to the service (called when the dialog closes)."""
        if not self._connected:
            return
        for signal, slot in (
            (self._service.import_finished, self._on_import_finished),
            (self._service.progress_changed, self._on_progress),
            (self._service.import_started, self._on_import_started),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connected = False

    # ---- views ----

    def show_view(self, key: str) -> None:
        changed = key != self._view
        self._view = key
        self.view_buttons[key].setChecked(True)
        for page_key, page in self.pages.items():
            page.setHidden(page_key != key)
        self.range_combo.setHidden(key != VIEW_MODEL)
        self.day_chip.setHidden(key != VIEW_MODEL or self._day_filter is None)
        self._trend_metric_bar.setHidden(key != VIEW_TREND)
        self.compare_change_btn.setHidden(
            key != VIEW_TREND or not self._trend_compare_available
        )
        self.limit_changes_btn.setHidden(key != VIEW_TREND)
        if changed:
            self._remember_display_choice("details_view", key)

    def current_view(self) -> str:
        return self._view

    def card_text(self, metric: str, key: str) -> str:
        return self.cards[metric].labels[key].text()

    # ---- data ----

    def refresh(self) -> None:
        store = self._service.store
        now = self._now()
        provider = self._provider
        last_import = store.last_import(provider)
        running = self._service.is_running()
        self.updated_label.setText(
            "Not imported yet" if last_import is None
            else f"Updated {relative_time(last_import, now)}"
        )
        self._sync_import_status(running)

        recognized = store.get_meta(f"recognized:{provider}")
        notes = []
        if recognized is False:
            notes.append(
                "These logs weren't recognized, so usage can't be shown. "
                "An AI Gauge update may be needed."
            )
        if running:
            notes.append("Import in progress; numbers are partial until it finishes.")
        if self._rates.errors:
            notes.append('Some prices failed to load; those models show "no price".')
        self.status_label.setText(" ".join(notes))
        self.status_label.setHidden(not notes)

        available = last_import is not None and recognized is not False
        windows = (
            current_windows(
                provider, self._snapshot, store, now, account_id=self._account_id
            )
            if available
            else {}
        )
        self._assign_colors(now, available)
        self._fill_cards(windows, now, available, running)
        self._refresh_models(now, windows, available)
        self._refresh_daily(now, available)
        self._refresh_trend(available)
        self.show_view(self._view)

    def _window_usage(self, spec: WindowSpec, start: datetime, end: datetime) -> list[ModelUsage]:
        rows = self._service.store.usage_by_model(self._provider, start, end)
        if spec.model_filter:
            rows = [row for row in rows if spec.model_filter in row.model.lower()]
        return rows

    def _fill_cards(
        self, windows: dict[str, WindowSpec], now: datetime, available: bool, partial: bool
    ) -> None:
        visible = []
        for metric, card in self.cards.items():
            spec = windows.get(metric)
            if metric == FABLE and spec is None:
                continue  # only shown for accounts with a Fable limit
            visible.append(card)
            if spec is None or not available:
                card.set_values(CARD_TITLES[metric], UNAVAILABLE, UNAVAILABLE, "")
                continue
            title = f"{CARD_TITLES[metric]} · {format_time_left(spec.resets_at, now)}"
            summary = summarize_costs(self._window_usage(spec, spec.start, now), self._rates)
            cost = format_total_cost(summary) + (" (partial)" if partial else "")
            pct = spec.pct
            limit = pct is not None and pct >= 100
            allowance = "Fable allowance" if metric == FABLE else "allowance"
            if pct is None:
                quota = "No reading yet"
            elif limit:
                quota = f"{pct:.0f}% · limit reached"
            else:
                quota = f"{pct:.0f}% of {allowance} used"
            card.set_values(title, cost, quota, self._per_point_text(spec, pct, limit), limit=limit)
        self.card_row.set_visible_cards(visible)

    def _per_point_text(self, spec: WindowSpec, pct: float | None, limit: bool) -> str:
        if pct is None or spec.last_reading_at is None:
            return ""
        if limit:
            return "No cost per 1% at the limit"
        if pct < MIN_COUNTABLE_PCT:
            return "Too early for cost per 1%"
        at_reading = summarize_costs(
            self._window_usage(spec, spec.start, spec.last_reading_at), self._rates
        )
        if at_reading.has_unpriced:
            if at_reading.unpriced_share > UNPRICED_TOLERANCE or at_reading.priced_cost <= 0:
                return unpriced_note(at_reading)
            return (
                f"≈ {format_cost(at_reading.priced_cost / pct)} per 1% · "
                f"excludes {at_reading.unpriced_share * 100:.0f}%"
            )
        return f"≈ {format_cost(at_reading.priced_cost / pct)} per 1%"

    def _assign_colors(self, now: datetime, available: bool) -> None:
        """Colour models by 30-day cost, so a model keeps its colour across views."""
        if not available:
            return
        today = local_date(now)
        daily = self._service.store.daily_totals(
            self._provider, today - timedelta(days=DAILY_DAYS - 1), today
        )
        rows = [row for day_rows in daily.values() for row in day_rows]
        for model_row in summarize_costs(rows, self._rates).rows:
            if model_row.model not in self._model_colors:
                self._model_colors[model_row.model] = MODEL_COLORS[
                    len(self._model_colors) % len(MODEL_COLORS)
                ]

    def _color(self, model: str) -> str:
        if model not in self._model_colors:
            self._model_colors[model] = MODEL_COLORS[len(self._model_colors) % len(MODEL_COLORS)]
        return self._model_colors[model]

    def _range_bounds(
        self, key: str, now: datetime, windows: dict[str, WindowSpec]
    ) -> tuple[datetime, datetime] | None:
        if self._day_filter is not None:
            return local_day_bounds(self._day_filter)
        if key in (RANGE_SESSION, RANGE_WEEK):
            spec = windows.get(SESSION if key == RANGE_SESSION else WEEKLY)
            return (spec.start, now) if spec else None
        days = {RANGE_TODAY: 1, RANGE_7D: 7, RANGE_30D: 30}.get(key)
        if days is None:
            return None
        start, _ = local_day_bounds(local_date(now) - timedelta(days=days - 1))
        return start, now

    def _range_usage(
        self, key: str, now: datetime, windows: dict[str, WindowSpec]
    ) -> list[ModelUsage] | None:
        store = self._service.store
        if self._day_filter is None and key in (RANGE_SESSION, RANGE_WEEK):
            bounds = self._range_bounds(key, now, windows)
            return None if bounds is None else store.usage_by_model(self._provider, *bounds)
        if self._day_filter is not None:
            first = last = self._day_filter
        else:
            days = {RANGE_TODAY: 1, RANGE_7D: 7, RANGE_30D: 30}.get(key, 1)
            last = local_date(now)
            first = last - timedelta(days=days - 1)
        # Daily totals outlive raw rows, so longer ranges keep working.
        daily = store.daily_totals(self._provider, first, last)
        return [row for rows in daily.values() for row in rows]

    def _refresh_models(
        self, now: datetime, windows: dict[str, WindowSpec], available: bool
    ) -> None:
        table = self.model_table
        table.clearSpans()
        table.setRowCount(0)
        key = self.range_combo.currentData()
        usage = self._range_usage(key, now, windows) if available else None
        share_column = _COLUMN_INDEX["share"]
        if usage is None:
            _placeholder(table, "Nothing to show for this range yet.")
            _fit(table, {share_column: 170})
            return
        summary = summarize_costs(usage, self._rates)
        if not summary.rows:
            _placeholder(table, "No usage in this range.")
            _fit(table, {share_column: 170})
            return
        sidechain: dict[str, int] = {}
        if self._provider == CLAUDE:
            bounds = self._range_bounds(key, now, windows)
            if bounds is not None:
                sidechain = self._service.store.sidechain_output(*bounds)
        table.setRowCount(len(summary.rows) + 1)
        for row, model_row in enumerate(summary.rows):
            tokens = model_row.tokens
            color = self._color(model_row.model)
            side = sidechain.get(model_row.model)
            cost_text = (
                format_cost(model_row.cost)
                if model_row.cost is not None else "price unavailable"
            )
            priced_cost = None if model_row.has_unpriced else model_row.cost
            average_rate = average_cost_per_million(priced_cost, tokens)
            values = {
                "model": (f"● {display_model_name(model_row.model)}", False, color),
                "volume": (format_tokens(token_volume(tokens)), True, None),
                "average_rate": (
                    format_cost(average_rate) if average_rate is not None else "price unavailable",
                    True, TEXT if average_rate is not None else DIM,
                ),
                "cost": (cost_text, True, TEXT if model_row.cost is not None else DIM),
                "output": (format_tokens(tokens.output), True, None),
                "messages": (f"{model_row.messages:,}", True, None),
                "input": (format_tokens(tokens.input), True, None),
                "cache_read": (format_tokens(tokens.cache_read), True, None),
                "cache_write": (format_tokens(tokens.cache_write_5m + tokens.cache_write_1h), True, None),
                "reasoning": (format_tokens(tokens.reasoning), True, None),
                "sidechain": (
                    f"{100 * side / tokens.output:.0f}%" if side is not None and tokens.output else "n/a",
                    True, None,
                ),
            }
            for col_key, (text, right, text_color) in values.items():
                item = _item(text, right=right, color=text_color)
                if (
                    col_key == "model"
                    and display_model_name(model_row.model) != model_row.model
                ):
                    item.setToolTip(model_row.model)
                elif col_key == "volume":
                    exclusion = unpriced_note(summary) if model_row.has_unpriced else ""
                    item.setToolTip(
                        token_volume_tooltip(
                            tokens,
                            cost=None if model_row.has_unpriced else model_row.cost,
                            exclusion=exclusion,
                        )
                    )
                elif col_key == "average_rate":
                    exclusion = unpriced_note(summary) if model_row.has_unpriced else ""
                    item.setToolTip(
                        average_rate_tooltip(
                            tokens, cost=priced_cost, exclusion=exclusion
                        )
                    )
                table.setItem(row, _COLUMN_INDEX[col_key], item)
            share = summary.share(model_row)
            if share is None:
                table.setItem(row, share_column, _item("n/a", right=False, color=DIM))
            else:
                table.setCellWidget(row, share_column, _Bar([(share, color)], f"{share * 100:.0f}%"))
        total = len(summary.rows)
        tokens = summary.tokens
        total_priced_cost = None if summary.has_unpriced else summary.priced_cost
        total_average_rate = average_cost_per_million(total_priced_cost, tokens)
        totals = {
            "model": ("Total", False),
            "volume": (format_tokens(token_volume(tokens)), True),
            "average_rate": (
                format_cost(total_average_rate)
                if total_average_rate is not None else "price unavailable",
                True,
            ),
            "cost": (format_total_cost(summary), True),
            "output": (format_tokens(tokens.output), True),
            "messages": (f"{summary.messages:,}", True),
            "input": (format_tokens(tokens.input), True),
            "cache_read": (format_tokens(tokens.cache_read), True),
            "cache_write": (format_tokens(tokens.cache_write_5m + tokens.cache_write_1h), True),
            "reasoning": (format_tokens(tokens.reasoning), True),
        }
        for col_key, (text, right) in totals.items():
            item = _item(text, right=right, bold=True)
            if col_key == "cost" and summary.has_unpriced:
                item.setToolTip(unpriced_note(summary))
            elif col_key == "volume":
                exclusion = unpriced_note(summary) if summary.has_unpriced else ""
                item.setToolTip(
                    token_volume_tooltip(
                        tokens,
                        cost=None if summary.has_unpriced else summary.priced_cost,
                        exclusion=exclusion,
                    )
                )
            elif col_key == "average_rate":
                exclusion = unpriced_note(summary) if summary.has_unpriced else ""
                item.setToolTip(
                    average_rate_tooltip(
                        tokens, cost=total_priced_cost, exclusion=exclusion
                    )
                )
            table.setItem(total, _COLUMN_INDEX[col_key], item)
        self._on_token_details(self.token_details_cb.isChecked(), refit=False)
        _fit(table, {share_column: 170})

    def _refresh_daily(self, now: datetime, available: bool) -> None:
        table = self.daily_table
        table.clearSpans()
        table.setRowCount(0)
        self._daily_days = []
        self.day_legend.setText("")
        if not available:
            _placeholder(table, "Nothing to show yet.")
            _fit(table, {3: 300})
            return
        today = local_date(now)
        daily = self._service.store.daily_totals(
            self._provider, today - timedelta(days=DAILY_DAYS - 1), today
        )
        if not daily:
            _placeholder(table, "No daily usage yet.")
            _fit(table, {3: 300})
            return
        summaries = {day: summarize_costs(rows, self._rates) for day, rows in daily.items()}
        models = sorted(
            {r.model for s in summaries.values() for r in s.rows},
            key=lambda m: list(self._model_colors).index(m) if m in self._model_colors else 99,
        )
        self.day_legend.setText(
            "&nbsp;&nbsp;&nbsp;".join(
                f'<span style="color:{self._color(m)}">●</span> {display_model_name(m)}'
                for m in models
            )
        )
        friendly_models = [
            f"{display_model_name(model)} — {model}"
            for model in models
            if display_model_name(model) != model
        ]
        self.day_legend.setToolTip("\n".join(friendly_models))
        peak = max((s.priced_cost for s in summaries.values()), default=0.0) or 1.0
        days = sorted(summaries, reverse=True)
        table.setRowCount(len(days))
        for row, day in enumerate(days):
            summary = summaries[day]
            self._daily_days.append(day)
            table.setItem(row, 0, _item(day.strftime("%a %b %d"), right=False))
            cost_item = _item(format_total_cost(summary))
            if summary.has_unpriced:
                cost_item.setToolTip(unpriced_note(summary))
            table.setItem(row, 1, cost_item)
            table.setItem(row, 2, _item(f"{summary.messages:,}"))
            bar = _Bar(
                [((r.cost or 0.0) / peak, self._color(r.model)) for r in summary.rows if r.cost]
            )
            bar.setToolTip(
                "\n".join(
                    f"{display_model_name(r.model)}: {format_cost(r.cost)}"
                    for r in summary.rows
                )
            )
            table.setCellWidget(row, 3, bar)
        _fit(table, {3: 300})

    def limit_change_dates(self) -> list[date]:
        settings = getattr(self._service.config.local_usage, self._provider)
        out = []
        for value in settings.limit_changes:
            try:
                out.append(date.fromisoformat(value))
            except ValueError:
                continue
        return sorted(out)

    def set_limit_changes(self, dates: list[date]) -> None:
        settings = getattr(self._service.config.local_usage, self._provider)
        settings.limit_changes = [day.isoformat() for day in sorted(set(dates))]
        try:
            self._service.config.save()
        except OSError:
            pass
        self.refresh()

    def _edit_limit_changes(self) -> None:
        dialog = LimitChangesDialog(self.limit_change_dates(), self._display_name, self)
        if dialog.exec():
            self.set_limit_changes(dialog.dates())

    def trend_report(self) -> TrendReport:
        metric = self._trend_metric
        return build_trend(
            load_summaries(self._service.store, self._account_id, metric),
            metric,
            self._rates,
            [local_day_bounds(day)[0] for day in self.limit_change_dates()],
            recent_windows=TREND_RECENT.get(metric, 1),
            baseline_windows=TREND_BASELINE.get(metric, 8),
        )

    def trend_text(self) -> str:
        parts = (self.trend_headline_label.text(), self.trend_detail_label.text())
        return "\n".join(part for part in parts if part)

    def set_trend_metric(self, metric: str) -> None:
        self._trend_metric = metric
        self._trend_show_all = False
        for key, button in self.trend_metric_buttons.items():
            button.setChecked(key == metric)
        self.refresh()

    def _toggle_trend_show_all(self) -> None:
        self._trend_show_all = not self._trend_show_all
        self.refresh()

    def _toggle_trend_skipped(self) -> None:
        self._trend_skipped_open = not self._trend_skipped_open
        self.refresh()

    def _set_compare_change(self, enabled: bool) -> None:
        self._trend_compare_change = enabled
        self.refresh()

    def _refresh_trend(self, available: bool) -> None:
        metric = self._trend_metric
        _unit, units = TREND_UNITS.get(metric, ("window", "windows"))
        table = self.trend_table
        skipped_table = self.trend_skipped_table
        for each in (table, skipped_table):
            each.clearSpans()
            each.setRowCount(0)
        for column in range(TREND_BASIC_COLUMNS, len(TREND_COLUMNS)):
            table.setColumnHidden(column, not self.trend_details_cb.isChecked())

        if not available:
            self._trend_compare_available = False
            self._trend_compare_change = False
            self.compare_change_btn.blockSignals(True)
            self.compare_change_btn.setChecked(False)
            self.compare_change_btn.blockSignals(False)
            self.compare_change_btn.setHidden(True)
            self.trend_comparison.set_data([])
            self.trend_answer.setHidden(True)
            self.trend_headline_label.setText("Nothing to summarize yet.")
            self.trend_headline_label.setHidden(False)
            self.trend_detail_label.setText("")
            self.trend_detail_label.setHidden(True)
            self.trend_warning_label.setHidden(True)
            self.trend_chart.set_data([], None, [], format_cost, "")
            table.setColumnHidden(5, True)
            _placeholder(table, f"No compared {units} yet.")
            _fit(table)
            self.trend_show_all_btn.setHidden(True)
            self.trend_skipped_btn.setHidden(True)
            skipped_table.setHidden(True)
            return

        report = self.trend_report()
        lines = trend_summary_lines(report, metric)
        self.trend_headline_label.setText(trend_recent_text(report, metric))
        self.trend_headline_label.setToolTip(TREND_DEFINITION)
        self.trend_detail_label.setText("\n".join(lines[1:]))
        metrics = []
        if (
            report.dollars_change is not None
            and report.dollars_baseline.average is not None
            and report.recent_dollars_per_point is not None
        ):
            metrics.append(
                ComparisonMetric(
                    "API-equiv. / 1%",
                    report.dollars_baseline.average,
                    report.recent_dollars_per_point,
                    format_cost(report.dollars_baseline.average),
                    format_cost(report.recent_dollars_per_point),
                    report.dollars_change,
                )
            )
        if (
            report.output_change is not None
            and report.output_baseline.average is not None
            and report.recent_output_per_point is not None
        ):
            metrics.append(
                ComparisonMetric(
                    "Output / 1%",
                    report.output_baseline.average,
                    report.recent_output_per_point,
                    format_tokens(report.output_baseline.average),
                    format_tokens(report.recent_output_per_point),
                    report.output_change,
                )
            )
        recent_count = len(report.recent_rows)
        baseline_period = (
            f"the {report.dollars_baseline.windows or report.output_baseline.windows} windows "
            "before the marked change"
            if report.baseline_before_change
            else "the earlier baseline windows"
        )
        comparison_tip = (
            f"Ballpark comparison. Recent pools the last {recent_count} {units}; the reference "
            f"pools {baseline_period}. Each average is total usage divided by total percentage used. "
            "Model choice and cache use can move these values."
        )
        self.trend_comparison.set_data(
            metrics,
            comparison_tip,
            reference_label="Before" if report.baseline_before_change else "Earlier",
        )
        self._trend_compare_available = bool(metrics) and report.baseline_before_change
        if not self._trend_compare_available:
            self._trend_compare_change = False
        self.compare_change_btn.blockSignals(True)
        self.compare_change_btn.setChecked(self._trend_compare_change)
        self.compare_change_btn.blockSignals(False)
        if report.limit_change:
            self.compare_change_btn.setText(
                f"Compare {report.limit_change.astimezone():%b %d}"
            )
        self.compare_change_btn.setHidden(
            self._view != VIEW_TREND or not self._trend_compare_available
        )
        show_compare = self._trend_compare_available and self._trend_compare_change
        table.setColumnHidden(5, not show_compare)
        self.trend_answer.setHidden(not show_compare)
        self.trend_headline_label.setHidden(show_compare)
        self.trend_detail_label.setHidden(True)
        warnings = mix_warnings(report)
        mixed = (
            report.dollars_change is not None
            and report.output_change is not None
            and report.dollars_change * report.output_change < 0
        )
        if mixed:
            self.trend_warning_label.setText("Mixed signal ⓘ")
            warning_tip = (
                "API-equivalent value and output moved in opposite directions. This usually "
                "means the model or cache mix changed, so treat the percentages as ballpark."
            )
        elif warnings:
            self.trend_warning_label.setText("Usage mix changed ⓘ")
            warning_tip = "\n".join(warnings)
        else:
            self.trend_warning_label.setText("")
            warning_tip = ""
        if warnings and mixed:
            warning_tip += "\n\n" + "\n".join(warnings)
        self.trend_warning_label.setToolTip(warning_tip)
        self.trend_warning_label.setHidden(not (mixed or warnings))

        counted = [row for row in report.rows if row.counted]
        context_rows = [row for row in report.rows if row.reason in TREND_CONTEXT_REASONS]
        display_rows = [
            row for row in report.rows
            if row.counted or row.reason in TREND_CONTEXT_REASONS
        ]
        changes = [local_day_bounds(day)[0] for day in self.limit_change_dates()]
        if show_compare and report.current_segment > 0:
            current_rows = [row for row in counted if row.segment == report.current_segment]
            previous_rows = [row for row in counted if row.segment == report.current_segment - 1]
            previous_count = min(len(previous_rows), TREND_CHART_POINTS // 2)
            chart_rows = (
                current_rows[: TREND_CHART_POINTS - previous_count]
                + previous_rows[:previous_count]
            )
            for row in context_rows:
                if row.segment in (report.current_segment, report.current_segment - 1):
                    chart_rows.append(row)
        else:
            chart_rows = display_rows[:TREND_CHART_POINTS]
        chart_rows = sorted(chart_rows, key=lambda row: row.summary.resets_at)
        rolling_window = TREND_ROLLING.get(metric, 5)
        unit, _units = TREND_UNITS.get(metric, ("window", "windows"))
        title_suffix = f"{rolling_window}-{unit} rolling average"

        current_chart_rows = [
            row for row in display_rows if row.segment == report.current_segment
        ]
        if any(row.dollars_per_point is not None for row in current_chart_rows):
            points = [
                (
                    r.summary.resets_at,
                    r.dollars_per_point,
                    trend_window_label(r),
                    r.segment,
                )
                for r in chart_rows
                if r.dollars_per_point is not None
            ]
            rolling = rolling_average_points(chart_rows, rolling_window, dollars=True)
            baseline_average = report.dollars_baseline.average
            current_average = report.recent_dollars_per_point
            baseline_line_rows = [
                row for row in report.baseline_rows if row.dollars_per_point is not None
            ]
            recent_line_rows = [
                row for row in report.recent_rows if row.dollars_per_point is not None
            ]
            formatter = format_cost
            title = f"Cost per 1% · {title_suffix}"
        else:
            points = [
                (
                    r.summary.resets_at,
                    r.output_per_point,
                    trend_window_label(r),
                    r.segment,
                )
                for r in chart_rows
                if r.output_per_point is not None
            ]
            rolling = rolling_average_points(chart_rows, rolling_window, dollars=False)
            baseline_average = report.output_baseline.average
            current_average = report.recent_output_per_point
            baseline_line_rows = [
                row for row in report.baseline_rows if row.output_per_point is not None
            ]
            recent_line_rows = [
                row for row in report.recent_rows if row.output_per_point is not None
            ]
            formatter = format_tokens
            title = f"Output per 1% · {title_suffix}"

        average_lines = []
        if show_compare and baseline_average is not None and current_average is not None and points:
            def line_span(rows):
                starts = [row.summary.resets_at for row in rows]
                if not starts:
                    return None
                start, end = min(starts), max(starts)
                if start == end:
                    start = rows[0].summary.window_start
                return start, end

            baseline_span = line_span(baseline_line_rows)
            recent_span = line_span(recent_line_rows)
            baseline_label = "before avg" if report.baseline_before_change else "earlier avg"
            if baseline_span:
                average_lines.append(
                    (*baseline_span, baseline_average, baseline_label, MUTED)
                )
            if recent_span:
                average_lines.append(
                    (*recent_span, current_average, "recent avg", "#60a5fa")
                )
        self.trend_chart.set_data(
            points,
            None,
            changes,
            formatter,
            title,
            rolling=rolling,
            average_lines=average_lines,
            current_segment=report.current_segment,
            focus_trend=not show_compare,
        )

        shown = display_rows if self._trend_show_all else display_rows[:TREND_RECENT_ROWS]
        typical_cost = report.dollars_baseline.average
        typical_output = report.output_baseline.average
        if not shown:
            _placeholder(table, f"No compared {units} yet.")
        else:
            table.setRowCount(len(shown))
            for index, row in enumerate(shown):
                summary = row.summary
                if row.counted and row.dollars_per_point is not None and typical_cost:
                    versus = _signed_percent(row.dollars_per_point / typical_cost - 1)
                elif row.counted and row.output_per_point is not None and typical_output:
                    versus = _signed_percent(row.output_per_point / typical_output - 1)
                else:
                    versus = "n/a"
                cost_per_point = (
                    format_cost(row.dollars_per_point) + (" *" if row.dollars_note else "")
                    if row.dollars_per_point is not None else "n/a"
                )
                cells = (
                    trend_window_label(row),
                    f"{row.pct:.0f}%" if row.pct is not None else "n/a",
                    format_total_cost(row.cost),
                    cost_per_point,
                    format_tokens(row.output_per_point) if row.output_per_point is not None else "n/a",
                    versus,
                    f"{100 * row.cache_share:.0f}%" if row.cache_share is not None else "n/a",
                    display_model_name(row.top_model) if row.top_model else "n/a",
                    "from history" if summary.origin == ORIGIN_BACKFILL else "tracked",
                )
                for column, text in enumerate(cells):
                    item = _item(text, right=column not in (0, 7, 8))
                    context_tip = trend_context_tooltip(row)
                    if column == 0 and context_tip:
                        item.setToolTip(context_tip)
                    elif column == 2 and row.cost.has_unpriced:
                        item.setToolTip(unpriced_note(row.cost))
                    elif column == 3:
                        if row.dollars_note:
                            item.setToolTip(f"* {row.dollars_note}")
                        elif row.dollars_reason != COUNTED:
                            item.setToolTip(row.dollars_reason)
                        elif context_tip:
                            item.setToolTip(context_tip)
                    elif column == 4 and context_tip:
                        item.setToolTip(context_tip)
                    elif (
                        column == 7
                        and row.top_model
                        and display_model_name(row.top_model) != row.top_model
                    ):
                        item.setToolTip(row.top_model)
                    table.setItem(index, column, item)
        _fit(table)
        self.trend_show_all_btn.setHidden(len(display_rows) <= TREND_RECENT_ROWS)
        self.trend_show_all_btn.setText(
            f"Show recent {TREND_RECENT_ROWS}"
            if self._trend_show_all else f"Show all {len(display_rows)}"
        )

        skipped = [
            row for row in report.rows
            if not row.counted and row.reason not in TREND_CONTEXT_REASONS
        ]
        self.trend_skipped_btn.setHidden(not skipped)
        if skipped:
            counts = Counter(SHORT_REASONS.get(row.reason, row.reason) for row in skipped)
            arrow = "▾" if self._trend_skipped_open else "▸"
            noun = "window" if len(skipped) == 1 else "windows"
            reasons = " · ".join(f"{label} {count}" for label, count in counts.most_common())
            self.trend_skipped_btn.setText(f"{arrow} {len(skipped)} {noun} not compared ({reasons})")
            skipped_table.setRowCount(len(skipped))
            for index, row in enumerate(skipped):
                summary = row.summary
                skipped_table.setItem(index, 0, _item(window_label(summary.window_start, summary.resets_at), right=False))
                skipped_table.setItem(index, 1, _item(f"{row.pct:.0f}%" if row.pct is not None else "n/a"))
                skipped_table.setItem(index, 2, _item(row.reason, right=False, color=MUTED))
            _fit(skipped_table)
        skipped_table.setHidden(not (skipped and self._trend_skipped_open))

    # ---- events ----

    def _on_token_details(self, show: bool, refit: bool = True) -> None:
        for index, (_key, _title, details) in enumerate(MODEL_COLUMNS):
            self.model_table.setColumnHidden(index, details and not show)
        if refit:
            _fit(self.model_table, {_COLUMN_INDEX["share"]: 170})

    def _on_range_changed(self, _index: int) -> None:
        self._day_filter = None
        key = self.range_combo.currentData()
        if isinstance(key, str):
            self._remember_display_choice("details_range", key)
        self.refresh()

    def _on_day_clicked(self, row: int, _column: int) -> None:
        if row >= len(self._daily_days):
            return
        self._day_filter = self._daily_days[row]
        self.day_chip.setText(f"{self._day_filter:%b %d}  ✕")
        self.show_view(VIEW_MODEL)
        self.refresh()

    def _clear_day_filter(self) -> None:
        self._day_filter = None
        self.refresh()

    def _remember_display_choice(self, name: str, value: str) -> None:
        settings = self._service.config.local_usage
        if getattr(settings, name) == value:
            return
        setattr(settings, name, value)
        try:
            self._service.config.save()
        except OSError:
            pass

    def _sync_import_status(self, running: bool) -> None:
        self.cancel_btn.setHidden(not running)
        if not running:
            self.importing_label.setText("")
            return
        done, total = self._service.progress()
        percent = f" {100 * done / total:.0f}%" if total else ""
        self.importing_label.setText(f"Importing{percent}")

    def _on_import_started(self, _kind: str) -> None:
        self._sync_import_status(True)

    def _on_progress(self, _done: int, _total: int) -> None:
        self._sync_import_status(self._service.is_running())

    def _on_import_finished(self, _results) -> None:
        self.refresh()

    def _show_disclosure(self) -> None:
        QMessageBox.information(self, "About local usage", "\n\n".join(SETTINGS_DISCLOSURE))
