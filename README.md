**Type `/auto` in BharatCode Desktop and your task runs on whichever model will start it sooner, DeepSeek or Qwen, so you don't sit in a queue.**

## What it does

- **Picks the faster model.** If your model is busy, spare-gpu runs the task on the other one when that starts it sooner. Moving up to DeepSeek is always allowed. Moving down to Qwen happens for docs jobs, the one job type where Qwen was measured to be just as good, and for questions, which go to the faster model by design rather than by measurement. Tests and reviews stay on DeepSeek.
- **One command: `/auto`.** Type `/auto` and what you want: a question, tests, docs or a code review. The reply says which model it used and how much waiting that saved.
- **Does the chores in the background.** Tests, docs and reviews run as their own Desktop session, so your chat stays free.
- **Keeps only good work.** Generated tests are kept only if they pass, and docs jobs may not change code. The files a job edited are copied to `spare-gpu/<date>_<time>/` in your project. Your own files are never touched: open the folder and copy over what you want.
- **Keeps you in control of tokens.** Every job has a token budget, and a job that crashes resumes where it stopped.

## Set up

With BharatCode Desktop installed and signed in, paste this into PowerShell:

```powershell
irm https://raw.githubusercontent.com/subidhkhanal/spare-gpu/master/install.ps1 | iex
```

Then fully close BharatCode Desktop and open it again. That's all: no API key needed.

The installer ([install.ps1](install.ps1)) installs Python and git with winget if they're missing, installs spare-gpu into its own folder (`%LOCALAPPDATA%\spare-gpu`), connects it to BharatCode Desktop, adds `/auto` and adds the `gtu` command. It installs the newest tagged release and prints the version it installed; set `$env:SPARE_GPU_VERSION = '0.1.0'` first to pin one. Run the same line again to update. (Until the first release is published it installs the development version from `master`, and says so.)

To remove it: `gtu integrate --uninstall`, then delete `%LOCALAPPDATA%\spare-gpu`.

Prefer to do it by hand? With Python 3.11+ and git installed: `pipx install git+https://github.com/subidhkhanal/spare-gpu`, then `gtu integrate`.

## `/auto` mode

### Turn it on

The installer adds `/auto` to BharatCode Desktop. After installing, fully close Desktop and open it again, then open a chat in your project and type `/`: `/auto` is in the command list. If you installed spare-gpu before `/auto` existed, run the install line again to update.

### Use it

Type `/auto` and then what you want, in plain words:

| You type | What happens |
|---|---|
| `/auto what does a race condition mean?` | Answers the question right in the chat, on the faster model |
| `/auto write tests for src/payments.py` | Writes tests in the background and keeps only the ones that pass |
| `/auto write docs for src/utils.py` | Adds docstrings and README text in the background, without changing code |
| `/auto review src/gate.py` | Writes a code review in the background |
| `/auto` | Shows how long each model would wait right now |

For example, on 5 Nov DeepSeek had a 3-minute queue while Qwen had a free slot:

```
/auto what is the name of the latest mac chip
→ Apple M5 (M5 Pro / M5 Max for pro Macs)
  (Answered by Qwen 3.8 27B: saves about 2m 43s of waiting over DeepSeek V4.1 Flash, and its lane has a free slot.)
```

### How it picks the model

`/auto` starts from DeepSeek V4.1 Flash, the stronger model, and checks BharatCode's live queues each time:

| Right now | Questions and docs run on | Tests and reviews run on |
|---|---|---|
| DeepSeek free | DeepSeek | DeepSeek |
| DeepSeek queued, Qwen free | **Qwen** | DeepSeek (Qwen isn't measured as good enough yet) |
| Qwen queued, DeepSeek free | DeepSeek | DeepSeek |

It only switches when that saves at least 20 seconds and the other model has a free slot, so switched work never lands in a queue. If your plan doesn't include DeepSeek, everything runs on Qwen.

Questions aren't measured: a quick answer from whichever model is free is the point of `/auto`, so they go to the faster model by design, and the reply names the model that answered. Job types move only after measurement: docs were measured as good on Qwen; tests and reviews weren't shown to be, so they stay on DeepSeek.

### Turn it off

`gtu integrate --uninstall` removes `/auto` and spare-gpu. If you edited your copy of `/auto` (`command/auto.md` in Desktop's config folder), it's left as it is.

## The pack (in progress)

`gtu integrate --pack` adds a pack of commands for BharatCode Desktop that split each job between the two models: DeepSeek plans, judges and verifies; Qwen does the many quick, parallel, self-contained steps. It uses only BharatCode's own extension points (Markdown commands and agents, spare-gpu's MCP tools, and a config file of the pack's own next to yours, which is never edited). Components, chosen with `--only`: `split` (titles and the explore subagent run on Qwen), `research` (`/deep-research`), `review` (`/code-review`), `security` (`/security-review`), `simplify` (`/simplify`) and `context` (`/context-report`). The commands land one by one; `split` is in. `--lite` is for a Qwen-only plan. `gtu integrate --pack --uninstall` removes exactly what the pack added. Model ids are read from bharatcode.ai's model list at install time.

## What you get

Background work lands in your project when it's done. Ask "is my job done?" to check on it.

```
your-project/
└── spare-gpu/
    └── 2026-11-05_12-38-50/      ← when the job finished
        ├── README.md             ← only the files the job edited
        └── src/
            └── mathx.py
```

## Or just ask

You don't have to type `/auto`. spare-gpu also understands plain requests in any Desktop chat, and picks the faster model the same way, starting from your chat's model:

| You say | What happens |
|---|---|
| "write tests for src/pay.py on whichever model is free" | Hands it to the model that starts sooner |
| "queue tests for src/payments" | Runs it in the background; the result lands in `spare-gpu/` |
| "how long is DeepSeek's queue?" | Shows each model's current wait |
| "is my tests job done?" | Shows queued, running and finished jobs |
| "what did spare-gpu do last night?" | Finished jobs, where their files landed and the tokens used (`gtu report` in a terminal has the full summary) |
| "cancel the docs job" | Cancels it |

## Why it's useful

- **Less waiting.** DeepSeek's queue can reach minutes at busy times, while Qwen often has a free slot, and the other way round. `/auto` sends your work to whichever is free, without you watching queues.
- **No quality loss on background work.** Jobs only move to Qwen for job types where Qwen was measured to be just as good, and every result is checked before it's kept. Questions are the one unmeasured case: they go to the faster model by design, and the reply says which model answered.
- **Less babysitting.** Chores run on their own, get checked, and wait for you in the `spare-gpu/` folder.
- **No surprises.** It never edits your files, never pushes, and only runs your test, build and lint commands.

## Good to know

- Windows only, and jobs run while BharatCode Desktop is open.
- `/auto`'s own handoff, a single short request, always runs on Qwen. If Qwen is packed, that one request waits briefly before your task moves to DeepSeek.
- Works in any project folder: a git repo or not, even with unsaved changes. Each job starts from a private copy of your files as they are, and when it finishes only the `spare-gpu/<date>_<time>/` folder is left: no branches or history in your project. That folder ignores itself in git, so it never shows up as a change.
- Needs git installed on your PC (the installer adds it), since spare-gpu uses it to manage each job's private copy. Your project doesn't need to use git.
- Dependency, cache and build-output folders (`node_modules`, `venv`, `dist`, `build`, `target`, `.next`, `.gradle`, `out`, `coverage` and the like) aren't copied into a job's copy, unless your repo tracks them. If your project isn't a git repo and its copy would be over 200 MB, queueing a job says so, and the job's log repeats it.
- Which job types may move to Qwen comes from a small evaluation (3 tasks per type); see [research/eval/](research/eval/). Questions aren't part of it.
- More detail and the measurements behind it: [research/](research/).

MIT licence, see [LICENSE](LICENSE). An independent project for the BharatCode Build League, not affiliated with BharatCode.
