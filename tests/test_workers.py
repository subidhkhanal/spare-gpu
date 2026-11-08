"""workers.build: who does a job's steps, inside BharatCode Desktop and outside it."""

import pytest

from gpu_token_utilization import desktop, workers
from gpu_token_utilization.jobs import JobConfig


def test_inside_desktop_every_step_is_a_desktop_session(store, monkeypatch):
    monkeypatch.setattr(desktop, "discover", lambda: desktop.Connection("http://127.0.0.1:4321", "u", "p"))
    logs = []
    worker = workers.build(store, logs.append, JobConfig(agent_timeout=12.0))
    assert isinstance(worker, workers.Worker)
    assert isinstance(worker.agent, desktop.DesktopAgent) and worker.agent.timeout == 12.0
    assert worker.agent.conn.url == "http://127.0.0.1:4321"
    assert logs == ["Running jobs through BharatCode Desktop (http://127.0.0.1:4321)."]
    worker.close()


def test_outside_desktop_no_worker_is_built_and_the_reason_is_given(store):
    logs = []
    with pytest.raises(desktop.DesktopNotFound, match="wasn't started by BharatCode Desktop"):
        workers.build(store, logs.append)  # no Desktop server password in this test's environment
    assert logs == []


def test_a_desktop_that_cannot_be_reached_is_not_found_either(store, monkeypatch):
    def unreachable():
        raise desktop.DesktopNotFound("couldn't reach BharatCode Desktop's server (tried http://127.0.0.1:1)")

    monkeypatch.setattr(desktop, "discover", unreachable)
    with pytest.raises(desktop.DesktopNotFound, match="couldn't reach"):
        workers.build(store, lambda line: None)
