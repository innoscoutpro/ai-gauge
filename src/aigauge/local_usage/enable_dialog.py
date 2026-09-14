"""The prompt shown when local usage tracking is turned on with OK."""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox, QWidget

from .importer import ProviderScan
from .store import CLAUDE

IMPORT_HISTORY = "import_history"
START_FROM_NOW = "start_from_now"

_NAMES = {CLAUDE: "Claude", "codex": "Codex"}


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def backfill_prompt_text(scans: list[ProviderScan]) -> str:
    """Describe what can be imported. Built from folder listings only."""
    lines = []
    for scan in scans:
        name = _NAMES.get(scan.provider, scan.provider)
        oldest = scan.oldest_mtime
        if oldest is None:
            lines.append(f"No {name} logs were found yet.")
        else:
            lines.append(
                f"{name} logs go back to {oldest.astimezone():%b %d} "
                f"({format_bytes(scan.total_bytes)})."
            )
    lines.append(
        "The import runs in the background and can be cancelled. Start from now skips "
        "past usage; you can still import it later from Settings."
    )
    return " ".join(lines)


def ask_backfill(parent: QWidget | None, scans: list[ProviderScan]) -> str:
    box = QMessageBox(parent)
    box.setWindowTitle("Import past usage")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("Import past usage from your logs?")
    box.setInformativeText(backfill_prompt_text(scans))
    import_btn = box.addButton("Import history", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Start from now", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(import_btn)
    box.exec()
    return IMPORT_HISTORY if box.clickedButton() is import_btn else START_FROM_NOW
