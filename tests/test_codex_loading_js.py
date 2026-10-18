"""Exercise the actual Codex extractor against transient React shells."""
import json
import shutil
import subprocess

import pytest

from aigauge.providers.codex import EXTRACTOR_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")


def _run(tmp_path, body: str, *, path: str = "/settings/usage") -> dict:
    harness = tmp_path / "harness.js"
    script = tmp_path / "extractor.js"
    state = tmp_path / "state.json"
    harness.write_text("""
const fs = require('fs');
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
global.document = {
  body: {innerText: state.body}, title: 'Settings',
  querySelectorAll: () => [], querySelector: () => null,
};
global.location = {href: `https://chatgpt.com${state.path}?tab=overview`, pathname: state.path};
process.stdout.write(JSON.stringify(eval(fs.readFileSync(process.argv[3], 'utf8'))));
""", encoding="utf-8")
    script.write_text(EXTRACTOR_JS, encoding="utf-8")
    state.write_text(json.dumps({"body": body, "path": path}), encoding="utf-8")
    return json.loads(subprocess.check_output(
        ["node", str(harness), str(state), str(script)], text=True
    ))


def test_loading_usage_settings_requests_another_dom_sample(tmp_path):
    result = _run(tmp_path, "Skip to content Usage Loading usage settings…")
    assert result["__retry_after_ms"] > 0
    assert result["__retry_reason"] == "usage settings still loading"


def test_rendered_plan_limit_does_not_retry(tmp_path):
    result = _run(
        tmp_path,
        "Usage Overview Plan limits Weekly limit Resets in 3d 12h 75% left",
    )
    assert "__retry_after_ms" not in result


def test_login_route_does_not_retry_loading_shell(tmp_path):
    result = _run(tmp_path, "Loading usage settings…", path="/auth/login")
    assert "__retry_after_ms" not in result
    assert result["logged_out"]
