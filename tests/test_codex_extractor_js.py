"""Execute the injected extractor against the new usage Overview layout."""

import json
import shutil
import subprocess

import pytest

from aigauge.models import SnapshotStatus
from aigauge.providers import codex
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
// <progress> elements, one list per extractor run (the last list repeats).
// The properties mimic the DOM: a bar without a value attribute still has
// .value 0, and one without max has .max 1.
const barRuns = (state.progress || [[]]).map(bars => bars.map(attrs => ({
  value: 'value' in attrs ? Number(attrs.value) : 0,
  max: 'max' in attrs ? Number(attrs.max) : 1,
  getAttribute: name => (name in attrs ? String(attrs[name]) : null),
})));
let run = 0;
global.window = global;
global.document = {
  body: {innerText: state.body}, title: 'ChatGPT',
  querySelectorAll: selector =>
    selector === 'progress' ? barRuns[Math.min(run, barRuns.length - 1)] : tabs,
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
for (; run < (state.repeat || 1); run++) results.push(eval(source));
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


# Not a capture, and not verified against a real page. The weekly strings are
# the ones in DraftingDreamer's fixture (dd/main:tests/codex_dom_fixture.py:31);
# the 5-hour strings are made up in the same style. All that matters here is
# that none of the English labels is present.
ZH_TW_OVERVIEW = (
    "用量 概覽 分析 5 小時上限 5 小時後重設 剩餘 100% "
    "每週上限 5 天 10 小時後重設 剩餘 94%"
)
ZH_TW_TABS = [
    {"label": "概覽", "attrs": {"aria-pressed": "true"}},
    {"label": "分析", "attrs": {"aria-pressed": "false"}},
]
# English wording that the label path does not know.
REWORDED_OVERVIEW = (
    "Usage Plan allowance Short window 100% remaining Long window 94% remaining"
)


def run_extractor(tmp_path, body, tabs=None, repeat=1, progress=None, progress_runs=None):
    """Run the extractor ``repeat`` times in one document.

    ``progress`` is the page's <progress> elements for every run;
    ``progress_runs`` gives a separate list for each run instead.
    """
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    state = tmp_path / "state.json"
    harness.write_text(HARNESS, encoding="utf-8")
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    state.write_text(
        json.dumps({
            "body": body,
            "tabs": tabs or [],
            "repeat": repeat,
            "progress": progress_runs or [progress or []],
        }),
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


def bar(value, max=100):
    """A limit card's <progress>; ``None`` leaves that attribute off."""
    attrs = {"aria-label": "Usage remaining"}
    if max is not None:
        attrs["max"] = str(max)
    if value is not None:
        attrs["value"] = str(value)
    return attrs


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


@pytest.mark.parametrize(("body", "tabs"), [
    (ZH_TW_OVERVIEW, ZH_TW_TABS),
    (REWORDED_OVERVIEW, None),
])
def test_two_bars_without_readable_labels_give_both_metrics(tmp_path, body, tabs):
    output = run_extractor(tmp_path, body, tabs=tabs, progress=[bar(100), bar(94)])
    payload = output["result"]
    assert output["clicked"] == []
    assert "__retry_after_ms" not in payload
    assert payload["label_free"] is True
    assert payload["session"] == {
        "raw": "progress value=100", "percent": 100, "kind": "remaining", "reset_text": None,
    }
    assert payload["weekly"]["percent"] == 94
    snapshot = _build_snapshot(payload)
    assert snapshot.status == SnapshotStatus.OK
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [
        ("Session", 0.0), ("Weekly", 6.0)
    ]
    assert all(m.resets_at is None and m.reset_label is None for m in snapshot.metrics)
    assert all(m.note == codex._LABEL_FREE_NOTE for m in snapshot.metrics)


def test_single_bar_is_confirmed_by_one_retry_then_read_as_weekly(tmp_path):
    output = run_extractor(tmp_path, ZH_TW_OVERVIEW, tabs=ZH_TW_TABS,
        progress=[bar(42)], repeat=2,
    )
    first, second = output["results"]
    assert first["__retry_after_ms"] == 1200
    assert first["__retry_reason"] == "confirming single usage bar"
    assert first["session"] is None and first["weekly"] is None
    assert "label_free" not in first

    assert "__retry_after_ms" not in second
    assert second["label_free"] is True
    assert second["session"] is None
    assert second["weekly"]["percent"] == 42
    snapshot = _build_snapshot(second)
    assert snapshot.status == SnapshotStatus.OK
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 58.0)]


def test_second_bar_arriving_during_the_retry_gives_both_metrics(tmp_path):
    output = run_extractor(tmp_path, ZH_TW_OVERVIEW, tabs=ZH_TW_TABS,
        progress_runs=[[bar(94)], [bar(100), bar(94)]], repeat=2,
    )
    assert output["results"][0]["__retry_reason"] == "confirming single usage bar"
    payload = output["result"]
    assert payload["label_free"] is True
    assert (payload["session"]["percent"], payload["weekly"]["percent"]) == (100, 94)


def test_single_bar_must_be_seen_on_consecutive_runs(tmp_path):
    output = run_extractor(tmp_path, ZH_TW_OVERVIEW, tabs=ZH_TW_TABS,
        progress_runs=[[bar(42)], [], [bar(42)]], repeat=3,
    )
    first, second, third = output["results"]
    assert first["__retry_reason"] == "confirming single usage bar"
    assert "__retry_after_ms" not in second and second["weekly"] is None
    assert third["__retry_reason"] == "confirming single usage bar"


@pytest.mark.parametrize("progress", [
    [],
    [bar(100), bar(94), bar(50)],
    [bar(None), bar(94)],
    [bar(None)],
    [bar(140), bar(94)],
    [bar(-1)],
    [bar("soon"), bar(94)],
], ids=["none", "three", "unloaded-of-two", "unloaded", "over-100", "negative", "not-a-number"])
def test_other_bar_counts_and_unusable_bars_leave_the_result_unchanged(tmp_path, progress):
    output = run_extractor(tmp_path, ZH_TW_OVERVIEW, tabs=ZH_TW_TABS,
        progress=progress, repeat=2,
    )
    for payload in output["results"]:
        assert "__retry_after_ms" not in payload
        assert "label_free" not in payload
        assert payload["session"] is None and payload["weekly"] is None
    assert _build_snapshot(output["result"]).status == SnapshotStatus.ERROR


def test_bars_on_another_scale_are_not_limit_cards(tmp_path):
    output = run_extractor(tmp_path, ZH_TW_OVERVIEW, tabs=ZH_TW_TABS,
        progress=[bar(0.4, max=1), bar(100), bar(30, max=None), bar(94)],
    )
    payload = output["result"]
    assert payload["label_free"] is True
    assert (payload["session"]["percent"], payload["weekly"]["percent"]) == (100, 94)


def test_readable_labels_win_over_bars_and_carry_no_marker(tmp_path):
    output = run_extractor(tmp_path,
        "Usage Overview Analytics Plan limits Shared across Codex, Work "
        "5-hour limit Resets in 5h 0m 100% left Weekly limit Resets in 6d 10h 96% left "
        "Credits 0 workspace credits",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
        progress=[bar(11), bar(22)],
    )
    payload = output["result"]
    assert "label_free" not in payload
    assert (payload["session"]["percent"], payload["weekly"]["percent"]) == (100, 96)
    assert payload["weekly"]["reset_text"] == "6d 10h"
    snapshot = _build_snapshot(payload)
    assert all(m.resets_at is not None for m in snapshot.metrics)
    assert all(m.note != codex._LABEL_FREE_NOTE for m in snapshot.metrics)


def test_partly_read_labels_do_not_fall_back_to_bars(tmp_path):
    output = run_extractor(tmp_path,
        "Usage Overview Analytics Plan limits 5-hour limit Resets in 5h 0m 100% left Weekly limit",
        tabs=[pressed_tab("Overview", True), pressed_tab("Analytics", False)],
        progress=[bar(100), bar(96)],
    )
    payload = output["result"]
    assert "label_free" not in payload
    assert payload["session"]["percent"] == 100
    assert payload["weekly"] is None
    assert _build_snapshot(payload).status == SnapshotStatus.ERROR


def test_logged_out_page_does_not_read_bars(tmp_path):
    output = run_extractor(tmp_path, "Welcome back Log in Sign up",
        progress=[bar(50), bar(60)],
    )
    payload = output["result"]
    assert payload["logged_out"] is True
    assert "label_free" not in payload
    assert payload["session"] is None and payload["weekly"] is None
