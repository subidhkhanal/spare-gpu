---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Suggest allow rules for the read-only commands you keep approving, so Desktop asks less often
agent: spare-helper
---
Command: /prompts. The user's words after it, between the markers (may be empty):
<<<$ARGUMENTS>>>

Call the `prompt_rules` tool once:
- days: a number of days the words give, else 30;
- min_sessions: a minimum the words give (`--min 5`, "at least 5 times"), else 3;
- channel: a Desktop build the words name (stable, beta, dev or local), else leave it out.

Relay its answer: the table of commands, the snippet exactly as it comes (in a code block), and
the file it goes in. Say that nothing was changed: the user pastes the snippet themselves.
