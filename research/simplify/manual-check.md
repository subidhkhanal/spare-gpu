# Manual check on Windows: /simplify

Run after `gtu integrate --pack --only split,simplify` and a full restart of BharatCode Desktop,
in a project with a test command spare-gpu allows. Record the outcome of each check.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Finders in parallel | `/simplify src/` on a folder with a duplicated helper and a dead branch. | Several `simplify-finder` sessions (Qwen) start from one message; the lead (DeepSeek) shows an accepted list with file:line, what, why, risk. | |
| 2 | Only behaviour-preserving changes | Look at the accepted list. | No renames, formatting, docstring or test changes; nothing touching a public interface. | |
| 3 | Applied in a private copy | After the lead's reply, `gtu list`. | A `fix` job exists; when done, `spare-gpu/<date>_<time>/` holds the changed files; your files are untouched. | |
| 4 | Kept only if tests pass | Make the test command fail (break a test), run `/simplify` again. | The job's verify step says `Kept the fix in 0 file(s): the test command failed with it`; nothing lands in spare-gpu/. | |
| 5 | Nothing to do | `/simplify` on a small clean file. | "as simple as it should be" and no job started. | |
| 6 | Reviewer acceptance | See research/simplify/benchmark.md. | At least 70% of applied changes accepted by a reviewer. | |
