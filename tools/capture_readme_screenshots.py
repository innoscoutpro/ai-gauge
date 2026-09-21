"""Generate README widget screenshots from deterministic, non-sensitive data."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Capture the widget itself instead of a real desktop. This keeps credentials,
# account names, window chrome, and host-specific wallpaper out of the images.
# Headless Linux needs Qt's offscreen backend. Windows must keep its native
# backend so the system font engine remains available during widget grabs.
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QFont  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from aigauge.config import Config  # noqa: E402
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot  # noqa: E402
from aigauge.ratio import RatioEstimate  # noqa: E402
from aigauge.widget import UsageWidget  # noqa: E402


def _snapshots(now: datetime) -> tuple[tuple[UsageSnapshot, str], ...]:
    return (
        (
            UsageSnapshot(
                provider="claude",
                status=SnapshotStatus.OK,
                fetched_at=now,
                metrics=[
                    UsageMetric("Session", 53, reset_label="45m", window=timedelta(hours=5)),
                    UsageMetric("Weekly", 13, reset_label="6.3d", window=timedelta(days=7)),
                    UsageMetric(
                        "Fable this week", 7, reset_label="6.3d", window=timedelta(days=7)
                    ),
                ],
            ),
            "Claude",
        ),
        (
            UsageSnapshot(
                provider="codex",
                status=SnapshotStatus.OK,
                fetched_at=now,
                metrics=[
                    UsageMetric("Session", 22, reset_label="3h 10m", window=timedelta(hours=5)),
                    UsageMetric("Weekly", 41, reset_label="4.8d", window=timedelta(days=7)),
                ],
            ),
            "Codex",
        ),
        (
            UsageSnapshot(
                provider="opencode_go",
                status=SnapshotStatus.OK,
                fetched_at=now,
                metrics=[
                    UsageMetric("Rolling", 8, reset_label="4h 35m", window=timedelta(hours=5)),
                    UsageMetric("Weekly", 49, reset_label="3.5d", window=timedelta(days=7)),
                    UsageMetric("Monthly", 74, reset_label="12d", window=timedelta(days=30)),
                ],
            ),
            "OpenCode",
        ),
        (
            UsageSnapshot(
                provider="copilot",
                status=SnapshotStatus.OK,
                fetched_at=now,
                metrics=[
                    UsageMetric(
                        "Credits (540/1500)",
                        36,
                        reset_label="12d",
                        window=timedelta(days=30),
                    )
                ],
            ),
            "Copilot",
        ),
        (
            UsageSnapshot(
                provider="openrouter",
                status=SnapshotStatus.OK,
                fetched_at=now,
                metrics=[
                    UsageMetric("Balance $26.18 · Today $0.68 · Month $70.31"),
                    UsageMetric(
                        "Today ($0.68/$2.00)",
                        34,
                        reset_label="8h 20m",
                        window=timedelta(days=1),
                    ),
                ],
            ),
            "OpenRouter",
        ),
    )


def _ratio(sessions_per_week: float) -> RatioEstimate:
    return RatioEstimate(
        sessions_per_week=sessions_per_week,
        weekly_pct_per_session=100 / sessions_per_week,
        coverage_pct=42,
        sample_count=12,
        confident=True,
        source="current",
    )


def _capture(app: QApplication, output: Path, *, collapsed: bool) -> None:
    config = Config()
    config.window.always_on_top = False
    config.window.collapsed = collapsed
    config.window.width = 440
    config.window.collapsed_width = 440
    config.expanded_tiles = ["claude", "codex", "opencode_go", "openrouter"]

    now = datetime.now().replace(second=0, microsecond=0)
    widget = UsageWidget(config)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    for snapshot, display_name in _snapshots(now):
        widget.update_snapshot(snapshot, display_name)
    widget.set_ratio("claude", _ratio(10.1), [9.7, 10.4, 10.1])
    widget.set_ratio("codex", _ratio(6.7), [6.2, 6.8, 6.7])
    widget.set_refresh_state(True, 5, now + timedelta(minutes=5))
    widget.show()

    # Layout fitting is queued through zero-delay timers. Several event passes
    # make the result reliable without sleeping or contacting a window system.
    for _ in range(6):
        app.processEvents()

    output.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if pixmap.isNull() or not pixmap.save(str(output), "PNG"):
        raise RuntimeError(f"Could not write screenshot: {output}")
    widget.close()
    app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # Windows' offscreen plugin can select a logical UI font that has no
    # drawable glyphs. Arial is present on Windows and has compatible fallbacks
    # on macOS/Linux, so explicitly selecting it keeps headless captures legible.
    app.setFont(QFont("Arial", 10))
    output_dir = ROOT / "docs" / "screenshots"
    _capture(app, output_dir / "win-panel-full.png", collapsed=False)
    _capture(app, output_dir / "win-panel-compact.png", collapsed=True)
    print(f"Updated README screenshots in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
