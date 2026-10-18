"""Exercise the MiniMax extractor without a live authenticated page."""

import json
import shutil
import subprocess

import pytest

from aigauge.models import SnapshotStatus
from aigauge.providers.minimax import EXTRACTOR_JS, MINIMAX_USAGE_URL, _build_snapshot

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")


def _extract(tmp_path, *, url: str, body: str) -> dict:
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    state = tmp_path / "state.json"
    harness.write_text(
        """
const fs = require('fs');
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
global.document = {body: {innerText: state.body}, title: 'MiniMax'};
global.location = new URL(state.url);
process.stdout.write(JSON.stringify(eval(fs.readFileSync(process.argv[3], 'utf8'))));
""",
        encoding="utf-8",
    )
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    state.write_text(json.dumps({"url": url, "body": body}), encoding="utf-8")
    return json.loads(
        subprocess.check_output(["node", str(harness), str(state), str(script)], text=True)
    )


def test_extractor_keeps_only_my_usage_and_reads_live_fixture_percent(tmp_path):
    payload = _extract(
        tmp_path,
        url=MINIMAX_USAGE_URL,
        body=(
            "Plan Usage My usage\n5h limit Resets in 1 hr 5 min\n"
            "Total quota 100% Used 19%\nWeekly limit Unlimited\n"
            "Credit balance private-account-data Usage Overview private-history-data"
        ),
    )

    assert payload["logged_out"] is False
    assert "private-account-data" not in payload["usage_text"]
    assert "private-history-data" not in payload["usage_text"]
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.metrics[0].percent_used == 19.0
    assert snapshot.raw == {"url": MINIMAX_USAGE_URL, "logged_out": False}


def test_extractor_drops_usage_text_when_credit_balance_boundary_is_missing(tmp_path):
    payload = _extract(
        tmp_path,
        url=MINIMAX_USAGE_URL,
        body="My usage 5h limit Resets in 1 hr Total quota 100% Used 19% Weekly limit Unlimited private-data",
    )

    assert payload["usage_text"] == ""
    assert _build_snapshot(payload).status == SnapshotStatus.ERROR
