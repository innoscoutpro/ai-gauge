"""A small dialog for the dates a provider changed its usage limits."""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LimitChangesDialog(QDialog):
    def __init__(self, dates: list[date], provider_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Limit changes")
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Mark the dates {provider_name} changed its usage limits. The trend then "
            "keeps the earlier history and compares the windows before and after each "
            "change. A window that spans a change is skipped."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QListWidget()
        self.list.setObjectName("limit_changes_list")
        layout.addWidget(self.list)
        for day in sorted(set(dates)):
            self._add_item(day)

        row = QHBoxLayout()
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("MMM d, yyyy")
        row.addWidget(self.date_edit, 1)
        add = QPushButton("Add")
        add.clicked.connect(self._add_selected)
        row.addWidget(add)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        row.addWidget(remove)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_item(self, day: date) -> None:
        item = QListWidgetItem(day.strftime("%b %d, %Y"))
        item.setData(256, day.isoformat())
        self.list.addItem(item)

    def add_date(self, day: date) -> None:
        if day in self.dates():
            return
        self._add_item(day)
        self.list.sortItems()

    def _add_selected(self) -> None:
        self.add_date(self.date_edit.date().toPyDate())

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def dates(self) -> list[date]:
        return sorted(
            date.fromisoformat(self.list.item(i).data(256)) for i in range(self.list.count())
        )
