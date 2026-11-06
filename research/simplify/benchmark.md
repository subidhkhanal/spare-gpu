# /simplify benchmark (plan §8)

**Status: designed, not run.** Needs BharatCode Desktop on Windows. No number below is a result.

## Ship rule

Tests pass in 100% of applied runs (guaranteed by the fix job: a run whose tests fail applies
nothing), and at least 70% of the applied changes are accepted by a reviewer who knows the code.

## Material

15 files from 5 repositories with a working test command, chosen to include duplicated helpers,
dead branches, and loops that do double work, and some files that are already clean.

## Method

Run `/simplify <file>` on each. Record: candidates proposed, accepted by the lead, applied by the
job (tests passed or not), and, for each applied change, the reviewer's verdict (accept, reject,
and why). Record minutes, model per phase and tokens per model.

## Results

| Files | Candidates | Accepted by lead | Applied (tests passed) | Accepted by reviewer |
|---|---|---|---|---|
| | | | | |
