"""Reading `bharatcode stats --models` for pack_cost and the benchmarks: the MODEL USAGE box is
parsed with BharatCode's own rounding, and a missing CLI or stats command gives None."""

import sqlite3
import sys
from pathlib import Path

from gpu_token_utilization import stats

from conftest import STATS

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


def desktop_env(tmp_path, channel_folder):
    data = tmp_path / "local" / "BharatCode Beta" / "Data"
    data.mkdir(parents=True)
    db = sqlite3.connect(data / "bharatcode.db")
    db.execute("create table message (id text)")
    db.execute("insert into message values ('msg_1')")
    db.commit()
    db.close()
    return {"XDG_STATE_HOME": str(tmp_path / "roaming" / channel_folder), "LOCALAPPDATA": str(tmp_path / "local")}, data / "bharatcode.db"


def test_the_desktop_channel_that_started_us_names_its_session_database(tmp_path):
    env, db = desktop_env(tmp_path, "ai.bharatcode.desktop.beta")
    assert stats.desktop_db(env) == db
    assert stats.desktop_db({**env, "XDG_STATE_HOME": str(tmp_path / "ai.bharatcode.desktop")}) is None  # stable: the CLI's own
    assert stats.desktop_db({**env, "XDG_STATE_HOME": str(tmp_path / "ai.bharatcode.desktop.dev")}) is None  # no Dev data here
    assert stats.desktop_db({**env, stats.DB_ENV: "C:\\chosen.db"}) is None  # already chosen
    assert stats.desktop_db({"LOCALAPPDATA": env["LOCALAPPDATA"]}) is None  # not started by Desktop


def test_inside_beta_the_cli_reads_a_snapshot_of_betas_sessions_never_the_live_file(tmp_path, monkeypatch):
    env, db = desktop_env(tmp_path, "ai.bharatcode.desktop.beta")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    before = db.read_bytes()
    show = [sys.executable, "-c", "import os, sqlite3; p = os.environ['OPENCODE_DB']; "
            "print(p); print(sqlite3.connect(p).execute('select id from message').fetchone()[0])"]
    path, row = stats.usage(1, command=show).splitlines()
    assert Path(path) != db and Path(path).name == "bharatcode.db" and row == "msg_1"  # the copy holds Beta's sessions
    assert not Path(path).exists() and db.read_bytes() == before  # the copy is gone; the live file untouched


def test_escape_sequences_dates_and_paths_do_not_break_the_parsing():
    noisy = "\x1b[2K\x1b[?25l2026/10/30-2026/11/06\n~/.config/bharatcode\n" + STATS + "┌──┐\n│ TOOL USAGE │\n│ bharatcode/x │\n│   Messages 99 │\n"
    import re
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", noisy)  # what usage() strips
    figures = stats.parse_stats(cleaned)
    assert set(figures) == {"bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b"}  # no phantom date, path or tool-box model
    assert figures["bharatcode/qwen-3.8-27b"]["messages"] == 30
