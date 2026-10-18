from datetime import timedelta

from aigauge.models import SnapshotStatus
from aigauge.providers.minimax import MINIMAX_USAGE_URL, _build_snapshot


SIGNED_IN_TEXT = (
    "MINIMAX Subscription Plan Usage Plan Usage My usage "
    "5h limit Resets in 3 hr 9 min Total quota 100% Used 11% "
    "Weekly limit ∞ ∞ Unlimited Credit balance You have no credits yet "
    "MiniMax Subscription Plan Usage Details Usage Overview "
    "LLM 100.84M Tokens Cache Hit Rate 96.6%"
)


def test_monthly_plus_coding_plan_has_five_hour_limit_and_unlimited_weekly():
    snapshot = _build_snapshot({
        "url": MINIMAX_USAGE_URL,
        "title": "Usage - MiniMax",
        "body_text": SIGNED_IN_TEXT,
        "logged_out": False,
    })

    assert snapshot.status == SnapshotStatus.OK
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [
        ("5h", 11.0),
        ("Weekly", None),
    ]
    assert snapshot.metrics[0].window == timedelta(hours=5)
    assert snapshot.metrics[0].resets_at is not None
    assert snapshot.metrics[1].reset_label == "Unlimited"
    assert snapshot.metrics[1].window is None


def test_usage_history_or_credit_percent_is_not_quota():
    snapshot = _build_snapshot({
        "url": MINIMAX_USAGE_URL,
        "title": "Usage - MiniMax",
        "body_text": (
            "Plan Usage Credit balance Usage Overview "
            "LLM 100.84M Tokens Cache Hit Rate 96.6%"
        ),
        "logged_out": False,
    })
    assert snapshot.status == SnapshotStatus.ERROR
    assert snapshot.metrics == []


def test_logged_out_minimax_page_requests_sign_in():
    snapshot = _build_snapshot({
        "url": "https://platform.minimax.io/login",
        "title": "Sign in - MiniMax",
        "body_text": "Sign in to MiniMax",
        "logged_out": True,
    })
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert snapshot.metrics == []
