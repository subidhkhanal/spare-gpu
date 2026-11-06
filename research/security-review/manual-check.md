# Manual check on Windows: /security-review

Run after `gtu integrate --pack --only split,security` and a full restart of BharatCode Desktop.
Record the outcome of each check. Nothing here was run by the implementer.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Seeded injection is found | Add an uncommitted change that formats a request parameter into an SQL string, then `/security-review`. | Five `security-finder` sessions (Qwen) start at once; `security_filter` is called; the verifier (DeepSeek) keeps the finding with a score of 8 or more; the report names `file:line`, the exploit scenario and a fix. | |
| 2 | Known false positives are dropped by rule | Add a change that only removes a rate limit and one in a `tests/` file. | Candidates for them appear in the finders' output and are listed as DROPPED by `security_filter`; none is in the report. | |
| 3 | Nothing runs | Watch every session's tool calls. | Only read, glob, grep, list and git's read-only commands; no file is written; nothing is executed. | |
| 4 | Clean diff | A harmless change, then `/security-review`. | "No findings" and the sentence that this is not proof the code is secure. | |
| 5 | `deep` | `/security-review deep`. | The verifier runs twice, the second time refuting; only candidates that survive both are reported. | |
| 6 | A commit and a branch | `/security-review <sha>`, `/security-review <branch>`. | The lead uses `git show` / `git diff <base>...<branch>`. | |
| 7 | Cost summary | The last paragraph. | Model per phase, runs, queue time saved from `spare_waits` (or "too little data"). | |
| 8 | Benchmark | See research/security-review/benchmark.md. | Precision at or above the built-in `/review` on the seeded set. | |
