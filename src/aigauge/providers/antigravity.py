from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from .base import Provider
from .idle import idle_reset_state

_LATCH_ERROR = (
    "agy did not treat /quota as a built-in command; polling paused to avoid "
    "spending model quota. Update agy and restart AI Gauge."
)


def resolve_cli(cli_path: str | None) -> str | None:
    if cli_path and Path(cli_path).is_file():
        return cli_path

    found = shutil.which("agy")
    if found:
        return found

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _invoke_cli(exe: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        return subprocess.run(
            [
                exe,
                "--print",
                "/quota",
                "--output-format",
                "json",
                "--mode",
                "plan",
                "--sandbox",
                "--print-timeout",
                "30s",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=60,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _decode_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout.strip())
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass

    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("unexpected agy output: stdout is not JSON")


def _parse_reset_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (ValueError, OverflowError):
        return None


def _build_snapshot(
    payload: dict[str, Any],
    *,
    show_gemini: bool = True,
    show_third_party: bool = True,
) -> UsageSnapshot:
    # A broken CLI version may send /quota as a normal model prompt.
    command = payload.get("command")
    usage = payload.get("usage")
    num_turns = payload.get("num_turns")
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if (
        "command" not in payload
        or (isinstance(num_turns, (int, float)) and num_turns > 0)
        or (isinstance(total_tokens, (int, float)) and total_tokens > 0)
    ):
        raise RuntimeError(_LATCH_ERROR)

    if payload.get("status") != "SUCCESS":
        raise ValueError("unexpected agy output: status is not SUCCESS")
    if num_turns != 0:
        raise ValueError("unexpected agy output: num_turns is not 0")
    if not isinstance(usage, dict):
        raise ValueError("unexpected agy output: usage is not an object")
    if total_tokens != 0:
        raise ValueError("unexpected agy output: usage.total_tokens is not 0")
    if not isinstance(command, dict):
        raise ValueError("unexpected agy output: command is not an object")
    if command.get("name") != "usage":
        raise ValueError("unexpected agy output: command.name is not usage")

    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("unexpected agy output: command.data.groups is not a list")

    metrics: list[UsageMetric] = []
    summary: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("unexpected agy output: group is not an object")
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            raise ValueError("unexpected agy output: group.buckets is not a list")

        group_metrics: list[tuple[int, UsageMetric]] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError("unexpected agy output: bucket is not an object")
            bucket_id = bucket.get("id")
            bucket_id = bucket_id if isinstance(bucket_id, str) else ""
            if bucket_id.startswith("gemini-"):
                group_label = "Gemini"
                if not show_gemini:
                    continue
            elif bucket_id.startswith("3p-"):
                group_label = "Claude+GPT"
                if not show_third_party:
                    continue
            else:
                group_label = str(group.get("name") or "")

            window_value = bucket.get("window")
            window_text = str(window_value) if window_value is not None else ""
            if window_value == "5h":
                window = timedelta(hours=5)
                order = 0
            elif window_value == "weekly":
                window = timedelta(days=7)
                order = 1
            else:
                window = None
                order = 2

            remaining = bucket.get("remaining_fraction")
            if (
                isinstance(remaining, (int, float))
                and not isinstance(remaining, bool)
                and math.isfinite(remaining)
            ):
                percent = max(0.0, min(100.0, (1.0 - remaining) * 100.0))
            else:
                percent = None

            resets_at = _parse_reset_at(bucket.get("reset_time"))
            reset_label = None
            note = None
            if window is not None:
                if percent == 0:
                    resets_at = None
                resets_at, reset_label, note = idle_reset_state(
                    percent=percent,
                    resets_at=resets_at,
                    window=window,
                )
            metric = UsageMetric(
                label=f"{group_label} {window_text}",
                percent_used=percent,
                resets_at=resets_at,
                reset_label=reset_label,
                note=note,
                window=window,
                tag=None,
            )
            group_metrics.append((order, metric))
            summary.append(
                {"id": bucket_id, "remaining_fraction": remaining}
            )

        metrics.extend(metric for _, metric in sorted(group_metrics, key=lambda x: x[0]))

    return UsageSnapshot(
        provider="antigravity",
        status=SnapshotStatus.OK,
        metrics=metrics,
        raw={"buckets": summary},
    )


class AntigravityProvider(Provider):
    name = "antigravity"
    display_name = "Antigravity"

    def __init__(
        self,
        cli_path: str | None = None,
        show_gemini: bool = True,
        show_third_party: bool = True,
    ):
        self._cli_path = cli_path
        self._show_gemini = show_gemini
        self._show_third_party = show_third_party
        self._latched_error: str | None = None

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        def work() -> UsageSnapshot:
            if self._latched_error:
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error=self._latched_error[:200],
                )

            exe = resolve_cli(self._cli_path)
            if exe is None:
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error="agy CLI not found",
                )
            try:
                result = _invoke_cli(exe)
            except subprocess.TimeoutExpired:
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error="agy timed out",
                )
            except (FileNotFoundError, OSError) as exc:
                reason = str(exc).splitlines()[0][:120]
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error=f"failed to start agy: {reason}"[:200],
                )

            try:
                payload = _decode_output(result.stdout)
                return _build_snapshot(
                    payload,
                    show_gemini=self._show_gemini,
                    show_third_party=self._show_third_party,
                )
            except RuntimeError as exc:
                self._latched_error = str(exc)[:200]
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error=self._latched_error,
                )
            except (TypeError, ValueError) as exc:
                return UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error=str(exc)[:200],
                )

        self._run_async(work, on_done)
