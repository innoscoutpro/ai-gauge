"""The "Usage and cost" tab of the details dialog."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import UsageSnapshot
from ..ratio import MIN_COUNTABLE_PCT
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
from .trend import BASELINE_MIN_WINDOWS, COUNTED, TrendReport, build_trend
from .windows import SESSION, WEEKLY, WindowSpec, current_windows

RANGE_SESSION = "session"
RANGE_WEEK = "week"
RANGE_TODAY = "today"
RANGE_7D = "7d"
RANGE_30D = "30d"
RANGE_DAY = "day"
RANGES = (
    (RANGE_SESSION, "Current session"),
    (RANGE_WEEK, "Current week"),
    (RANGE_TODAY, "Today"),
    (RANGE_7D, "Last 7 days"),
    (RANGE_30D, "Last 30 days"),
)
DAILY_DAYS = 30
UNAVAILABLE = "unavailable"
FOOTER_TEXT = "This computer's logs only · API-equivalent estimate"

MODEL_COLORS = (
    "#60a5fa", "#f59e0b", "#34d399", "#f472b6",
    "#a78bfa", "#f87171", "#22d3ee", "#a3e635",
)

MODEL_COLUMNS = (
    ("model", "Model", False),
    ("messages", "Msgs", False),
    ("input", "Input", False),
    ("output", "Output", False),
    ("cache_read", "Cache rd", False),
    ("cache_write", "Cache wr", False),
    ("cache_write_5m", "Cache 5m", True),
    ("cache_write_1h", "Cache 1h", True),
    ("reasoning", "Reasoning", True),
    ("sidechain", "Subagent out", True),
    ("cost", "Est. cost", False),
    ("share", "Share", False),
)
_COLUMN_INDEX = {key: i for i, (key, _title, _extra) in enumerate(MODEL_COLUMNS)}


def format_tokens(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    if value < 1000:
        return f"{value:.0f}"
    if value < 10_000:
        return f"{value / 1000:.1f}K"
    if value < 1_000_000:
        return f"{value / 1000:.0f}K"
    return f"{value / 1_000_000:.1f}M"


def format_time_left(resets_at: datetime, now: datetime) -> str:
    seconds = int((resets_at - now).total_seconds())
    if seconds <= 0:
        return "resetting"
    if seconds >= 86400:
        return f"{seconds // 86400}d left"
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h {rem // 60}m left" if hours else f"{rem // 60}m left"


def share_bar(fraction: float | None, width: int = 10) -> str:
    if fraction is None:
        return "n/a"
    eighths = round(max(0.0, min(1.0, fraction)) * width * 8)
    full, part = divmod(eighths, 8)
    bar = "█" * full + ("▏▎▍▌▋▊▉"[part - 1] if part else "")
    return f"{fraction * 100:.0f}% {bar}"


def window_label(start: datetime, end: datetime) -> str:
    start_local, end_local = start.astimezone(), end.astimezone()
    if (end - start) <= timedelta(hours=12):
        return f"{start_local:%b %d %H:%M} to {end_local:%H:%M}"
    return f"{start_local:%b %d} to {end_local:%b %d}"


def _signed_percent(change: float) -> str:
    return f"{change * 100:+.0f}%"


def trend_summary_text(report: TrendReport) -> str:
    """The baseline and change line above the trend table."""
    current = report.current
    if current is None:
        return "No comparable completed windows yet."
    if report.collecting:
        return f"Collecting windows ({report.output_baseline.windows} of {BASELINE_MIN_WINDOWS})."
    parts = []
    dollars = report.dollars_baseline
    if report.dollars_change is not None and current.dollars_per_point is not None:
        parts.append(
            f"$/pt {format_cost(current.dollars_per_point)} vs median "
            f"{format_cost(dollars.median)} of {dollars.windows} earlier windows "
            f"(range {format_cost(dollars.low)} to {format_cost(dollars.high)}): "
            f"{_signed_percent(report.dollars_change)}"
        )
    output = report.output_baseline
    if report.output_change is not None and current.output_per_point is not None:
        parts.append(
            f"output tok/pt {format_tokens(current.output_per_point)} vs median "
            f"{format_tokens(output.median)} of {output.windows} earlier windows: "
            f"{_signed_percent(report.output_change)}"
        )
    pooled = []
    if report.pooled_dollars_per_point is not None:
        pooled.append(format_cost(report.pooled_dollars_per_point) + "/pt")
    if report.pooled_output_per_point is not None:
        pooled.append(format_tokens(report.pooled_output_per_point) + " output tok/pt")
    text = "Latest window: " + "; ".join(parts) if parts else "Latest window counted."
    if pooled:
        text += "\nAll counted windows: " + " · ".join(pooled)
    return text


class _StackedBar(QWidget):
    def __init__(self, segments: list[tuple[float, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self._segments = segments
        self.setMinimumWidth(80)
        self.setFixedHeight(14)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        x = rect.left()
        for fraction, color in self._segments:
            width = rect.width() * max(0.0, fraction)
            if width <= 0:
                continue
            painter.fillRect(QRectF(x, rect.top(), width, rect.height()), QColor(color))
            x += width
        painter.end()


def _item(text: str, align_right: bool = True, muted: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if muted:
        item.setForeground(QColor("#6b7280"))
    return item


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(False)
    table.setShowGrid(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return table


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
        self._provider = service.provider_for_account(account_id) or CLAUDE
        self._snapshot = snapshot
        self._rates = rate_table or load_rate_table()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._day_filter: date | None = None
        self._model_colors: dict[str, str] = {}
        self._connected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("<b>Usage and cost</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.updated_label = QLabel("")
        self.updated_label.setStyleSheet("color:#9ca3af; font-size:11px;")
        title_row.addWidget(self.updated_label)
        layout.addLayout(title_row)

        subtitle_row = QHBoxLayout()
        subtitle = QLabel(f"This computer · {display_name}")
        subtitle.setStyleSheet("color:#9ca3af; font-size:11px;")
        subtitle_row.addWidget(subtitle)
        subtitle_row.addStretch(1)
        self.importing_label = QLabel("")
        self.importing_label.setStyleSheet("color:#fbbf24; font-size:11px;")
        subtitle_row.addWidget(self.importing_label)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("✕")
        self.cancel_btn.setToolTip("Cancel import")
        self.cancel_btn.clicked.connect(self._service.cancel)
        subtitle_row.addWidget(self.cancel_btn)
        layout.addLayout(subtitle_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#fbbf24; font-size:11px;")
        layout.addWidget(self.status_label)

        layout.addLayout(self._build_window_grid())

        models_row = QHBoxLayout()
        models_title = QLabel("<b>Models</b>")
        models_title.setTextFormat(Qt.TextFormat.RichText)
        models_row.addWidget(models_title)
        self.range_combo = QComboBox()
        self.range_combo.setObjectName("usage_range_combo")
        for key, label in RANGES:
            self.range_combo.addItem(label, key)
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        models_row.addWidget(self.range_combo)
        models_row.addStretch(1)
        self.more_columns_cb = QCheckBox("More columns")
        self.more_columns_cb.toggled.connect(self._sync_columns)
        models_row.addWidget(self.more_columns_cb)
        layout.addLayout(models_row)

        self.model_table = _table([title for _key, title, _extra in MODEL_COLUMNS])
        self.model_table.setObjectName("usage_model_table")
        self.model_table.setMinimumHeight(140)
        layout.addWidget(self.model_table, 1)
        self._sync_columns()

        self.lower_tabs = QTabWidget()
        self.daily_table = _table(["Day", "Est. cost", "Msgs", "By model"])
        self.daily_table.setObjectName("usage_daily_table")
        self.daily_table.cellClicked.connect(self._on_day_clicked)
        self.daily_table.setMinimumHeight(160)
        self.lower_tabs.addTab(self.daily_table, "Daily")
        self.lower_tabs.addTab(self._build_trend_page(), "Allowance trend")
        layout.addWidget(self.lower_tabs, 1)

        footer = QHBoxLayout()
        footer_label = QLabel(FOOTER_TEXT)
        footer_label.setStyleSheet("color:#6b7280; font-size:10px;")
        footer.addWidget(footer_label)
        info = QToolButton()
        info.setText("i")
        info.setToolTip("About these numbers")
        info.clicked.connect(self._show_disclosure)
        footer.addWidget(info)
        footer.addStretch(1)
        layout.addLayout(footer)

        self._connect()
        self.refresh()

    # ---- building ----

    def _build_window_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(3)
        self._window_headers: dict[str, QLabel] = {}
        self._window_cells: dict[tuple[str, str], QLabel] = {}
        rows = (
            ("cost", "Est. API cost"),
            ("quota", "Quota used"),
            ("per_point", "$ per point"),
            ("output_per_point", "Output tok/pt"),
        )
        for col, metric in enumerate((SESSION, WEEKLY), start=1):
            header = QLabel(metric)
            header.setStyleSheet("color:#9ca3af; font-size:11px; font-weight:700;")
            grid.addWidget(header, 0, col)
            self._window_headers[metric] = header
        for row, (key, label) in enumerate(rows, start=1):
            name = QLabel(label)
            name.setStyleSheet("color:#9ca3af; font-size:11px;")
            grid.addWidget(name, row, 0)
            for col, metric in enumerate((SESSION, WEEKLY), start=1):
                cell = QLabel(UNAVAILABLE)
                cell.setObjectName(f"usage_{metric.lower()}_{key}")
                cell.setStyleSheet("color:#e5e7eb; font-size:12px; font-weight:600;")
                grid.addWidget(cell, row, col)
                self._window_cells[(metric, key)] = cell
        grid.setColumnStretch(3, 1)
        return grid

    def _build_trend_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Windows:"))
        self.trend_metric_combo = QComboBox()
        self.trend_metric_combo.setObjectName("usage_trend_metric")
        self.trend_metric_combo.addItem(SESSION, SESSION)
        self.trend_metric_combo.addItem(WEEKLY, WEEKLY)
        self.trend_metric_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.trend_metric_combo)
        row.addStretch(1)
        layout.addLayout(row)
        self.trend_summary_label = QLabel("")
        self.trend_summary_label.setObjectName("usage_trend_summary")
        self.trend_summary_label.setWordWrap(True)
        self.trend_summary_label.setStyleSheet("color:#e5e7eb; font-size:11px;")
        layout.addWidget(self.trend_summary_label)
        self.trend_table = _table(
            ["Window", "Est. cost", "Quota", "$/pt", "Output tok/pt",
             "Cache share", "Top model", "Origin", "Status"]
        )
        self.trend_table.setObjectName("usage_trend_table")
        self.trend_table.setMinimumHeight(160)
        layout.addWidget(self.trend_table, 1)
        note = QLabel(
            "Compares this computer's recorded activity with the account's usage "
            "percentage. A different cache or model mix moves $/pt without any change "
            "to the allowance, so check cache share and top model before reading "
            "anything into a change."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#6b7280; font-size:10px;")
        layout.addWidget(note)
        return page

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

    # ---- data ----

    def window_cell_text(self, metric: str, key: str) -> str:
        return self._window_cells[(metric, key)].text()

    def refresh(self) -> None:
        store = self._service.store
        now = self._now()
        provider = self._provider
        last_import = store.last_import(provider)
        running = self._service.is_running()
        self.updated_label.setText(
            "Not imported yet" if last_import is None else f"Updated {relative_time(last_import, now)}"
        )
        self._sync_import_status(running)

        recognized = store.get_meta(f"recognized:{provider}")
        notes = []
        if recognized is False:
            notes.append("Logs found, usage not recognized. Numbers are unavailable.")
        if running:
            notes.append("Importing: numbers are partial until it finishes.")
        if self._rates.errors:
            notes.append("Some prices could not be loaded; see the log for details.")
        self.status_label.setText(" ".join(notes))
        self.status_label.setVisible(bool(notes))

        available = last_import is not None and recognized is not False
        windows = current_windows(provider, self._snapshot, store, now) if available else {}
        for metric in (SESSION, WEEKLY):
            self._fill_window(metric, windows.get(metric), now, available, running)
        self._refresh_models(now, windows, available)
        self._refresh_daily(now, available)
        self._refresh_trend(available)

    def _fill_window(
        self, metric: str, spec: WindowSpec | None, now: datetime, available: bool, partial: bool
    ) -> None:
        header = self._window_headers[metric]
        cells = {key: self._window_cells[(metric, key)] for key in
                 ("cost", "quota", "per_point", "output_per_point")}
        if spec is None or not available:
            header.setText(metric)
            for cell in cells.values():
                cell.setText(UNAVAILABLE)
            return
        header.setText(f"{metric} ({format_time_left(spec.resets_at, now)})")
        store = self._service.store
        summary = summarize_costs(store.usage_by_model(self._provider, spec.start, now), self._rates)
        suffix = " (partial)" if partial else ""
        cells["cost"].setText(format_total_cost(summary) + suffix)
        pct = spec.pct
        if pct is None:
            cells["quota"].setText(UNAVAILABLE)
        elif pct >= 100:
            cells["quota"].setText(f"{pct:.0f}% (limit reached, extra usage possible)")
        else:
            cells["quota"].setText(f"{pct:.0f}%")
        if pct is None or pct < MIN_COUNTABLE_PCT or spec.last_reading_at is None:
            cells["per_point"].setText("n/a")
            cells["output_per_point"].setText("n/a")
            return
        at_reading = summarize_costs(
            store.usage_by_model(self._provider, spec.start, spec.last_reading_at), self._rates
        )
        cells["per_point"].setText(
            "n/a (unpriced usage)" if at_reading.has_unpriced
            else format_cost(at_reading.priced_cost / pct)
        )
        cells["output_per_point"].setText(format_tokens(at_reading.tokens.output / pct))

    def _range_bounds(
        self, key: str, now: datetime, windows: dict[str, WindowSpec]
    ) -> tuple[datetime, datetime] | None:
        today = local_date(now)
        if key == RANGE_DAY and self._day_filter is not None:
            return local_day_bounds(self._day_filter)
        if key == RANGE_SESSION:
            spec = windows.get(SESSION)
            return (spec.start, now) if spec else None
        if key == RANGE_WEEK:
            spec = windows.get(WEEKLY)
            return (spec.start, now) if spec else None
        days = {RANGE_TODAY: 1, RANGE_7D: 7, RANGE_30D: 30}.get(key)
        if days is None:
            return None
        start, _ = local_day_bounds(today - timedelta(days=days - 1))
        return start, now

    def _range_usage(
        self, key: str, now: datetime, windows: dict[str, WindowSpec]
    ) -> list[ModelUsage] | None:
        store = self._service.store
        if key in (RANGE_SESSION, RANGE_WEEK):
            bounds = self._range_bounds(key, now, windows)
            return None if bounds is None else store.usage_by_model(self._provider, *bounds)
        today = local_date(now)
        if key == RANGE_DAY and self._day_filter is not None:
            first = last = self._day_filter
        else:
            days = {RANGE_TODAY: 1, RANGE_7D: 7, RANGE_30D: 30}.get(key, 1)
            first, last = today - timedelta(days=days - 1), today
        # Daily totals outlive raw rows, so longer ranges keep working.
        daily = store.daily_totals(self._provider, first, last)
        return [row for rows in daily.values() for row in rows]

    def _refresh_models(self, now: datetime, windows: dict[str, WindowSpec], available: bool) -> None:
        table = self.model_table
        table.setRowCount(0)
        key = self.range_combo.currentData()
        usage = self._range_usage(key, now, windows) if available else None
        if usage is None:
            table.setRowCount(1)
            table.setItem(0, 0, _item("Usage unavailable for this range.", align_right=False, muted=True))
            return
        summary = summarize_costs(usage, self._rates)
        self._assign_colors(summary)
        sidechain = {}
        if self._provider == CLAUDE:
            bounds = self._range_bounds(key, now, windows)
            if bounds is not None:
                sidechain = self._service.store.sidechain_output(*bounds)
        if not summary.rows:
            table.setRowCount(1)
            table.setItem(0, 0, _item("No usage recorded in this range.", align_right=False, muted=True))
            return
        table.setRowCount(len(summary.rows) + 1)
        for row, model_row in enumerate(summary.rows):
            tokens = model_row.tokens
            side = sidechain.get(model_row.model)
            values = {
                "model": model_row.model,
                "messages": f"{model_row.messages:,}",
                "input": format_tokens(tokens.input),
                "output": format_tokens(tokens.output),
                "cache_read": format_tokens(tokens.cache_read),
                "cache_write": format_tokens(tokens.cache_write_5m + tokens.cache_write_1h),
                "cache_write_5m": format_tokens(tokens.cache_write_5m),
                "cache_write_1h": format_tokens(tokens.cache_write_1h),
                "reasoning": format_tokens(tokens.reasoning),
                "sidechain": (
                    f"{100 * side / tokens.output:.0f}%" if side is not None and tokens.output
                    else "n/a"
                ),
                "cost": (
                    format_cost(model_row.cost) + (" + unpriced" if model_row.has_unpriced else "")
                    if model_row.cost is not None else "no price"
                ),
                "share": share_bar(summary.share(model_row)),
            }
            for col_key, text in values.items():
                item = _item(text, align_right=col_key not in ("model", "share"),
                             muted=model_row.cost is None)
                if col_key == "model":
                    item.setForeground(QColor(self._model_colors.get(model_row.model, "#e5e7eb")))
                table.setItem(row, _COLUMN_INDEX[col_key], item)
        self._fill_total_row(len(summary.rows), summary)

    def _fill_total_row(self, row: int, summary: CostSummary) -> None:
        tokens = summary.tokens
        values = {
            "model": "Total",
            "messages": f"{summary.messages:,}",
            "input": format_tokens(tokens.input),
            "output": format_tokens(tokens.output),
            "cache_read": format_tokens(tokens.cache_read),
            "cache_write": format_tokens(tokens.cache_write_5m + tokens.cache_write_1h),
            "cache_write_5m": format_tokens(tokens.cache_write_5m),
            "cache_write_1h": format_tokens(tokens.cache_write_1h),
            "reasoning": format_tokens(tokens.reasoning),
            "sidechain": "",
            "cost": format_total_cost(summary),
            "share": "",
        }
        for col_key, text in values.items():
            item = _item(text, align_right=col_key != "model")
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            self.model_table.setItem(row, _COLUMN_INDEX[col_key], item)

    def _assign_colors(self, summary: CostSummary) -> None:
        for model_row in summary.rows:
            if model_row.model not in self._model_colors:
                self._model_colors[model_row.model] = MODEL_COLORS[
                    len(self._model_colors) % len(MODEL_COLORS)
                ]

    def _refresh_daily(self, now: datetime, available: bool) -> None:
        table = self.daily_table
        table.setRowCount(0)
        self._daily_days: list[date] = []
        if not available:
            table.setRowCount(1)
            table.setItem(0, 0, _item("Usage unavailable.", align_right=False, muted=True))
            return
        today = local_date(now)
        daily = self._service.store.daily_totals(
            self._provider, today - timedelta(days=DAILY_DAYS - 1), today
        )
        if not daily:
            table.setRowCount(1)
            table.setItem(0, 0, _item("No daily usage recorded yet.", align_right=False, muted=True))
            return
        summaries = {day: summarize_costs(rows, self._rates) for day, rows in daily.items()}
        for summary in summaries.values():
            self._assign_colors(summary)
        peak = max((s.priced_cost for s in summaries.values()), default=0.0) or 1.0
        days = sorted(summaries, reverse=True)
        table.setRowCount(len(days))
        for row, day in enumerate(days):
            summary = summaries[day]
            self._daily_days.append(day)
            table.setItem(row, 0, _item(day.strftime("%a %b %d"), align_right=False))
            table.setItem(row, 1, _item(format_total_cost(summary)))
            table.setItem(row, 2, _item(f"{summary.messages:,}"))
            segments = [
                ((r.cost or 0.0) / peak, self._model_colors.get(r.model, "#9ca3af"))
                for r in summary.rows
                if r.cost
            ]
            bar = _StackedBar(segments)
            bar.setToolTip(
                "\n".join(f"{r.model}: {format_cost(r.cost)}" for r in summary.rows)
            )
            table.setCellWidget(row, 3, bar)

    def trend_report(self) -> TrendReport:
        metric = self.trend_metric_combo.currentData()
        return build_trend(load_summaries(self._service.store, self._account_id, metric), metric, self._rates)

    def _refresh_trend(self, available: bool) -> None:
        table = self.trend_table
        table.setRowCount(0)
        if not available:
            self.trend_summary_label.setText("Allowance trend unavailable.")
            return
        report = self.trend_report()
        self.trend_summary_label.setText(trend_summary_text(report))
        if not report.rows:
            table.setRowCount(1)
            table.setItem(0, 0, _item("No completed windows yet.", align_right=False, muted=True))
            return
        table.setRowCount(len(report.rows))
        for row_index, row in enumerate(report.rows):
            summary = row.summary
            status = "counted" if row.counted else row.reason
            if row.counted and row.dollars_reason != COUNTED:
                status = f"counted ($/pt left out: {row.dollars_reason})"
            cells = (
                window_label(summary.window_start, summary.resets_at),
                format_total_cost(row.cost),
                f"{row.pct:.0f}%" if row.pct is not None else UNAVAILABLE,
                format_cost(row.dollars_per_point) if row.dollars_per_point is not None else "n/a",
                format_tokens(row.output_per_point) if row.output_per_point is not None else "n/a",
                f"{100 * row.cache_share:.0f}%" if row.cache_share is not None else "n/a",
                row.top_model or "n/a",
                "backfill" if summary.origin == ORIGIN_BACKFILL else "live",
                status,
            )
            for col, text in enumerate(cells):
                table.setItem(
                    row_index, col,
                    _item(text, align_right=col in (1, 2, 3, 4, 5), muted=not row.counted),
                )

    # ---- events ----

    def _sync_columns(self) -> None:
        show_extra = self.more_columns_cb.isChecked()
        for index, (_key, _title, extra) in enumerate(MODEL_COLUMNS):
            self.model_table.setColumnHidden(index, extra and not show_extra)

    def _on_range_changed(self, _index: int) -> None:
        if self.range_combo.currentData() != RANGE_DAY:
            day_index = self.range_combo.findData(RANGE_DAY)
            if day_index >= 0:
                self.range_combo.blockSignals(True)
                self.range_combo.removeItem(day_index)
                self.range_combo.blockSignals(False)
            self._day_filter = None
        self.refresh()

    def _on_day_clicked(self, row: int, _column: int) -> None:
        days = getattr(self, "_daily_days", [])
        if row >= len(days):
            return
        self._day_filter = days[row]
        label = f"Day: {self._day_filter:%b %d}"
        index = self.range_combo.findData(RANGE_DAY)
        self.range_combo.blockSignals(True)
        if index < 0:
            self.range_combo.addItem(label, RANGE_DAY)
            index = self.range_combo.count() - 1
        else:
            self.range_combo.setItemText(index, label)
        self.range_combo.setCurrentIndex(index)
        self.range_combo.blockSignals(False)
        self.refresh()

    def _sync_import_status(self, running: bool) -> None:
        self.cancel_btn.setVisible(running)
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
