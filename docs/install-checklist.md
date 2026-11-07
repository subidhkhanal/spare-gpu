# Install checklist (clean Windows machine)

spare-gpu supports Windows 10 and 11 only. Use a Windows machine, VM or fresh user account
**with no development setup**: no project checkout, no virtual environment, nothing installed
by hand. Run the commands in PowerShell. Tick each line; if one fails, note the exact error text.

You need a BharatCode API key from <https://bharatcode.ai/account>.

Follow the README's **Windows quickstart** word for word, pasting each command unchanged; this
checklist records whether each step did what it promises.

## A. pipx (recommended)

1. [ ] Python 3.11 or newer and Git are installed (`py --version`, `git --version` in a new
   terminal). Before that, `python` prints "Python was not found; run without arguments to
   install from the Microsoft Store".
2. [ ] `py -m pip install --user pipx` (a "not on PATH" warning is expected) then
   `py -m pipx ensurepath`. Close every terminal and open a **new** one.
3. [ ] `pipx install spare-gpu` finishes without errors, and `gtu` is found in the new terminal
   without any further steps. Repeat steps 4-5 in Command Prompt as well as PowerShell.
4. [ ] `gtu --version` prints the version you released (e.g. `0.1.0`).
5. [ ] `gtu --help` lists `demo`, `add`, `start`, `stop`, `report`.

## B. pip

1. [ ] `py -m pip install spare-gpu` (inside a virtual environment, or with `--user`).
2. [ ] `gtu --version` works. If "not recognized": the scripts folder isn't on PATH; use pipx instead.

There is no standalone `gtu.exe`: Windows Smart App Control blocks an unsigned executable
("An Application Control policy has blocked this file"), so it isn't shipped.

## C. First run (whichever install route worked)

In a folder with a small git repo of your own:

1. [ ] `gtu check` with no key says "no BharatCode API key found", with the link and `gtu setkey`.
2. [ ] `gtu setkey`, paste the key (nothing is shown while typing). It says BharatCode accepted
   it and where it was saved.
3. [ ] Close the terminal, open a new one: `gtu check` says the key was found in Windows
   Credential Manager and "OK: API key accepted". Repeat in Command Prompt.
4. [ ] `gtu demo` picks a file, prints a short review, and tells you what to do next. It usually takes
   under a minute; a real morning run (08:00–11:00 IST) took 73 s, 60 s of it in BharatCode's queue.
5. [ ] `gtu add review <that file>` then `gtu start --dry-run` shows the job and a token estimate,
   and sends nothing.
6. [ ] `gtu start` runs the review in front of you, printing each step; the report lands in
   `spare-gpu/<date>_<time>/`, no branch is made, and your own files are unchanged (`git status`
   shows nothing but that folder, which ignores itself).
7. [ ] `gtu report` prints a summary and saves it under `.gtu/reports/`.

## D. Tests and docs jobs (need BharatCode Desktop)

1. [ ] Before installing Desktop: `gtu check` says "BharatCode Desktop: no config folder found
   yet"; `gtu add tests <file>` queues the job; `gtu start` in an ordinary terminal runs any
   review jobs and leaves the tests job **queued** (`gtu list`), not failed, saying it needs Desktop.
2. [ ] Install BharatCode Desktop, sign in, and open it once. `gtu integrate`, then restart Desktop.
3. [ ] `gtu check` says "BharatCode Desktop: found (stable)".
4. [ ] In Desktop's terminal, in the repo: `gtu start`. It says "Running jobs through BharatCode
   Desktop"; the queued tests job runs as a Desktop session named "spare-gpu: ..." and its files
   in `spare-gpu\<date>_<time>\` are only passing tests.
5. [ ] In another terminal while a job runs: `gtu stop` asks the run to stop and says it stopped
   cleanly (nothing is killed), and `gtu list` shows the job as paused, not failed; `gtu start`
   resumes it.

## E. Windows specifics

1. [ ] Everything above also works for a repo in a folder with spaces (e.g.
   `C:\Users\<you>\Documents\my project`) and a file name with spaces.
2. [ ] A docs job on a `.py` file saved by Notepad or `Set-Content -Encoding utf8` (byte-order
   mark, CRLF): `git diff main gtu/<job-id>` shows only added lines.

## Record

| Route | Windows version | Worked? | Notes / exact errors |
|---|---|---|---|
| pipx |  |  |  |
| pip |  |  |  |
