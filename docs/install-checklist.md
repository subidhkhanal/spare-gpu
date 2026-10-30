# Install checklist (clean Windows machine)

spare-gpu supports Windows 10 and 11 only. Use a Windows machine, VM or fresh user account
**with no development setup**: no project checkout, no virtual environment, nothing installed
by hand. Run the commands in PowerShell. Tick each line; if one fails, note the exact error text.

You need a BharatCode API key from <https://bharatcode.ai/account>.

## A. pipx (recommended)

1. [ ] Python 3.11 or newer is installed (`python --version`).
2. [ ] `python -m pip install --user pipx` then `python -m pipx ensurepath`. Open a **new** terminal.
3. [ ] `pipx install spare-gpu` finishes without errors.
4. [ ] `gtu --version` prints the version you released (e.g. `0.1.0`).
5. [ ] `gtu --help` lists `demo`, `add`, `start`, `stop`, `report`.

## B. pip

1. [ ] `python -m pip install spare-gpu` (inside a virtual environment, or with `--user`).
2. [ ] `gtu --version` works. If "not recognized": the scripts folder isn't on PATH; use pipx instead.

There is no standalone `gtu.exe`: Windows Smart App Control blocks an unsigned executable
("An Application Control policy has blocked this file"), so it isn't shipped.

## C. First run (whichever install route worked)

In a folder with a small git repo of your own:

1. [ ] Set the key: `$env:BHARATCODE_API_KEY = "..."`.
2. [ ] `gtu check` lists the models and says "API key accepted".
3. [ ] `gtu demo` picks a file, prints a short review, and tells you what to do next. It usually takes
   under a minute; a real morning run (08:00–11:00 IST) took 73 s, 60 s of it in BharatCode's queue.
4. [ ] `gtu add review <that file>` then `gtu start --dry-run` shows the job and a token estimate,
   and sends nothing.
5. [ ] `gtu start --now` runs the review; `git branch` shows a new `gtu/<job-id>` branch, and your
   own branch and files are unchanged (`git status` is clean).
6. [ ] `gtu report` prints a summary and says the report was committed to the job's branch.

## D. Tests and docs jobs (need BharatCode's own CLI)

1. [ ] Node.js is installed, then `npm install -g bharatcode` and `bharatcode auth login`.
2. [ ] `gtu add tests <a small source file>`, `gtu start --now`: the job finishes and its branch
   contains only passing tests (`git show gtu/<job-id> --stat`).
3. [ ] In another terminal while a job runs: `gtu stop` says it stopped, and `gtu list` shows the
   job as paused, not failed.

## Record

| Route | Windows version | Worked? | Notes / exact errors |
|---|---|---|---|
| pipx |  |  |  |
| pip |  |  |  |
