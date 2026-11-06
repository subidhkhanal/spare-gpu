"""Reading `bharatcode stats --models` for pack_cost and the benchmarks: the MODEL USAGE box is
parsed with BharatCode's own rounding, and a missing CLI or stats command gives None."""

import sys

from gpu_token_utilization import stats

from test_mcp import STATS

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]


def test_the_model_usage_box_is_parsed_with_its_rounding():
    figures = stats.parse_stats(STATS)
    assert set(figures) == {"bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b"}
    deepseek = figures["bharatcode/deepseek-v4.1-flash"]
    assert deepseek["messages"] == 12 and deepseek["input"] == 120_500 and deepseek["cache_read"] == 40_000
    assert deepseek["tokens"] == 120_500 + 8_200 + 40_000
    assert stats.parse_stats("no model usage section at all") == {}
    assert stats._stats_number("1.2M") == 1_200_000 and stats._stats_number("$0.0012") == 0.0012


def test_usage_comes_from_the_cli_or_is_none(monkeypatch):
    assert stats.usage(7, command=FAKE_CLI) is None  # the fake has no stats command
    echo = [sys.executable, "-c", "import sys; print('\\x1b[32mTotal tokens: 12,345\\x1b[0m'); print('qwen: 9,000')"]
    assert stats.usage(7, command=echo) == "Total tokens: 12,345\nqwen: 9,000"
    assert stats.model_usage(7, command=FAKE_CLI) is None
    monkeypatch.delenv("GTU_BHARATCODE_CMD")
    monkeypatch.setattr("gpu_token_utilization.agent_cli.shutil.which", lambda name: None)
    assert stats.usage(7) is None  # no CLI at all
