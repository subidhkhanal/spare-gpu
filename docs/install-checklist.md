# Install checklist (clean Windows machine)

spare-gpu supports Windows 10 and 11 only. Use a Windows machine, VM or fresh user account
**with no development setup**: no project checkout, no virtual environment, nothing installed
by hand. You need BharatCode Desktop, installed and signed in. Tick each line; if one fails,
note the exact error text.

Follow the README's **Install** section word for word; this checklist records whether each step
did what it promises.

## A. The installer (recommended)

1. [ ] Paste the install line into PowerShell. It installs Python and git if they're missing, says
   which release it installed, and ends with "spare-gpu <version> is installed."
2. [ ] Its output shows `gtu integrate --pack` adding the /auto command and the pack's commands
   (/code-review, /apply, /undo, /cost, /prompts, /doctor and the rest) to Desktop's config folder.
3. [ ] In a **new** terminal, `gtu --version` prints the version, and `gtu --help` lists only
   `integrate`.

## B. pipx (by hand)

1. [ ] With Python 3.11+ and git: `pipx install git+https://github.com/subidhkhanal/spare-gpu`
   finishes without errors, and `gtu` is found in a new terminal.
2. [ ] `gtu integrate --pack` says what it added and how to undo it.

There is no standalone `gtu.exe`: Windows Smart App Control blocks an unsigned executable
("An Application Control policy has blocked this file"), so it isn't shipped.

## C. First run, in Desktop

Fully close BharatCode Desktop (also from the taskbar tray) and open it again, on a folder with a
small project of your own.

1. [ ] `/doctor` in a chat answers with PASS lines for spare-gpu, Desktop's config, the MCP entry
   (and that the server starts) and the pack, and ends with "Result: PASS" (or WARN, each with
   its fix).
2. [ ] `/auto write docs for <a file>` runs a job as a Desktop session named "spare-gpu: ...";
   the reply says Done and names a `spare-gpu/<date>_<time>/` folder; `git status` shows nothing
   but that folder, which ignores itself.
3. [ ] `/apply --dry-run` says what it would copy and writes nothing; `/apply` copies the files in;
   `/undo` puts your files back exactly (`git status` is clean again).
4. [ ] "cancel the docs job" while a job runs: the reply says it stops within a few seconds, and
   nothing is copied to `spare-gpu/`.
5. [ ] `/cost` answers with tokens per model for the last 24 hours and this project's jobs.
6. [ ] `/prompts` answers with a table of approved commands (or says there were too few) and a
   snippet to paste, and changes no file.
7. [ ] `/code-review` on a small uncommitted change ends with ranked findings (or "no findings")
   and a cost summary.

## D. Windows specifics

1. [ ] Everything above also works for a project in a folder with spaces (e.g.
   `C:\Users\<you>\Documents\my project`) and a file name with spaces.
2. [ ] A docs job on a `.py` file saved by Notepad or `Set-Content -Encoding utf8` (byte-order
   mark, CRLF): the file in `spare-gpu\<date>_<time>\` differs from yours only by added lines.

## Record

| Route | Windows version | Worked? | Notes / exact errors |
|---|---|---|---|
| installer |  |  |  |
| pipx |  |  |  |
