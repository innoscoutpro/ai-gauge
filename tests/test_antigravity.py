import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aigauge.app import App
from aigauge.config import Config
from aigauge.models import SnapshotStatus
from aigauge.providers import antigravity
from aigauge.providers.antigravity import (
    AntigravityProvider,
    _build_snapshot,
    _decode_output,
    _invoke_cli,
    normalize_cli_path,
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
    provider = AntigravityProvider()
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
        (lambda payload: payload.update(status="FAILED"), "agy error: FAILED"),
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
    provider = AntigravityProvider()
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
    monkeypatch.delenv("AGY_CLI_DISABLE_AUTO_UPDATE", raising=False)
    monkeypatch.setenv("AI_GAUGE_TEST_ENV", "preserved")

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        assert Path(kwargs["cwd"]).is_dir()
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(antigravity.subprocess, "run", fake_run)

    assert "AGY_CLI_DISABLE_AUTO_UPDATE" not in antigravity.os.environ
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
    assert captured["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert captured["env"]["AI_GAUGE_TEST_ENV"] == "preserved"
    assert "AGY_CLI_DISABLE_AUTO_UPDATE" not in antigravity.os.environ


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  C:/tools/agy.exe  ", "C:/tools/agy.exe"),
        ('"C:/tools/agy.exe"', "C:/tools/agy.exe"),
        ("'C:/tools/agy.exe'", "C:/tools/agy.exe"),
        ("   ", None),
        ("", None),
    ],
)
def test_normalize_cli_path_trims_and_unquotes(value, expected):
    assert normalize_cli_path(value) == expected


def test_normalize_cli_path_expands_home_and_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AGY_BIN", str(tmp_path / "agy.exe"))
    assert Path(normalize_cli_path("~/bin/agy.exe")) == tmp_path / "bin" / "agy.exe"
    assert normalize_cli_path("$AGY_BIN") == str(tmp_path / "agy.exe")
    if os.name == "nt":
        assert normalize_cli_path("%AGY_BIN%") == str(tmp_path / "agy.exe")


def test_provider_uses_explicit_existing_file_without_autodetect(monkeypatch, tmp_path):
    executable = tmp_path / "agy.exe"
    executable.touch()
    provider = AntigravityProvider(cli_path=f' "{executable}" ')
    _sync(provider, monkeypatch)
    which = lambda _: pytest.fail("explicit path must not trigger PATH lookup")
    monkeypatch.setattr(antigravity.shutil, "which", which)
    calls = []
    monkeypatch.setattr(
        antigravity,
        "_invoke_cli",
        lambda path: calls.append(path)
        or subprocess.CompletedProcess([], 0, json.dumps({
            "status": "SUCCESS", "num_turns": 0, "usage": {"total_tokens": 0},
            "command": {"name": "usage", "data": {"groups": []}},
        }), ""),
    )
    result = []
    provider.refresh(result.append)
    assert calls == [str(executable)]
    assert result[0].status is SnapshotStatus.OK


def test_provider_reports_missing_explicit_file_without_autodetect(
    monkeypatch, tmp_path
):
    missing = tmp_path / "missing agy.exe"
    provider = AntigravityProvider(cli_path=f' "{missing}" ')
    _sync(provider, monkeypatch)
    monkeypatch.setattr(
        antigravity.shutil, "which",
        lambda _: pytest.fail("invalid explicit path must not autodetect"),
    )
    result = []
    provider.refresh(result.append)
    assert result[0].status is SnapshotStatus.ERROR
    assert result[0].error == f"agy CLI not found at {missing}"


def test_none_keeps_path_then_windows_fallback_order(monkeypatch, tmp_path):
    path_cli = tmp_path / "path-agy.exe"
    fallback = tmp_path / "agy" / "bin" / "agy.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    monkeypatch.setattr(antigravity.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(antigravity.shutil, "which", lambda _: str(path_cli))
    assert resolve_cli(None) == str(path_cli)
    monkeypatch.setattr(antigravity.shutil, "which", lambda _: None)
    assert resolve_cli(None) == str(fallback)


def _provider_for_payload(monkeypatch, tmp_path, payload):
    cli = tmp_path / "agy.exe"
    cli.touch()
    provider = AntigravityProvider(cli_path=str(cli))
    _sync(provider, monkeypatch)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(antigravity.subprocess, "run", run)
    return provider, calls


def test_offline_eligibility_error_does_not_latch(monkeypatch, tmp_path):
    payload = {
        "conversation_id": "test",
        "duration_seconds": 0.1,
        "error": (
            'Eligibility check failed: Post "https://googleapis.com/": '
            "proxyconnect tcp: dial tcp 127.0.0.1:9: connectex: No connection"
        ),
        "num_turns": 0,
        "response": "",
        "status": "ERROR",
        "usage": {"total_tokens": 0},
    }
    provider, calls = _provider_for_payload(monkeypatch, tmp_path, payload)
    snapshots = []

    provider.refresh(snapshots.append)
    provider.refresh(snapshots.append)

    assert len(calls) == 2
    assert snapshots[0].status is SnapshotStatus.ERROR
    assert snapshots[0].error.startswith("agy error:")
    assert provider._latched_error is None


@pytest.mark.parametrize("usage_change", [{"num_turns": 1}, {"total_tokens": 1}])
def test_token_usage_latches_and_stops_future_calls(monkeypatch, tmp_path, usage_change):
    payload = {
        "status": "SUCCESS",
        "num_turns": 0,
        "usage": {"total_tokens": 0},
        "command": {"name": "usage", "data": {"groups": []}},
    }
    payload.update({k: v for k, v in usage_change.items() if k == "num_turns"})
    if "total_tokens" in usage_change:
        payload["usage"]["total_tokens"] = usage_change["total_tokens"]
    provider, calls = _provider_for_payload(monkeypatch, tmp_path, payload)
    snapshots = []

    provider.refresh(snapshots.append)
    provider.refresh(snapshots.append)

    assert len(calls) == 1
    assert snapshots[0].error == snapshots[1].error
    assert provider._latched_error


def test_indeterminate_usage_latches(monkeypatch, tmp_path):
    provider, calls = _provider_for_payload(
        monkeypatch, tmp_path, {"status": "ERROR", "error": "unknown"}
    )
    snapshots = []

    provider.refresh(snapshots.append)
    provider.refresh(snapshots.append)

    assert len(calls) == 1
    assert provider._latched_error


def test_success_without_command_and_zero_tokens_does_not_latch(monkeypatch, tmp_path):
    payload = {
        "status": "SUCCESS",
        "num_turns": 0,
        "usage": {"total_tokens": 0},
    }
    provider, calls = _provider_for_payload(monkeypatch, tmp_path, payload)
    snapshots = []

    provider.refresh(snapshots.append)
    provider.refresh(snapshots.append)

    assert len(calls) == 2
    assert all(s.status is SnapshotStatus.ERROR for s in snapshots)
    assert snapshots[0].error == "unexpected agy output: command is missing"
    assert provider._latched_error is None


def test_wrong_explicit_executable_is_rejected_without_running(monkeypatch, tmp_path):
    executable = tmp_path / "notepad.exe"
    executable.touch()
    provider = AntigravityProvider(cli_path=str(executable))
    _sync(provider, monkeypatch)
    monkeypatch.setattr(
        antigravity.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    snapshots = []

    provider.refresh(snapshots.append)

    assert snapshots[0].error.startswith("Not the agy CLI:")
    assert snapshots[0].raw["config_error"] is True


def test_missing_explicit_path_is_configuration_error(monkeypatch, tmp_path):
    missing = tmp_path / "agy.exe"
    provider = AntigravityProvider(cli_path=str(missing))
    _sync(provider, monkeypatch)
    snapshots = []

    provider.refresh(snapshots.append)

    assert snapshots[0].error.startswith("agy CLI not found at")
    assert snapshots[0].raw["config_error"] is True


def test_existing_agy_executable_is_invoked(monkeypatch, tmp_path):
    payload = {
        "status": "SUCCESS",
        "num_turns": 0,
        "usage": {"total_tokens": 0},
        "command": {"name": "usage", "data": {"groups": []}},
    }
    provider, calls = _provider_for_payload(monkeypatch, tmp_path, payload)
    snapshots = []

    provider.refresh(snapshots.append)

    assert len(calls) == 1
    assert snapshots[0].status is SnapshotStatus.OK


def _provider_app(config):
    app = App.__new__(App)
    app._config = config
    app._providers = {}
    app._snapshots = {}
    app._widget = SimpleNamespace(
        _tiles={}, ensure_tile=lambda name, label: app._widget._tiles.setdefault(name, label),
        remove_tile=lambda name: app._widget._tiles.pop(name, None),
    )
    app._sync_usage_cache = lambda: None
    return app


def test_antigravity_latch_survives_provider_rebuild_disable_and_reenable(monkeypatch):
    config = Config()
    config.providers.claude = config.providers.codex = False
    config.providers.copilot = config.providers.openrouter = False
    config.providers.antigravity = True
    app = _provider_app(config)
    calls = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
        "status": "SUCCESS", "num_turns": 1,
        "usage": {"total_tokens": 1},
    }), ""))
    monkeypatch.setattr(antigravity, "_invoke_cli", calls)
    monkeypatch.setattr(antigravity, "resolve_cli", lambda _: "agy")
    monkeypatch.setattr(AntigravityProvider, "_run_async", lambda self, work, done: done(work()))
    app._build_providers()
    app._providers["antigravity"].refresh(lambda snapshot: None)
    assert antigravity._PROCESS_LATCHED_ERROR
    app._build_providers()
    app._providers["antigravity"].refresh(lambda snapshot: None)
    assert calls.call_count == 1
    config.providers.antigravity = False
    app._build_providers()
    config.providers.antigravity = True
    app._build_providers()
    snapshots = []
    app._providers["antigravity"].refresh(snapshots.append)
    assert calls.call_count == 1
    assert snapshots[0].error == antigravity._PROCESS_LATCHED_ERROR
