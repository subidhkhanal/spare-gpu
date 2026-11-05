**Hand off tests, docs and code reviews inside BharatCode Desktop, and skip DeepSeek's queue when you don't need to wait in it.**

## What it does

- **Does the chores in the background.** Ask for tests, docs or a review in plain words. spare-gpu runs the job as its own Desktop session, so your chat stays free.
- **Keeps only good work.** Generated tests are kept only if they pass, and docs jobs may not change code. Each job lands on its own git branch (`gtu/<job-id>`), and your branch is never touched.
- **Picks the faster model.** When DeepSeek has a queue, spare-gpu can run the task on Qwen instead. It only does that for task types where Qwen was measured to be just as good: docs today. Tests and reviews stay on DeepSeek.
- **Keeps you in control of tokens.** Every job has a token budget, and a job that crashes resumes where it stopped.

## Set up

With BharatCode Desktop installed and signed in:

```powershell
pipx install git+https://github.com/subidhkhanal/spare-gpu
gtu integrate
```

Then restart BharatCode Desktop. No API key is needed. To remove it: `gtu integrate --uninstall`.

## Use it

Type in any Desktop chat:

```
/auto write docs for src/utils.py
```

Or just ask:

| You say | What happens |
|---|---|
| "queue tests for src/payments" | Runs it in the background on its own branch |
| "how long is DeepSeek's queue?" | Shows each model's current wait |
| "is my tests job done?" | Shows queued, running and finished jobs |
| "what did spare-gpu do last night?" | What ran, what it produced, and the tokens used |
| "cancel the docs job" | Cancels it |

## Why it's useful

- **Less waiting.** DeepSeek's queue can reach minutes at busy times; we measured a 3½-minute wait on 5 Nov while Qwen had a free slot. For a docs task, `/auto` starts it on Qwen at once.
- **Less babysitting.** Chores run on their own, get checked, and wait for you on a branch.
- **No surprises.** It never touches `main`, never pushes, and only runs your test, build and lint commands.

## Good to know

- Windows only, and jobs run while BharatCode Desktop is open.
- Which task types may switch models comes from a small evaluation (3 tasks per type); see [research/eval/](research/eval/).
- More detail and the measurements behind it: [research/](research/).

MIT licence, see [LICENSE](LICENSE). An independent project for the BharatCode Build League, not affiliated with BharatCode.
