---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Copy a finished job's files from spare-gpu/ into your project; /undo takes it back
agent: spare-helper
---
Command: /apply. The user's words after it, between the markers (may be empty):
<<<$ARGUMENTS>>>

Call the `apply_result` tool once:
- stamp: a result folder the words name (like `2026-11-05_12-38-50`), else "latest";
- dry_run: true if the words say `--dry-run`, "dry run", "preview" or "what would happen";
- force: true only if the words say `--force` or ask to replace files that changed.

Relay its answer. If files were copied in, end with one line: `/undo` takes them back.
