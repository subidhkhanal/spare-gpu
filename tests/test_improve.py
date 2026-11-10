"""The improvement loop: candidates are saved, scored, compared task by task, promoted only when
better (and archived first), and rolled back; prompts are proposed by the heavy model from the
failures, keep their frontmatter, and must be installed in Desktop before they are scored."""

import json

import pytest

from gpu_token_utilization import improve, pack
from gpu_token_utilization.agent import AgentResult
from gpu_token_utilization.bench import Score


def score(*pairs):
    return Score(sum(s for _, s in pairs) / len(pairs), [{"id": i, "score": s} for i, s in pairs])


def test_a_candidate_is_better_only_by_the_margin_and_without_doing_worse_on_the_tasks():
    now = score(("a", 0.5), ("b", 0.5), ("c", 0.5), ("d", 0.5))
    assert improve.compare(now, score(("a", 0.7), ("b", 0.7), ("c", 0.6), ("d", 0.6)))["verdict"] == "better"
    assert improve.compare(now, score(("a", 0.51), ("b", 0.5), ("c", 0.5), ("d", 0.5)))["verdict"] == "not better"  # under 5%
    mixed = improve.compare(now, score(("a", 1.0), ("b", 1.0), ("c", 0.0), ("d", 0.5)))
    assert mixed["verdict"] == "not better" and mixed["tasks_worse"] == ["c"]  # a better mean, but worse on a task


@pytest.fixture
def params_target(tmp_path, monkeypatch):
    """verify-refs with a fake benchmark (higher title_match scores better) and its own tuning file."""
    tuning = tmp_path / "tuning.json"
    tuning.write_text(json.dumps({"verify_refs": {"title_match": 0.85}, "router": {"min_saving": 20.0}}), encoding="utf-8")
    monkeypatch.setattr(improve.tuning, "TUNING_FILE", tuning)
    monkeypatch.setattr(improve, "score_params", lambda t, s: score(("x", s["title_match"]), ("y", s["title_match"])))
    return tmp_path / "improve", tuning


def test_a_settings_candidate_is_searched_scored_decided_promoted_and_rolled_back(params_target):
    base, tuning = params_target
    cid, result = improve.search_params("verify-refs", base=base)
    assert cid and result["verdict"] == "better" and result["candidate"] == 0.9
    assert improve.load_candidate(improve.target("verify-refs"), cid, base)["params"] == {"title_match": 0.9}
    with pytest.raises(improve.ImproveError, match="isn't recorded as better"):
        improve.promote("verify-refs", cid, base=base, tuning_file=tuning)
    improve.evaluate("verify-refs", base=base)
    improve.evaluate("verify-refs", cid, base=base)
    assert improve.decide("verify-refs", cid, base=base)["verdict"] == "better"
    entry = improve.promote("verify-refs", cid, base=base, tuning_file=tuning)
    data = json.loads(tuning.read_text(encoding="utf-8"))
    assert data["verify_refs"]["title_match"] == 0.9 and data["router"] == {"min_saving": 20.0}  # only its section
    assert entry["changed"] == ["title_match"] and (base / "verify-refs" / "archive").is_dir()
    improve.rollback("verify-refs", base=base, tuning_file=tuning)
    assert json.loads(tuning.read_text(encoding="utf-8"))["verify_refs"]["title_match"] == 0.85
    assert [e["action"] for e in improve.history(base, "verify-refs")] == ["promote", "rollback"]
    with pytest.raises(improve.ImproveError, match="nothing of verify-refs to roll back"):
        improve.rollback("verify-refs", base=base, tuning_file=tuning)


def test_nothing_is_saved_when_no_setting_wins(params_target, monkeypatch):
    base, _ = params_target
    monkeypatch.setattr(improve, "score_params", lambda t, s: score(("x", 0.5)))
    cid, result = improve.search_params("verify-refs", base=base)
    assert cid is None and result["verdict"] == "not better" and not (base / "verify-refs" / "candidates").exists()


@pytest.fixture
def prompts_target(tmp_path, monkeypatch):
    """auto-handoff with a package folder and a Desktop config folder of its own."""
    package = tmp_path / "pkg"
    (package / "agents").mkdir(parents=True)
    (package / "agents" / "spare-auto.md").write_text("---\n# Installed by spare-gpu\nmode: primary\n---\nYou are a relay.\n",
                                                       encoding="utf-8")
    config = tmp_path / "config"
    monkeypatch.setattr(improve, "PACKAGE_DIR", package)
    monkeypatch.setattr(improve, "_config_folders", lambda: [config])
    monkeypatch.setattr(pack, "resolve_models", lambda *a, **k: (pack.Models("bharatcode/deepseek-v4.1-flash",
                                                                              "bharatcode/qwen-3.8-27b"), ""))
    return tmp_path / "improve", package, config


class Rewriter:
    def __init__(self, text):
        self.text, self.calls = text, []

    def run_agent(self, workdir, agent, prompt, **kw):
        self.calls.append({"agent": agent, "prompt": prompt, **kw})
        return AgentResult(0, self.text)


def test_a_prompt_candidate_comes_from_the_failures_keeps_its_frontmatter_and_must_be_installed(prompts_target, monkeypatch, tmp_path):
    base, package, config = prompts_target
    monkeypatch.setattr(improve.bench, "LIVE", {"auto-handoff": lambda **kw: score(("hello", 0.0), ("empty", 1.0))})
    with pytest.raises(improve.ImproveError, match="doesn't have the current prompts installed"):
        improve.evaluate("auto-handoff", base=base)
    improve.install("auto-handoff", base=base)
    improve.evaluate("auto-handoff", base=base)  # the current prompts' failures: "hello" was answered directly
    reply = ("=== FILE: agents/spare-auto.md ===\n---\nmode: subagent\n---\nYou are a relay. Call the tool first, even for hello.\n"
             "=== END ===\nI made the first call explicit.")
    rewriter = Rewriter(reply)
    cid = improve.propose_prompts("auto-handoff", agent=rewriter, scratch=tmp_path, base=base)
    asked = rewriter.calls[0]
    assert asked["permission"] == improve.DENY_ALL and asked["model"] == "bharatcode/deepseek-v4.1-flash"
    assert "- hello: score 0.0" in asked["prompt"] and "You are a relay." in asked["prompt"]
    text = improve.load_candidate(improve.target("auto-handoff"), cid, base)["texts"]["agents/spare-auto.md"]
    assert text.startswith("---\n# Installed by spare-gpu\nmode: primary\n---\n")  # its own frontmatter kept
    assert "even for hello" in text
    with pytest.raises(improve.ImproveError, match="doesn't have the .* prompts installed"):
        improve.evaluate("auto-handoff", cid, base=base)  # the candidate isn't installed yet
    improve.install("auto-handoff", cid, base=base)
    assert "even for hello" in (config / "agent" / "spare-auto.md").read_text(encoding="utf-8")
    monkeypatch.setattr(improve.bench, "LIVE", {"auto-handoff": lambda **kw: score(("hello", 1.0), ("empty", 1.0))})
    improve.evaluate("auto-handoff", cid, base=base)
    assert improve.decide("auto-handoff", cid, base=base)["verdict"] == "better"
    improve.promote("auto-handoff", cid, base=base)
    assert "even for hello" in (package / "agents" / "spare-auto.md").read_text(encoding="utf-8")
    improve.rollback("auto-handoff", base=base)
    assert (package / "agents" / "spare-auto.md").read_text(encoding="utf-8").endswith("You are a relay.\n")


def test_a_rewrite_that_leaves_a_file_out_or_bloats_it_is_refused(prompts_target):
    t = improve.target("auto-handoff")
    texts = improve.current_texts(t)
    with pytest.raises(improve.ImproveError, match="left out"):
        improve.parse_rewrite(t, "no files here", texts)
    with pytest.raises(improve.ImproveError, match="grew"):
        improve.parse_rewrite(t, "=== FILE: agents/spare-auto.md ===\n" + "word " * 200 + "\n=== END ===", texts)


def test_pack_prompts_are_installed_with_models_filled_and_the_standard_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "resolve_models", lambda *a, **k: (pack.Models("bharatcode/ds", "bharatcode/qw"), ""))
    filled = dict(((k, n), text) for k, n, text in improve._filled(improve.target("deep-research"),
                                                                    improve.current_texts(improve.target("deep-research"))))
    assert "model: bharatcode/ds" in filled[("agent", "research-lead")]
    assert ("agent", "research-checker-standard") in filled and "model: bharatcode/qw" in filled[("agent", "research-checker-standard")]


def test_code_targets_have_nothing_to_propose_or_promote():
    with pytest.raises(improve.ImproveError, match="no settings to search"):
        improve.search_params("collect-sources")
    with pytest.raises(improve.ImproveError, match="is code"):
        improve.evaluate("collect-sources", "x")
    with pytest.raises(improve.ImproveError, match="unknown target"):
        improve.target("nope")
