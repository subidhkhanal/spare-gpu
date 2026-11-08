---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Review your changes, a commit, a branch or a GitHub pull request with parallel finders on the standard model and a verifier on the heavy model; --fix applies the kept findings in a private copy, --post comments on the PR
agent: review-lead
---
Review request, between the markers:
<<<$ARGUMENTS>>>

It may start with an effort level (low, medium, high or deep; default medium), then name a
target (a commit, a branch, a pull request such as `42`, `#42`, `pr 42` or a PR link, or nothing
for the uncommitted changes), and may end with `--fix`, `--post` (pull requests only: post the
findings as a PR comment) or `--plan`. Carry out the review-lead procedure from its first step.
