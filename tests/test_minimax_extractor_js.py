"""Exercise the actual MiniMax WebEngine extractor without network access."""

import json
import shutil
import subprocess

import pytest

from aigauge.providers.minimax import EXTRACTOR_JS, MINIMAX_USAGE_URL, _build_snapshot
from aigauge.models import SnapshotStatus

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
    return json.loads(subprocess.check_output(
        ["node", str(harness), str(state), str(script)], text=True
    ))


def test_authenticated_page_yields_five_hour_meter_and_unlimited_weekly(tmp_path):
    payload = _extract(
        tmp_path,
        url=MINIMAX_USAGE_URL,
        body=(
            "Plan Usage My usage\n5h limit Resets in 3 hr 9 min\n"
            "Total quota 100% Used 11%\nWeekly limit ∞ ∞ Unlimited\n"
            "Credit balance 0 credits\nUsage Overview Cache Hit Rate 96.6%"
        ),
    )
    assert payload["logged_out"] is False
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.metrics[0].percent_used == 11.0
    assert snapshot.metrics[1].reset_label == "Unlimited"


def test_login_route_does_not_look_like_a_usage_page(tmp_path):
    payload = _extract(
        tmp_path, url="https://platform.minimax.io/login", body="Sign in to MiniMax"
    )
    assert payload["logged_out"] is True
    assert _build_snapshot(payload).status == SnapshotStatus.AUTH_REQUIRED
