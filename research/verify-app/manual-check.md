# Manual check on Windows: /verify-app

Needs the Playwright capability from BharatCode's marketplace (Node.js and npx), then
`gtu integrate --pack --only split,verify` and a full restart of BharatCode Desktop. Start a small
web app yourself (any dev server). Record the outcome of each check.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | No URL, no server started | `/verify-app check that the home page loads`. | The lead says how the app is usually started and stops; nothing is launched. | |
| 2 | Drivers in parallel | `/verify-app http://localhost:3000 the home page shows the title; the about link opens /about`. | `verify/<slug>/plan.md` lists two checks; two `verify-driver` sessions (Qwen) start from one message and use the `playwright_*` tools; each writes its own `steps-<n>.md`. | |
| 3 | Judging from evidence | Read the lead's reply. | Each verdict quotes the notes line that shows it; an impossible check is "unclear", not "pass". | |
| 4 | Drivers can't change code or run commands | A driver's session: tool list. | read, glob, grep, list, the Playwright tools and its one notes file; no bash, no other edits. | |
| 5 | No credentials | Include "log in with my account" in a check. | The driver stops at the login step and says so. | |
| 6 | Playwright missing | Uninstall the Playwright capability and repeat check 2. | The lead says the marketplace's Playwright capability must be installed and stops. | |
| 7 | Cost summary | The last paragraph. | Model per phase, driver runs, tokens per model from `pack_cost` (or "not available"), queue time saved or "too little data". | |
