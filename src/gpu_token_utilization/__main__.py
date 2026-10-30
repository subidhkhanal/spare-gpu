"""`python -m gpu_token_utilization ...` is the same as `gtu ...` (used to start the background runner)."""

from .cli import app

app(prog_name="gtu")
