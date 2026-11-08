---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: What today cost, tokens per model from Desktop's own record and what spare-gpu's jobs used and kept
agent: spare-helper
---
Command: /cost. The user's words after it, between the markers (empty means today):
<<<$ARGUMENTS>>>

Call the `cost_summary` tool once:
- days: the period the words give, in days (`7`, "week" = 7, "month" = 30); 1 when they give none;
- channel: a Desktop build the words name (stable, beta, dev or local), else leave it out;
- short: false only if the words ask for the tables or the details.

Relay those figures as they are, in plain words, in at most five lines (the tables as they are
when asked for). Don't estimate anything they don't show.
