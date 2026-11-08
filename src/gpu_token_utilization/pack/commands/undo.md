---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Take back the last /apply, so your files are as they were
agent: spare-helper
---
Command: /undo. The user's words after it, between the markers (may be empty):
<<<$ARGUMENTS>>>

Call the `undo_result` tool once, with stamp: a result folder the words name (like
`2026-11-05_12-38-50`), else "latest". Relay its answer.
