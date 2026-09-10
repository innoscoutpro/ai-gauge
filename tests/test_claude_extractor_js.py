"""DOM-level checks for the Claude usage extractor.

The extractor is JavaScript injected into the webview, so the Python provider
tests exercise only its *output* shape. These tests run the real script under
Node against a mocked usage dialog, which is the only place row-matching bugs
can be caught. Skipped when Node is unavailable; GitHub's runners all ship it.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.providers.claude import EXTRACTOR_JS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js not available"
)

HARNESS = """
const fs = require('fs');
const nodes = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const elements = nodes.map(([text, height]) => ({
  textContent: text,
  getBoundingClientRect: () => ({ height }),
}));
global.document = {
  body: { textContent: nodes[0][0] },
  title: 'Claude',
  querySelectorAll: (sel) => (sel.includes('a[href') ? [] : elements),
  querySelector: () => null,
};
global.location = {
  href: 'https://claude.ai/new#settings/usage',
  pathname: '/new',
  hash: '#settings/usage',
  hostname: 'claude.ai',
};
const result = eval(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(result));
"""

SESSION = "Current session Resets in 3 hr 10 min 18% used"
ALL_MODELS = "All models Resets in 22 hr 0 min 2% used"
FABLE = "Fable Resets in 22 hr 0 min 4% used"
IDLE_SESSION = "Current session Starts when a message is sent 0% used"
IDLE_FABLE = "Fable You haven’t used Fable yet 0% used"
LIVE_WEEKLY = "All models Resets Mon 5:59 PM 2% used"
NEW_SESSION = "Current session Resets at 11:00 PM 12% used"
NEW_WEEKLY = "This week Resets Monday 6:00 PM 77% used"
NEW_FABLE = (
    "Fable this week Separate weekly limit for Fable · "
    "Resets Monday 6:00 PM 63% used"
)
# Real banner text from the Max-plan usage dialog. It names Fable but is not a
# usage row, and it sits directly above the bars.
BANNER = (
    "Fable 5 is still included with your Max plan. If you see a prompt to "
    "set up usage credits for it, restart Claude Code."
)


def run_extractor(tmp_path, rows: list[tuple[str, int]]) -> dict:
    """Run EXTRACTOR_JS against a mock DOM. rows are (text, height) pairs.

    The first row stands in for the page wrapper and supplies document.body
    text, matching how the real page nests every row inside it.
    """
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    nodes = tmp_path / "nodes.json"
    harness.write_text(HARNESS, encoding="utf-8")
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    nodes.write_text(json.dumps(rows), encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(nodes), str(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def percents(result: dict) -> dict:
    return {
        key: (result[key] or {}).get("percent")
        for key in ("session", "weekly_all", "weekly_fable")
    }


def test_max_plan_layout_reads_all_three_rows(tmp_path):
    wrapper = " ".join(
        ["Plan usage limits Max (5x)", SESSION, "Weekly limits", BANNER, ALL_MODELS, FABLE]
    )
    result = run_extractor(
        tmp_path,
        [
            (wrapper, 900),
            (SESSION, 60),
            (" ".join([BANNER, ALL_MODELS, FABLE]), 400),
            (BANNER, 90),
            (ALL_MODELS, 60),
            (FABLE, 60),
        ],
    )

    assert percents(result) == {"session": 18, "weekly_all": 2, "weekly_fable": 4}
    assert result["weekly_fable"]["reset_text"] == "22 hr 0 min"


def test_renamed_september_2026_usage_rows(tmp_path):
    """Claude's new dialog calls the weekly rows This week/Fable this week."""
    wrapper = " ".join(
        [
            "Your usage Max (5x)",
            "Heads up. At this pace you’ll run out tomorrow evening.",
            NEW_SESSION,
            NEW_WEEKLY,
            NEW_FABLE,
            "Usage credits",
        ]
    )
    result = run_extractor(
        tmp_path,
        [
            (wrapper, 1100),
            (NEW_SESSION, 60),
            (NEW_WEEKLY, 60),
            (NEW_FABLE, 90),
        ],
    )

    assert percents(result) == {
        "session": 12,
        "weekly_all": 77,
        "weekly_fable": 63,
    }
    assert result["session"]["reset_text"] == "at 11:00 PM"
    assert result["weekly_all"]["reset_text"] == "Monday 6:00 PM"
    assert result["weekly_fable"]["reset_text"] == "Monday 6:00 PM"
    assert "__retry_after_ms" not in result


def test_fable_banner_without_a_fable_row_is_not_read_as_a_row(tmp_path):
    """The banner names Fable but carries no usage of its own.

    A container holding the banner plus the All-models row must not be read as
    the Fable row — that would report the neighbouring row's percentage.
    """
    wrapper = " ".join(["Plan usage limits", SESSION, "Weekly limits", BANNER, ALL_MODELS])
    result = run_extractor(
        tmp_path,
        [
            (wrapper, 900),
            (SESSION, 60),
            (" ".join([BANNER, ALL_MODELS]), 300),
            (BANNER, 90),
            (ALL_MODELS, 60),
        ],
    )

    assert percents(result) == {"session": 18, "weekly_all": 2, "weekly_fable": None}


def test_plan_without_fable_row_still_reads_session_and_weekly(tmp_path):
    wrapper = " ".join(["Plan usage limits", SESSION, ALL_MODELS])
    result = run_extractor(
        tmp_path, [(wrapper, 900), (SESSION, 60), (ALL_MODELS, 60)]
    )

    assert percents(result) == {"session": 18, "weekly_all": 2, "weekly_fable": None}


def test_fable_row_is_found_when_only_nested_beside_the_banner(tmp_path):
    """No tight row element — the banner and the row share one container."""
    wrapper = " ".join(["Plan usage limits", SESSION, ALL_MODELS, BANNER, FABLE])
    result = run_extractor(
        tmp_path,
        [
            (wrapper, 900),
            (SESSION, 60),
            (ALL_MODELS, 60),
            (" ".join([BANNER, FABLE]), 130),
        ],
    )

    assert percents(result)["weekly_fable"] == 4


def test_versioned_fable_row_label_still_matches(tmp_path):
    versioned = "Fable 5 Resets in 22 hr 0 min 7% used"
    wrapper = " ".join(["Plan usage limits", SESSION, ALL_MODELS, versioned])
    result = run_extractor(
        tmp_path,
        [(wrapper, 900), (SESSION, 60), (ALL_MODELS, 60), (versioned, 60)],
    )

    assert percents(result)["weekly_fable"] == 7


def test_current_claude_idle_session_layout_reports_mixed_usage(tmp_path):
    wrapper = " ".join(
        [
            "Plan usage limits Max (5x)",
            IDLE_SESSION,
            "Weekly limits",
            BANNER,
            LIVE_WEEKLY,
            IDLE_FABLE,
        ]
    )
    result = run_extractor(
        tmp_path,
        [
            (wrapper, 1100),
            (IDLE_SESSION, 80),
            (" ".join([BANNER, LIVE_WEEKLY, IDLE_FABLE]), 400),
            (BANNER, 90),
            (LIVE_WEEKLY, 60),
            (IDLE_FABLE, 80),
        ],
    )

    assert percents(result) == {"session": 0, "weekly_all": 2, "weekly_fable": 0}
    assert result["session"]["reset_text"] is None
    assert result["weekly_all"]["reset_text"] == "Mon 5:59 PM"
    assert result["weekly_fable"]["reset_text"] is None
    assert "__retry_after_ms" not in result


def test_idle_copy_overrides_neighbor_percentage_in_shared_container(tmp_path):
    shared = " ".join([IDLE_SESSION, LIVE_WEEKLY])

    result = run_extractor(tmp_path, [(shared, 100)])

    assert percents(result) == {
        "session": 0,
        "weekly_all": 2,
        "weekly_fable": None,
    }


def test_legacy_idle_panel_without_percentages_reports_zero_rows(tmp_path):
    idle = (
        "Plan usage limits Current session Resets when you next use this limit "
        "All models Resets when you next use this limit"
    )
    result = run_extractor(tmp_path, [(idle, 400)])

    assert percents(result) == {
        "session": 0,
        "weekly_all": 0,
        "weekly_fable": None,
    }
