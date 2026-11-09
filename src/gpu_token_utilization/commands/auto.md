---
# Installed by spare-gpu (`gtu integrate`); `gtu integrate --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
# The handoff itself runs on Qwen (every plan has it, and it's rarely queued): it is one tool call,
# and running it on a stalled DeepSeek would make /auto wait in the very queue it exists to avoid.
# It runs as the small spare-auto agent (agent/spare-auto.md), not the full Build agent.
description: Do anything a normal chat does, on whichever model starts it sooner
model: bharatcode/qwen-3.8-27b
agent: spare-auto
---
The user's request is between the markers:
<<<$ARGUMENTS>>>

Follow exactly one of these, checked in this order, and call exactly one tool:

1. Empty request (nothing between <<< and >>>): call `spare_waits` and show its answer as it comes back. Call no other tool. Never queue any work for an empty request.

2. The request asks for tests, docs or a code review of a file or folder in this project, and names it; or asks to make the tests pass, or to fix the failing tests; or asks to implement a plan file: call `spare_delegate` with
   - type: "tests" (write tests), "docs" (docstrings and README text, no code changes), "review" (a code review), "green" ("make the tests pass", "fix the failing tests": change the code until the project's tests pass, never the tests) or "implement" ("implement docs/plan.md": carry out a Markdown plan file);
   - path: the file or folder the request names, exactly as written; for "green" with no file or folder named, "."; for "implement", the plan file the request names;
   - note: any other instruction in the request, in its own words;
   - your_model: "deepseek-v4.1-flash" (it runs there unless the other model would start it sooner).
   The job runs now, in front of the user, as a Desktop session named "spare-gpu: ..." that they can open and watch; the tool returns when it is done. Then reply with spare_delegate's answer as it comes back: how it went, where its files landed, which model and why. Don't do the work yourself.

3. Anything else (a question, a change to make in the project, anything a normal chat would do): call `spare_auto` with
   - request: the request, exactly as written between the markers; if it starts with the word "new", leave that word out;
   - new: true only if the request starts with the word "new" (the user wants a fresh conversation);
   - your_model: "deepseek-v4.1-flash".
   It runs as a normal BharatCode session with every tool, on the faster model, and remembers this chat's earlier /auto turns. Then reply with spare_auto's answer as it comes back, including the line saying which model answered and where its session is. Don't do the work yourself.
