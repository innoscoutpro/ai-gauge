from datetime import timedelta

from aigauge.models import SnapshotStatus
from aigauge.providers.minimax import MINIMAX_USAGE_URL, _build_snapshot


def test_live_usage_fixture_reads_used_percent_not_total_quota():
    """Regression fixture transcribed from the live My usage screenshot."""
    snapshot = _build_snapshot(
        {
            "url": MINIMAX_USAGE_URL,
            "usage_text": (
                "My usage 5h limit Resets in 1 hr 5 min Total quota 100% "
                "Used 19% Weekly limit Unlimited"
            ),
            "logged_out": False,
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [(metric.label, metric.percent_used) for metric in snapshot.metrics] == [
        ("5h", 19.0),
        ("Weekly", None),
    ]
    assert snapshot.metrics[0].window == timedelta(hours=5)
    assert snapshot.metrics[0].resets_at is not None
    assert snapshot.metrics[1].reset_label == "Unlimited"
    assert snapshot.raw == {"url": MINIMAX_USAGE_URL, "logged_out": False}


def test_credit_or_usage_history_text_is_not_treated_as_a_plan_limit():
    snapshot = _build_snapshot(
        {
            "url": MINIMAX_USAGE_URL,
            "usage_text": "Credit balance Usage Overview LLM 100.84M Tokens",
            "logged_out": False,
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert snapshot.metrics == []
    assert snapshot.raw == {"url": MINIMAX_USAGE_URL, "logged_out": False}


def test_logged_out_minimax_page_requests_sign_in():
    snapshot = _build_snapshot(
        {
            "url": "https://platform.minimax.io/login",
            "usage_text": "Sign in to MiniMax",
            "logged_out": True,
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert snapshot.metrics == []
    assert snapshot.raw == {
        "url": "https://platform.minimax.io/login",
        "logged_out": True,
    }
