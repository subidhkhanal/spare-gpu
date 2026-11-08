---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: What today cost, tokens per model from Desktop's own record and what spare-gpu's jobs used and kept
---
Period asked for, between the markers (empty means today):
<<<$ARGUMENTS>>>

Run `gtu cost --short` in this project with your shell tool; for a period other than today, add
`--days N` (N days back, e.g. `--days 7` for a week). Run it here, in the project: it reads this
project's jobs from its .gtu folder.

Relay those figures as they are, in plain words, in at most five lines. Don't estimate anything
they don't show. If the command fails or `gtu` isn't found, say so and that `gtu cost` in a
terminal in this project shows the same.
