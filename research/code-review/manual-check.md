# Manual check on Windows: /code-review

Run after `gtu integrate --pack --only split,review` and a full restart of BharatCode Desktop, in a
project with a test command spare-gpu allows (`python -m pytest`, `pytest`, `npm test`, `go test`,
or GTU_ALLOWED_COMMANDS). Record the outcome of each check. Nothing here was run by the implementer.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Uncommitted changes, default effort | Edit a file so it has a real bug (an off-by-one), then `/code-review`. | The lead (DeepSeek) prints the diff summary, starts 4 `review-finder` sessions at once (Qwen), then one `review-verifier` (DeepSeek), and reports the bug with file:line and a failure scenario. | |
| 2 | Effort levels | `/code-review low`, `/code-review high`, `/code-review deep`. | 2, 6, and 12 finder runs; deep runs the verifier twice (the second told to refute). | |
| 3 | A commit and a branch | `/code-review <sha>` and `/code-review <branch>`. | The lead uses `git show` / `git diff <base>...<branch>`; a pull request number is refused with a request for the branch name. | |
| 4 | Finders can't edit or run other commands | Open a finder's session: tool list. | read, glob, grep, list and git's read-only commands only. | |
| 5 | Clean diff | Make a harmless change (a comment) and `/code-review`. | "No findings" is reported plainly; no invented problem. | |
| 6 | --fix keeps only a passing fix | With the off-by-one from check 1: `/code-review --fix`. | The lead calls `spare_delegate` with type `fix`; `gtu list` shows the job; when done, `spare-gpu/<date>_<time>/` holds the fixed file; your own files are untouched. | |
| 7 | --fix drops a failing fix | Add a finding by hand to the note whose "fix" breaks the tests (or break the test command). | The job's verify step says `Kept the fix in 0 file(s): the test command failed with it`; nothing lands in spare-gpu/. | |
| 8 | A fix can't weaken tests | Ask in the note to "simplify tests/test_x.py". | `reverted tests/test_x.py: a fix may not change existing tests` in the verify output. | |
| 9 | Cost summary | The lead's last paragraph. | Model per phase, number of finder runs, queue time saved from `spare_waits` (or "too little data"). | |
| 10 | Benchmark against built-in /review | See research/code-review/benchmark.md. | Recall at or above /review's; precision at or above 70%. | |
