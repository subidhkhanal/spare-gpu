# /code-review benchmark against BharatCode's built-in /review (plan §8)

**Status: designed, not run.** Needs BharatCode Desktop on Windows. No number below is a result.

## Ship rule

Recall at or above the built-in `/review` on the seeded bugs, and precision at or above 70%
(findings that are real bugs, judged by a person). If `/code-review` isn't better than `/review`
on recall, it doesn't ship as a command; the agents stay as an experiment.

## Material

- 5 or more real repositories with a working test command (Python, JavaScript, Go), small enough
  to review in minutes.
- 30 seeded bugs, 6 per repository, made as mutations of committed code: off-by-one, inverted
  condition, wrong default, missing null check, swapped arguments, a resource not closed. Each
  bug is one uncommitted change; record the file:line and the mutation.
- 10 clean diffs (comments, renames, docs) to measure false positives.

## Method

For each diff, run `/review` (built in), then `/code-review` at medium, high and deep. Record per
run: the findings (file:line, claim), minutes, the model per phase, tokens per model (Desktop's
session detail), and `spare_waits` at the start. A finding matches a seeded bug when it names the
bug's line (or the line that fails because of it) and the claim describes the mutation.

Recall = matched seeded bugs / 30. Precision = findings judged real / all findings. Also record
DeepSeek calls per run: `/review` uses one; `/code-review` uses 1 (lead) + 1 or 2 (verifier).

## Results

| Setup | Recall | Precision | Minutes (median) | DeepSeek calls | Qwen calls |
|---|---|---|---|---|---|
| built-in /review | | | | 1 | 0 |
| /code-review medium | | | | | |
| /code-review high | | | | | |
| /code-review deep | | | | | |
