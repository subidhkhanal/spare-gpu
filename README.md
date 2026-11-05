**Hand off tests, docs and code reviews inside BharatCode Desktop, and skip DeepSeek's queue when you don't need to wait in it.**

## What it does

- **Does the chores in the background.** Ask for tests, docs or a review in plain words. spare-gpu runs the job as its own Desktop session, so your chat stays free.
- **Keeps only good work.** Generated tests are kept only if they pass, and docs jobs may not change code. When a job finishes, the files it edited are copied to `spare-gpu/<date>_<time>/` in your project, with their paths kept (for example `spare-gpu/2026-11-05_14-32-07/src/utils.py`). No branches or `git diff` to dig through, and your own files are never touched: open the folder and copy over what you want.
- **Picks the faster model.** When DeepSeek has a queue, spare-gpu can run the task on Qwen instead. It only does that for task types where Qwen was measured to be just as good: docs today. Tests and reviews stay on DeepSeek.
- **Keeps you in control of tokens.** Every job has a token budget, and a job that crashes resumes where it stopped.

## Set up

With BharatCode Desktop installed and signed in:

```powershell
pipx install git+https://github.com/subidhkhanal/spare-gpu
gtu integrate
```

Then restart BharatCode Desktop. No API key is needed. To remove it: `gtu integrate --uninstall`.

## What you get

```
your-project/
└── spare-gpu/
    └── 2026-11-05_12-38-50/      ← when the job finished
        ├── README.md             ← only the files the job edited
        └── src/
            └── mathx.py
```

## `/auto` mode

### Turn it on

`/auto` is a slash command that `gtu integrate` adds to BharatCode Desktop. Nothing else is needed:

1. Run `gtu integrate`. If you set up spare-gpu before `/auto` existed, update first: `pipx install --force git+https://github.com/subidhkhanal/spare-gpu`.
2. Restart BharatCode Desktop.
3. Open a chat in your project and type `/`. `/auto` appears in the command list.

### Use it

Type `/auto` and then what you want, in plain words:

| You type | What happens |
|---|---|
| `/auto write tests for src/payments.py` | Writes tests in the background and keeps only the ones that pass |
| `/auto write docs for src/utils.py` | Adds docstrings and README text in the background, without changing code |
| `/auto review src/gate.py` | Writes a code review in the background |
| `/auto what does a race condition mean?` | Answers the question right in the chat |
| `/auto` | Shows how long each model would wait right now |

Each time, `/auto` picks the model that will start sooner: DeepSeek V4.1 Flash, or Qwen 3.8 27B when DeepSeek has a queue. The reply says which model it used and why. For tests, docs and reviews, it only moves to Qwen for task types measured to be just as good on Qwen (docs today).

Background work lands in `spare-gpu/<date>_<time>/` when it's done. Ask "is my job done?" to check on it.

### Turn it off

`gtu integrate --uninstall` removes `/auto` and spare-gpu. If you edited your copy of `/auto` (`command/auto.md` in Desktop's config folder), it's left as it is.

## Or just ask

You don't have to use `/auto`. spare-gpu also understands plain requests in any Desktop chat:

| You say | What happens |
|---|---|
| "queue tests for src/payments" | Runs it in the background; the result lands in `spare-gpu/` |
| "how long is DeepSeek's queue?" | Shows each model's current wait |
| "is my tests job done?" | Shows queued, running and finished jobs |
| "what did spare-gpu do last night?" | What ran, what it produced, and the tokens used |
| "cancel the docs job" | Cancels it |

## Why it's useful

- **Less waiting.** DeepSeek's queue can reach minutes at busy times; we measured a 3½-minute wait on 5 Nov while Qwen had a free slot. For a docs task, `/auto` starts it on Qwen at once.
- **Less babysitting.** Chores run on their own, get checked, and wait for you in the `spare-gpu/` folder.
- **No surprises.** It never edits your files, never pushes, and only runs your test, build and lint commands.

## Good to know

- Windows only, and jobs run while BharatCode Desktop is open.
- The `spare-gpu/` folder ignores itself in git, so it never shows up as a change in your repo.
- Works in any project folder: a git repo or not, even with unsaved changes. Each job starts from a private copy of your files as they are, and when it finishes only the `spare-gpu/<date>_<time>/` folder is left: no branches or history in your project.
- Needs git installed on your PC, since spare-gpu uses it to manage each job's private copy. Your project doesn't need to use git.
- Which task types may switch models comes from a small evaluation (3 tasks per type); see [research/eval/](research/eval/).
- More detail and the measurements behind it: [research/](research/).

MIT licence, see [LICENSE](LICENSE). An independent project for the BharatCode Build League, not affiliated with BharatCode.
