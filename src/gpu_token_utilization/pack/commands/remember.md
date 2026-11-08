---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Add a note to this project's memory file, which every agent reads at the start of each request
model: {{STANDARD_MODEL}}
---
Note to remember, between the markers:
<<<$ARGUMENTS>>>

The memory file is `.gtu/memory.md` in this project. Every agent gets it with every request, so it
costs tokens on every step: keep it short and keep it facts.

1. If the note is empty: read `.gtu/memory.md` (say there is none yet if it doesn't exist), show it,
   and stop. Change nothing.
2. Read `.gtu/memory.md`. If it doesn't exist, create it with this first line and nothing else:
   `# Project memory (read by every agent at the start of each request; keep it short)`.
3. Append one line: `- ` followed by the note in the user's words, trimmed to one sentence, with
   no secrets (no keys, tokens, passwords or private links; refuse those and say why).
4. If the file is now over 60 lines or 4,000 characters, say so, name the lines that look stale or
   duplicated, and ask whether to drop them. Never delete lines on your own.
5. Reply with the line you added and the file's line count. Don't touch any other file.
