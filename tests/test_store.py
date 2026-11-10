import sqlite3

import pytest

from gpu_token_utilization.store import JOB_KINDS, SCHEMA, STORED_KINDS, RequestLog, Store, StoreError


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".gtu" / "gtu.db") as s:
        yield s


def test_store_gitignores_its_folder_but_the_settings_and_memory(tmp_path):
    from gpu_token_utilization.store import IGNORE

    Store(tmp_path / ".gtu" / "gtu.db").close()
    assert (tmp_path / ".gtu" / ".gitignore").read_text() == IGNORE == "*\n!.gitignore\n!settings.json\n!memory.md\n"


def test_a_store_elsewhere_never_gitignores_that_folder(tmp_path):
    Store(tmp_path / "gtu.db").close()  # GTU_DB pointing at a repo root, say
    assert not (tmp_path / ".gitignore").exists()


def test_add_get_and_list_jobs(store, job_repo):
    a = store.add_job("fix", "src/a.py", job_repo)
    b = store.add_job("fix", "src", job_repo, note="focus on errors")
    assert len(a.id) == 8 and a.status == "queued"
    assert store.get_job(b.id).note == "focus on errors"
    assert [j.id for j in store.list_jobs()] == [a.id, b.id]


def test_an_older_store_learns_the_fix_kind_and_keeps_its_rows_of_kinds_that_are_gone(tmp_path, job_repo):
    # a store made before the fix kind existed: its CHECK constraint names only the first three,
    # kinds that can no longer be queued but whose rows must still open and list
    path = tmp_path / ".gtu" / "gtu.db"
    path.parent.mkdir()
    kinds_sql = ", ".join(f"'{k}'" for k in STORED_KINDS)
    assert kinds_sql in SCHEMA
    old = sqlite3.connect(path)
    old.executescript(SCHEMA.replace(kinds_sql, "'tests', 'docs', 'review'"))
    for n, kind in enumerate(("tests", "docs", "review"), 1):
        old.execute("INSERT INTO jobs (id, kind, target, repo, status, note, created_at, updated_at) VALUES "
                    f"('old0000{n}', ?, 'src', ?, 'done', 'keep me', '2026-01-0{n}T00:00:00+00:00', "
                    f"'2026-01-0{n}T00:00:00+00:00')", (kind, job_repo))
        old.execute(f"INSERT INTO job_steps (job_id, seq, name, status) VALUES ('old0000{n}', 1, 'write', 'done')")
    old.commit()
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("INSERT INTO jobs (id, kind, target, repo, created_at, updated_at) VALUES ('x', 'fix', '.', 'r', 'a', 'a')")
    old.close()
    with Store(path) as store:
        assert [(j.id, j.kind) for j in store.list_jobs()] == [
            ("old00001", "tests"), ("old00002", "docs"), ("old00003", "review")]
        assert store.get_job("old00001").note == "keep me"
        assert [s["name"] for s in store.steps("old00002")] == ["write"]
        new = store.add_job("fix", ".", job_repo, note="src/a.py:3 off by one")
        assert new.kind == "fix"
        with pytest.raises(StoreError, match="unknown job kind 'docs'"):
            store.add_job(store.get_job("old00002").kind, "README.md", job_repo)
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "'fix'" in store.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    with Store(path) as again:  # a second open changes nothing
        assert [j.id for j in again.list_jobs()] == ["old00001", "old00002", "old00003", new.id]


def test_only_fix_jobs_can_be_queued(store, job_repo):
    assert JOB_KINDS == ("fix",)
    for gone in ("tests", "docs", "review", "green", "implement"):
        with pytest.raises(StoreError, match="expected one of fix"):
            store.add_job(gone, ".", job_repo)


def test_unknown_kind_is_rejected(store, job_repo):
    with pytest.raises(StoreError):
        store.add_job("bulk-generate", "x", job_repo)


def test_unknown_job_is_an_error(store):
    with pytest.raises(StoreError):
        store.get_job("nope")


def test_cancel_rules(store, job_repo):
    job = store.add_job("fix", "README.md", job_repo)
    assert store.cancel_job(job.id).status == "cancelled"
    with pytest.raises(StoreError):
        store.cancel_job(job.id)
    done = store.add_job("fix", "a.py", job_repo)
    store.set_status(done.id, "done")
    with pytest.raises(StoreError):
        store.cancel_job(done.id)


def test_list_active_hides_finished(store, job_repo):
    keep = store.add_job("fix", "a.py", job_repo)
    gone = store.add_job("fix", "b.py", job_repo)
    store.cancel_job(gone.id)
    assert [j.id for j in store.list_jobs(include_finished=False)] == [keep.id]


def test_steps_are_numbered_in_order(store, job_repo):
    job = store.add_job("fix", "a.py", job_repo)
    first = store.add_step(job.id, "read code")
    store.add_step(job.id, "write tests")
    store.set_step(first, "running")
    store.set_step(first, "done", output="read 3 functions")
    steps = store.steps(job.id)
    assert [(s["seq"], s["name"], s["status"]) for s in steps] == [(1, "read code", "done"), (2, "write tests", "pending")]
    assert steps[0]["output"] == "read 3 functions"


def test_phase1_database_gains_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, sent_at TEXT, endpoint TEXT, attempt INTEGER)")
    conn.commit()
    conn.close()
    with Store(path) as s:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(requests)")}
    assert {"gate_source", "queue_at_send", "active_at_send"} <= cols


def test_interrupted_steps_become_pending(store, job_repo):
    job = store.add_job("fix", "a.py", job_repo)
    step = store.add_step(job.id, "write tests")
    store.set_step(step, "running")
    assert store.reset_interrupted_steps(job.id) == 1
    assert store.steps(job.id)[0]["status"] == "pending"


def test_request_summary(store, job_repo):
    job = store.add_job("fix", "a.py", job_repo)
    store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=503, request_id="r1", job_id=job.id))
    store.log_request(
        RequestLog(endpoint="/chat/completions", attempt=2, status_code=200, request_id="r2",
                   total_tokens=40, reasoning_tokens=7, truncated=True, job_id=job.id)
    )
    store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, total_tokens=99))
    assert store.request_summary(job.id) == {
        "requests": 2, "total_tokens": 40, "reasoning_tokens": 7, "truncated": 1, "busy": 1, "last_request_id": "r2",
    }
    assert store.request_summary()["requests"] == 3
