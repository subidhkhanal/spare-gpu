# Manual check on Windows: memory across chats (`/remember`, `/handoff`, `/pickup`)

These checks need BharatCode Desktop on Windows, so they are run by hand. Record the outcome of
each in the last column (date, pass/fail, what you saw). `gtu doctor --live` decides row 1 from
the merged config the CLI reports and writes its verdict into the Result column itself.

Setup: `gtu integrate --pack` (or `--pack --only memory`), fully close and reopen Desktop, open a
project in Desktop, run `gtu init` in it once.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Desktop reads the memory file | `gtu doctor --live`. | The merged config's `instructions` lists `.gtu/memory.md`. | |
| 2 | Every agent sees it | `/remember the API lives in src/api and tests run with pytest -q`. Open a new chat on any agent and ask: "what does the project memory say?" | The reply quotes that line without reading any file (no read tool call in the session). | |
| 3 | The cap is enforced by advice, not deletion | Add notes with `/remember` until the file passes 60 lines. | The reply says the file is over the cap and names stale-looking lines; nothing was deleted; `git diff .gtu/memory.md` shows only additions. | |
| 4 | The hand-off is written from the chat | In a chat where you changed two files and ran the tests: `/handoff`. | `.gtu/handoff.md` has Goal, Done (with the two paths), Next, Open questions, Files (the two files from `git status`), Pointers; no secrets; the reply ends with "A new chat picks this up with /pickup." | |
| 5 | A fresh chat picks it up | New chat: `/pickup`. | The summary names the goal and the next step from the file, and notes any file that changed since; it does nothing else until asked. | |
| 6 | Memory is committable, the hand-off is not | `git status --porcelain --untracked-files=all` after `gtu init`. | `.gtu/memory.md` and `.gtu/settings.json` show; `.gtu/handoff.md` and `.gtu/gtu.db` don't. | |
| 7 | Cost is visible | `gtu doctor` in the project. | The memory line shows the file's size and the tokens it adds per step, and WARNs once the file is over 4,000 characters. | |
