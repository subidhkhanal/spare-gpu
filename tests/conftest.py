from datetime import time

import httpx
import pytest

from gpu_token_utilization.client import BharatCodeClient, RetryPolicy
from gpu_token_utilization.fakeapi import FakeBharatCode
from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.store import Store

KEY = "bc_live_fake_key"


@pytest.fixture
def fake():
    return FakeBharatCode(api_key=KEY)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".gtu" / "gtu.db") as s:
        yield s


@pytest.fixture
def client(fake, store):
    # Scheduler-style client: no retries, the gate decides when to try again.
    return BharatCodeClient(
        api_key=KEY, store=store, transport=fake.transport(), retry=RetryPolicy(max_retries=0),
        sleep=fake.clock.sleep, clock=fake.clock, rng=lambda: 0.0,
    )


@pytest.fixture
def make_gate(fake, store, client):
    def make(probe=True, **overrides):
        # Tests run at any hour, so the fallback window is all day unless a test says otherwise.
        cfg = GateConfig(**{"window": (time(0, 0), time(0, 0)), **overrides})
        return Gate(
            store, cfg, http=httpx.Client(transport=fake.transport()),
            client=client if probe else None, now=fake.clock.utcnow,
        )

    return make
