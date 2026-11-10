# Benchmarks for the improvement loop

`scripts/improve.py` scores a change to spare-gpu against the current version on one of these
benchmarks, and keeps the change only when it wins by a clear margin and you promote it. The code
is in `src/gpu_token_utilization/improve.py` (the loop) and `bench.py` (the benchmarks); the
records of every run, candidate and promotion are in `research/improve/`.

| Target | Kind | What is scored | Material |
|---|---|---|---|
| `verify-refs` | settings | Does `verify_refs` keep the real references and drop the bad ones? | `verify-refs/references.json`: references from real `/deep-research` runs with the registries' recorded answers; `labels.json`: the hard cases a person decided |
| `router` | settings | Would `/auto`'s routing have saved waiting, with a move charged 10 s? | `router/starts.json`: when each real Desktop reply started, per model |
| `collect-sources` | code | Does `collect_sources` read every scout's entry and group the same source once? | `collect-sources/runs/*/notes`: real scouts' notes; `expected.json`: a person's grouping |
| `code-review` | prompts | Is each planted bug found, and is a harmless change left alone? | `code-review/project` and `seeds.json` (8 bugs, 2 harmless changes) |
| `security-review` | prompts | Is each planted vulnerability found, and is the bait left alone? | `security-review/project` and `seeds.json` (8 vulnerabilities, 3 decoys) |
| `simplify` | prompts | Does a clean-up land, shorten the file and keep hidden tests passing? Is a clean file left alone? | `simplify/project`, `tasks.json`, `hidden_tests/` (never shown to the job) |
| `auto-handoff` | prompts | Is the handoff tool the agent's first call? | `auto-handoff/messages.json` (20 messages, some trying to make it answer itself) |
| `deep-research` | prompts | References kept, claims the checker left unmarked, checklist items covered | `deep-research/tasks.json`: one question of each kind |

The first three replay recorded material: they take seconds, and `tests/test_bench.py` fails if
the current code scores below the bar in `research/improve/<target>/bar.json`. The other five run
the command in BharatCode Desktop, so they cost tokens and time (a `/deep-research` task takes
about half an hour) and run only when you start them.

## A settings target

```powershell
python scripts/improve.py router evaluate          # the current settings' score
python scripts/improve.py router propose           # searches the grid; saves a candidate only if one wins
python scripts/improve.py router evaluate --candidate <id>
python scripts/improve.py router decide <id>
python scripts/improve.py router promote <id>      # writes tuning.json, archived first
python scripts/improve.py router rollback          # if you change your mind
```

## A prompts target (from Desktop's terminal)

```powershell
python scripts/improve.py code-review install                    # the current prompts; restart Desktop
python scripts/improve.py code-review evaluate                   # score and failures
python scripts/improve.py code-review propose                    # DeepSeek rewrites the prompts from the failures
python scripts/improve.py code-review install --candidate <id>   # restart Desktop
python scripts/improve.py code-review evaluate --candidate <id>
python scripts/improve.py code-review decide <id>
python scripts/improve.py code-review promote <id>               # or `install` to go back; restart Desktop
```

`--only id,id` runs a few tasks for a quick look. A rewrite keeps every file's frontmatter
(model, permissions, steps) and may not grow a file by more than a quarter; read the candidate's
files before you install them.

## A code target

Change the code, run `python scripts/improve.py collect-sources evaluate`, and keep the change
only if the score holds or rises. `bar` records the new score as the bar the test holds.

## Refreshing the recorded material

`python scripts/build_benchmarks.py` rebuilds it from your own runs (it needs Desktop and the
internet): `refs <research folders>` records the registries' answers and lists the references a
person should label, `notes <name>=<lead session>` exports the scouts' notes of a run, and
`starts` exports reply start times. Review what it writes before committing it.

## Rules

- A candidate is better when it beats the current version by 5% of its score, or by a fifth of
  the room left below a perfect 1.0 when that is smaller (at least 0.005), and isn't worse task by task (a paired bootstrap's lower bound of the differences is not below zero).
- Nothing is promoted without your command, nothing is committed or pushed, and every promotion is
  archived so `rollback` can undo it.
