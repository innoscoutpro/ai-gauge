import json

import pytest

from aigauge.local_usage.rates import (
    format_total_cost,
    load_rate_table,
    summarize_costs,
)
from aigauge.local_usage.store import ModelUsage
from aigauge.local_usage.tokens import TokenCounts

MILLION = 1_000_000


@pytest.fixture
def table(tmp_path):
    return load_rate_table(override_path=tmp_path / "missing.json")


def test_bundled_table_loads_with_sources(table):
    assert table.version == "2026-09-14"
    assert not table.errors
    opus = table.rates_for("claude-opus-5")
    assert opus.source.startswith("https://")
    assert opus.as_of == "2026-09-14"


def test_all_five_categories_are_priced(table):
    tokens = TokenCounts(
        input=MILLION, output=MILLION, cache_read=MILLION,
        cache_write_5m=MILLION, cache_write_1h=MILLION, reasoning=MILLION,
    )
    # 5 + 25 + 0.5 + 6.25 + 10; reasoning is part of output and not priced again
    assert table.cost("claude-opus-5", "", tokens) == pytest.approx(46.75)


def test_fast_variant_uses_fast_rates(table):
    tokens = TokenCounts(output=MILLION)
    assert table.cost("claude-opus-5", "fast", tokens) == pytest.approx(50.0)
    assert table.cost("claude-sonnet-5", "fast", tokens) is None


def test_long_bucket_without_surcharge_uses_base_rates(table):
    tokens = TokenCounts(input=MILLION)
    assert table.cost("claude-sonnet-5", "long", tokens) == pytest.approx(2.0)
    assert table.cost("gpt-5.5", "long", tokens) == pytest.approx(10.0)
    assert table.cost("gpt-5.4-mini", "long", tokens) == pytest.approx(0.75)


def test_unpublished_variant_combination_is_unpriced(table):
    assert table.cost("gpt-5.5", "fast+long", TokenCounts(input=MILLION)) is None


def test_unknown_model_is_unpriced_never_zero(table):
    summary = summarize_costs(
        [
            ModelUsage("claude-opus-5", "", TokenCounts(output=MILLION), 3),
            ModelUsage("codex-auto-review", "", TokenCounts(output=500), 2),
        ],
        table,
    )

    unpriced = next(r for r in summary.rows if r.model == "codex-auto-review")
    assert unpriced.cost is None
    assert unpriced.tokens.output == 500
    assert summary.rows[-1] is unpriced  # unpriced rows sort last
    assert summary.priced_cost == pytest.approx(25.0)
    assert format_total_cost(summary) == "$25.00 + unpriced"


def test_dated_model_names_fall_back_to_base_entry(table):
    assert table.cost("claude-sonnet-5-20260801", "", TokenCounts(output=MILLION)) == pytest.approx(10.0)


def test_override_file_takes_precedence(tmp_path):
    override = tmp_path / "rates.override.json"
    override.write_text(
        json.dumps(
            {
                "models": {
                    "claude-opus-5": {"rates": {"input": 1, "output": 2, "cache_read": 0,
                                                "cache_write_5m": 0, "cache_write_1h": 0}},
                    "codex-auto-review": {"rates": {"input": 1, "output": 1, "cache_read": 0,
                                                    "cache_write_5m": 0, "cache_write_1h": 0}},
                }
            }
        ),
        encoding="utf-8",
    )
    table = load_rate_table(override_path=override)

    assert table.cost("claude-opus-5", "", TokenCounts(output=MILLION)) == pytest.approx(2.0)
    assert table.cost("codex-auto-review", "", TokenCounts(output=MILLION)) == pytest.approx(1.0)
    assert "override" in table.describe()


def test_broken_override_keeps_bundled_prices(tmp_path):
    override = tmp_path / "rates.override.json"
    override.write_text("{not json", encoding="utf-8")

    table = load_rate_table(override_path=override)

    assert table.cost("claude-opus-5", "", TokenCounts(output=MILLION)) == pytest.approx(25.0)
    assert table.errors


def test_changing_the_rate_table_reprices_history(tmp_path):
    usage = [ModelUsage("claude-opus-5", "", TokenCounts(output=MILLION), 1)]
    cheaper = tmp_path / "rates.override.json"
    cheaper.write_text(
        json.dumps({"models": {"claude-opus-5": {"rates": {
            "input": 1, "output": 10, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}}}}),
        encoding="utf-8",
    )

    before = summarize_costs(usage, load_rate_table(override_path=tmp_path / "none.json"))
    after = summarize_costs(usage, load_rate_table(override_path=cheaper))

    assert before.priced_cost == pytest.approx(25.0)
    assert after.priced_cost == pytest.approx(10.0)
