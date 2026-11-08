"""The record of every job (.gtu/jobs/<id>/ledger.json), the RUN.md and manifest.json written next
to a job's files, and `gtu apply` / `gtu undo`: real git, the fake BharatCode Desktop, the fake API."""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import landing, ledger, worktree
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
GOOD_AND_BAD_TESTS = IMPORT_SRC + (
    "def test_adds_numbers():\n    assert add(2, 3) == 5\n\n\n"
    "def test_adds_negatives():\n    assert add(-2, -3) == -5\n\n\n"
    "def test_wrong_on_purpose():\n    assert add(2, 2) == 5\n"
)
CALC = "def add(a, b):\n    return a + b\n"
DOCUMENTED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
MINE = "def add(a, b):\n    return b + a  # my own edit, after the job started\n"
NEW_TEST = "def test_add():\n    assert True\n"
WHEN = datetime(2026, 11, 5, 14, 32, 7)
STAMP = "2026-11-05_14-32-07"
EXPORTED_AT = datetime(2026, 11, 5, 9, 2, 7, tzinfo=timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def run_jobs(store, client, fake, desktop_fake):
    def run():
        logs = []
        Scheduler(store, client, agent=desktop_fake.agent(timeout=60),
                  config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                  sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
        return logs

    return run


def ledger_of(repo, job_id) -> dict:
    return json.loads((Path(repo) / ".gtu" / "jobs" / job_id / "ledger.json").read_text(encoding="utf-8"))


def folder_of(store, job_id) -> Path:
    job = store.get_job(job_id)
    return Path(job.repo) / job.output_dir


# --- the ledger ---------------------------------------------------------------------------------

def test_a_tests_job_leaves_one_ledger_entry_per_step_with_the_tools_it_used(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS,
                                                 "src/calc.py": "def add(a, b):\n    return a - b\n"}},
                    {"tokens": 400}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    assert store.get_job(job.id).status == "done"
    doc = ledger_of(git_repo, job.id)
    assert (doc["job"], doc["kind"], doc["target"]) == (job.id, "tests", "src/calc.py")
    write, verify = doc["steps"]
    assert (write["step"], write["status"], write["tokens"]) == ("write", "done", 11900)
    assert write["model"] == "qwen-3.8-27b"  # the fake Desktop's default model, as the requests logged it
    # the tools as the agent reported them, targets inside the job's copy shown relative to it
    assert write["tools"] == ["edit tests/test_gtu_calc.py", "edit src/calc.py"]
    assert write["outcome"] == store.steps(job.id)[0]["output"]
    assert verify["step"] == "verify" and verify["tools"] == [] and verify["tokens"] == 0
    assert "Kept 2 passing test(s), dropped 1" in verify["outcome"] and "reverted src/calc.py" in verify["outcome"]
    for entry in (write, verify):
        assert entry["started"].endswith("+00:00") and entry["finished"] >= entry["started"]
    assert write["finished"] <= verify["started"]


def test_the_ledger_is_written_after_every_step_not_only_at_the_end(git_repo, store, scenario, run_jobs, monkeypatch):
    from gpu_token_utilization import jobs

    scenario(steps=[{"tokens": 500, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS}}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    monkeypatch.setattr(jobs, "_run_pytest", lambda ctx, path: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):  # the process dies during verify
        run_jobs()
    doc = ledger_of(git_repo, job.id)
    assert [e["step"] for e in doc["steps"]] == ["write"] and doc["steps"][0]["tokens"] == 500


def test_a_review_step_is_recorded_with_its_tokens_and_no_tools(git_repo, store, scenario, run_jobs, fake):
    fake.queue(Reply(content="## Likely bugs\nNothing found.", prompt_tokens=300, completion_tokens=40))
    scenario()
    job = store.add_job("review", "src", str(git_repo))
    run_jobs()
    (entry,) = ledger_of(git_repo, job.id)["steps"]
    assert (entry["step"], entry["status"], entry["tokens"], entry["tools"]) == ("review", "done", 340, [])
    assert entry["model"] == "qwen-3.8-27b" and entry["outcome"].startswith("Wrote gtu-review-")


def test_a_failed_step_is_recorded_too(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 100}], error="Unexpected server error")
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    assert store.get_job(job.id).status == "failed"
    (entry,) = ledger_of(git_repo, job.id)["steps"]
    assert entry["status"] == "failed" and "Unexpected server error" in entry["outcome"] and entry["tokens"] == 100


def test_relative_tools_only_touch_targets_inside_the_copy(tmp_path):
    copy = tmp_path / "copy"
    tools = [f"edit {copy / 'src' / 'a.py'}", "bash python -m pytest tests/test_a.py", f"read {tmp_path / 'other.txt'}", "", "list"]
    assert ledger.relative_tools(tools, copy) == ["edit src/a.py", "bash python -m pytest tests/test_a.py",
                                                  f"read {tmp_path / 'other.txt'}", "list"]
    assert ledger.relative_tools(["edit /x"], None) == ["edit /x"]


# --- RUN.md and manifest.json next to the files -------------------------------------------------

def test_the_manifest_hashes_match_the_files_and_their_base_versions(git_repo, store, scenario, run_jobs):
    (git_repo / "README.md").write_text("# Sample\n\nIntro line.\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "readme")
    scenario(steps=[{"tokens": 11000, "writes": {
        "src/calc.py": DOCUMENTED,
        "README.md": "# Sample\n\nIntro line.\n\n## calc\n\nAdds numbers.\n",
        "docs/calc.md": "# calc\n",
    }}])
    job = store.add_job("docs", "src", str(git_repo))
    run_jobs()
    folder = folder_of(store, job.id)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["job"], manifest["kind"], manifest["target"], manifest["tokens"]) == (job.id, "docs", "src", 11000)
    assert manifest["model"] == "qwen-3.8-27b" and manifest["exported_at"].endswith("+00:00")
    by_path = {f["path"]: f for f in manifest["files"]}
    assert set(by_path) == {"README.md", "docs/calc.md", "src/calc.py"}
    assert by_path["src/calc.py"]["change"] == "M"
    # Hashes are of the files' real bytes: on Windows the test repo's files have CRLF endings, and a job
    # keeps a file's own endings, so compare bytes for the hash and the text without the endings.
    base, new = (git_repo / "src" / "calc.py").read_bytes(), (folder / "src" / "calc.py").read_bytes()
    assert by_path["src/calc.py"]["base_sha256"] == sha(base) and base.replace(b"\r\n", b"\n") == CALC.encode()
    assert by_path["src/calc.py"]["new_sha256"] == sha(new) and new.replace(b"\r\n", b"\n") == DOCUMENTED.encode()
    assert by_path["docs/calc.md"] == {"path": "docs/calc.md", "change": "A", "base_sha256": None,
                                       "new_sha256": sha((folder / "docs" / "calc.md").read_bytes())}
    readme = (git_repo / "README.md").read_bytes()
    assert by_path["README.md"]["base_sha256"] == sha(readme) and readme.replace(b"\r\n", b"\n") == b"# Sample\n\nIntro line.\n"
    # the records sit next to the job's files and are never counted as its output
    assert sorted(p.name for p in folder.iterdir()) == ["README.md", "RUN.md", "docs", "manifest.json", "src"]
    assert landing.exported_files(folder) == ["README.md", "docs/calc.md", "src/calc.py"]
    assert git(git_repo, "status", "--porcelain") == ""  # still nothing of spare-gpu's shows in git


def test_run_md_summarises_the_job_its_steps_its_files_and_what_was_reverted(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS,
                                                 "src/calc.py": "def add(a, b):\n    return a - b\n"}},
                    {"tokens": 400}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    folder = folder_of(store, job.id)
    text = (folder / "RUN.md").read_text(encoding="utf-8")
    assert text.startswith(f"# spare-gpu job {job.id}: tests `src/calc.py`\n")
    assert "- Kind: tests\n- Target: `src/calc.py`\n" in text and "- Tokens: 11,900\n" in text
    assert "- Files: 1 added, 0 changed, 0 deleted" in text
    # both of the fake Desktop session's steps happen inside the job's one write step, hence 11,900 there
    assert "| write | done | qwen-3.8-27b |" in text and "| 11,900 | edit tests/test_gtu_calc.py; edit src/calc.py |" in text
    assert "| verify | done | qwen-3.8-27b |" in text and "| 0 | none |" in text
    assert "- `tests/test_gtu_calc.py` (added): 2 passing test(s), dropped 1 failing" in text
    assert "## Reverted by the checks\n\n- `src/calc.py`: tests jobs only add new test files" in text
    assert f"`gtu apply {folder.name}`" in text and f"`gtu undo {folder.name}`" in text
    assert "\u2014" not in text  # plain punctuation only


# --- gtu apply and gtu undo ---------------------------------------------------------------------

def snapshot(repo: Path) -> dict[str, bytes]:
    """Every file of the user's project, byte for byte (spare-gpu's own folders left out)."""
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in Path(repo).rglob("*")
            if p.is_file() and p.relative_to(repo).parts[0] not in (".git", ".gtu", "spare-gpu")}


def finished(repo: Path, job_id: str, edits, kind="fix", target=".", model="deepseek-v4.1-flash", steps=()):
    """A job that made `edits(copy)` in its copy, exported with its records, its copy removed."""
    copy = worktree.create(repo, job_id)
    edits(copy)
    worktree.commit(copy, "job work")
    folder = worktree.export(repo, job_id, None, WHEN)
    job = SimpleNamespace(id=job_id, kind=kind, target=target, model=model)
    landing.write_records(folder, copy, job, base=None, ledger_doc=ledger.empty(), steps=list(steps),
                          tokens=1234, exported_at=EXPORTED_AT)
    worktree.remove(repo, job_id)
    return folder


@pytest.fixture
def result(git_repo):
    """A finished fix job: it changed src/calc.py and win.txt (CRLF), added tests/test_calc.py and
    deleted old.txt."""
    (git_repo / "old.txt").write_bytes(b"old\n")  # bytes: write_text would make it CRLF on Windows
    (git_repo / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "more files")

    def edits(copy):
        (copy / "src" / "calc.py").write_text(DOCUMENTED)
        (copy / "tests").mkdir()
        (copy / "tests" / "test_calc.py").write_text(NEW_TEST)
        (copy / "old.txt").unlink()
        (copy / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    return finished(git_repo, "job1", edits)


def test_the_manifest_records_a_deletion_and_apply_carries_it_out(git_repo, result):
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    old = next(f for f in manifest["files"] if f["path"] == "old.txt")
    assert old == {"path": "old.txt", "change": "D", "base_sha256": sha(b"old\n"), "new_sha256": None}
    assert not (result / "old.txt").exists()  # nothing to copy for a deletion
    landing.apply(git_repo, STAMP)
    assert not (git_repo / "old.txt").exists()
    assert (result / ".undo" / "old.txt").read_bytes() == b"old\n"


def test_apply_copies_the_files_in_and_saves_what_it_replaced(git_repo, result):
    outcome = landing.apply(git_repo, "latest")
    assert (git_repo / "src" / "calc.py").read_text() == DOCUMENTED
    assert (git_repo / "tests" / "test_calc.py").read_text() == NEW_TEST
    assert (git_repo / "win.txt").read_bytes() == b"one\r\ntwo\r\nthree\r\n"  # bytes copied, no line-ending translation
    assert not (git_repo / "old.txt").exists()
    undo = result / ".undo"
    assert (undo / "src" / "calc.py").read_text() == CALC and (undo / "win.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert not (undo / "tests").exists()  # a new file had nothing to save
    record = json.loads((undo / "manifest.json").read_text(encoding="utf-8"))
    assert record["job"] == "job1" and record["stamp"] == STAMP
    assert {f["path"]: f["previous"] for f in record["files"]} == {"src/calc.py": True, "tests/test_calc.py": False,
                                                                   "old.txt": True, "win.txt": True}
    assert outcome.counts == {"applied": 4, "skipped": 0, "unchanged": 0}
    assert "replaced: src/calc.py" in outcome.lines and "added: tests/test_calc.py" in outcome.lines \
        and "deleted: old.txt" in outcome.lines
    assert outcome.lines[-1].startswith("Applied 4 file(s) from spare-gpu/2026-11-05_14-32-07") \
        and outcome.lines[-1].endswith(f"gtu undo {STAMP}")
    assert "spare-gpu" not in git(git_repo, "status", "--porcelain")  # the folder still ignores itself


def test_apply_skips_a_file_that_changed_since_the_job_started(git_repo, result):
    (git_repo / "src" / "calc.py").write_text(MINE)
    outcome = landing.apply(git_repo, "latest")
    assert (git_repo / "src" / "calc.py").read_text() == MINE  # left alone
    assert "skipped: src/calc.py (it changed since the job started; add --force to replace it anyway)" in outcome.lines
    assert outcome.counts == {"applied": 3, "skipped": 1, "unchanged": 0}
    assert not (result / ".undo" / "src").exists() and (git_repo / "tests" / "test_calc.py").exists()
    # a file the job added that the user has since made themselves is skipped too
    other = finished(git_repo, "job2", lambda copy: ((copy / "docs").mkdir(), (copy / "docs" / "mine.md").write_text("job's\n")))
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "mine.md").write_text("mine\n")
    outcome = landing.apply(git_repo, other.name)
    assert "skipped: docs/mine.md (it exists already; add --force to replace it anyway)" in outcome.lines
    assert (git_repo / "docs" / "mine.md").read_text() == "mine\n" and not (other / ".undo").exists()
    assert outcome.lines[-1].startswith("Nothing was applied")


def test_force_applies_anyway_and_still_saves_the_undo_copy(git_repo, result):
    (git_repo / "src" / "calc.py").write_text(MINE)
    outcome = landing.apply(git_repo, "latest", force=True)
    assert (git_repo / "src" / "calc.py").read_text() == DOCUMENTED
    assert "replaced: src/calc.py (forced: it had changed since the job started)" in outcome.lines
    assert (result / ".undo" / "src" / "calc.py").read_text() == MINE
    landing.undo(git_repo, "latest")
    assert (git_repo / "src" / "calc.py").read_text() == MINE


def test_dry_run_says_what_would_happen_and_writes_nothing(git_repo, result):
    (git_repo / "src" / "calc.py").write_text(MINE)
    before = snapshot(git_repo)
    outcome = landing.apply(git_repo, "latest", dry_run=True)
    assert snapshot(git_repo) == before and not (result / ".undo").exists()
    assert "would add: tests/test_calc.py" in outcome.lines and "would delete: old.txt" in outcome.lines \
        and "would replace: win.txt" in outcome.lines
    assert "skipped: src/calc.py (it changed since the job started; add --force to replace it anyway)" in outcome.lines
    assert outcome.lines[-1] == "Dry run: would apply 3 file(s) from spare-gpu/2026-11-05_14-32-07, skip 1. Nothing was written."
    landing.apply(git_repo, "latest")  # a dry run doesn't count as an apply


def test_apply_twice_is_refused_until_undo(git_repo, result):
    landing.apply(git_repo, "latest")
    with pytest.raises(landing.LandingError, match=f"already applied.*gtu undo {STAMP}"):
        landing.apply(git_repo, "latest")
    with pytest.raises(landing.LandingError, match="already applied"):
        landing.apply(git_repo, "latest", dry_run=True)
    landing.undo(git_repo, "latest")
    assert landing.apply(git_repo, "latest").counts["applied"] == 4


def test_undo_restores_exactly_and_removes_the_files_apply_added(git_repo, result):
    before = snapshot(git_repo)
    landing.apply(git_repo, STAMP)
    assert snapshot(git_repo) != before
    outcome = landing.undo(git_repo, STAMP)
    assert snapshot(git_repo) == before  # byte for byte, old.txt back, win.txt's CRLF back
    assert not (git_repo / "tests").exists() or not (git_repo / "tests" / "test_calc.py").exists()
    assert not (result / ".undo").exists()
    assert outcome.counts == {"restored": 3, "removed": 1}
    assert "restored: src/calc.py" in outcome.lines and "removed: tests/test_calc.py" in outcome.lines
    assert outcome.lines[-1].endswith(f"gtu apply {STAMP}")
    assert git(git_repo, "status", "--porcelain") == ""
    with pytest.raises(landing.LandingError, match="nothing to undo"):
        landing.undo(git_repo, "latest")


def test_undo_says_when_a_file_was_edited_after_apply(git_repo, result):
    landing.apply(git_repo, "latest")
    (git_repo / "src" / "calc.py").write_text(MINE)
    outcome = landing.undo(git_repo, "latest")
    assert "restored: src/calc.py (it had been edited after apply; that edit is gone)" in outcome.lines
    assert (git_repo / "src" / "calc.py").read_text() == CALC


def test_a_file_already_as_the_job_left_it_is_reported_unchanged(git_repo, result):
    (git_repo / "src" / "calc.py").write_text(DOCUMENTED)
    outcome = landing.apply(git_repo, "latest")
    assert "unchanged: src/calc.py (already as the job left it)" in outcome.lines
    assert outcome.counts == {"applied": 3, "skipped": 0, "unchanged": 1}
    landing.undo(git_repo, "latest")
    assert (git_repo / "src" / "calc.py").read_text() == DOCUMENTED  # not apply's doing, so not undone


@pytest.mark.parametrize("bad", ["../evil.txt", "/etc/evil", "C:\\evil.txt", "C:/evil.txt", "src/../../evil.txt",
                                 "\\\\server\\share\\x", "file.txt:stream", ""])
def test_paths_outside_the_project_are_refused_whole(git_repo, result, bad):
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].insert(0, {"path": bad, "change": "A", "base_sha256": None, "new_sha256": "x"})
    (result / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = snapshot(git_repo)
    with pytest.raises(landing.LandingError, match="outside the project|empty path"):
        landing.apply(git_repo, "latest")
    assert snapshot(git_repo) == before and not (result / ".undo").exists()  # nothing at all was applied
    assert not (git_repo.parent / "evil.txt").exists()


def test_latest_means_the_newest_folder_with_a_manifest_and_names_are_taken_in_any_form(git_repo, result):
    older = git_repo / "spare-gpu" / "2026-11-01_08-00-00"
    older.mkdir()
    (older / "x.txt").write_text("from an older spare-gpu, no manifest\n")
    (git_repo / "spare-gpu" / "2026-11-09_08-00-00").mkdir()  # no manifest either
    assert landing.find(git_repo, "latest") == result == landing.find(git_repo, None)
    assert landing.find(git_repo, f"spare-gpu/{STAMP}/") == result == landing.find(git_repo, str(result))
    with pytest.raises(landing.LandingError, match="no folder spare-gpu/2026-01-01_00-00-00 in .*The newest are:"):
        landing.find(git_repo, "2026-01-01_00-00-00")
    with pytest.raises(landing.LandingError, match="has no manifest.json"):
        landing.apply(git_repo, "2026-11-01_08-00-00")
    with pytest.raises(landing.LandingError, match="nothing has been applied"):
        landing.undo(git_repo, "latest")


def test_the_commands_work_from_a_subfolder_of_the_project(git_repo, result, monkeypatch):
    monkeypatch.chdir(git_repo / "src")
    runner = CliRunner()
    dry = runner.invoke(app, ["apply", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would replace: src/calc.py" in dry.output and (git_repo / "src" / "calc.py").read_text() == CALC
    done = runner.invoke(app, ["apply", "latest"])
    assert done.exit_code == 0, done.output
    assert (git_repo / "src" / "calc.py").read_text() == DOCUMENTED and f"gtu undo {STAMP}" in done.output
    again = runner.invoke(app, ["apply", STAMP])
    assert again.exit_code == 1 and f"gtu undo {STAMP}" in again.output
    back = runner.invoke(app, ["undo"])
    assert back.exit_code == 0, back.output
    assert (git_repo / "src" / "calc.py").read_text() == CALC and not (result / ".undo").exists()
    nothing = runner.invoke(app, ["undo"])
    assert nothing.exit_code == 1 and "nothing to undo" in nothing.output


def test_apply_works_in_a_plain_folder_from_a_subfolder(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "calc.py").write_text(CALC)
    finished(plain, "j", lambda copy: (copy / "src" / "calc.py").write_text(DOCUMENTED), kind="docs", target="src", model=None)
    assert landing.find_root(plain / "src") == plain.resolve()
    monkeypatch.chdir(plain / "src")
    result = CliRunner().invoke(app, ["apply"])
    assert result.exit_code == 0, result.output
    assert (plain / "src" / "calc.py").read_text() == DOCUMENTED and not (plain / ".git").exists()


def test_a_job_that_only_deleted_a_file_still_gets_a_folder_with_the_deletion_on_record(git_repo):
    copy = worktree.create(git_repo, "del")
    (copy / "src" / "calc.py").unlink()
    worktree.commit(copy, "delete")
    assert worktree.changes(copy, None) == [("D", "src/calc.py")]
    assert worktree.base_bytes(copy, None, "src/calc.py") == (git_repo / "src" / "calc.py").read_bytes()
    folder = worktree.export(git_repo, "del", None, WHEN)
    assert folder is not None and landing.exported_files(folder) == []
    assert worktree.changed_files(copy, None) == []  # nothing to copy, as before


def test_the_report_reads_the_manifest(git_repo, store, scenario, run_jobs):
    from gpu_token_utilization import report

    scenario(steps=[{"tokens": 1000, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS}}, {"tokens": 100}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    assert report.build_job(store, store.get_job(job.id)).files == [("A", "tests/test_gtu_calc.py")]
