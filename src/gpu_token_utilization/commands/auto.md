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

Follow exactly one of these and call exactly one tool:

1. Empty request (nothing between <<< and >>>): call `spare_waits` and show its answer as it comes back. Call no other tool.

2. Anything else (a question, a change to make in the project, anything a normal chat would do): call `spare_auto` with
   - request: the request, exactly as written between the markers; if it starts with the word "new", leave that word out;
   - new: true only if the request starts with the word "new" (the user wants a fresh conversation);
   - your_model: "deepseek-v4.1-flash".
   It runs as a normal BharatCode session with every tool, on the faster model, and remembers this chat's earlier /auto turns. Then reply with spare_auto's answer as it comes back, including the line saying which model answered and where its session is. Don't do the work yourself.
