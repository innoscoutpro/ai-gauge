from datetime import datetime, timezone
from pathlib import Path

from aigauge.local_usage import claude_logs, codex_logs
from aigauge.local_usage.codex_logs import CodexParser, CodexParserState, CodexParseStats
from aigauge.local_usage.tokens import TokenCounts

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
CLAUDE_PROJECT = FIXTURES / "claude" / "projects" / "c--proj"
CODEX_DAY = FIXTURES / "codex" / "sessions" / "2026" / "09" / "10"
SESSION_ONE = CODEX_DAY / "rollout-2026-09-10T10-00-00-sess-1.jsonl"
SESSION_TWO = CODEX_DAY / "rollout-2026-09-10T11-00-00-sess-2.jsonl"


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def _by_model(items) -> dict[str, TokenCounts]:
    out: dict[str, TokenCounts] = {}
    for item in items:
        out.setdefault(item.model, TokenCounts()).add(item.tokens)
    return out


# ---- Claude ----


def _claude_messages(stats=None):
    main = claude_logs.parse_lines(_lines(CLAUDE_PROJECT / "sess-a.jsonl"), stats)
    sub = claude_logs.parse_lines(
        _lines(CLAUDE_PROJECT / "sess-a" / "subagents" / "agent-x.jsonl"), stats
    )
    return main + sub


def test_claude_totals_match_hand_worked_values():
    totals = _by_model(_claude_messages())

    # msg_A (final line) + msg_D (combined cache figure) + msg_C (subagent)
    assert totals["claude-opus-5"] == TokenCounts(
        input=8, output=936, cache_read=607, cache_write_5m=90, cache_write_1h=80
    )
    assert totals["claude-sonnet-5"] == TokenCounts(
        input=10, output=200, cache_read=1000, cache_write_1h=300, reasoning=20
    )
    assert set(totals) == {"claude-opus-5", "claude-sonnet-5"}


def test_claude_placeholder_lines_are_replaced_by_the_last_line():
    messages = claude_logs.parse_lines(_lines(CLAUDE_PROJECT / "sess-a.jsonl"))
    msg_a = [m for m in messages if m.message_id == "msg_A"]

    assert len(msg_a) == 1
    assert msg_a[0].tokens.output == 871
    assert msg_a[0].timestamp == datetime(2026, 9, 10, 12, 0, 1, tzinfo=timezone.utc)


def test_claude_subagent_usage_is_included_and_flagged():
    messages = _claude_messages()
    sidechain = [m for m in messages if m.is_sidechain]

    assert [m.message_id for m in sidechain] == ["msg_C"]
    assert sidechain[0].tokens.output == 60


def test_claude_synthetic_and_usage_less_lines_are_skipped():
    stats = claude_logs.ClaudeParseStats()
    messages = _claude_messages(stats)

    ids = {m.message_id for m in messages}
    assert "msg_S" not in ids
    assert "msg_N" not in ids
    assert stats.synthetic_skipped == 1
    assert stats.parsed_messages == 6  # three msg_A copies, msg_B, msg_D, msg_C
    assert stats.versions["2.1.265"] == 4


def test_claude_half_written_line_is_ignored_without_error():
    messages = claude_logs.parse_lines(_lines(CLAUDE_PROJECT / "sess-a.jsonl"))

    assert "msg_T" not in {m.message_id for m in messages}


def test_claude_combined_cache_figure_is_counted_as_five_minute_writes():
    messages = claude_logs.parse_lines(_lines(CLAUDE_PROJECT / "sess-a.jsonl"))
    msg_d = next(m for m in messages if m.message_id == "msg_D")

    assert msg_d.tokens.cache_write_5m == 40
    assert msg_d.tokens.cache_write_1h == 0
    assert msg_d.speed is None


def test_claude_unrecognized_assistant_lines_count_as_candidates():
    stats = claude_logs.ClaudeParseStats()
    line = b'{"type":"assistant","message":{"model":"claude-opus-5","tokens":{"in":1}}}'

    assert claude_logs.parse_lines([line], stats) == []
    assert stats.candidate_lines == 1
    assert stats.parsed_messages == 0


def test_claude_default_roots_honour_config_dir(tmp_path):
    home = tmp_path / "home"
    roots = claude_logs.default_roots({"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, home)

    assert roots == [
        tmp_path / "cfg" / "projects",
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]


# ---- Codex ----


def _parse_codex(path: Path, state=None, stats=None):
    parser = CodexParser(path.stem, state, stats)
    usage, readings = [], []
    for line in _lines(path):
        item, found = parser.feed(line)
        if item is not None:
            usage.append(item)
        readings.extend(found)
    return parser, usage, readings


def test_codex_totals_match_hand_worked_values():
    _parser, usage, _readings = _parse_codex(SESSION_ONE)
    totals = _by_model(usage)

    assert totals["gpt-5.5"] == TokenCounts(
        input=1500, output=600, cache_read=3500, reasoning=150
    )
    # 14:06 adds 2000/1500/300/50; 14:07 drops (new baseline); 14:08 adds
    # 300/100/40/10 over the new baseline.
    assert totals["gpt-5.6-sol"] == TokenCounts(
        input=700, output=340, cache_read=1600, reasoning=60
    )
    assert {u.session_id for u in usage} == {"sess-1"}


def test_codex_repeated_total_does_not_double_count():
    _parser, usage, _readings = _parse_codex(SESSION_ONE)

    stamps = [u.timestamp.strftime("%H:%M:%S") for u in usage]
    assert "14:01:05" not in stamps
    assert stamps.count("14:01:00") == 1


def test_codex_dropping_total_starts_new_baseline_without_negative_usage():
    _parser, usage, _readings = _parse_codex(SESSION_ONE)

    stamps = [u.timestamp.strftime("%H:%M") for u in usage]
    assert "14:07" not in stamps
    assert all(v >= 0 for u in usage for v in u.tokens.to_dict().values())


def test_codex_missing_info_or_rate_limits_does_not_fail():
    stats = CodexParseStats()
    _parser, usage, readings = _parse_codex(SESSION_ONE, stats=stats)

    assert any(u.timestamp.strftime("%H:%M") == "14:04" for u in usage)
    at_1403 = [r for r in readings if r.timestamp.strftime("%H:%M") == "14:03"]
    assert [(r.slot, r.used_percent) for r in at_1403] == [("primary", 6.0)]
    assert stats.candidate_lines == 8
    assert stats.formats["0.153.4"] == 1


def test_codex_quota_readings_carry_window_and_reset():
    _parser, _usage, readings = _parse_codex(SESSION_ONE)

    first = readings[0]
    assert (first.limit_id, first.slot, first.window_minutes) == ("codex", "primary", 300)
    assert first.resets_at == datetime.fromtimestamp(1789400000, tz=timezone.utc)
    assert first.plan_type == "team"
    weekly = [r for r in readings if r.window_minutes == 10080]
    assert [r.used_percent for r in weekly] == [10.0, 10.0, 10.0, 11.0, 11.0, 11.0]


def test_codex_event_without_turn_context_is_unknown_model():
    _parser, usage, readings = _parse_codex(SESSION_TWO)

    assert {u.model for u in usage} == {"unknown"}
    assert {u.session_id for u in usage} == {SESSION_TWO.stem}
    assert readings[0].limit_id == "premium"
    assert readings[0].resets_at is None


def test_codex_cache_writes_are_taken_out_of_uncached_input():
    _parser, usage, _readings = _parse_codex(SESSION_TWO)

    assert usage[1].tokens == TokenCounts(input=50, output=10, cache_read=20, cache_write_5m=30)


def test_codex_state_round_trip_resumes_mid_file_with_same_totals():
    lines = _lines(SESSION_ONE)
    parser = CodexParser(SESSION_ONE.stem)
    usage = []
    for line in lines[:6]:
        item, _ = parser.feed(line)
        if item is not None:
            usage.append(item)
    resumed = CodexParser(
        SESSION_ONE.stem, CodexParserState.from_json(parser.state.to_json())
    )
    for line in lines[6:]:
        item, _ = resumed.feed(line)
        if item is not None:
            usage.append(item)

    _full_parser, full_usage, _ = _parse_codex(SESSION_ONE)
    assert _by_model(usage) == _by_model(full_usage)


def test_codex_default_root_uses_codex_home(tmp_path):
    assert codex_logs.default_root({"CODEX_HOME": str(tmp_path)}) == tmp_path
    assert codex_logs.default_root({}, tmp_path) == tmp_path / ".codex"
