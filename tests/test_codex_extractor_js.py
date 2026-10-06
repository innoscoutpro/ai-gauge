"""Execute the injected extractor against the new usage Overview layout."""

import json
import shutil
import subprocess

import pytest

from aigauge.models import SnapshotStatus
from aigauge.providers.codex import EXTRACTOR_JS, _build_snapshot

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available")

HARNESS = """
const fs = require('fs');
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const clicked = [];
const tabs = (state.tabs || []).map(tab => ({
  innerText: tab.label,
  className: '',
  getAttribute: name => name === 'aria-selected' ? String(!!tab.selected) : null,
  closest() { return this; },
  click() { clicked.push(tab.label); },
}));
global.document = {
  body: {innerText: state.body}, title: 'ChatGPT',
  querySelectorAll: () => tabs,
  querySelector: () => null,
};
global.location = {
  href: 'https://chatgpt.com/settings/usage?tab=overview',
  pathname: '/settings/usage',
};
const result = eval(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify({result, clicked}));
"""


def run_extractor(tmp_path, body, tabs=None):
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    state = tmp_path / "state.json"
    harness.write_text(HARNESS, encoding="utf-8")
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    state.write_text(json.dumps({"body": body, "tabs": tabs or []}), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(state), str(script)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def test_overview_reads_distinct_rows_and_ignores_credit_discounts(tmp_path):
    output = run_extractor(tmp_path,
        "Usage Overview Analytics Plan limits Shared across Codex, Work, Workspace Agents "
        "5-hour limit Resets in 5h 0m 100% left Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits Automatic reload Up to 40% off "
        "Usage limit resets Restore your 5-hour limit, weekly limit, or both "
        "Full reset (Weekly + 5 hr) Expires October 22 Use reset",
        tabs=[{"label": "Overview", "selected": True}],
    )
    payload = output["result"]
    assert output["clicked"] == []
    assert payload["session"]["percent"] == 100
    assert payload["session"]["reset_text"] == "5h 0m"
    assert payload["weekly"]["percent"] == 96
    assert payload["weekly"]["reset_text"] == "6d 10h"
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert [m.percent_used for m in snapshot.metrics] == [0.0, 4.0]


def test_weekly_only_overview_is_ready_without_clicking(tmp_path):
    output = run_extractor(tmp_path,
        "Usage Plan limits Shared across Codex, Work Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits Usage limit resets Restore your 5-hour limit",
        tabs=[{"label": "Overview", "selected": True}],
    )
    assert output["clicked"] == []
    assert output["result"]["session"] is None
    assert _build_snapshot(output["result"]).status == SnapshotStatus.OK


def test_analytics_switches_to_overview_and_retries(tmp_path):
    output = run_extractor(tmp_path,
        "Usage Overview Analytics Usage history Plan usage By product By model Codex",
        tabs=[{"label": "Overview"}, {"label": "Analytics", "selected": True}],
    )
    assert output["clicked"] == ["Overview"]
    assert output["result"]["__retry_after_ms"] == 1200
    assert output["result"]["__retry_reason"] == "selected overview tab"


def test_loading_overview_waits_without_clicking_again(tmp_path):
    output = run_extractor(tmp_path, "Usage Overview Analytics Plan limits",
        tabs=[{"label": "Overview", "selected": True}],
    )
    assert output["clicked"] == []
    assert output["result"]["__retry_reason"] == "waiting for plan limits"


def test_unloaded_limits_ignore_other_sections(tmp_path):
    output = run_extractor(tmp_path,
        "Plan limits 5-hour limit Weekly limit Credits Up to 40% off "
        "Usage limit resets Restore your 5-hour limit Weekly limit 100% left",
    )
    assert output["result"]["session"] is None
    assert output["result"]["weekly"] is None
    assert _build_snapshot(output["result"]).status == SnapshotStatus.ERROR
