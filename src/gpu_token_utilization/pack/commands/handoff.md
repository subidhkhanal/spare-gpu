---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Write where this chat stopped into the hand-off file, so a fresh chat can pick it up with /pickup
---
Extra note from the user, between the markers (may be empty):
<<<$ARGUMENTS>>>

Working tree right now (`git status --porcelain`; empty means clean or not a repo):
!`git status --porcelain`

Recent commits:
!`git log --oneline -5`

Write `.gtu/handoff.md` (replace it if it exists) from this conversation, with these sections:

## Goal
What this chat was trying to achieve, in one or two sentences.
## Done
What was finished, with file paths. Facts only: nothing you didn't see happen in this chat.
## Next
The concrete next steps, in order, each one line.
## Open questions
Decisions still to be made, and what each one depends on.
## Files
From the status above: which changed files belong to this work, and which were already there.
## Pointers
Commands that matter (how to run the tests, how to start the app), decisions made and why.

Keep it under 80 lines and put no secrets in it. Then reply with each section's first line and
end with: "A new chat picks this up with /pickup." If something came up that every future chat
should know (a convention, a trap, a decision that holds), suggest the exact line to add with
/remember; don't add it yourself.
