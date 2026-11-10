"""Progress output: a running step or a wait for BharatCode must never look like a hang."""

from gpu_token_utilization.agent import BusyError
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.fakedesktop import Step
from gpu_token_utilization.jobs import JobConfig, run_agent
from gpu_token_utilization.scheduler import Scheduler


def test_scheduler_narrates_each_step(desktop_fake, clock, store, job_repo):
    job = store.add_job("fix", ".", job_repo, "a.py:1 fix it")
    desktop_fake.steps = [Step(tokens=2500, text="fixed")]
    logs = []

    def check(ctx):
        run_agent(ctx, "look at a.py", edit=False, commands=())
        return "done"

    Scheduler(store, plans={"fix": [("check", check)]}, agent=desktop_fake.agent(timeout=60),
              sleep=clock.sleep, now=clock.utcnow, isolate=False, log=logs.append).run_job(job.id)
    assert logs[0].endswith("step 1 of 1 (check) started: one step.")
    assert logs[1].startswith("  BharatCode Desktop finished a step") and "(2,500 tokens)" in logs[1]
    assert logs[2].endswith("step 1 (check) done.")
    assert logs[-1].startswith(f"Job {job.id}: finished;")


def test_every_busy_wait_is_announced(desktop_fake, clock, store, job_repo):
    job = store.add_job("fix", ".", job_repo, "a.py:1 fix it")
    left = {"busy": 3}

    def write(ctx):
        if left["busy"]:
            left["busy"] -= 1
            raise BusyError("BharatCode Desktop did not finish within 60 s")
        return "done"

    logs = []
    Scheduler(store, plans={"fix": [("write", write)]}, agent=desktop_fake.agent(timeout=60),
              sleep=clock.sleep, now=clock.utcnow, isolate=False, log=logs.append).run_job(job.id)
    assert [line for line in logs if "trying again" in line] == [
        f"BharatCode is busy; trying again in {w} s." for w in (60, 120, 240)
    ]
    assert clock.sleeps == [60.0, 120.0, 240.0]


def test_session_steps_are_narrated_as_they_finish(git_repo, store, clock, scenario):
    desktop = scenario(steps=[{"tokens": 11500}, {"tokens": 300}])
    job = store.add_job("fix", ".", str(git_repo), "src/calc.py:1 add a docstring")
    logs = []
    Scheduler(store, agent=desktop.agent(timeout=60), budgets=Budgets(),
              config=JobConfig(), sleep=clock.sleep, now=clock.utcnow, log=logs.append).run_job(job.id)
    assert any("step 1 of 2 (write) started: BharatCode Desktop is working on it (Qwen 3.8 27B)" in line for line in logs)
    step_lines = [line for line in logs if "BharatCode Desktop finished a step" in line]
    assert len(step_lines) == 2 and "(11,500 tokens)" in step_lines[0] and "(300 tokens)" in step_lines[1]
    assert any("step 2 of 2 (verify) started: running locally." in line for line in logs)
