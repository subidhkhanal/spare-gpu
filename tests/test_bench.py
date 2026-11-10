"""The improvement loop's benchmarks. The replay ones run on the recorded material in benchmarks/
and must not fall below the bar recorded in research/improve/<target>/bar.json: a change that makes
verify_refs judge references worse, collect_sources group sources worse, or /auto's routing pick
worse fails here. The live ones run against fake Desktop agents."""

import json
from pathlib import Path

import pytest

from gpu_token_utilization import bench, improve, refs
from gpu_token_utilization.agent import AgentResult

QWEN, DEEPSEEK = "qwen-3.8-27b", "deepseek-v4.1-flash"


# --- the replay benchmarks hold their bar -------------------------------------------------------------

@pytest.mark.parametrize("name", ["verify-refs", "collect-sources", "router"])
def test_each_replay_benchmark_holds_its_recorded_bar(name):
    bar = improve.bar(name)
    assert bar is not None, f"record one: python scripts/improve.py {name} bar"
    t = improve.target(name)
    score = bench.collect_sources() if t.kind == "code" else improve.score_params(t, improve.current_settings(t))
    assert score.value >= bar["value"] - 1e-9, (name, score.value, bar["value"])


def test_the_reference_benchmark_has_labelled_real_and_bad_references():
    entries = json.loads((bench.BENCH_DIR / "verify-refs" / "references.json").read_text(encoding="utf-8"))
    labels = {e["label"] for e in entries}
    assert len(entries) >= 100 and {"keep", "drop"} <= labels
    assert sum(e.get("labelled_by") == "person" for e in entries) >= 10  # the hard cases a person decided


def test_verify_refs_scoring_compares_each_verdict_with_its_label(tmp_path):
    paper = {"title": "Deep learning", "authors": ["Yann LeCun", "Yoshua Bengio"], "year": 2015, "doi": "10.1038/nature14539"}
    data = tmp_path / "refs.json"
    data.write_text(json.dumps([
        {"ref": {"key": "real", "title": "Deep learning", "doi": "10.1038/nature14539"}, "recorded": {"paper": paper}, "label": "keep"},
        {"ref": {"key": "fake", "title": "Quantum swarms", "doi": "10.1000/fake"}, "recorded": {}, "label": "drop"},
        {"ref": {"key": "busy", "title": "A page", "url": "https://x.org/p"}, "recorded": {"page": {"status": 429}}, "label": "unchecked"},
        {"ref": {"key": "wrong", "title": "Deep learning", "doi": "10.1038/nature14539"}, "recorded": {"paper": paper}, "label": "drop"},
    ]), encoding="utf-8")
    score = bench.verify_refs(data=data)
    assert score.value == 0.75 and score.detail["bad_kept"] == 1 and score.detail["real_dropped"] == 0
    strict = bench.verify_refs({"title_match": 1.01, "title_shortened": 1.01, "title_exact": 1.01}, data=data)
    assert strict.detail["real_dropped"] == 1  # a stricter setting drops the real paper
    assert refs.TITLE_MATCH == 0.85  # and the module's own settings are put back


def test_collect_sources_scoring_counts_entries_read_and_grouping(tmp_path):
    (tmp_path / "runs" / "r" / "notes").mkdir(parents=True)
    (tmp_path / "runs" / "r" / "notes" / "1.md").write_text(
        "```sources\n"
        '{"key": "a", "title": "One", "doi": "10.1000/one"}\n'
        '{"key": "b", "title": "One again", "doi": "10.1000/one"}\n'
        '{"key": "c", "title": "Two", "url": "https://example.org/two"}\n'
        "```\n", encoding="utf-8")
    (tmp_path / "expected.json").write_text(json.dumps({"r": {"entries": 4, "groups": [["a", "b"], ["c"], ["d"]]}}),
                                            encoding="utf-8")
    score = bench.collect_sources(tmp_path)
    (row,) = score.tasks
    assert row["entries_read"] == 3 and row["merge_f1"] == 1.0 and score.value == pytest.approx(0.5 * 0.75 + 0.5)


def _starts(tmp_path, replies):
    path = tmp_path / "starts.json"
    path.write_text(json.dumps({"replies": replies}), encoding="utf-8")
    return path


def test_the_router_replay_rewards_moving_only_when_the_other_model_is_really_faster(tmp_path):
    # DeepSeek starts in 60 s and Qwen in 5 s, all along: moving saves time
    fast_qwen = [[m, t, 60.0 if m == DEEPSEEK else 5.0] for t in range(0, 4000, 20) for m in (DEEPSEEK, QWEN)]
    data = _starts(tmp_path, fast_qwen)
    moved = bench.router_replay(data=data)
    assert moved.detail["moves"] > 0 and moved.value > 0.5
    assert bench.router_replay({"min_saving": 1000.0}, data=data).value == 0.0  # never moves: nothing saved
    # Qwen is the slow one: the rule stays put, and a rule forced to move loses
    slow_qwen = [[m, t, 5.0 if m == DEEPSEEK else 60.0] for t in range(0, 4000, 20) for m in (DEEPSEEK, QWEN)]
    data = _starts(tmp_path, slow_qwen)
    assert bench.router_replay(data=data).detail["moves"] == 0
    assert bench.router_replay({"min_saving": -1000.0}, data=data).value < 0


# --- the live benchmarks, against fake agents -----------------------------------------------------------

class FakeLead:
    """A Desktop agent that answers each run with what `reply` returns for (workdir, agent, prompt)."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def run_agent(self, workdir, agent, prompt, **kw):
        self.calls.append({"workdir": Path(workdir), "agent": agent, "prompt": prompt, **kw})
        return self.reply(Path(workdir), agent, prompt, kw)


def test_review_scores_a_found_planted_bug_and_silence_on_a_harmless_change(tmp_path):
    seeds = {s["id"]: s for s in json.loads((bench.BENCH_DIR / "code-review" / "seeds.json").read_text(encoding="utf-8"))}

    def reply(ws, agent, prompt, kw):
        diff = __import__("subprocess").run(["git", "-C", str(ws), "diff", "--name-only"], capture_output=True, text=True).stdout
        text = {s["id"]: (ws / s["file"]).read_text(encoding="utf-8") for s in seeds.values() if s["file"] in diff}
        seed = next(seeds[i] for i, t in text.items() if seeds[i]["find"] not in t and seeds[i]["replace"] in t)
        return AgentResult(0, "" if seed.get("clean") else f"Findings:\n- `{seed['file']}:{seed['line'] + 1}` the bug")

    lead = FakeLead(reply)
    score = bench.review("code-review", agent=lead, scratch=tmp_path, only=["tier-boundary", "clean-rename"])
    assert {r["id"]: r["score"] for r in score.tasks} == {"tier-boundary": 1.0, "clean-rename": 1.0}
    assert score.detail["recall"] == 1.0 and score.detail["findings_on_harmless"] == 0
    assert lead.calls[0]["agent"] == "review-lead" and "<<<medium>>>" in lead.calls[0]["prompt"]
    noisy = bench.review("code-review", agent=FakeLead(lambda *a: AgentResult(0, "x `README.md:1` and `shopcart/cart.py:3`")),
                         scratch=tmp_path, only=["clean-docstring", "round-truncates"])
    assert {r["id"]: r["score"] for r in noisy.tasks} == {"clean-docstring": 0.0, "round-truncates": 0.0}


def test_every_seed_applies_once_to_its_project():
    for target in ("code-review", "security-review"):
        project = bench.BENCH_DIR / target / "project"
        for seed in json.loads((bench.BENCH_DIR / target / "seeds.json").read_text(encoding="utf-8")):
            assert (project / seed["file"]).read_text(encoding="utf-8").count(seed["find"]) == 1, seed["id"]


def test_simplify_scores_a_landed_change_by_the_hidden_tests(tmp_path, monkeypatch):
    def reply(ws, agent, prompt, kw):  # the "fix job": shorten normalize.py without changing behaviour
        path = ws / "textkit" / "normalize.py"
        path.write_text('"""Normalising words and titles."""\n\n\ndef normalize_word(word):\n'
                        '    return "".join(ch.lower() for ch in word if ch.isalnum())\n\n\n'
                        'def normalize_title(title):\n    return " ".join(w for w in map(normalize_word, title.split(" ")) if w)\n\n\n'
                        'def same_title(a, b):\n    return normalize_title(a) == normalize_title(b)\n', encoding="utf-8")
        return AgentResult(0, "done")

    class Job:
        status, output_dir = "done", "spare-gpu/x"

    monkeypatch.setattr(bench, "_wait_for_jobs", lambda ws, since, timeout, sleep: [Job()])
    monkeypatch.setattr(bench.landing, "apply", lambda ws, stamp, force: type("O", (), {"counts": {"applied": 1}})())
    score = bench.simplify(agent=FakeLead(reply), scratch=tmp_path, only=["normalize"])
    (row,) = score.tasks
    assert row["hidden_tests_pass"] and row["applied"] and row["lines_after"] < row["lines_before"] and row["score"] == 1.0

    def breaks(ws, agent, prompt, kw):
        (ws / "textkit" / "normalize.py").write_text("def normalize_word(w):\n    return w\n", encoding="utf-8")
        return AgentResult(0, "done")
    assert bench.simplify(agent=FakeLead(breaks), scratch=tmp_path, only=["normalize"]).tasks[0]["score"] == 0.0


def test_auto_handoff_scores_the_first_tool_call_with_spare_gpu_denied(tmp_path):
    def reply(ws, agent, prompt, kw):
        empty = "<<<>>>" in prompt or "<<<   >>>" in prompt
        return AgentResult(0, "", tools=["spare-gpu_spare_waits" if empty else "spare-gpu_spare_auto"])

    lead = FakeLead(reply)
    score = bench.auto_handoff(agent=lead, scratch=tmp_path, only=["hello", "empty", "ignore-rules"])
    assert score.value == 1.0
    call = lead.calls[0]
    assert call["agent"] == "spare-auto" and call["model"] == "bharatcode/qwen-3.8-27b"
    assert call["permission"] == [{"permission": "spare-gpu_*", "pattern": "*", "action": "deny"}]
    answered = bench.auto_handoff(agent=FakeLead(lambda *a: AgentResult(0, "Hi!", tools=[])), scratch=tmp_path, only=["hello"])
    assert answered.value == 0.0 and answered.tasks[0]["first_tool"] is None


def test_deep_research_scores_kept_references_unmarked_claims_and_coverage(tmp_path):
    def reply(ws, agent, prompt, kw):
        out = ws / "research" / "q"
        out.mkdir(parents=True)
        (out / "report.md").write_text("# R\n\nText.\n", encoding="utf-8")
        (out / "sources.json").write_text(json.dumps({"sources": [{"key": "a", "verified": True}, {"key": "b", "verified": True},
                                                                  {"key": "c", "verified": False}, {"key": "d", "verified": True}]}),
                                          encoding="utf-8")
        (out / "review.md").write_text("Claims checked: 20 · marked: 5 · checklist items covered: 6 of 8\n", encoding="utf-8")
        return AgentResult(0, "done")

    score = bench.deep_research(agent=FakeLead(reply), scratch=tmp_path, only=["fact-python-version"])
    (row,) = score.tasks
    assert row["claims_checked"] == 20 and row["claims_marked"] == 5 and (row["items_covered"], row["items"]) == (6, 8)
    assert row["score"] == pytest.approx(0.4 * 0.75 + 0.3 * 0.75 + 0.3 * 0.75, abs=1e-4)
    nothing = bench.deep_research(agent=FakeLead(lambda *a: AgentResult(0, "")), scratch=tmp_path, only=["fact-python-version"])
    assert nothing.value == 0.0


@pytest.mark.parametrize("text, expected", [
    ("Claims checked: 32 · marked: 20 · checklist items covered: 6 of 8", (32, 20, 6, 8)),
    ("Checked 33 distinct report claims; marked: 17; 8/8 checklist items covered", (33, 17, 8, 8)),
    ("Checked 32 claim clusters across 9 sections; 16 marked; 8 of 8 checklist items covered", (32, 16, 8, 8)),
])
def test_review_figures_read_the_verdict_lines_checkers_actually_wrote(tmp_path, text, expected):
    (tmp_path / "review.md").write_text(f"# Review\n\n{text}\n", encoding="utf-8")
    got = bench.review_figures(tmp_path)
    assert (got["claims_checked"], got["claims_marked"], got["items_covered"], got["items"]) == expected
