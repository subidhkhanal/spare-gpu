"""Routing inside the job runner: the chosen model sticks, a routed result that keeps nothing reruns
once on the preferred model, a model the plan refuses is swapped for one it includes, and
spare_delegate picks the model on the spot."""

from datetime import datetime, timezone

import httpx
import pytest

from gpu_token_utilization import router, waits
from gpu_token_utilization.client import BharatCodeError
from gpu_token_utilization.mcp_server import SpareTools, db_path
from gpu_token_utilization.router import Choice
from gpu_token_utilization.scheduler import Scheduler, kept_nothing
from gpu_token_utilization.store import Store

DEEPSEEK, QWEN = "deepseek-v4.1-flash", "qwen-3.8-27b"
REFUSAL = ("Your BharatCode Lite plan includes Qwen 3.8 27B (qwen-3.8-27b). DeepSeek V4.1 Flash needs "
           "BharatCode Pro (₹999/month): https://bharatcode.ai/subscribe.")


class Worker:
    """Stands in for DesktopAgent: only the model and the copy-on-another-model matter here."""
    label = "BharatCode Desktop"
    timeout = 60

    def __init__(self, model=f"bharatcode/{DEEPSEEK}"):
        self.model = model

    def with_model(self, model):
        return Worker(model)


def plans(used, verify_outputs, refuse=()):
    outputs = iter(verify_outputs)

    def write(ctx):
        used.append(waits.model_id(ctx.agent.model))
        if waits.model_id(ctx.agent.model) in refuse:
            raise BharatCodeError(f"BharatCode Desktop failed: {REFUSAL}")
        return "wrote tests"

    def verify(ctx):
        return next(outputs)

    return {"tests": [("write", write), ("verify", verify)]}


def run(store, make_gate, job_plans, route=None):
    return Scheduler(store, make_gate(probe=False), None, plans=job_plans, agent=Worker(), isolate=False,
                     ignore_gate=True, route=route).run()


def test_the_chosen_model_is_stored_and_used_for_the_whole_job(store, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    used = []
    route = lambda j: Choice(QWEN, DEEPSEEK, "saves about 2m 05s of waiting over DeepSeek V4.1 Flash")
    run(store, make_gate, plans(used, ["Kept 3 passing test(s), dropped 0."]), route)
    job = store.get_job(job.id)
    assert used == [QWEN] and job.status == "done"
    assert (job.model, job.preferred_model, job.fallback) == (QWEN, DEEPSEEK, 0)
    assert "saves about" in job.route_reason


def test_a_routed_job_whose_check_keeps_nothing_reruns_once_on_the_preferred_model(store, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    used = []
    route = lambda j: Choice(QWEN, DEEPSEEK, "faster")
    run(store, make_gate, plans(used, ["Kept 0 passing test(s), dropped 2.", "Kept 2 passing test(s), dropped 0."]),
        route)
    job = store.get_job(job.id)
    assert used == [QWEN, DEEPSEEK] and job.status == "done"
    assert job.model == DEEPSEEK and job.fallback == 1 and "fell back from Qwen 3.8 27B" in job.route_reason


def test_a_job_on_its_preferred_model_never_falls_back(store, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    used = []
    route = lambda j: Choice(DEEPSEEK, DEEPSEEK, "not switch-safe")
    run(store, make_gate, plans(used, ["No test files were generated; nothing to keep."]), route)
    assert used == [DEEPSEEK] and store.get_job(job.id).status == "done"


def test_a_model_the_plan_refuses_is_swapped_for_the_one_it_includes(store, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    used = []
    run(store, make_gate, plans(used, ["Kept 1 passing test(s), dropped 0."], refuse={DEEPSEEK}))
    job = store.get_job(job.id)
    assert used == [DEEPSEEK, QWEN] and job.status == "done" and job.model == QWEN
    assert "plan doesn't include DeepSeek V4.1 Flash" in job.route_reason
    assert router.State.load(store.path.parent.parent / "repo" / ".gtu").unavailable == [DEEPSEEK]


def test_a_refusal_of_the_only_allowed_model_still_fails_the_job(store, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    used = []
    run(store, make_gate, plans(used, [], refuse={DEEPSEEK, QWEN}), route=lambda j: Choice(QWEN, QWEN, "only one"))
    assert used == [QWEN] and store.get_job(job.id).status == "failed"


def test_kept_nothing_reads_the_verify_summaries():
    assert kept_nothing("Kept 0 passing test(s), dropped 3.\n- ...")
    assert kept_nothing("No documentation changes were generated; nothing to keep.")
    assert kept_nothing("Kept documentation changes in 0 file(s).")
    assert not kept_nothing("Kept 10 passing test(s), dropped 0.") and not kept_nothing(None)


# --- spare_delegate -------------------------------------------------------------------------------

def wait_figures() -> httpx.Client:
    """DeepSeek queued (p75 125 s), Qwen idle: what the live test on 4 Nov saw. Stamped when asked,
    as the real endpoint is, so the figures are never stale however long the suite has run."""
    def handle(request):
        now = datetime.now(timezone.utc).isoformat()
        if request.url.path.endswith("/queue-waits"):
            return httpx.Response(200, json={"window_minutes": 60, "as_of": now, "models": [
                {"id": DEEPSEEK, "sample_count": 149, "p50_ms": 92948, "p75_ms": 124851, "p90_ms": 182107},
                {"id": QWEN, "sample_count": 83, "p50_ms": 2, "p75_ms": 3, "p90_ms": 7}]})
        lane = lambda a, q, s: {"active_requests": a, "queued_requests": q, "slot_capacity": s, "available": True}
        return httpx.Response(200, json={"generated_at": now, "models": [
            {"id": DEEPSEEK, "api": lane(2, 8, 2)}, {"id": QWEN, "api": lane(1, 0, 4)}]})
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def fresh_wait_cache(monkeypatch):
    waits._cache.clear()
    for name in (router.SWITCH_SAFE_ENV, router.PREFERRED_ENV, router.ROUTE_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    waits._cache.clear()


def test_delegate_routes_a_switch_safe_task_and_says_why(git_repo):
    (git_repo / ".gtu").mkdir(exist_ok=True)
    (git_repo / ".gtu" / "switch-safe").write_text("tests\n", encoding="utf-8")
    tools = SpareTools(git_repo, launch=lambda repo: 4242, http=wait_figures)
    text = tools.delegate("tests", "src", your_model=f"bharatcode/{DEEPSEEK}")
    assert text.startswith("Handed off: tests for src (job ")
    assert "Runs on Qwen 3.8 27B: saves about 2m 05s of waiting over DeepSeek V4.1 Flash" in text
    assert "DeepSeek V4.1 Flash ≈ 2m 05s" in text and "Qwen 3.8 27B ≈ 0 s (free slot, empty queue)" in text
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
    assert (job.model, job.preferred_model) == (QWEN, DEEPSEEK)


def test_delegate_from_qwen_would_move_up_to_deepseek_but_its_lane_is_full(git_repo):
    # moving up to DeepSeek is allowed for any type, but here DeepSeek's lane has a queue
    text = SpareTools(git_repo, launch=lambda repo: 4242, http=wait_figures).delegate("docs", "src", your_model=QWEN)
    assert "Runs on Qwen 3.8 27B: no other model has a free slot right now" in text


def test_delegate_from_qwen_moves_up_to_deepseek_when_qwen_is_packed(git_repo):
    def flipped() -> httpx.Client:
        """Qwen queued, DeepSeek idle: the reverse of wait_figures."""
        def handle(request):
            now = datetime.now(timezone.utc).isoformat()
            if request.url.path.endswith("/queue-waits"):
                return httpx.Response(200, json={"window_minutes": 60, "as_of": now, "models": [
                    {"id": QWEN, "sample_count": 149, "p50_ms": 92948, "p75_ms": 124851, "p90_ms": 182107},
                    {"id": DEEPSEEK, "sample_count": 83, "p50_ms": 2, "p75_ms": 3, "p90_ms": 7}]})
            lane = lambda a, q, s: {"active_requests": a, "queued_requests": q, "slot_capacity": s, "available": True}
            return httpx.Response(200, json={"generated_at": now, "models": [
                {"id": QWEN, "api": lane(2, 8, 2)}, {"id": DEEPSEEK, "api": lane(1, 0, 4)}]})
        return httpx.Client(transport=httpx.MockTransport(handle))

    text = SpareTools(git_repo, launch=lambda repo: 4242, http=flipped).delegate("tests", "src", your_model=QWEN)
    assert "Runs on DeepSeek V4.1 Flash: saves about 2m 05s of waiting over Qwen 3.8 27B" in text


def test_delegate_without_the_callers_model_prefers_deepseek_outside_desktop(git_repo):
    text = SpareTools(git_repo, launch=lambda repo: 4242, http=wait_figures).delegate("review", "src")
    assert "Runs on DeepSeek V4.1 Flash" in text
