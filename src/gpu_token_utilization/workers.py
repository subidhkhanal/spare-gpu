"""Who does a job's steps.

Inside BharatCode Desktop (the usual case: spare-gpu's MCP server is started by Desktop, or
`gtu start` runs in Desktop's terminal), every step is a Desktop session on Desktop's own server
and sign-in, so no API key is needed. Outside Desktop, review jobs use BharatCode's API with the
user's key; tests, docs and fix jobs need a Desktop session, so they wait for Desktop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from . import desktop
from .client import BharatCodeClient, RetryPolicy
from .jobs import JobConfig
from .store import Store

Agent = desktop.DesktopAgent


@dataclass
class Worker:
    client: BharatCodeClient | None  # None inside Desktop: no API key, every request goes through Desktop
    agent: Agent | None  # runs the write steps (and, inside Desktop, the reviews too)
    desktop_missing: str | None  # why tests, docs and fix jobs can't run here, if they can't
    inside_desktop: bool

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def build(store: Store, log: Callable[[str], None], config: JobConfig | None = None) -> Worker:
    """The worker for this machine right now. Raises api.ApiKeyMissing outside Desktop when no
    key is set up (the message says how to set one)."""
    config = config or JobConfig()
    try:
        agent = desktop.DesktopAgent(desktop.discover(), timeout=config.agent_timeout)
    except desktop.DesktopNotFound as exc:
        if os.environ.get(desktop.PASSWORD_ENV):  # started from Desktop, yet its server wasn't found
            log(f"Not using BharatCode Desktop: {exc}.")
    else:
        log(f"Running jobs through BharatCode Desktop ({agent.conn.url}); no API key needed.")
        return Worker(None, agent, None, True)
    client = BharatCodeClient(store=store, retry=RetryPolicy.from_env(), on_retry=lambda message: log(f"  {message}"))
    return Worker(client, None, desktop.DESKTOP_NEEDED, False)
