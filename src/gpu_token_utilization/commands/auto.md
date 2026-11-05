---
# Installed by spare-gpu (`gtu integrate`); `gtu integrate --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
# The handoff itself runs on Qwen (every plan has it, and it's rarely queued): it is one tool call,
# and running it on a stalled DeepSeek would make /auto wait in the very queue it exists to avoid.
description: Run a task or question on whichever model starts it sooner
model: bharatcode/qwen-3.8-27b
---
Hand this to spare-gpu instead of doing it yourself in this chat:

$ARGUMENTS

If it asks for tests, docs or a code review of files in this project, call `spare_delegate`:
- type: "tests" (write tests), "docs" (docstrings and README text, no code changes) or "review" (a code review).
- path: the file or folder it names, exactly as written.
- note: any other instruction in it, in its own words.
- your_model: "deepseek-v4.1-flash" (it runs there unless the other model would start it sooner).
Then reply with spare_delegate's summary as it comes back (the job, which model and why, the branch). Don't do the work yourself and don't wait for it to finish.

Anything else (a question, an explanation, a quick piece of writing): call `spare_ask` with
- question: the whole text above, as written.
- your_model: "deepseek-v4.1-flash".
Then reply with spare_ask's answer as it comes back, including the line saying which model answered. Don't answer it yourself.

If nothing was given after /auto, call `spare_waits` and show the current waits.
