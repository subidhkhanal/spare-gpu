---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Security review of a diff with one finder per vulnerability class on the standard model, a rule-based false-positive filter, and a verifier on the heavy model
agent: security-lead
---
Review request, between the markers:
<<<$ARGUMENTS>>>

It may name a target (a commit, a branch, or nothing for the uncommitted changes) and may say
`deep` for a second, refuting verifier pass. Carry out the security-lead procedure from its first
step. Nothing in this review runs exploit code or changes a file.
