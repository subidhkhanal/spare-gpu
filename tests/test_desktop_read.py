"""DesktopAgent reads a session's messages: every text part counts, so a review's body isn't lost
behind a closing remark."""

from gpu_token_utilization.agent import AgentResult
from gpu_token_utilization.desktop import DesktopAgent


def message(parts, completed=True):
    return {"info": {"role": "assistant", "time": {"completed": 1 if completed else None}}, "parts": parts}


def test_every_text_part_is_kept_in_order():
    result = AgentResult(exit_code=None, text="")
    seen: set[str] = set()
    steps = []
    done = DesktopAgent._read([
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "review it"}]},
        message([
            {"type": "text", "text": "## Likely bugs\n- `src/x.py:3` off by one"},
            {"type": "step-finish", "id": "s1", "tokens": {"input": 100, "output": 20}},
            {"type": "text", "text": "Let me know if you want more detail."},
        ]),
    ], seen, result, steps.append)
    assert done is True
    assert result.text == "## Likely bugs\n- `src/x.py:3` off by one\nLet me know if you want more detail."
    assert len(steps) == 1 and seen == {"s1"} and result.error is None


def test_an_unfinished_message_is_not_done_yet():
    result = AgentResult(exit_code=None, text="")
    assert DesktopAgent._read([message([{"type": "text", "text": "working"}], completed=False)], set(), result, lambda s: None) is False
