---
# Installed by spare-gpu (`gtu integrate`); `gtu integrate --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
# The handoff itself runs on Qwen (every plan has it, and it's rarely queued): it is one tool call,
# and running it on a stalled DeepSeek would make /auto wait in the very queue it exists to avoid.
description: Run a task or question on whichever model starts it sooner
model: bharatcode/qwen-3.8-27b
---
The user's request is between the markers:
<<<$ARGUMENTS>>>

Follow exactly one of these, checked in this order, and call exactly one tool:

1. Empty request (nothing between <<< and >>>): call `spare_waits` and show its answer as it comes back. Call no other tool. Never queue any work for an empty request.

2. The request asks for tests, docs or a code review of a file or folder in this project, and names it: call `spare_delegate` with
   - type: "tests" (write tests), "docs" (docstrings and README text, no code changes) or "review" (a code review);
   - path: the file or folder the request names, exactly as written;
   - note: any other instruction in the request, in its own words;
   - your_model: "deepseek-v4.1-flash" (it runs there unless the other model would start it sooner).
   The job runs now, in front of the user, as a Desktop session named "spare-gpu: ..." that they can open and watch; the tool returns when it is done. Then reply with spare_delegate's answer as it comes back: how it went, where its files landed, which model and why. Don't do the work yourself.

3. Anything else (a question, an explanation, a quick piece of writing): call `spare_ask` with
   - question: the request, exactly as written between the markers;
   - your_model: "deepseek-v4.1-flash".
   Then reply with spare_ask's answer as it comes back, including the line saying which model answered. Don't answer it yourself.
