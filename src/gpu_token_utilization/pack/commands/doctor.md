---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Check the whole spare-gpu install and say what to fix; /doctor live also measures a real session
agent: spare-helper
---
Command: /doctor. The user's words after it, between the markers (may be empty):
<<<$ARGUMENTS>>>

Call the `doctor_check` tool once:
- live: true if the words say `live` or `--live` (it runs one short model call);
- channel: a Desktop build the words name (stable, beta, dev or local), else leave it out.

Relay its answer: every FAIL and WARN line with its fix, then how many checks passed, then the
result line. Leave out nothing that failed.
