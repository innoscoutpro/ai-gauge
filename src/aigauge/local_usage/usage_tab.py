"""The "Usage and cost" tab of the details dialog.

Top to bottom: a card for each current window (session, week, and a
model-specific limit such as Fable), one view at a time (by model, by day or
trend), and a footer that stays in view. The tab scrolls as a single area;
tables grow to fit their rows instead of scrolling inside it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
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
    UNPRICED_TOLERANCE,
    TrendReport,
    build_trend,
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

# key, header, shown only with "Token details"
MODEL_COLUMNS = (
    ("model", "Model", False),
    ("cost", "Est. cost", False),
    ("share", "Share of cost", False),
    ("output", "Output", False),
    ("output_share", "Share of output", False),
    ("messages", "Msgs", False),
    ("input", "Input", True),
    ("cache_read", "Cache read", True),
    ("cache_write", "Cache write", True),
    ("reasoning", "Reasoning", True),
    ("sidechain", "Subagent %", True),
)
_COLUMN_INDEX = {key: i for i, (key, _title, _details) in enumerate(MODEL_COLUMNS)}
TREND_COLUMNS = (
    "Window", "Est. cost", "Quota", "Cost/1%", "Output/1%",
    "Cache share", "Top model", "Source", "Status",
)

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


def _signed_percent(change: float) -> str:
    return f"{change * 100:+.0f}%"


def trend_summary_text(report: TrendReport) -> str:
    """The comparison line above the trend table."""
    current = report.current
    since = (
        f"Baseline restarted after the limit change on {report.limit_change.astimezone():%b %d}. "
        if report.limit_change else ""
    )
    if current is None:
        return since + "No completed windows to compare yet."
    if report.collecting:
        return since + (
            f"Needs {BASELINE_MIN_WINDOWS} earlier completed windows to compare; "
            f"{report.output_baseline.windows} so far."
        )
    lines = []
    dollars = report.dollars_baseline
    if report.dollars_change is not None and current.dollars_per_point is not None:
        lines.append(
            f"Cost per 1%: {format_cost(current.dollars_per_point)} in the latest window, "
            f"{_signed_percent(report.dollars_change)} vs the typical "
            f"{format_cost(dollars.median)} ({dollars.windows} earlier windows ranged "
            f"{format_cost(dollars.low)} to {format_cost(dollars.high)})."
        )
    output = report.output_baseline
    if report.output_change is not None and current.output_per_point is not None:
        lines.append(
            f"Output per 1%: {format_tokens(current.output_per_point)}, "
            f"{_signed_percent(report.output_change)} vs the typical "
            f"{format_tokens(output.median)}."
        )
    return "\n".join(lines) if lines else "Not enough priced windows to compare cost yet."


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


class _WindowCard(QFrame):
    def __init__(self, metric: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.metric = metric
        self.setObjectName("usage_card")
        self.setStyleSheet(
            "QFrame#usage_card { background:#111827; border:1px solid #374151; "
            "border-radius:6px; }"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
        self.labels["per_point"].setHidden(not per_point)


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
        self._view = VIEW_MODEL
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
        self.show_view(VIEW_MODEL)
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
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        bar.addWidget(self.range_combo)
        self.trend_metric_combo = QComboBox()
        self.trend_metric_combo.setObjectName("usage_trend_metric")
        self.trend_metric_combo.addItem("Session windows", SESSION)
        self.trend_metric_combo.addItem("Weekly windows", WEEKLY)
        self.trend_metric_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.trend_metric_combo)
        bar.addSpacing(6)
        self.limit_changes_btn = QPushButton("Limits changed…")
        self.limit_changes_btn.setObjectName("usage_limit_changes_btn")
        self.limit_changes_btn.setToolTip(
            "Mark when the provider changed its limits, so the trend starts a fresh baseline"
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
        layout.addWidget(self.model_table)
        row = QHBoxLayout()
        row.addStretch(1)
        self.token_details_cb = QCheckBox("Token details")
        self.token_details_cb.setToolTip("Show input, cache, reasoning and subagent columns")
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
        layout.setSpacing(6)
        self.trend_summary_label = _label("", TEXT, 12)
        self.trend_summary_label.setObjectName("usage_trend_summary")
        self.trend_summary_label.setWordWrap(True)
        layout.addWidget(self.trend_summary_label)
        self.trend_table = _table(TREND_COLUMNS)
        self.trend_table.setObjectName("usage_trend_table")
        layout.addWidget(self.trend_table)
        note = _label(
            "A change in cache use or model mix moves cost per 1% even when the "
            "allowance hasn't changed; check those columns before reading into a change.",
            DIM, 10,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
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
        self._view = key
        self.view_buttons[key].setChecked(True)
        for page_key, page in self.pages.items():
            page.setHidden(page_key != key)
        self.range_combo.setHidden(key != VIEW_MODEL)
        self.day_chip.setHidden(key != VIEW_MODEL or self._day_filter is None)
        self.trend_metric_combo.setHidden(key != VIEW_TREND)
        self.limit_changes_btn.setHidden(key != VIEW_TREND)

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
        windows = current_windows(provider, self._snapshot, store, now) if available else {}
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
                return "Some models have no price"
            return f"≈ {format_cost(at_reading.priced_cost / pct)} per 1% (priced models only)"
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
                format_cost(model_row.cost) + (" + unpriced" if model_row.has_unpriced else "")
                if model_row.cost is not None else "no price"
            )
            values = {
                "model": (f"● {model_row.model}", False, color),
                "cost": (cost_text, True, TEXT if model_row.cost is not None else DIM),
                "output": (format_tokens(tokens.output), True, None),
                "output_share": (
                    f"{100 * tokens.output / summary.tokens.output:.0f}%"
                    if summary.tokens.output else "n/a",
                    True, None,
                ),
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
                table.setItem(row, _COLUMN_INDEX[col_key], _item(text, right=right, color=text_color))
            share = summary.share(model_row)
            if share is None:
                table.setItem(row, share_column, _item("n/a", right=False, color=DIM))
            else:
                table.setCellWidget(row, share_column, _Bar([(share, color)], f"{share * 100:.0f}%"))
        total = len(summary.rows)
        tokens = summary.tokens
        totals = {
            "model": ("Total", False),
            "cost": (format_total_cost(summary), True),
            "output": (format_tokens(tokens.output), True),
            "messages": (f"{summary.messages:,}", True),
            "input": (format_tokens(tokens.input), True),
            "cache_read": (format_tokens(tokens.cache_read), True),
            "cache_write": (format_tokens(tokens.cache_write_5m + tokens.cache_write_1h), True),
            "reasoning": (format_tokens(tokens.reasoning), True),
        }
        for col_key, (text, right) in totals.items():
            table.setItem(total, _COLUMN_INDEX[col_key], _item(text, right=right, bold=True))
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
                f'<span style="color:{self._color(m)}">●</span> {m}' for m in models
            )
        )
        peak = max((s.priced_cost for s in summaries.values()), default=0.0) or 1.0
        days = sorted(summaries, reverse=True)
        table.setRowCount(len(days))
        for row, day in enumerate(days):
            summary = summaries[day]
            self._daily_days.append(day)
            table.setItem(row, 0, _item(day.strftime("%a %b %d"), right=False))
            table.setItem(row, 1, _item(format_total_cost(summary)))
            table.setItem(row, 2, _item(f"{summary.messages:,}"))
            bar = _Bar(
                [((r.cost or 0.0) / peak, self._color(r.model)) for r in summary.rows if r.cost]
            )
            bar.setToolTip("\n".join(f"{r.model}: {format_cost(r.cost)}" for r in summary.rows))
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
        metric = self.trend_metric_combo.currentData()
        return build_trend(
            load_summaries(self._service.store, self._account_id, metric),
            metric,
            self._rates,
            [local_day_bounds(day)[0] for day in self.limit_change_dates()],
        )

    def _refresh_trend(self, available: bool) -> None:
        table = self.trend_table
        table.clearSpans()
        table.setRowCount(0)
        if not available:
            self.trend_summary_label.setText("Nothing to compare yet.")
            _placeholder(table, "No completed windows yet.")
            _fit(table)
            return
        report = self.trend_report()
        self.trend_summary_label.setText(trend_summary_text(report))
        if not report.rows:
            _placeholder(table, "No completed windows yet.")
            _fit(table)
            return
        table.setRowCount(len(report.rows))
        for row_index, row in enumerate(report.rows):
            summary = row.summary
            status = "included" if row.counted else f"skipped: {row.reason}"
            if row.counted and row.dollars_reason != COUNTED:
                status = f"included; cost not compared, {row.dollars_reason}"
            elif row.counted and row.dollars_note:
                status = f"included; {row.dollars_note}"
            cells = (
                window_label(summary.window_start, summary.resets_at),
                format_total_cost(row.cost),
                f"{row.pct:.0f}%" if row.pct is not None else "n/a",
                format_cost(row.dollars_per_point) if row.dollars_per_point is not None else "n/a",
                format_tokens(row.output_per_point) if row.output_per_point is not None else "n/a",
                f"{100 * row.cache_share:.0f}%" if row.cache_share is not None else "n/a",
                row.top_model or "n/a",
                "from history" if summary.origin == ORIGIN_BACKFILL else "tracked",
                status,
            )
            for col, text in enumerate(cells):
                table.setItem(
                    row_index, col,
                    _item(text, right=col in (1, 2, 3, 4, 5), color=None if row.counted else DIM),
                )
        _fit(table)

    # ---- events ----

    def _on_token_details(self, show: bool, refit: bool = True) -> None:
        for index, (_key, _title, details) in enumerate(MODEL_COLUMNS):
            self.model_table.setColumnHidden(index, details and not show)
        if refit:
            _fit(self.model_table, {_COLUMN_INDEX["share"]: 170})

    def _on_range_changed(self, _index: int) -> None:
        self._day_filter = None
        self.refresh()

    def _on_day_clicked(self, row: int, _column: int) -> None:
        if row >= len(self._daily_days):
            return
        self._day_filter = self._daily_days[row]
        self.day_chip.setText(f"{self._day_filter:%b %d}  ✕")
        self._view = VIEW_MODEL
        self.refresh()

    def _clear_day_filter(self) -> None:
        self._day_filter = None
        self.refresh()

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
