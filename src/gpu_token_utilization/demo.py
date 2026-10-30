"""`gtu demo`: see the tool work a minute or two after installing it.

Picks a small source file, sends one short review straight away (no gate, no git
branch, no clean-repo requirement) and shows the answer. Real jobs are the same
review, queued and run when BharatCode has capacity.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .client import BharatCodeClient, ChatResult, RetryPolicy
from .jobs import SKIP_DIRS, numbered_listing, review_messages
from .worktree import GIT

# Impatient on purpose: a demo should answer or say why not. Not shorter than the normal
# timeout, though: a real morning demo spent 60 s of 73 s in BharatCode's queue.
DEMO_TIMEOUT = 180.0
DEMO_RETRY = RetryPolicy(max_retries=1, base_delay=5.0, max_delay=10.0)

SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".swift", ".scala", ".lua", ".sh",
}
IDEAL_BYTES = 1500  # big enough to have something to say, small enough to answer fast
MIN_BYTES, MAX_BYTES = 150, 8000
DEMO_MAX_TOKENS = 700


def _candidates(root: Path) -> list[Path]:
    """Tracked files in a git repo, otherwise everything under root not in a skipped folder."""
    proc = subprocess.run([*GIT, "ls-files", "-z"], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode == 0 and proc.stdout.strip("\0"):
        paths = [root / name for name in proc.stdout.split("\0") if name]
    else:
        paths = [p for p in root.rglob("*") if p.is_file()]
    out = []
    for path in paths:
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        name = path.name.lower()
        if path.suffix.lower() not in SOURCE_SUFFIXES or name.startswith("test") or "_test." in name \
                or ".test." in name or ".min." in name or "tests" in {p.lower() for p in rel.parts[:-1]}:
            continue
        out.append(path)
    return out


def pick_file(root: Path) -> Path | None:
    """The small source file closest to IDEAL_BYTES, or None if there isn't one.

    Files under MIN_BYTES are used only when nothing bigger fits, so a brand-new repo with
    one tiny file still gets a demo.
    """
    sized = []
    for path in _candidates(root):
        try:
            size = path.stat().st_size
            if 0 < size <= MAX_BYTES and b"\0" not in path.read_bytes()[:4096]:
                sized.append((size < MIN_BYTES, abs(size - IDEAL_BYTES), str(path), path))
        except OSError:
            continue
    return min(sized)[-1] if sized else None


@dataclass
class DemoResult:
    file: str
    result: ChatResult
    saved_to: Path


def run_demo(root: Path, file: Path, client: BharatCodeClient) -> DemoResult:
    root, file = root.resolve(), file.resolve()
    rel = file.relative_to(root).as_posix()
    listing, _ = numbered_listing(root, rel, max_chars=MAX_BYTES * 3)
    result = client.chat(review_messages(listing), max_tokens=DEMO_MAX_TOKENS, reasoning_effort="none",
                         gate={"gate_source": "demo"})
    out = root / ".gtu" / "demo" / f"review-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gtu" / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    body = f"# Demo review: `{rel}`\n\n{result.content.strip()}\n"
    if result.truncated:
        body += "\n> Cut off at the demo's short output limit; real review jobs allow much longer answers.\n"
    out.write_text(body, encoding="utf-8")
    return DemoResult(rel, result, out)
