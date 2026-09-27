import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aigauge.models import SnapshotStatus
from aigauge.providers import antigravity
from aigauge.providers.antigravity import (
    AntigravityProvider,
    _build_snapshot,
    _decode_output,
    _invoke_cli,
    resolve_cli,
)

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity" / "quota_ok.json"
LATCH_ERROR = (
    "agy did not treat /quota as a built-in command; polling paused to avoid "
    "spending model quota. Update agy and restart AI Gauge."
)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sync(provider: AntigravityProvider, monkeypatch) -> None:
    monkeypatch.setattr(provider, "_run_async", lambda work, on_done: on_done(work()))


def _refresh_with_payload(monkeypatch, payload: dict) -> AntigravityProvider:
    provider = AntigravityProvider(cli_path="agy")
    _sync(provider, monkeypatch)
    monkeypatch.setattr(antigravity, "resolve_cli", lambda _: "agy")
    monkeypatch.setattr(
        antigravity,
        "_invoke_cli",
        lambda _: subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
    )
    return provider


def test_fixture_maps_metrics_in_group_and_window_order():
    snapshot = _build_snapshot(_payload())

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == [
        "Gemini 5h",
        "Gemini weekly",
        "Claude+GPT 5h",
        "Claude+GPT weekly",
    ]
    assert [metric.percent_used for metric in snapshot.metrics] == pytest.approx(
        [14.962172508239746, 13.184946775436401, 0.0, 88.507279753684998]
    )
    assert [metric.window for metric in snapshot.metrics] == [
        timedelta(hours=5),
        timedelta(days=7),
        timedelta(hours=5),
        timedelta(days=7),
    ]
    assert snapshot.metrics[2].resets_at is None
    assert snapshot.metrics[2].reset_label == "idle"
    assert all(
        metric.resets_at is not None for index, metric in enumerate(snapshot.metrics)
        if index != 2
    )
    assert snapshot.metrics[0].resets_at.tzinfo is None
    assert all(metric.tag is None for metric in snapshot.metrics)
    assert len(snapshot.raw["buckets"]) == 4


def test_bucket_without_description_is_parsed_and_integer_one_is_idle():
    payload = _payload()
    payload["command"]["data"]["groups"][1]["buckets"][1]["reset_time"] = (
        "2026-10-26T06:40:58Z"
    )
    snapshot = _build_snapshot(payload)
    metric = snapshot.metrics[2]

    assert metric.percent_used == 0
    assert metric.reset_label == "idle"
    assert metric.resets_at is None


@pytest.mark.parametrize(
    ("remaining", "expected_reset", "expected_label"),
    [
        (1, None, "idle"),
        (0.75, datetime(2026, 10, 26, 6, 40, 58), None),
    ],
)
def test_zero_usage_is_idle_but_used_limit_keeps_reset_time(
    remaining, expected_reset, expected_label
):
    payload = _payload()
    bucket = payload["command"]["data"]["groups"][1]["buckets"][1]
    bucket["remaining_fraction"] = remaining
    bucket["reset_time"] = "2026-10-26T06:40:58Z"

    metric = next(
        item
        for item in _build_snapshot(payload).metrics
        if item.label == "Claude+GPT 5h"
    )

    if expected_reset is None:
        assert metric.resets_at is None
    else:
        assert metric.resets_at == antigravity._parse_reset_at(
            bucket["reset_time"]
        )
    assert metric.reset_label == expected_label
    if remaining == 1:
        assert metric.note == "Countdown starts when you next use this limit."


def test_unknown_window_and_group_prefix_use_fallback_labels():
    payload = _payload()
    payload["command"]["data"]["groups"] = [
        {
            "name": "Other Provider",
            "buckets": [
                {
                    "id": "other-special",
                    "window": "quarterly",
                    "remaining_fraction": 0.5,
                    "reset_time": "not a date",
                }
            ],
        }
    ]

    metric = _build_snapshot(payload).metrics[0]

    assert metric.label == "Other Provider quarterly"
    assert metric.window is None
    assert metric.percent_used == 50
    assert metric.resets_at is None


def test_empty_groups_produce_ok_snapshot_without_metrics():
    payload = _payload()
    payload["command"]["data"]["groups"] = []

    snapshot = _build_snapshot(payload)

    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.metrics == []


def test_decode_output_rejects_non_json_and_finds_final_json_line():
    with pytest.raises(ValueError, match="stdout is not JSON"):
        _decode_output("not JSON")

    assert _decode_output("diagnostic noise\n{\"status\": \"SUCCESS\"}\n") == {
        "status": "SUCCESS"
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda payload: payload.update(status="FAILED"), "status is not SUCCESS"),
        (
            lambda payload: payload["command"].update(name="other"),
            "command.name is not usage",
        ),
        (
            lambda payload: payload["command"]["data"].update(groups={}),
            "command.data.groups is not a list",
        ),
    ],
)
def test_success_contract_rejections(change, message):
    payload = _payload()
    change(payload)

    with pytest.raises(ValueError, match=message):
        _build_snapshot(payload)


def test_prompt_fallback_latches_polling_without_second_cli_call(monkeypatch):
    payload = _payload()
    payload["num_turns"] = 1
    calls = []
    provider = _refresh_with_payload(monkeypatch, payload)

    def invoke(_):
        calls.append(1)
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    monkeypatch.setattr(antigravity, "_invoke_cli", invoke)
    first = []
    second = []

    provider.refresh(first.append)
    provider.refresh(second.append)

    assert len(calls) == 1
    assert first[0].status == second[0].status == SnapshotStatus.ERROR
    assert first[0].error == second[0].error == LATCH_ERROR


@pytest.mark.parametrize("field", ["num_turns", "total_tokens"])
def test_prompt_fallback_latches_for_turns_or_tokens(monkeypatch, field):
    payload = _payload()
    if field == "num_turns":
        payload["num_turns"] = 1
    else:
        payload["usage"]["total_tokens"] = 3
    provider = _refresh_with_payload(monkeypatch, payload)
    result = []

    provider.refresh(result.append)

    assert result[0].status == SnapshotStatus.ERROR
    assert result[0].error == LATCH_ERROR


def test_missing_executable_reports_error(monkeypatch):
    provider = AntigravityProvider()
    _sync(provider, monkeypatch)
    monkeypatch.setattr(antigravity, "resolve_cli", lambda _: None)
    result = []

    provider.refresh(result.append)

    assert result[0].status == SnapshotStatus.ERROR
    assert result[0].error == "agy CLI not found"


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (FileNotFoundError("missing"), "failed to start agy: missing"),
        (subprocess.TimeoutExpired("agy", 60), "agy timed out"),
    ],
)
def test_cli_start_and_timeout_errors(monkeypatch, exception, expected):
    provider = AntigravityProvider(cli_path="agy")
    _sync(provider, monkeypatch)
    monkeypatch.setattr(antigravity, "resolve_cli", lambda _: "agy")

    def fail(_):
        raise exception

    monkeypatch.setattr(antigravity, "_invoke_cli", fail)
    result = []
    provider.refresh(result.append)

    assert result[0].status == SnapshotStatus.ERROR
    assert result[0].error == expected


@pytest.mark.parametrize(
    ("show_gemini", "show_third_party", "labels"),
    [
        (False, True, ["Claude+GPT 5h", "Claude+GPT weekly"]),
        (True, False, ["Gemini 5h", "Gemini weekly"]),
        (False, False, []),
    ],
)
def test_group_visibility_filters(show_gemini, show_third_party, labels):
    snapshot = _build_snapshot(
        _payload(),
        show_gemini=show_gemini,
        show_third_party=show_third_party,
    )

    assert [metric.label for metric in snapshot.metrics] == labels


def test_resolve_cli_prefers_explicit_path_then_which(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit-agy.exe"
    explicit.touch()
    discovered = tmp_path / "which-agy.exe"
    discovered.touch()
    monkeypatch.setattr(antigravity.shutil, "which", lambda _: str(discovered))

    assert resolve_cli(str(explicit)) == str(explicit)
    assert resolve_cli(None) == str(discovered)


def test_resolve_cli_uses_local_app_data_fallback_on_windows(monkeypatch, tmp_path):
    fallback = tmp_path / "agy" / "bin" / "agy.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    monkeypatch.setattr(antigravity.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(antigravity.shutil, "which", lambda _: None)

    assert resolve_cli(None) == str(fallback)


def test_invoke_cli_arguments_and_isolated_working_directory(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        assert Path(kwargs["cwd"]).is_dir()
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(antigravity.subprocess, "run", fake_run)

    _invoke_cli("agy-executable")

    assert captured["args"] == [
        "agy-executable",
        "--print",
        "/quota",
        "--output-format",
        "json",
        "--mode",
        "plan",
        "--sandbox",
        "--print-timeout",
        "30s",
    ]
    assert "shell" not in captured
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["timeout"] == 60
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
