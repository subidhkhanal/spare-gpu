---
# Installed by spare-gpu (`gtu integrate`); `gtu integrate --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
# The handoff itself runs on Qwen (every plan has it, and it's rarely queued): it is one tool call,
# and running it on a stalled DeepSeek would make /auto wait in the very queue it exists to avoid.
description: Hand a task to spare-gpu, which runs it on whichever model starts it sooner
model: bharatcode/qwen-3.8-27b
---
Hand this task to spare-gpu with its `spare_delegate` tool instead of doing it yourself in this chat:

$ARGUMENTS

- type: "tests" (write tests), "docs" (docstrings and README text, no code changes) or "review" (a code review). If the task is none of these, say that spare-gpu handles only tests, docs and review, and stop.
- path: the file or folder the task names, exactly as written.
- note: any other instruction in the task, in its own words.
- Leave your_model out: the task then runs on DeepSeek V4.1 Flash unless the other model would start it sooner.

If no task was given after /auto, call `spare_waits` instead and show the current waits.

Reply with spare_delegate's summary as it comes back (the job, which model it runs on and why, the branch). Don't do the work yourself and don't wait for it to finish.
