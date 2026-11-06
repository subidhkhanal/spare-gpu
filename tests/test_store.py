import sqlite3

import pytest

from gpu_token_utilization.store import SCHEMA, RequestLog, Store, StoreError


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".gtu" / "gtu.db") as s:
        yield s


def test_store_gitignores_its_folder(tmp_path):
    Store(tmp_path / ".gtu" / "gtu.db").close()
    assert (tmp_path / ".gtu" / ".gitignore").read_text() == "*\n"


def test_add_get_and_list_jobs(store, job_repo):
    a = store.add_job("tests", "src/a.py", job_repo)
    b = store.add_job("review", "src", job_repo, note="focus on errors")
    assert len(a.id) == 8 and a.status == "queued"
    assert store.get_job(b.id).note == "focus on errors"
    assert [j.id for j in store.list_jobs()] == [a.id, b.id]


def test_an_older_store_learns_the_fix_kind_and_keeps_its_rows(tmp_path, job_repo):
    # a store made before the fix kind existed: its CHECK constraint names only the first three
    path = tmp_path / ".gtu" / "gtu.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(SCHEMA.replace("'tests', 'docs', 'review', 'fix'", "'tests', 'docs', 'review'"))
    old.execute("INSERT INTO jobs (id, kind, target, repo, note, created_at, updated_at) VALUES "
                "('old00001', 'tests', 'src', ?, 'keep me', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')", (job_repo,))
    old.execute("INSERT INTO job_steps (job_id, seq, name, status) VALUES ('old00001', 1, 'write', 'done')")
    old.commit()
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("INSERT INTO jobs (id, kind, target, repo, created_at, updated_at) VALUES ('x', 'fix', '.', 'r', 'a', 'a')")
    old.close()
    with Store(path) as store:
        new = store.add_job("fix", ".", job_repo, note="src/a.py:3 off by one")
        assert new.kind == "fix" and store.get_job("old00001").note == "keep me"
        assert [s["name"] for s in store.steps("old00001")] == ["write"]
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "'fix'" in store.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    with Store(path) as again:  # a second open changes nothing
        assert [j.id for j in again.list_jobs()] == ["old00001", new.id]


def test_unknown_kind_is_rejected(store, job_repo):
    with pytest.raises(StoreError):
        store.add_job("bulk-generate", "x", job_repo)


def test_unknown_job_is_an_error(store):
    with pytest.raises(StoreError):
        store.get_job("nope")


def test_cancel_rules(store, job_repo):
    job = store.add_job("docs", "README.md", job_repo)
    assert store.cancel_job(job.id).status == "cancelled"
    with pytest.raises(StoreError):
        store.cancel_job(job.id)
    done = store.add_job("docs", "a.py", job_repo)
    store.set_status(done.id, "done")
    with pytest.raises(StoreError):
        store.cancel_job(done.id)


def test_list_active_hides_finished(store, job_repo):
    keep = store.add_job("tests", "a.py", job_repo)
    gone = store.add_job("tests", "b.py", job_repo)
    store.cancel_job(gone.id)
    assert [j.id for j in store.list_jobs(include_finished=False)] == [keep.id]


def test_steps_are_numbered_in_order(store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
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
    job = store.add_job("tests", "a.py", job_repo)
    step = store.add_step(job.id, "write tests")
    store.set_step(step, "running")
    assert store.reset_interrupted_steps(job.id) == 1
    assert store.steps(job.id)[0]["status"] == "pending"


def test_request_summary(store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
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
