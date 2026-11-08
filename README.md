# spare-gpu

**Less waiting in BharatCode Desktop.** Type `/auto` before your request, and spare-gpu runs it on whichever model can start it sooner, DeepSeek or Qwen.

## The problem it solves

BharatCode Desktop gives you two models: **DeepSeek** (stronger) and **Qwen** (faster to get). At busy times, one of them has a queue of several minutes while the other is free. Normally you sit in that queue.

spare-gpu checks BharatCode's live queues for you. When the other model would start sooner, and it's good enough for the task, your work goes there instead.

## What it does for you

- **Answers questions sooner** on whichever model is free.
- **Writes tests, docs and code reviews** for your project. Each job runs as its own Desktop chat that you can open and watch.
- **Never touches your files.** Each job works on a private copy of your project. The files it changed are put in a `spare-gpu/<date>_<time>/` folder, and you copy over what you want.
- **Keeps only good work.** Generated tests are kept only if they pass. Docs jobs may add documentation but not change code.
- **Tells you what it did:** which model it used, how much waiting that saved, and the tokens it used.

## Install

You need Windows and BharatCode Desktop, installed and signed in.

1. Open **PowerShell** and paste:
   ```powershell
   irm https://raw.githubusercontent.com/subidhkhanal/spare-gpu/master/install.ps1 | iex
   ```
2. Fully close BharatCode Desktop (also from the taskbar tray) and open it again.
3. In each project you use it in, run `gtu init` once. It finds how your tests run and saves that (see [Settings](#settings)).

That's it. No API key is needed. The installer adds Python and git if they're missing, installs spare-gpu in `%LOCALAPPDATA%\spare-gpu`, and connects it to Desktop. Run the same line again to update.

<details>
<summary>Installer options and manual install</summary>

- Pin a version: `$env:SPARE_GPU_VERSION = '0.1.0'` before the install line.
- Install from another copy of the repository: `$env:SPARE_GPU_REPO = 'owner/name'`.
- Until the first release is published, it installs the latest code from `master` and says so.
- By hand, with Python 3.11+ and git: `pipx install git+https://github.com/subidhkhanal/spare-gpu`, then `gtu integrate`.

</details>

## Use it

Open a chat in your project and type `/auto` followed by what you want:

| You type | What happens |
|---|---|
| `/auto what does a race condition mean?` | Answers in the chat, on the faster model |
| `/auto write tests for src/payments.py` | Writes tests and keeps only the ones that pass |
| `/auto write docs for src/utils.py` | Adds docstrings and README text without changing code |
| `/auto review src/router.py` | Writes a code review |
| `/auto` | Shows how long each model would wait right now |

Example from 5 Nov, when DeepSeek had a 3-minute queue:

```
/auto what is the name of the latest mac chip
→ Apple M5 (M5 Pro / M5 Max for pro Macs)
  (Answered by Qwen 3.8 27B: saves about 2m 43s of waiting over DeepSeek V4.1 Flash, and its lane has a free slot.)
```

You can also just ask in plain words, without `/auto`:

| You say | What happens |
|---|---|
| "write tests for src/pay.py on whichever model is free" | Runs it on the model that starts sooner |
| "how long is DeepSeek's queue?" | Shows each model's current wait |
| "is my tests job done?" | Shows queued, running and finished jobs |
| "what did spare-gpu do today?" | Lists finished jobs, their files and tokens |
| "cancel the docs job" | Cancels it within a few seconds |

## Where results go

When a job finishes, the chat tells you where its files are:

```
your-project/
└── spare-gpu/
    └── 2026-11-05_12-38-50/      ← when the job finished
        ├── README.md             ← only the files the job changed
        └── src/
            └── mathx.py
```

That folder is ignored by git, so it never shows up as a change.

## How it picks the model

It starts from DeepSeek, the stronger model, and switches only when that saves at least 20 seconds and the other model has a free slot:

| Right now | Questions and docs run on | Tests and reviews run on |
|---|---|---|
| DeepSeek free | DeepSeek | DeepSeek |
| DeepSeek queued, Qwen free | **Qwen** | DeepSeek |
| Qwen queued, DeepSeek free | DeepSeek | DeepSeek |

Moving up to DeepSeek is always allowed. Moving down to Qwen happens for docs jobs, the one job type where Qwen was measured to be just as good, and for questions, which go to the faster model by design rather than by measurement. Tests and reviews stay on DeepSeek. Questions aren't measured: a quick answer is the point, and the reply always names the model that answered. If your plan has only Qwen, everything runs on Qwen.

The measurement is a small evaluation, 3 tasks per job type: see [research/eval/](research/eval/).

## Extra commands (optional)

```powershell
gtu integrate --pack
```

This adds bigger commands. In each one, DeepSeek plans and checks, and several Qwen helpers do the many small steps at the same time. All of them are **experimental** until their benchmark in `research/<command>/` has been run.

| Command | What it does |
|---|---|
| `/code-review [low\|medium\|high\|deep] [commit or branch] [--fix]` | Reviews your uncommitted changes (or a commit or branch). Several helpers each look for one kind of bug; DeepSeek keeps only the findings with real evidence. With `--fix`, the fixes are applied in a private copy and kept only if your tests still pass. |
| `/security-review [commit or branch] [deep]` | A read-only security review. Five helpers each look for one kind of problem (injection, login and access, secrets, code execution, data leaks). Known false alarms are dropped by fixed rules, and only high-confidence findings are reported. |
| `/deep-research <question> [--depth quick\|standard\|deep]` | Writes a report with sources into `research/<topic>/`. Helpers search research papers (OpenAlex, Crossref, arXiv) and the web (through Exa, no key needed; your searches go to Exa). Every source is checked against the paper registries, and fake or wrong ones are removed. |
| `/simplify [file or folder]` | Suggests safe cleanups that don't change behaviour. They are kept only if your tests still pass. |
| `/verify-app <url> <what to check>` | Checks a running app in a real browser. Needs the Playwright add-on from Desktop's marketplace. |
| `/remember <note>` | Adds a note to `.gtu/memory.md`, which every chat in this project reads. `/remember` alone shows it. Keep it short: it costs tokens on every step. |
| `/handoff` | Saves where this chat stopped (goal, what's done, next steps, files involved) to `.gtu/handoff.md`. |
| `/pickup` | In a new chat: reads the hand-off and the memory, says what changed since, and waits for you. |

Each command ends with a cost summary: which model did what, and the exact tokens used in your project's Desktop chats.

Good to know about the pack:
- The pack's helpers and tools are hidden from your normal chats, so a normal chat doesn't pay for them (budget: at most 1,500 extra tokens per step).
- Some helper roles also ship as a standard-model variant on Qwen. They are only used once a benchmark shows Qwen is good enough for them; until then they stay on DeepSeek.
- `--only research,review` installs only some commands. `--lite` is for a Qwen-only plan.
- Remove it with `gtu integrate --pack --uninstall`. That removes exactly what the pack added; your own settings file is never edited.

## Check your install

```powershell
gtu doctor
```

Checks everything (spare-gpu, git, Desktop's settings, the pack, this project's jobs) and tells you how to fix each problem. It changes nothing.

`gtu doctor --live`, run from **Desktop's terminal**, also sends one tiny request to measure the pack's token cost. Run it once with the pack and once without.

## Safety

- Your files are never changed. Jobs work on a private copy.
- It never pushes and never commits to your project.
- Jobs may only read, edit their copy, and run your test command. No web access, no other commands, and your other MCP servers are switched off inside a job.
- Big folders like `node_modules`, `venv`, `dist` and `build` aren't copied, unless git tracks them.

## Settings

`gtu init` saves your project's settings in `.gtu/settings.json`. It looks at `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml` or a `Makefile` to find how your tests run. Commit this file; the rest of `.gtu` stays out of git. Without it, spare-gpu assumes `python -m pytest`.

| Setting | What it sets | Override for one run |
|---|---|---|
| `test_command` | How your tests run. Tests and fixes are kept only if this passes. | `GTU_TEST_COMMAND` |
| `allowed_commands` | Commands a job may run (must include the test command) | `GTU_ALLOWED_COMMANDS` |
| `test_timeout` | Seconds one test run may take | `GTU_TEST_TIMEOUT` |
| `agent_timeout` | Seconds one job's Desktop session may take | `GTU_AGENT_TIMEOUT` |
| `job_token_budget` | Tokens one job may use before it pauses | `GTU_JOB_TOKEN_BUDGET` |
| `preferred_model` | The model jobs start from | `GTU_PREFERRED_MODEL` |
| `switch_safe` | Extra job types allowed to move to the faster model | `GTU_SWITCH_SAFE` |

`gtu doctor` shows which settings are in use and where each one came from.

## Good to know

- Windows only. Jobs run while BharatCode Desktop is open.
- Works in any project folder, with or without git, even with unsaved changes.
- Each job has a token budget (`GTU_JOB_TOKEN_BUDGET`). A stopped job continues where it left off with `gtu start`.
- `gtu stop` pauses a running job; it never kills anything.
- Measurements and details: [research/](research/).

## Remove

```powershell
gtu integrate --uninstall
```

Then delete the `%LOCALAPPDATA%\spare-gpu` folder.

---

MIT licence, see [LICENSE](LICENSE). Credits for code reused from other projects: [ATTRIBUTION.md](src/gpu_token_utilization/pack/ATTRIBUTION.md). An independent project for the BharatCode Build League, not affiliated with BharatCode.
