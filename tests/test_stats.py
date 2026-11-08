"""Tokens per model from BharatCode Desktop's own session database, for pack_cost and the
benchmarks: exact, from the replies since a moment, optionally one project's only; the channel
that started us picks the database; nothing is ever written to it."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gpu_token_utilization import stats

from conftest import desktop_db

QWEN, DEEPSEEK = "bharatcode/qwen-3.8-27b", "bharatcode/deepseek-v4.1-flash"
START = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
MS = int(START.timestamp() * 1000)


def tokens(inp, out, read=0, write=0, reasoning=0):
    return {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write},
            "total": inp + out + read + write + reasoning}


def test_tokens_are_summed_per_model_from_the_replies_since_the_start(tmp_path):
    project, other = str(tmp_path / "project"), str(tmp_path / "other")
    db = desktop_db(tmp_path / "bharatcode.db", [
        (project, MS - 60_000, QWEN, tokens(999, 9)),  # before the start: not counted
        (project, MS + 1_000, QWEN, tokens(500, 40, read=8000)),
        (project, MS + 2_000, QWEN, tokens(300, 20, reasoning=5)),
        (project, MS + 3_000, DEEPSEEK, tokens(180, 769, read=25600)),
        (other, MS + 4_000, QWEN, tokens(7000, 70)),  # another project
    ])
    mine = stats.tokens_since(START, Path(project), db=db)
    assert set(mine) == {QWEN, DEEPSEEK}
    assert mine[QWEN] == {"messages": 2, "input": 800, "output": 60, "reasoning": 5, "cache_read": 8000,
                          "cache_write": 0, "tokens": 8865}
    assert mine[DEEPSEEK]["tokens"] == 26549  # exact: no rounding to a hundred
    everyone = stats.tokens_since(START, db=db)
    assert everyone[QWEN]["messages"] == 3 and everyone[QWEN]["input"] == 7800


def test_the_project_folder_matches_however_desktop_spells_it(tmp_path):
    folder = tmp_path / "Spare-Check"
    db = desktop_db(tmp_path / "bharatcode.db", [(str(folder).replace("\\", "/"), MS + 1, QWEN, tokens(10, 1))])
    assert stats.tokens_since(START, folder, db=db)[QWEN]["messages"] == 1


def test_the_channel_that_started_us_names_the_database(tmp_path):
    local = tmp_path / "local"
    for name in ("BharatCode", "BharatCode Beta"):
        desktop_db(local / name / "Data" / "bharatcode.db", [])
    env = {"LOCALAPPDATA": str(local)}
    beta = {**env, "XDG_STATE_HOME": str(tmp_path / "roaming" / "ai.bharatcode.desktop.beta")}
    assert stats.channel(beta) == "beta" and stats.session_db(beta) == local / "BharatCode Beta" / "Data" / "bharatcode.db"
    assert stats.channel(env) == "stable" and stats.session_db(env) == local / "BharatCode" / "Data" / "bharatcode.db"
    dev = {**env, "XDG_STATE_HOME": str(tmp_path / "ai.bharatcode.desktop.dev")}
    assert stats.channel(dev) == "dev" and stats.session_db(dev) is None  # no Dev data on this machine
    chosen = desktop_db(tmp_path / "copy.db", [])
    assert stats.session_db({**beta, stats.DB_ENV: str(chosen)}) == chosen


def test_no_database_means_no_figures_never_a_guess(tmp_path):
    assert stats.tokens_since(START, db=None) is None  # conftest points LOCALAPPDATA at an empty folder
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database")
    assert stats.tokens_since(START, db=broken) is None


def test_the_database_is_only_read(tmp_path):
    db = desktop_db(tmp_path / "bharatcode.db", [(str(tmp_path), MS + 1, QWEN, tokens(10, 1))])
    before = db.read_bytes()
    stats.tokens_since(START, tmp_path, db=db)
    assert db.read_bytes() == before
    conn = sqlite3.connect(db)
    assert conn.execute("select count(*) from message").fetchone()[0] == 2
    conn.close()
