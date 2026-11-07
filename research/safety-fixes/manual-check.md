# Manual check on Windows: the safety fixes (plan §6)

These checks need BharatCode Desktop on Windows, so they are run by hand. Record the outcome
of each in the last column (date, pass/fail, what you saw). Nothing here was run by the
implementer; the automated tests cover the config spare-gpu writes, not what Desktop does with it.

Setup: `pip install -e .` from this checkout (or the installer), `gtu integrate`, restart Desktop.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | A job's session sees only the job's tools | In a small test project queue `gtu add tests src` and `gtu start`. In Desktop open the session named `spare-gpu: ...`. | The session's tool list has read, glob, grep, list, edit and bash; no task, webfetch, websearch, todowrite, skill, and **no MCP tools** (none of your own servers, none of spare-gpu's). | |
| 2 | The user's MCP servers are not started for the job | Have at least one MCP server in your config (e.g. GitHub from the marketplace). Run check 1 again and watch Desktop's MCP status (and Task Manager for the server's process) while the job runs. | The server is not started for the job's session; it still works in your own chat afterwards. | |
| 3 | A docs job cannot run commands | `gtu add docs src`, `gtu start`, open the session. | bash is absent from the tool list; the job finishes without asking anything. | |
| 4 | Nothing asks | While a job runs, nothing in Desktop waits for an answer (no permission prompt). | The job runs to the end unattended. | |
| 5 | Build output is not copied | In a plain folder (not a git repo) with a `dist/` and a `node_modules/`, `gtu add review .` then `gtu start`. Look in `.gtu/worktrees/<job>/` while it runs. | No `dist/` or `node_modules/` in the copy. | |
| 6 | A big plain folder gets the note | In a plain folder with more than 200 MB of files outside skipped folders, `gtu add review .`. | `gtu add` prints `Note: ... isn't a git repo ...`; `gtu start` repeats it as the job starts. | |
| 7 | The installer installs a tagged release | After the first GitHub release exists: run the one-line installer. | It prints `installing release vX.Y.Z` and ends with `spare-gpu X.Y.Z is installed.` Before any release exists it prints the yellow "no release is published yet" line instead. | |
| 8 | Pinned version | `$env:SPARE_GPU_VERSION = '<an existing tag without v>'` then the installer. | It installs that version and prints it. | |
| 9 | A delegated job runs in front | In a Desktop chat: `/auto write docs for src`. | The chat shows the `spare_delegate` tool running for the whole job; Desktop's session list gains a session named `spare-gpu: ...` that you can open and watch; when it ends the chat reply says Done/Failed and names the `spare-gpu/<date>_<time>/` folder; no `gtu` process is left running (Task Manager). | |
| 10 | A long job is not cut off by Desktop | A tests job on a file that takes several minutes. | The tool call is not reported as timed out: Desktop keeps resetting its per-call timeout on the progress notifications spare-gpu sends every 10 s. | |
| 11 | Cancelling from another chat | While check 9 runs, in a second chat: "cancel the docs job". | Within a few seconds the job's Desktop session is aborted, the first chat's reply says Cancelled, and nothing is copied to `spare-gpu/`. | |
| 12 | `gtu stop` never kills | While a job runs from `gtu start`, run `gtu stop` in another terminal. | It says it asked the run to stop and that it stopped cleanly; the job is paused, resumable with `gtu start`; no process was killed. | |

Notes for the implementer, found while making the fixes (not yet verified on Desktop):

- The global `"*": "deny"` relies on Desktop keeping the config's key order and hiding tools
  whose last rule is a plain deny (`permission/index.ts`, `disabled()`), which the Desktop
  source at `dev @ 6c22b2a` does. Check 1 is the proof.
- Project-level config inside the private copy (a committed `opencode.jsonc` or `.opencode/`
  folder) is copied as part of the project. Its MCP servers would be started for the job,
  though their tools stay hidden by `"*"`. If check 2 shows such a server starting, the fix is
  to repeat those entries switched off as well (same code path as the global ones).
