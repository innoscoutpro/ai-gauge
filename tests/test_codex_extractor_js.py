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
const tabs = (state.tabs || []).map(tab => {
  // A tab given "attrs" carries exactly those attributes, like the live
  // buttons, and becomes pressed when clicked. Otherwise it reports
  // aria-selected only.
  const attrs = tab.attrs || {'aria-selected': String(!!tab.selected)};
  return {
    innerText: tab.label,
    className: '',
    getAttribute: name => (name in attrs ? String(attrs[name]) : null),
    closest() { return this; },
    click() {
      clicked.push(tab.label);
      if ('aria-pressed' in attrs) attrs['aria-pressed'] = 'true';
    },
  };
});
global.document = {
  body: {innerText: state.body}, title: 'ChatGPT',
  querySelectorAll: () => tabs,
  querySelector: () => null,
};
global.location = {
  href: 'https://chatgpt.com/settings/usage?tab=overview',
  pathname: '/settings/usage',
};
// The scraper re-runs the extractor in the same document when it asks for a
// retry, so repeated runs share one set of globals here too.
const source = fs.readFileSync(process.argv[3], 'utf8');
const results = [];
for (let run = 0; run < (state.repeat || 1); run++) results.push(eval(source));
process.stdout.write(JSON.stringify({result: results[results.length - 1], results, clicked}));
"""

# Enough unrelated page text to push "Plan limits" past the 2,000 characters
# of body_text the extractor returns.
LONG_PAGE = "Usage Overview Analytics " + "Recent chat title " * 150
ANALYTICS_HISTORY = (
    "Usage Overview Analytics Usage history Plan usage history By feature "
    "Weekly limits Period % of limit used Sep 26 – Oct 3 "
    "Usage as of Oct 2, 12:00 AM UTC 58.7%"
)


def run_extractor(tmp_path, body, tabs=None, repeat=1):
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    state = tmp_path / "state.json"
    harness.write_text(HARNESS, encoding="utf-8")
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    state.write_text(
        json.dumps({"body": body, "tabs": tabs or [], "repeat": repeat}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(state), str(script)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(completed.stdout)


def pressed_tab(label, pressed):
    """A tab as the live page renders it: a button with aria-pressed only."""
    return {"label": label, "attrs": {"aria-pressed": str(pressed).lower()}}


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


def test_weekly_only_limits_beyond_body_text_cut_are_accepted(tmp_path):
    output = run_extractor(tmp_path,
        LONG_PAGE + "Plan limits Shared across Codex, Work "
        "Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits Usage limit resets Restore your 5-hour limit",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
    )
    payload = output["result"]
    assert "Plan limits" not in payload["body_text"]
    assert payload["session"] is None
    assert payload["weekly"]["percent"] == 96
    assert payload["has_plan_limits"] is True
    assert payload["has_five_hour_heading"] is False
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 4.0)]


def test_dual_limits_beyond_body_text_cut_are_read(tmp_path):
    output = run_extractor(tmp_path,
        LONG_PAGE + "Plan limits Shared across Codex, Work "
        "5-hour limit Resets in 5h 0m 100% left Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
    )
    payload = output["result"]
    assert "Plan limits" not in payload["body_text"]
    assert payload["has_five_hour_heading"] is True
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert [m.percent_used for m in snapshot.metrics] == [0.0, 4.0]


def test_unloaded_five_hour_card_beyond_body_text_cut_is_retried(tmp_path):
    output = run_extractor(tmp_path,
        LONG_PAGE + "Plan limits 5-hour limit Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
    )
    payload = output["result"]
    assert "5-hour limit" not in payload["body_text"]
    assert payload["session"] is None
    assert payload["weekly"]["percent"] == 96
    assert payload["has_five_hour_heading"] is True
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.ERROR
    assert "part of the usage cards" in snapshot.error


def test_analytics_history_percent_is_not_loaded_limits(tmp_path):
    output = run_extractor(tmp_path, ANALYTICS_HISTORY,
        tabs=[pressed_tab("Overview", False), pressed_tab("Analytics", True)],
    )
    assert output["clicked"] == ["Overview"]
    assert output["result"]["__retry_after_ms"] == 1200
    assert output["result"]["__retry_reason"] == "selected overview tab"
    assert output["result"]["weekly"] is None


def test_pressed_overview_waits_without_clicking(tmp_path):
    output = run_extractor(tmp_path, "Usage Overview Analytics Plan limits",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
    )
    assert output["clicked"] == []
    assert output["result"]["__retry_reason"] == "waiting for plan limits"


def test_overview_is_clicked_once_across_reruns_on_same_document(tmp_path):
    output = run_extractor(tmp_path, "Usage Overview Analytics Usage history",
        tabs=[pressed_tab("Overview", False), pressed_tab("Analytics", True)],
        repeat=2,
    )
    assert output["clicked"] == ["Overview"]
    assert [item["__retry_reason"] for item in output["results"]] == [
        "selected overview tab",
        "waiting for plan limits",
    ]


@pytest.mark.parametrize("context", ["", "Usage Overview Analytics Plan limits "])
def test_weekly_heading_before_unloaded_value_cannot_borrow_session(tmp_path, context):
    output = run_extractor(tmp_path,
        context + "Weekly limit 5-hour limit Resets in 4h 0m 80% left",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
    )
    payload = output["result"]
    assert payload["session"]["percent"] == 80
    assert payload["weekly"] is None
    assert _build_snapshot(payload).status == SnapshotStatus.ERROR
