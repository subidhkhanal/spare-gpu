"""The benchmark machinery without BharatCode: the command prompt comes from the pack's own file,
a run through the (fake) CLI is parsed, research runs are tallied from sources.json, review
findings are matched to seeds, roles are judged with the eval_models rule, and the policy keeps
the job types."""

import json
import subprocess
import sys

import pytest

from gpu_token_utilization import evalpack

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]


def test_the_prompt_is_the_packs_command_with_the_arguments_filled_in():
    prompt = evalpack.command_prompt("deep-research", "what is X --depth quick")
    assert prompt.startswith("Research request, between the markers:\n<<<what is X --depth quick>>>")
    assert "$ARGUMENTS" not in prompt and "---" not in prompt.split("\n")[0]


def test_install_builds_the_right_gtu_call_per_setup(monkeypatch):
    calls = []
    evalpack.install("lite", ["research"], gtu=["gtu"], runner=lambda args, **kw: calls.append((args, kw["env"])))
    evalpack.install("deepseek", ["review"], gtu=["gtu"], runner=lambda args, **kw: calls.append((args, kw["env"])))
    evalpack.install("split-standard-roles", ["review"], gtu=["gtu"], runner=lambda args, **kw: calls.append((args, kw["env"])))
    assert calls[0][0] == ["gtu", "integrate", "--pack", "--only", "split,research", "--lite"]
    assert calls[1][0] == ["gtu", "integrate", "--pack", "--only", "split,review"] and calls[1][1]["GTU_MODELS"] == "deepseek-v4.1-flash,deepseek-v4.1-flash"
    assert calls[2][1]["GTU_SWITCH_SAFE"] == "research-checker,review-verifier,security-verifier"
    with pytest.raises(KeyError):
        evalpack.install("nope", [], runner=lambda *a, **k: None)


def test_a_run_through_the_cli_is_parsed(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [{"tokens": 5000}, {"tokens": 1000}], "text": "Findings: src/a.py:12 off by one"}))
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(scenario))
    run = evalpack.run_command(tmp_path, "review-lead", "review medium", "split", command=FAKE_CLI)
    assert run.exit_code == 0 and run.steps == 2 and run.tokens["input"] == 5900 and run.tokens["output"] == 100
    assert run.text == "Findings: src/a.py:12 off by one" and run.error is None
    assert evalpack.findings_from_text(run.text) == {("src/a.py", 12)}
    slow = evalpack.run_command(tmp_path, "x", "y", "split", timeout=0.01, command=[sys.executable, "-c", "import time; time.sleep(2)"])
    assert slow.exit_code == -1 and "timed out" in slow.error


def test_research_runs_are_tallied_from_sources_json(tmp_path):
    folder = tmp_path / "research" / "x"
    folder.mkdir(parents=True)
    (folder / "sources.json").write_text(json.dumps({"sources": [
        {"key": "a", "doi": "https://doi.org/10.1038/NATURE14539", "verified": True},
        {"key": "b", "arxiv": "arXiv:1706.03762v5", "verified": True, "resolved": {"doi": "10.48550/arxiv.1706.03762"}},
        {"key": "c", "doi": "10.1000/fake", "verified": False},
        {"key": "d", "title": "never checked"},
    ]}), encoding="utf-8")
    (folder / "report.md").write_text("one two three", encoding="utf-8")
    tally = evalpack.research_tally(folder, ["10.1038/nature14539", "1706.03762", "10.1000/missing-key"])
    assert (tally["sources"], tally["checked"], tally["resolved"], tally["dropped"]) == (4, 3, 2, 1)
    assert tally["resolved_share"] == pytest.approx(2 / 3) and tally["key_papers_covered"] == 2 and tally["coverage"] == pytest.approx(2 / 3)
    assert tally["claims_supported_share"] is None and tally["report_words"] == 3
    assert evalpack.latest_folder(tmp_path, "research") == folder and evalpack.latest_folder(tmp_path, "nothing") is None
    assert evalpack.research_tally(tmp_path, [])["sources"] == 0


def test_review_findings_match_seeds_within_a_few_lines_and_precision_waits_for_a_person():
    found = evalpack.findings_from_text("Bugs: `src/calc.py:14` and src\\util\\x.py:3 plus docs/readme.md:9")
    assert found == {("src/calc.py", 14), ("src/util/x.py", 3), ("docs/readme.md", 9)}
    seeds = [{"file": "src/calc.py", "line": 12}, {"file": "src/util/x.py", "line": 30}]
    hits, recall = evalpack.match_seeds(found, seeds)
    assert hits == [seeds[0]] and recall == 0.5
    assert evalpack.match_seeds(found, []) == ([], 0.0)
    assert evalpack.precision([{"real": None}]) is None
    assert evalpack.precision([{"real": True}, {"real": False}, {"real": None}]) == 0.5


def test_roles_are_judged_with_the_eval_models_rule_and_written_next_to_the_job_types(tmp_path):
    assert evalpack.decide_role([(None, None), (1.0, None)])["verdict"] == "unmeasured"
    assert evalpack.decide_role([(0.0, 0.0), (0.0, 0.0)])["verdict"] == "unmeasured"
    same = evalpack.decide_role([(1.0, 1.0)] * 10)
    assert same["verdict"] == "switch-safe" and same["mean_diff"] == 0.0
    worse = evalpack.decide_role([(1.0, 0.5)] * 10)
    assert worse["verdict"] == "keep-preferred" and worse["ci95_lower"] < -0.4
    policy = tmp_path / "routing_policy.json"
    policy.write_text(json.dumps({"version": 1, "types": {"tests": "keep-preferred", "review-verifier": "unmeasured"}, "evidence": {}}), encoding="utf-8")
    written = evalpack.write_policy(policy, {"review-verifier": same, "research-checker": {"verdict": "unmeasured", "tasks": 0}})
    assert written["types"] == {"tests": "keep-preferred", "review-verifier": "switch-safe", "research-checker": "unmeasured"}
    assert written["evidence"]["review-verifier"]["tasks"] == 10 and "research-checker" not in written["evidence"]


def test_stats_delta_and_the_markdown_table():
    before = {"m": {"tokens": 100.0}}
    after = {"m": {"tokens": 350.0}, "n": {"tokens": 20.0}}
    assert evalpack.stats_delta(before, after) == {"m": 250.0, "n": 20.0} and evalpack.stats_delta(None, after) == {}
    table = evalpack.markdown_table([{"setup": "split", "recall": 0.5, "precision": None}], ["setup", "recall", "precision"])
    assert table.splitlines() == ["| setup | recall | precision |", "|---|---|---|", "| split | 0.50 |  |"]


def test_seeds_apply_and_revert_in_a_repo(git_repo):
    seed = {"file": "src/calc.py", "line": 2, "patch": "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a - b\n"}
    evalpack.apply_seed(git_repo, seed)
    assert "a - b" in (git_repo / "src" / "calc.py").read_text()
    evalpack.reset_repo(git_repo)
    assert "a + b" in (git_repo / "src" / "calc.py").read_text()
    assert subprocess.run(["git", "-C", str(git_repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
