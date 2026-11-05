"""Wait-aware routing: which model a job runs on, and why."""

import json

import pytest

from gpu_token_utilization import router, waits
from gpu_token_utilization.gate import LaneStats
from gpu_token_utilization.router import State, choose
from gpu_token_utilization.waits import ModelWaits, View

DEEPSEEK, QWEN = "deepseek-v4.1-flash", "qwen-3.8-27b"
SAFE = {"types": {"tests": "switch-safe", "docs": "keep-preferred"}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # GTU_ROUTING_POLICY stays: conftest points it at a neutral policy, not the shipped one
    for name in (router.SWITCH_SAFE_ENV, router.PREFERRED_ENV, router.MIN_SAVING_ENV, router.ROUTE_ENV,
                 waits.MODELS_ENV):
        monkeypatch.delenv(name, raising=False)


def view(model, p75_s, active, queued, slots=4):
    g = ModelWaits(id=model, sample_count=100, p50_ms=p75_s * 700, p75_ms=p75_s * 1000, p90_ms=p75_s * 1400)
    return View(model, g, LaneStats(active_requests=active, queued_requests=queued, slot_capacity=slots, available=True))


def busy_deepseek():
    return view(DEEPSEEK, 125, 2, 8, slots=2)


def free_qwen():
    return view(QWEN, 0.003, 1, 0)


def test_a_switch_safe_task_moves_to_the_model_that_starts_sooner():
    choice = choose("tests", DEEPSEEK, [busy_deepseek(), free_qwen()], policy=SAFE)
    assert choice.model == QWEN and choice.preferred == DEEPSEEK and choice.routed
    assert "saves about 2m 05s" in choice.reason and "free slot" in choice.reason
    assert choice.summary().startswith("Runs on Qwen 3.8 27B: saves about")


def test_a_type_not_measured_as_switch_safe_never_moves_to_the_weaker_model():
    choice = choose("docs", DEEPSEEK, [busy_deepseek(), free_qwen()], policy=SAFE)
    assert choice.model == DEEPSEEK and "aren't marked switch-safe" in choice.reason


def busy_qwen():
    return view(QWEN, 125, 2, 8, slots=2)


def free_deepseek():
    return view(DEEPSEEK, 0.003, 1, 0)


def test_any_type_moves_up_to_the_stronger_model_when_its_own_is_packed():
    # moving to DeepSeek never costs quality, so switch-safe doesn't matter in this direction
    choice = choose("docs", QWEN, [busy_qwen(), free_deepseek()], policy=SAFE)
    assert choice.model == DEEPSEEK and choice.preferred == QWEN and "saves about 2m 05s" in choice.reason


def test_moving_up_still_needs_a_free_slot_and_a_real_saving():
    queued_deepseek = view(DEEPSEEK, 0.003, 4, 2)
    assert choose("docs", QWEN, [busy_qwen(), queued_deepseek], policy=SAFE).model == QWEN
    assert choose("docs", QWEN, [view(QWEN, 12, 1, 0, slots=2), free_deepseek()], policy=SAFE).model == QWEN


def test_the_strongest_model_is_the_first_listed():
    assert router.stronger(DEEPSEEK, QWEN) and not router.stronger(QWEN, DEEPSEEK)
    assert router.stronger(QWEN, "some-new-model")  # an unlisted model counts as the weakest


def test_a_small_saving_isnt_worth_switching_for():
    choice = choose("tests", DEEPSEEK, [view(DEEPSEEK, 12, 1, 0, slots=2), free_qwen()], policy=SAFE)
    assert choice.model == DEEPSEEK and "would save 20 s or more" in choice.reason


def test_never_routes_into_a_lane_with_a_queue():
    queued_qwen = view(QWEN, 0.003, 4, 2)
    choice = choose("tests", DEEPSEEK, [busy_deepseek(), queued_qwen], policy=SAFE)
    assert choice.model == DEEPSEEK and "no other model has a free slot" in choice.reason


def test_no_wait_data_on_the_preferred_model_means_no_switch():
    choice = choose("tests", DEEPSEEK, [View(DEEPSEEK), free_qwen()], policy=SAFE)
    assert choice.model == DEEPSEEK and "no wait data" in choice.reason


def test_switching_back_needs_two_readings_in_a_row():
    state = State()
    assert choose("tests", DEEPSEEK, [busy_deepseek(), free_qwen()], state, SAFE).model == QWEN
    calm = [view(DEEPSEEK, 5, 0, 0, slots=2), free_qwen()]
    first = choose("tests", DEEPSEEK, calm, state, SAFE)
    assert first.model == QWEN and "1 reading in a row" in first.reason
    assert choose("tests", DEEPSEEK, calm, state, SAFE).model == DEEPSEEK
    assert state.away_from is None


def test_routing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(router.ROUTE_ENV, "off")
    assert choose("tests", DEEPSEEK, [busy_deepseek(), free_qwen()], policy=SAFE).model == DEEPSEEK


def test_a_model_the_plan_refused_is_never_chosen_or_preferred():
    state = State(unavailable=[DEEPSEEK])
    choice = choose("tests", DEEPSEEK, [busy_deepseek(), free_qwen()], state, SAFE)
    assert choice.model == QWEN and "plan doesn't include DeepSeek V4.1 Flash" in choice.reason
    alone = choose("tests", QWEN, [busy_deepseek(), free_qwen()], state, SAFE)
    assert alone.model == QWEN and "plan includes no other model" in alone.reason


def test_bharatcodes_plan_refusal_is_recognised():
    error = ("BharatCode Desktop failed: Your BharatCode Lite plan includes Qwen 3.8 27B (qwen-3.8-27b). "
             "DeepSeek V4.1 Flash needs BharatCode Pro (₹999/month): https://bharatcode.ai/subscribe.")
    assert router.plan_refusal(error) == QWEN
    assert router.plan_refusal("Model overloaded") is None and router.plan_refusal(None) is None


def test_the_shipped_policy_is_well_formed(monkeypatch):
    monkeypatch.delenv(router.POLICY_ENV)
    policy = json.loads(router.POLICY_FILE.read_text(encoding="utf-8"))
    assert set(policy["types"]) == {"tests", "docs", "review"}
    assert set(policy["types"].values()) <= {"switch-safe", "keep-preferred", "unmeasured"}
    for kind, verdict in policy["types"].items():
        # a measured verdict always comes with the evidence behind it
        assert verdict == "unmeasured" or policy["evidence"][kind]["tasks"] >= 2


def test_with_no_policy_or_override_nothing_routes():
    policy = router.load_policy()
    assert not any(router.switch_safe(kind, policy) for kind in ("tests", "docs", "review"))


def test_the_project_file_and_the_env_var_override_the_policy(tmp_path, monkeypatch):
    (tmp_path / router.SWITCH_SAFE_FILE).write_text("tests\n\n", encoding="utf-8")
    monkeypatch.setenv(router.SWITCH_SAFE_ENV, "review")
    policy = router.load_policy(tmp_path)
    assert router.switch_safe("tests", policy) and router.switch_safe("review", policy)
    assert not router.switch_safe("docs", policy)


def test_state_is_kept_in_the_project_folder(tmp_path):
    state = State(away_from=DEEPSEEK, back_votes=1)
    state.mark_unavailable("bharatcode/" + DEEPSEEK)
    state.save(tmp_path)
    again = State.load(tmp_path)
    assert again.away_from == DEEPSEEK and again.back_votes == 1 and again.unavailable == [DEEPSEEK]
    assert json.loads((tmp_path / "router.json").read_text())["unavailable"] == [DEEPSEEK]
    assert State.load(tmp_path / "missing").away_from is None


def test_the_preferred_model_can_be_set(monkeypatch):
    assert router.preferred_model("bharatcode/" + QWEN) == QWEN
    monkeypatch.setenv(router.PREFERRED_ENV, DEEPSEEK)
    assert router.preferred_model(QWEN) == DEEPSEEK
