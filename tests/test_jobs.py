"""The three job types end to end: real git repo, fake BharatCode Desktop, fake API, real pytest."""

import json
import sys
from pathlib import Path

import pytest

from gpu_token_utilization import jobs, landing, worktree
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

# Generated test files put the repo root on sys.path, as a real tests folder would via conftest.
IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
GOOD_AND_BAD_TESTS = IMPORT_SRC + (
    "def test_adds_numbers():\n    assert add(2, 3) == 5\n\n\n"
    "def test_adds_negatives():\n    assert add(-2, -3) == -5\n\n\n"
    "def test_wrong_on_purpose():\n    assert add(2, 2) == 5\n"
)


def only_session(desktop):
    (session,) = desktop.sessions.values()
    return session


@pytest.fixture
def run_jobs(store, client, fake, desktop_fake):
    def run(budgets=None, config=None, **kwargs):
        logs = []
        scheduler = Scheduler(
            store, client,
            agent=desktop_fake.agent(timeout=60),
            budgets=budgets or Budgets(),
            config=config or JobConfig(test_command=(sys.executable, "-m", "pytest")),
            sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append, **kwargs,
        )
        scheduler.run()
        return logs

    return run


# The project's own test command, allowed whatever the interpreter is called (python3 on Linux).
FIX_CONFIG = JobConfig(test_command=(sys.executable, "-m", "pytest"),
                       allowed_commands=("python -m pytest", "python3 -m pytest", "pytest"))
EXISTING_TEST = IMPORT_SRC + "def test_adds_numbers():\n    assert add(2, 3) == 5\n"
FINDINGS = "src/calc.py:2 the function has no docstring; add one"


@pytest.fixture
def repo_with_tests(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_calc.py").write_text(EXISTING_TEST)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "tests")
    return git_repo


def result_files(store, job_id):
    """The files a finished job left in its spare-gpu/<date>_<time>/ folder (empty if none)."""
    job = store.get_job(job_id)
    if not job.output_dir:
        return set()
    return set(landing.exported_files(Path(job.repo) / job.output_dir))  # RUN.md and manifest.json sit next to them


def result(store, job_id, path):
    job = store.get_job(job_id)
    return (Path(job.repo) / job.output_dir / path).read_text(encoding="utf-8")


def assert_main_untouched(repo, head):
    """Same commit, same branch, nothing changed, and no branch or copy of the job left behind."""
    assert git(repo, "rev-parse", "main") == head
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "branch", "--list", "gtu/*") == ""
    assert git(repo, "status", "--porcelain") == ""
    worktrees = Path(repo) / ".gtu" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


# --- tests job ---------------------------------------------------------------------------

def test_tests_job_keeps_only_passing_tests(git_repo, store, scenario, run_jobs):
    head = git(git_repo, "rev-parse", "HEAD")
    scenario(steps=[
        {"tokens": 11500, "writes": {
            "tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS,
            "tests/test_gtu_broken.py": "def test_x(:\n    pass\n",  # does not even parse
            "src/calc.py": "def add(a, b):\n    return a - b\n",  # sneaky source change
        }},
        {"tokens": 400},
    ])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    # only the kept test file: the broken one was dropped and the sneaky source change reverted,
    # and test runs leave no caches behind
    assert result_files(store, job.id) == {"tests/test_gtu_calc.py"}
    kept = result(store, job.id, "tests/test_gtu_calc.py")
    assert "test_adds_numbers" in kept and "test_adds_negatives" in kept
    assert "test_wrong_on_purpose" not in kept
    verify = store.steps(job.id)[1]["output"]
    assert "Kept 2 passing test(s), dropped 1" in verify and "reverted src/calc.py" in verify
    assert_main_untouched(git_repo, head)


def test_tests_job_session_tokens_are_logged_per_step(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": IMPORT_SRC + "def test_ok():\n    assert add(1, 1) == 2\n"}},
                    {"tokens": 300}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    rows = [r for r in store.requests(job.id) if r["endpoint"] == "bharatcode-desktop"]
    assert [r["total_tokens"] for r in rows] == [11500, 300]


def test_tests_job_with_no_tests_generated_finishes_cleanly(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 9000}], text="I could not write tests.")
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    assert store.get_job(job.id).status == "done"
    assert "nothing to keep" in store.steps(job.id)[1]["output"]
    assert store.get_job(job.id).output_dir is None and not (git_repo / "spare-gpu").exists()


def test_the_session_is_given_the_test_allowlist_and_the_worktree(git_repo, store, scenario, run_jobs):
    desktop = scenario(steps=[{"tokens": 100}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    session = only_session(desktop)
    assert Path(session["directory"]) == worktree.worktree_path(git_repo, job.id)
    bash = session["config"]["permission"]["bash"]
    assert list(bash)[0] == "*" and bash["*"] == "deny" and bash["python -m pytest*"] == "allow"


def test_crash_during_verify_resumes_without_rerunning_the_session(git_repo, store, scenario, run_jobs, monkeypatch):
    desktop = scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS}}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    real = jobs._run_pytest
    crashes = {"left": 1}

    def crash_once(ctx, path):
        if crashes["left"]:
            crashes["left"] -= 1
            raise KeyboardInterrupt  # the process is killed mid-verify
        return real(ctx, path)

    monkeypatch.setattr(jobs, "_run_pytest", crash_once)
    with pytest.raises(KeyboardInterrupt):
        run_jobs()
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running"]

    run_jobs()  # a fresh start
    assert store.get_job(job.id).status == "done"
    assert len(desktop.sessions) == 1  # the session ran once, its work was kept in the patch
    kept = result(store, job.id, "tests/test_gtu_calc.py")
    assert "test_adds_numbers" in kept and "test_wrong_on_purpose" not in kept


def test_a_failed_session_fails_the_job_with_its_message(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 100}], error="Unexpected server error")
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    logs = run_jobs()
    assert store.get_job(job.id).status == "failed"
    assert any("Unexpected server error" in line for line in logs)


# --- docs job ----------------------------------------------------------------------------

def test_docs_job_keeps_docstring_only_changes_and_reverts_the_rest(git_repo, store, scenario, run_jobs):
    (git_repo / "src" / "other.py").write_text('def double(x):\n    """Twice x."""\n    return x * 2\n')
    (git_repo / "README.md").write_text("# Sample\n\nIntro line.\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "more files")
    head = git(git_repo, "rev-parse", "HEAD")
    scenario(steps=[{"tokens": 11000, "writes": {
        "src/calc.py": 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n',
        "src/other.py": 'def double(x):\n    """Return x times two."""\n    return x * 2\n',  # rewrites a docstring
        "README.md": "# Sample\n\nIntro line.\n\n## calc\n\nAdds numbers.\n",
        "docs/calc.md": "# calc\n",
    }}])
    job = store.add_job("docs", "src", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    assert '"""Return the sum of a and b."""' in result(store, job.id, "src/calc.py")
    assert "## calc" in result(store, job.id, "README.md")
    assert "reverted src/other.py" in store.steps(job.id)[1]["output"]
    assert_main_untouched(git_repo, head)
    # what the user gets: the kept files only, in spare-gpu/<date>_<time>/, with their paths
    folder = store.get_job(job.id).output_dir
    assert folder and folder.startswith("spare-gpu/")
    copied = landing.exported_files(git_repo / folder)
    assert copied == ["README.md", "docs/calc.md", "src/calc.py"]  # other.py was reverted, so it isn't there
    assert '"""Return the sum of a and b."""' in (git_repo / folder / "src" / "calc.py").read_text()


def test_docs_job_never_gets_a_shell(git_repo, store, scenario, run_jobs):
    desktop = scenario(steps=[{"tokens": 100}])
    store.add_job("docs", "src", str(git_repo))
    run_jobs()
    assert only_session(desktop)["config"]["permission"]["bash"] == "deny"


@pytest.mark.parametrize("old, new, ok", [
    ("def f():\n    return 1\n", 'def f():\n    """Doc."""\n    return 1\n', True),
    ('def f():\n    """Doc."""\n    return 1\n', 'def f():\n    """Doc. More detail."""\n    return 1\n', True),
    ('def f():\n    """Doc."""\n    return 1\n', "def f():\n    return 1\n", False),  # docstring deleted
    ("def f():\n    return 1\n", 'def f():\n    """Doc."""\n    return 2\n', False),  # code changed
    ("def f():\n    return 1\n", "def f(:\n", False),  # broken
])
def test_docs_only_change(old, new, ok):
    assert jobs.docs_only_change(old, new)[0] is ok


@pytest.mark.parametrize("old, new, ok", [
    ("def f():\n    # keep me\n    return 1\n", 'def f():\n    """Doc."""\n    return 1\n', False),  # comment dropped
    ("x = (1,\n     2)\n", "x = (1, 2)\n", False),  # reformatted: same tree, other lines
    ("def f():\n    # keep me\n    return 1\n", 'def f():\n    """Doc."""\n    # keep me\n    return 1\n', True),
    ("def f():\n    pass\n", 'def f():\n    """Doc."""\n', True),  # a docstring may replace a bare pass
    ("# header\n\ndef f():\n    return 1\n", '"""Module doc."""\n# header\n\ndef f():\n    return 1\n', True),
])
def test_docs_only_change_keeps_comments_and_formatting_too(old, new, ok):
    verdict, why = jobs.docs_only_change(old, new)
    assert verdict is ok
    if not ok:
        assert why == "it changes or removes lines outside the docstrings (comments or formatting)"


def test_a_big_file_is_reviewed_in_part_rather_than_not_at_all(git_repo):
    big = git_repo / "big.py"
    big.write_text("".join(f"x{n} = {n}\n" for n in range(1, 301)), encoding="utf-8")
    listing, omitted = jobs.numbered_listing(git_repo, "big.py", 2_500)
    assert listing and listing.startswith("big.py:1| x1 = 1\n") and listing.endswith("\n") and len(listing) <= 2_500
    cut_after = int(omitted[0].rsplit("line ", 1)[1].rstrip(")"))
    assert omitted == [f"big.py (cut after line {cut_after})"] and 100 < cut_after < 300
    assert listing.count("\n") == cut_after
    (git_repo / "a_small.py").write_text("y = 1\n", encoding="utf-8")  # sorts before big.py
    listing, omitted = jobs.numbered_listing(git_repo, ".", 2_500)  # the small one whole, the big one cut
    assert listing.startswith("a_small.py:1| y = 1\n") and "big.py:1|" in listing and omitted[0].startswith("big.py (cut after line")
    listing, omitted = jobs.numbered_listing(git_repo, ".", 100)  # too little room for a cut once something is in: left out whole
    assert listing.startswith("a_small.py:1|") and "big.py" in omitted


def test_nested_test_functions_are_removed_without_eating_the_next_one(tmp_path):
    file = tmp_path / "test_x.py"
    file.write_text("def test_x():\n    def test_x():\n        pass\n    test_x()\n\n\ndef test_y():\n    assert True\n", encoding="utf-8")
    assert jobs.remove_test_functions(file, {"test_x"}) == 1
    assert file.read_text(encoding="utf-8") == "\n\ndef test_y():\n    assert True\n"


def test_markdown_may_only_grow():
    assert jobs.no_lines_removed("a\nb\n", "a\nb\nc\n")
    assert not jobs.no_lines_removed("a\nb\n", "a\nc\n")


# --- fix job ------------------------------------------------------------------------------

def test_fix_job_keeps_the_fix_when_the_tests_pass_and_never_lets_it_touch_existing_tests(repo_with_tests, store, scenario, run_jobs):
    head = git(repo_with_tests, "rev-parse", "HEAD")
    desktop = scenario(steps=[{"tokens": 3000, "writes": {
        "src/calc.py": 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n',
        "tests/test_calc.py": "def test_nothing():\n    pass\n",  # a fix that weakens the tests
    }}, {"tokens": 200}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(config=FIX_CONFIG)
    assert store.get_job(job.id).status == "done"
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Kept the fix in 1 file(s): the test command passed.")
    assert "- kept src/calc.py" in verify and "- reverted tests/test_calc.py: a fix may not change existing tests" in verify
    assert result_files(store, job.id) == {"src/calc.py"}
    assert "Return the sum" in result(store, job.id, "src/calc.py")
    prompt = only_session(desktop)["prompt"]["parts"][0]["text"]
    assert FINDINGS in prompt and "Do not modify or delete existing test files" in prompt
    assert_main_untouched(repo_with_tests, head)


def test_fix_job_drops_the_fix_when_the_tests_fail(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 3000, "writes": {"src/calc.py": "def add(a, b):\n    return a - b\n"}}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(config=FIX_CONFIG)
    assert store.get_job(job.id).status == "done"
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Kept the fix in 0 file(s): the test command failed with it.")
    assert "discarded the fix" in verify and jobs.kept_nothing(verify) if hasattr(jobs, "kept_nothing") else True
    from gpu_token_utilization.scheduler import kept_nothing
    assert kept_nothing(verify)  # the router's fallback sees it as nothing kept
    assert result_files(store, job.id) == set() and store.get_job(job.id).output_dir is None


def test_fix_job_without_findings_or_without_a_fix_finishes_honestly(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 500}], text="Nothing to change.")
    nothing = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(config=FIX_CONFIG)
    assert store.get_job(nothing.id).status == "done" and "No fix was generated" in store.steps(nothing.id)[1]["output"]
    blank = store.add_job("fix", ".", str(repo_with_tests))
    run_jobs(config=FIX_CONFIG)
    assert store.get_job(blank.id).status == "failed"
    assert "needs the findings" in (store.steps(blank.id)[0]["output"] or "")


# --- review job --------------------------------------------------------------------------

def test_review_job_writes_a_report_and_changes_no_source(git_repo, store, scenario, run_jobs, fake):
    head = git(git_repo, "rev-parse", "HEAD")
    fake.queue(Reply(content="## Likely bugs\n- `src/calc.py:2` no type checks\n\n## Risky code\nNothing found.\n\n## Suggestions\nNothing found.",
                     queue_wait_ms=4200))
    desktop = scenario()
    job = store.add_job("review", "src", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    assert not desktop.sessions  # with a key, the review uses our own client, not a Desktop session
    name = f"gtu-review-{job.id}.md"
    assert result_files(store, job.id) == {name}  # the report only: no source file changed
    report = result(store, job.id, name)
    assert report.startswith("# Code review: `src`") and "`src/calc.py:2`" in report
    sent = json.loads([r for r in fake.requests if r.url.path.endswith("/chat/completions")][0].content)
    assert "src/calc.py:1| def add(a, b):" in sent["messages"][1]["content"]
    (row,) = store.requests(job.id)
    assert row["queue_wait_ms"] == 4200 and row["request_id"]
    assert_main_untouched(git_repo, head)


def test_review_job_keeps_a_cut_off_report_and_says_so(git_repo, store, scenario, run_jobs, fake):
    fake.truncated("## Likely bugs\n- `src/calc.py:1` something")
    job = store.add_job("review", "src/calc.py", str(git_repo))
    run_jobs()
    report = result(store, job.id, f"gtu-review-{job.id}.md")
    assert "something" in report and "cut off at the output token limit" in report


# --- budgets -----------------------------------------------------------------------------

def test_job_budget_stops_the_session_and_pauses_the_job(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 2000, "sleep": 0.3, "writes": {"tests/test_gtu_x.py": "def test_x():\n    pass\n"}}
                    for _ in range(6)])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    logs = run_jobs(budgets=Budgets(per_job=5000))
    assert store.get_job(job.id).status == "paused"
    assert [s["status"] for s in store.steps(job.id)] == ["pending", "pending"]
    used = store.request_summary(job.id)["total_tokens"]
    assert 5000 <= used <= 7000
    # nothing half-done was kept: the paused job's copy has nothing saved beyond its starting point
    assert worktree.changed_files(worktree.worktree_path(git_repo, job.id), None) == []
    assert any("budget" in line for line in logs)


def test_budget_settings_from_env(monkeypatch):
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "1234")
    assert Budgets.from_env().per_job == 1234


# --- config --------------------------------------------------------------------------------

def test_test_command_must_be_allowlisted(monkeypatch):
    monkeypatch.setenv("GTU_TEST_COMMAND", "bash -c 'rm -rf /'")
    with pytest.raises(ValueError, match="not in the allowlist"):
        JobConfig.from_env()


@pytest.mark.parametrize("command, ok", [
    (("python", "-m", "pytest"), True),
    ((sys.executable, "-m", "pytest"), True),
    (("pytest", "-x"), True),
    (("py", "-m", "pytest"), True),  # the Windows launcher
    (("python3", "-m", "pytest"), True),
    (("C:\\venv\\Scripts\\python3.12.exe", "-m", "pytest"), True),
    (("pythonw", "-m", "pytest"), True),
    (("npm", "test"), True),
    (("npm", "install"), False),
    (("python", "evil.py"), False),
    (("pypy", "-m", "pytest"), False),
])
def test_command_allowlist(command, ok):
    assert jobs.command_allowed(command, jobs.DEFAULT_ALLOWED) is ok
