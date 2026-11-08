"""Who does a job's steps: BharatCode Desktop. spare-gpu's MCP server is started by Desktop, so
every step is a Desktop session on Desktop's own server and sign-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import desktop
from .jobs import JobConfig
from .store import Store

Agent = desktop.DesktopAgent


@dataclass
class Worker:
    agent: Agent  # runs every step: the write steps as editing sessions, a review as a read-only one

    def close(self) -> None:
        pass


def build(store: Store, log: Callable[[str], None], config: JobConfig | None = None) -> Worker:
    """The worker for this machine right now. Raises desktop.DesktopNotFound when Desktop's server
    can't be reached (the message says why)."""
    config = config or JobConfig()
    agent = desktop.DesktopAgent(desktop.discover(), timeout=config.agent_timeout)
    log(f"Running jobs through BharatCode Desktop ({agent.conn.url}).")
    return Worker(agent)
