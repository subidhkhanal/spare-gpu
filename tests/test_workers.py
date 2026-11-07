"""workers.build: who does a job's steps, inside BharatCode Desktop and outside it."""

import pytest

from gpu_token_utilization import desktop, workers
from gpu_token_utilization.api import ApiKeyMissing
from gpu_token_utilization.jobs import JobConfig

KEY = "bc_live_test_key_not_real"


def test_inside_desktop_every_step_is_a_desktop_session_and_no_key_is_needed(store, monkeypatch):
    monkeypatch.setattr(desktop, "discover", lambda: desktop.Connection("http://127.0.0.1:4321", "u", "p"))
    logs = []
    worker = workers.build(store, logs.append, JobConfig(agent_timeout=12.0))
    assert worker.inside_desktop and worker.client is None and worker.desktop_missing is None
    assert isinstance(worker.agent, desktop.DesktopAgent) and worker.agent.timeout == 12.0
    assert logs == ["Running jobs through BharatCode Desktop (http://127.0.0.1:4321); no API key needed."]
    worker.close()


def test_outside_desktop_without_a_key_says_how_to_set_one(store):
    with pytest.raises(ApiKeyMissing):
        workers.build(store, lambda line: None)


def test_outside_desktop_with_a_key_reviews_run_on_the_api_and_the_rest_wait_for_desktop(store, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    worker = workers.build(store, lambda line: None)
    assert not worker.inside_desktop and worker.client is not None and worker.agent is None
    assert worker.desktop_missing == desktop.DESKTOP_NEEDED
    worker.close()
