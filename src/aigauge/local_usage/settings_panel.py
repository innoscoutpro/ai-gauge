"""The "Local usage" tab in Settings."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import BrowserAccount, Config, LocalUsageProviderConfig, account_display_name
from .formats import untested_versions
from .importer import provider_roots, scan_provider
from .store import CLAUDE, CODEX, PROVIDERS

PROVIDER_TITLES = {CLAUDE: "Claude Code", CODEX: "Codex"}

SETTINGS_DISCLOSURE = (
    "Reads the Claude Code and Codex log files on this computer to estimate usage and "
    "API-equivalent cost. Only token counts, models and times are kept, never prompts "
    "or responses. Nothing is uploaded.",
    "Includes only activity recorded on this computer. Browser chats, cloud tasks and "
    "other computers use the same allowance but are not counted. Claude Code deletes "
    "its logs after 30 days by default; AI Gauge keeps its own summary once imported.",
    "Costs are estimates at current API prices, not charges. Trends compare this "
    "computer's recorded activity with the account's usage percentage; they cannot "
    "confirm a provider changed its limits.",
)

ACCOUNT_DISCLOSURE = (
    "All of this provider's logs on this computer are counted against the selected "
    "account. If you also use other accounts or API keys here, the trend will be "
    "unreliable."
)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#9ca3af; font-size:11px;")
    return label


def relative_time(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return when.astimezone().strftime("%b %d")


class _ProviderControls(QGroupBox):
    def __init__(
        self,
        provider: str,
        settings: LocalUsageProviderConfig,
        accounts: list[BrowserAccount],
        parent: QWidget | None = None,
    ):
        super().__init__(PROVIDER_TITLES[provider], parent)
        self.provider = provider
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        self.enabled_cb = QCheckBox(f"Track {PROVIDER_TITLES[provider]} logs")
        self.enabled_cb.setObjectName(f"local_usage_{provider}_enabled")
        self.enabled_cb.setChecked(settings.enabled)
        layout.addWidget(self.enabled_cb)

        account_row = QHBoxLayout()
        account_row.addWidget(QLabel("Count against:"))
        self.account_combo = QComboBox()
        self.account_combo.setObjectName(f"local_usage_{provider}_account")
        matching = [account for account in accounts if account.kind == provider]
        if len(matching) != 1:
            self.account_combo.addItem("Choose an account…", None)
        for account in matching:
            self.account_combo.addItem(account_display_name(account), account.id)
        index = self.account_combo.findData(settings.account_id)
        if settings.account_id is None and len(matching) == 1:
            index = 0
        self.account_combo.setCurrentIndex(max(0, index))
        self._missing_account = (
            settings.account_id is not None
            and not any(a.id == settings.account_id for a in matching)
        )
        account_row.addWidget(self.account_combo, 1)
        layout.addLayout(account_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Log folder:"))
        self.folder_edit = QLineEdit(settings.log_root or "")
        self.folder_edit.setObjectName(f"local_usage_{provider}_folder")
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText(self._default_folder_text())
        folder_row.addWidget(self.folder_edit, 1)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        folder_row.addWidget(self.browse_btn)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.clicked.connect(lambda: self.folder_edit.setText(""))
        folder_row.addWidget(self.reset_btn)
        layout.addLayout(folder_row)

        self.status_label = _hint("")
        self.status_label.setObjectName(f"local_usage_{provider}_status")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

    def _default_folder_text(self) -> str:
        roots = provider_roots(self.provider, None)
        existing = [root for root in roots if root.is_dir()]
        chosen = existing[0] if existing else roots[0]
        if self.provider == CODEX:
            chosen = chosen.parent  # show the Codex home, not sessions/
        return f"Detected: {chosen}"

    def _browse(self) -> None:
        start = self.folder_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose log folder", start)
        if folder:
            self.folder_edit.setText(folder)

    def selected_account_id(self) -> str | None:
        data = self.account_combo.currentData()
        return data if isinstance(data, str) else None

    def log_root(self) -> str | None:
        return self.folder_edit.text().strip() or None

    def apply_to(self, settings: LocalUsageProviderConfig) -> None:
        settings.enabled = self.enabled_cb.isChecked()
        settings.account_id = self.selected_account_id()
        settings.log_root = self.log_root()

    @property
    def missing_account(self) -> bool:
        return self._missing_account


class LocalUsagePanel(QWidget):
    def __init__(
        self,
        config: Config,
        accounts: list[BrowserAccount],
        service=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._service = service
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.enabled_cb = QCheckBox("Track local usage")
        self.enabled_cb.setObjectName("local_usage_enabled")
        self.enabled_cb.setChecked(config.local_usage.enabled)
        layout.addWidget(self.enabled_cb)
        for paragraph in SETTINGS_DISCLOSURE:
            layout.addWidget(_hint(paragraph))

        self.controls: dict[str, _ProviderControls] = {}
        for provider in PROVIDERS:
            controls = _ProviderControls(
                provider, getattr(config.local_usage, provider), accounts, self
            )
            controls.enabled_cb.toggled.connect(self._sync_enabled)
            controls.account_combo.currentIndexChanged.connect(self.refresh_status)
            controls.folder_edit.textChanged.connect(self.refresh_status)
            self.controls[provider] = controls
            layout.addWidget(controls)
        layout.addWidget(_hint(ACCOUNT_DISCLOSURE))

        actions = QHBoxLayout()
        self.import_btn = QPushButton("Import history")
        self.import_btn.setObjectName("local_usage_import_btn")
        self.import_btn.clicked.connect(self._import_history)
        actions.addWidget(self.import_btn)
        self.clear_btn = QPushButton("Clear local usage data")
        self.clear_btn.setObjectName("local_usage_clear_btn")
        self.clear_btn.clicked.connect(self._clear_data)
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(True)
        progress_row.addWidget(self.progress_bar, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_import)
        progress_row.addWidget(self.cancel_btn)
        self._progress_widget = QWidget()
        self._progress_widget.setLayout(progress_row)
        self._progress_widget.setVisible(False)
        layout.addWidget(self._progress_widget)

        self.saved_hint = _hint(
            "Import and clear act on saved settings. Turn tracking on and press OK first."
        )
        layout.addWidget(self.saved_hint)
        self.rates_label = _hint("")
        self.rates_label.setObjectName("local_usage_rates")
        self.rates_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.rates_label)
        layout.addStretch(1)
        try:
            from .rates import load_rate_table

            table = load_rate_table()
            self.set_rates_text(
                f"{table.describe()}. Optional price override file: {table.override_path}"
            )
        except Exception:  # noqa: BLE001
            self.set_rates_text("Rate table unavailable.")

        self.enabled_cb.toggled.connect(self._sync_enabled)
        if service is not None:
            service.progress_changed.connect(self._on_progress)
            service.import_started.connect(lambda _kind: self._on_progress(*service.progress()))
            service.import_finished.connect(self._on_finished)
            if service.is_running():
                self._on_progress(*service.progress())
        self._sync_enabled()

    # ---- state ----

    def _service_active(self) -> bool:
        return self._service is not None and self._config.local_usage.enabled

    def _sync_enabled(self) -> None:
        on = self.enabled_cb.isChecked()
        for controls in self.controls.values():
            controls.setEnabled(on)
            tracked = controls.enabled_cb.isChecked()
            for widget in (controls.account_combo, controls.folder_edit,
                           controls.browse_btn, controls.reset_btn):
                widget.setEnabled(on and tracked)
        active = self._service_active()
        self.import_btn.setEnabled(active)
        self.clear_btn.setEnabled(active)
        self.saved_hint.setVisible(not active)
        self.refresh_status()

    def refresh_status(self) -> None:
        for provider, controls in self.controls.items():
            controls.status_label.setText(self.status_text(provider))

    def status_text(self, provider: str) -> str:
        controls = self.controls[provider]
        if not self.enabled_cb.isChecked():
            return "Off. No log files are read."
        if not controls.enabled_cb.isChecked():
            return "Not tracked."
        notes = []
        if controls.selected_account_id() is None:
            notes.append(
                "Paused: the assigned account was removed. Choose an account."
                if controls.missing_account
                else "Choose an account to start tracking."
            )
        scan = scan_provider(provider, provider_roots(provider, controls.log_root()))
        if not scan.files:
            notes.append("No logs found in this folder.")
            return " ".join(notes)
        parts = [f"{len(scan.files)} files", f"logs from {scan.oldest_mtime.astimezone():%b %d}"]
        if self._service_active():
            store = self._service.store
            parts.append(f"last import {relative_time(store.last_import(provider))}")
            if store.get_meta(f"recognized:{provider}") is False:
                notes.append("Logs found, usage not recognized.")
            untested = untested_versions(provider, store.get_meta(f"versions:{provider}", {}))
            if untested:
                shown = ", ".join(untested[-3:])
                notes.append(f"Note: CLI versions not yet tested ({shown}); importing continues.")
        return " · ".join(parts) + ("\n" + " ".join(notes) if notes else "")

    def set_rates_text(self, text: str) -> None:
        self.rates_label.setText(text)

    def apply_to(self, config: Config) -> None:
        config.local_usage.enabled = self.enabled_cb.isChecked()
        for provider, controls in self.controls.items():
            controls.apply_to(getattr(config.local_usage, provider))

    # ---- actions ----

    def _import_history(self) -> None:
        if self._service_active():
            self._service.import_history()

    def _cancel_import(self) -> None:
        if self._service is not None:
            self._service.cancel()

    def _clear_data(self) -> None:
        if not self._service_active():
            return
        answer = QMessageBox.question(
            self,
            "Clear local usage data",
            "Delete AI Gauge's local usage database? CLI logs, sign-ins, and the "
            "existing usage history are not touched. Tracking continues from now; "
            "Import history can read the logs again.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._service.clear_data()
        self._service.start_from_now()
        self._progress_widget.setVisible(False)
        self.refresh_status()

    def _on_progress(self, done: int, total: int) -> None:
        running = self._service is not None and self._service.is_running()
        self._progress_widget.setVisible(running)
        if total > 0:
            self.progress_bar.setValue(int(1000 * min(done, total) / total))
            self.progress_bar.setFormat(f"Importing {100 * done / total:.0f}%")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Importing…")

    def _on_finished(self, _results) -> None:
        if self._service is not None and not self._service.is_running():
            self._progress_widget.setVisible(False)
        self.refresh_status()
