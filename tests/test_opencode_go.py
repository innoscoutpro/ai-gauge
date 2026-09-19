from datetime import datetime, timedelta

import requests
import responses

from aigauge.config import Config
from aigauge.models import SnapshotStatus
from aigauge.providers.opencode_go import (
    OPENCODE_GO_USAGE_API,
    OpenCodeGoProvider,
    _build_snapshot,
    _parse_reset_at,
)


def _usage_payload() -> dict:
    return {
        "usage": {
            "rolling": {
                "status": "ok",
                "percent": 13,
                "resetsAt": "2026-09-19T20:30:00.000Z",
            },
            "weekly": {
                "status": "ok",
                "percent": 14.5,
                "resetsAt": "2026-09-22T19:00:00.000Z",
            },
            "monthly": {
                "status": "ok",
                "percent": 7,
                "resetsAt": "2026-10-20T12:00:00.000Z",
            },
        }
    }


def _sync(provider: OpenCodeGoProvider, monkeypatch) -> None:
    monkeypatch.setattr(
        provider,
        "_run_async",
        lambda work, on_done: on_done(work()),
    )


def test_parse_reset_at_converts_utc_to_local_naive_datetime():
    source = "2026-09-19T20:30:00.000Z"

    parsed = _parse_reset_at(source)

    expected = (
        datetime.fromisoformat(source.replace("Z", "+00:00"))
        .astimezone()
        .replace(tzinfo=None)
    )
    assert parsed == expected
    assert parsed.tzinfo is None


def test_build_snapshot_maps_official_api_response():
    payload = _usage_payload()

    snapshot = _build_snapshot(payload, account_id="opencode_go-work")

    assert snapshot.provider == "opencode_go-work"
    assert snapshot.status == SnapshotStatus.OK
    assert [(metric.label, metric.percent_used) for metric in snapshot.metrics] == [
        ("Rolling", 13.0),
        ("Weekly", 14.5),
        ("Monthly", 7.0),
    ]
    assert [metric.window for metric in snapshot.metrics] == [
        timedelta(hours=5),
        timedelta(days=7),
        timedelta(days=30),
    ]
    assert all(metric.resets_at is not None for metric in snapshot.metrics)
    assert snapshot.raw == payload


def test_build_snapshot_marks_rate_limited_window_full():
    payload = _usage_payload()
    payload["usage"]["weekly"].update(status="rate-limited", percent=84)

    snapshot = _build_snapshot(payload)

    weekly = snapshot.metrics[1]
    assert weekly.percent_used == 100.0
    assert weekly.note == "Rate limit reached."


def test_build_snapshot_rejects_incomplete_api_response():
    payload = _usage_payload()
    del payload["usage"]["monthly"]

    try:
        _build_snapshot(payload)
    except ValueError as exc:
        assert "monthly" in str(exc)
    else:
        raise AssertionError("incomplete response should not be accepted")


def test_refresh_without_key_requests_setup(monkeypatch):
    monkeypatch.setattr(
        "aigauge.providers.opencode_go.get_opencode_go_key",
        lambda account_id: None,
    )
    provider = OpenCodeGoProvider(Config(), account_id="opencode_go-work")
    captured = []

    provider.refresh(captured.append)

    assert len(captured) == 1
    assert captured[0].provider == "opencode_go-work"
    assert captured[0].status == SnapshotStatus.AUTH_REQUIRED
    assert "API key" in (captured[0].error or "")


@responses.activate
def test_refresh_uses_bearer_key_and_returns_usage(monkeypatch):
    monkeypatch.setattr(
        "aigauge.providers.opencode_go.get_opencode_go_key",
        lambda account_id: "test-key",
    )
    responses.add(
        responses.GET,
        OPENCODE_GO_USAGE_API,
        json=_usage_payload(),
        status=200,
    )
    provider = OpenCodeGoProvider(Config(), account_id="opencode_go-work")
    _sync(provider, monkeypatch)
    captured = []

    provider.refresh(captured.append)

    assert captured[0].status == SnapshotStatus.OK
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-key"


@responses.activate
def test_refresh_maps_invalid_key_to_auth_required(monkeypatch):
    monkeypatch.setattr(
        "aigauge.providers.opencode_go.get_opencode_go_key",
        lambda account_id: "bad-key",
    )
    responses.add(responses.GET, OPENCODE_GO_USAGE_API, status=401)
    provider = OpenCodeGoProvider(Config())
    _sync(provider, monkeypatch)
    captured = []

    provider.refresh(captured.append)

    assert captured[0].status == SnapshotStatus.AUTH_REQUIRED
    assert "rejected" in (captured[0].error or "")


@responses.activate
def test_refresh_maps_missing_subscription_to_error(monkeypatch):
    monkeypatch.setattr(
        "aigauge.providers.opencode_go.get_opencode_go_key",
        lambda account_id: "key-without-go",
    )
    responses.add(responses.GET, OPENCODE_GO_USAGE_API, status=403)
    provider = OpenCodeGoProvider(Config())
    _sync(provider, monkeypatch)
    captured = []

    provider.refresh(captured.append)

    assert captured[0].status == SnapshotStatus.ERROR
    assert "subscription" in (captured[0].error or "")


@responses.activate
def test_refresh_surfaces_network_failure(monkeypatch):
    monkeypatch.setattr(
        "aigauge.providers.opencode_go.get_opencode_go_key",
        lambda account_id: "test-key",
    )
    responses.add(
        responses.GET,
        OPENCODE_GO_USAGE_API,
        body=requests.ConnectionError("offline"),
    )
    provider = OpenCodeGoProvider(Config())
    _sync(provider, monkeypatch)
    captured = []

    provider.refresh(captured.append)

    assert captured[0].status == SnapshotStatus.ERROR
    assert "failed" in (captured[0].error or "")
