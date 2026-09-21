"""Capture AI Gauge's native macOS menu-bar item using safe demo data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture the native AI Gauge menu-bar item with demo usage."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "screenshots" / "mac-menubar.png",
        help="PNG path (default: docs/screenshots/mac-menubar.png)",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if sys.platform != "darwin":
        print("This command must run in a logged-in macOS desktop session.", file=sys.stderr)
        return 2

    # Import platform GUI modules only after the friendly platform check, so
    # --help and accidental runs on other operating systems remain useful.
    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication

    from aigauge.config import Config
    from aigauge.macos_status_item import NativeMacStatusItem
    from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot

    app = QApplication.instance() or QApplication([])
    config = Config()
    snapshots = {
        "claude": UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Weekly", 54)],
        ),
        "codex": UsageSnapshot(
            provider="codex",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Weekly", 11)],
        ),
        "copilot": UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Credits", 36)],
        ),
    }

    status = NativeMacStatusItem(on_activate=lambda: None, on_context=lambda: None)
    try:
        status.update(snapshots, ("claude", "codex", "copilot"), config)

        # Give AppKit one event-loop turn to lay out the variable-width native
        # status item before asking its NSView to render itself.
        loop = QEventLoop()
        QTimer.singleShot(250, loop.quit)
        loop.exec()

        args.output.parent.mkdir(parents=True, exist_ok=True)
        status.save_screenshot(args.output)
        print(f"Updated macOS menu-bar screenshot: {args.output.resolve()}")
    finally:
        status.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
