---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Pick up where the last chat stopped, from the hand-off file and the project memory
---
Request for this chat, between the markers (may be empty):
<<<$ARGUMENTS>>>

1. Read `.gtu/handoff.md`. If it doesn't exist, say there is no hand-off yet (a chat writes one
   with /handoff) and stop.
2. Run `git status --porcelain` in this project with your shell tool (an error means the folder
   isn't a git repo: say so and skip this step). Compare the hand-off's Files section with it: files it listed that no longer show as
   changed, files that are new since. Note them; don't guess what happened to them.
3. Summarise in a few lines: the goal, what was done, what moved since the hand-off, and the next
   step from its Next section. The project memory is already in your context; use it, don't repeat it.
4. If the request above asks for it, carry out that next step now. Otherwise stop and wait: the
   user decides what this chat does.
