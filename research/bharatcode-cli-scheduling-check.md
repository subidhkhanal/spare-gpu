# BharatCode CLI: scheduling / queueing / delay feature check

- **Date:** 2026-10-29
- **CLI version tested:** `bharatcode` 1.15.46 (installed via `npm install -g bharatcode`)
- **Binary location:** `C:\Users\subid\AppData\Roaming\npm\bharatcode.cmd` (npm shim; on Windows PowerShell the `.ps1` shim is blocked by execution policy, so all commands were run through `bharatcode.cmd`)
- **Method:** ran each command below on the machine and searched the captured output. Raw help text was captured to a temp file (659 lines) and searched for keywords.

## Result

**No scheduling, cron, queueing, delay, "run later", timer or defer feature was found in the CLI surface.**

The only keyword hit across all help text was `bharatcode debug wait`, described as "wait indefinitely (for debugging)". That is a debugging helper that blocks the process. It is not a scheduler.

Keywords searched (case-insensitive): `schedule`, `cron`, `delay`, `queue`, `later`, `background`, `wait`, `timer`, `defer`.

## What was checked

| # | Check | Outcome |
|---|-------|---------|
| 1 | `bharatcode --version` | `1.15.46`. `bharatcode version` is **not** a command: it was treated as a project path and failed with `Failed to change directory to ...\version`. |
| 2 | `bharatcode --help` | Full command list captured (below). |
| 3 | `bharatcode help` | Same output as `--help`. |
| 4 | `--help` on each of the 22 top-level subcommands | All captured. No scheduling options in any of them. |
| 5 | Keyword search over all help text | Only hit: `debug wait` (see above). |
| 6 | Config files | `%APPDATA%\bharatcode\Config\opencode.jsonc` contains only `{ "$schema": "https://opencode.ai/config.json" }`. No settings at all. Other common locations (`~\.bharatcode`, `~\.config\bharatcode`, project-level `bharatcode.json`) do not exist. |
| 7 | `bharatcode features list` | **Command does not exist.** It printed the top-level help instead. |
| 8 | Install path | `C:\Users\subid\AppData\Roaming\npm\bharatcode.cmd` |

## Top-level commands (from `--help`)

`completion`, `acp`, `mcp`, `[project]` (default: start TUI), `attach`, `run`, `debug`, `auth`, `agent`, `upgrade`, `uninstall`, `migrate`, `serve`, `web`, `models`, `stats`, `export`, `import`, `github`, `pr`, `session`, `plugin`, `db`.

Nested subcommands seen in the help text:

- `agent`: create, list
- `auth`: login, logout, reconnect, status
- `db`: path
- `debug`: agent, config, file, info, lsp, paths, rg, scrap, skill, snapshot, startup, v2, wait
- `github`: install, run
- `mcp`: add, auth, debug, list, logout
- `migrate`: capabilities, run
- `session`: delete, list

Notable options on the default command and `run`: `--continue`, `--session`, `--fork`, `--prompt`, `--agent`, `--model`, `--auto` (auto-approve permissions), `--mini`, `--format json`, `--attach`, `--port`, `--variant`, `--interactive`. None of these schedule or delay work.

## Limits of this check

This only covers what the CLI exposes through `--help` and local config. It does **not** rule out:

- Slash commands or features inside the interactive TUI that are not documented in `--help`.
- Scheduling provided by plugins (`bharatcode plugin`), MCP servers, or custom agents/skills.
- The third-level `debug` subcommands (`debug file`, `debug lsp`, `debug rg`, `debug snapshot`), whose help was not expanded.
- Features that exist server-side (`serve` / `web`) but are not described in help.

So the accurate claim is: "BharatCode CLI v1.15.46 exposes no built-in scheduling or queueing command or option", not "BharatCode cannot be scheduled by any means". External schedulers (Windows Task Scheduler, cron) combined with `bharatcode run "<message>"` are the obvious workaround.

## Side observation

Help output and config reference `opencode.local` (mDNS default domain), `OPENCODE_SERVER_PASSWORD`, `https://opencode.ai/config.json`, and `bharatcode migrate` mentions importing from "OpenCode". The CLI appears to be derived from OpenCode. Worth knowing before making claims in the README about what is original.

---

## Desktop app (source analysis)

- **Date:** 2026-10-29
- **Repo:** https://github.com/BharatCode-ai/bharatcode-desktop, shallow clone at `research/bharatcode-desktop/`, then deepened to the last 50 commits for the history check.
- **Commit analysed:** `f8c7feacae8a8cf89d9b9cb05a780e306f845d7d` (2026-10-29, "Merge pull request #70 from BharatCode-ai/reconcile-contributions"). Latest tags: `v1.15.46`, `desktop-beta-1.15.46`.
- **Method:** read-only. `git grep` and file reads only. Nothing from the cloned repo was installed, built or executed.

### 1. What the app is

- `README.md` is still the **unmodified upstream OpenCode README** ("The open source AI coding agent", opencode.ai install commands). It says nothing about BharatCode specifics.
- `CONTRIBUTING.md` and `docs/release-channels.md` are BharatCode-specific. They describe BharatCode Desktop as "the official desktop distribution" that "combines the local AI development engine with an Electron desktop application, first-party authentication, automatic updates across release channels, and localized developer tools."
- `docs/` contains only: `desktop-chat-import-2026-09-24.md`, `native-auth-cutover.md`, `release-channels.md`, `upstream-catchup-2026-09.md`. The last one confirms the codebase tracks upstream OpenCode ("upstream catch-up").
- The repo is a Bun/Turbo monorepo of about 5,176 tracked files. Root package name is still `opencode`; internal packages are `@opencode-ai/*`.

### 2. Stack

- **Electron** (not Tauri), in `packages/desktop` (`electron-vite`, `electron-builder`). Main process in TypeScript under `packages/desktop/src/main/`, plus `preload/` and `renderer/`.
- UI is **SolidJS** (`packages/app`, `packages/ui`, `packages/session-ui`), shared with the web app.
- Engine is TypeScript on **Bun** with the **Effect** library and SQLite via Drizzle (`packages/opencode`, `packages/core`).
- **Yes, it bundles the CLI as a sidecar.** `packages/desktop/electron-builder.config.ts:38` ships `resources/bharatcode-cli*` as `extraResources`. `packages/desktop/src/main/background-cli.ts` resolves the bundled executable, runs `--version`, and installs/starts it. `sidecar.ts`, `sidecar-auth.ts` and `server.ts` manage the local HTTP/SSE server the UI talks to. (The `resources/` folder in the repo only holds `entitlements.plist`; the CLI binary is added at release time.)

### 3. Keyword grep results

Run with `git grep -iIE` over all tracked files, excluding `bun.lock`, `packages/sdk/openapi.json`, translated `README.*.md`, `STATS.md`, `packages/app/src/i18n/**`, and `*.svg`.

| Keyword | Lines / files | Real scheduling feature? |
|---|---|---|
| schedule | 217 / 64 | **No**, except the GitHub agent note below. Other hits are UI render scheduling and timers. |
| scheduler | 21 / 5 | No. UI render batching (`scheduleRows` in `footer.prompt.tsx`, `scheduleReady` in `basic-tool.tsx`), editor `scheduleReconnect` in `packages/tui/src/context/editor.ts`, an Effect-runtime comment in a test AGENTS.md, and a spec line `ReloadScheduler.request()` in `specs/v2/catalog-config-plugin-lifecycle.md:74` (config reload, not user tasks). |
| cron | 15 / 8 | No. The word matches inside other words ("crontab" in the syntax-highlight list `markdown-inline-code-kind.ts`, a folder icon in `file-icon.tsx:526`), a recorded OpenAI response fixture, and **test fixture skills** `packages/opencode/test/fixture/skills/{agents-sdk,cloudflare}/SKILL.md` that merely describe other products' cron features. |
| recurring | 16 / 16 | No. Translation-guideline wording ("recurring UI terms"), one test-fixture skill, one spec doc. |
| night | 8 / 3 | No. "Night Owl" colour theme and a `night.json` theme in a plugin-loader test. |
| overnight, offpeak, off-peak | 0 | None. |
| delay | 388 / 97 | Not read line by line. Spot checks: UI debounce/animation, test sleeps. No user-facing "delay a run" option found. |
| queue | 1000 / 167 | Not read line by line. Targeted review found internal event/sync queues (`global-sync/queue.ts`, `server-sdk.tsx` event coalescing, `markdown-worker-queue.ts`) and the **follow-up prompt queue** (see near-misses). |
| later | 161 / 97 | Whole-word match. Comments and UI strings. No feature. |
| background | 1985 / 395 | Overwhelmingly CSS `background`. The only process-level hit is `background-cli.ts` (starts the bundled CLI sidecar at app launch). Not a scheduler. |
| wait | 404 / 110 | Whole-word match. Waiting on promises/tests/startup. No user-facing feature. |
| timer | 397 / 92 | Internal timers (debounce, update checks, a 180 s auth timeout in `account-session.ts:116`). None user-schedulable. |
| defer | 989 / 127 | Programming `defer`/Effect finalizers and a UI `defer` prop. |
| batch | 264 / 87 | Includes the `experimental.batch_tool` config flag (parallel tool calls inside one turn) and doc text about "batches" of PRs. See near-misses. |
| quiet | 191 / 36 | Log-level / "quiet" flags in tooling. Nothing like "quiet hours". |
| idle | 299 / 104 | Whole-word match. Session/server idle status (`CONTEXT.md:126`: "Context source changes never wake idle sessions"). Not a trigger to start work. |
| retry | 955 / 198 | API-error retry (see near-misses). |
| backoff | 31 / 11 | Retry backoff for provider/network calls. |

**Coverage note:** I read every hit for `cron`, `scheduler`, `recurring`, `night`, `overnight` and `offpeak`. For `schedule` (217 lines in 64 files) I listed every file and read the hits in product code (not every line in tests and docs). For the high-volume words (`delay`, `queue`, `later`, `background`, `wait`, `timer`, `defer`, `batch`, `quiet`, `idle`, `retry`, `backoff`) I did **not** read every line (thousands of hits, mostly tests and UI code). Instead I used the counts and then searched specifically for features those words could hide: message queue, goal mode, budget/step caps, power monitor, background services, API routes, plugin hooks. The OpenAPI route list (`packages/sdk/openapi.json`) has no path containing schedule, cron, queue, timer, job, task or trigger.

The one genuine "schedule" hit in product code:

- `packages/opencode/src/cli/cmd/github.handler.ts:151`: `REPO_EVENTS = ["schedule", "workflow_dispatch"]`. The **GitHub agent** (`bharatcode github run`, running inside GitHub Actions) can handle GitHub's `schedule` event (lines 404-545, branch prefix `schedule/...`). The timing is controlled by the user's own GitHub Actions `cron:` line, not by BharatCode. The workflow template that `github install` writes (line ~341) triggers only on `issue_comment` and `pull_request_review_comment`, with no cron.

### 4. UI screens, menus, settings and commands (from source)

**Native menus** (`packages/desktop/src/main/menu.ts`, `desktop-menu-actions.ts`): app relaunch; window new/close/minimize; view reload; standard edit actions (undo, redo, cut, copy, paste, delete). No task or schedule menu.

**Renderer pages** (`packages/app/src/pages/`): home, new-session, session (timeline, composer, review tab, file tabs, side panel, terminal panel), layout, error. Plus session archive and handoff helpers.

**Dialogs / settings** (`packages/app/src/components/`): settings (general, account, providers, models, keybinds, servers, marketplace; also `settings-v2/` equivalents), connect provider, custom provider, manage/select model, select MCP, select server, select directory/file, fork session, edit project, command palette, release notes, usage-exceeded dialog, model-recovery, status popover, chat-import notice.

**App settings** (`packages/app/src/context/settings.tsx`):
- general: `autoSave`, `releaseNotes`, `followup` (`"queue" | "steer"`), `showFileTree/Navigation/Search/Status/Terminal`, `showReasoningSummaries`, `shellToolPartsExpanded`, `editToolPartsExpanded`, `showCustomAgents`, `mobileTitlebarPosition`, layout flags
- appearance: `fontSize`, `mono`, `sans`, `terminal`
- `keybinds`; `permissions.autoApprove`
- notifications (`agent`, `permissions`, `errors`); sounds (per event)

**Built-in slash commands** (`packages/opencode/src/command/index.ts`): `/goal` ("set a durable Goal Mode objective"), `/init` (guided AGENTS.md setup), `/review` (review changes). Users can add more via the `command` config key.

**Engine config keys** (`packages/core/src/v1/config/config.ts`): `shell`, `logLevel`, `server`, `command`, `skills`, `references`, `watcher`, `snapshot`, `plugin`, `share`, `autoshare`, `autoupdate`, `disabled_providers`, `enabled_providers`, `model`, `small_model`, `default_agent`, `subagent_depth`, `username`, `mode`, `agent` (including `steps`), `provider`, `mcp`, `formatter`, `lsp`, `instructions`, `permission`, `tools`, `attachment`, `enterprise`, `tool_output`, `compaction`, and `experimental` (`disable_paste_summary`, `batch_tool`, `openTelemetry`, `primary_tools`, `continue_loop_on_deny`, `mcp_timeout`, `policies`).

**Settings that could enable scheduling or queueing indirectly:** `followup` (queue vs steer), `experimental.continue_loop_on_deny`, `agent.steps`, `plugin`, `command`, `mcp`, `server` (headless server). None of them takes a time, cron expression or delay.

(`packages/core/schema.json` is a Drizzle database snapshot, not a settings schema.)

### 5. Recent commits and release notes

- Read the last 50 commits (2026-09-23 to 2026-10-29). Subjects cover auth, release pipeline, CI, desktop onboarding, chat import, output-limit notices, retry behaviour and docs. **None mentions scheduling, cron, delay, queue or run-later.** Keyword matches in subjects: `fix(auth): retry transient Windows credential reads` and `fix(provider): show BharatCode API refusals as sent, and never retry them` (both error handling).
- There is no CHANGELOG file in the repo, and `docs/` has no scheduling content. The one scheduling-related sentence in `docs/upstream-catchup-2026-09.md:65` is about "test resource scheduling" in CI, and line 1968 notes a retired upstream workflow with "no issue/schedule/pull_request_target triggers".
- Limit: release notes shown in-app (`dialog-release-notes.tsx`) come from GitHub releases at runtime and were not checked here.

### 6. Near-miss features (so the README stays honest)

1. **Goal Mode (`/goal`)**, the closest match. `packages/schema/src/session-goal.ts` defines a per-session goal with status `active | paused | completed | blocked` and accumulated active time. `goal-assessment.ts` (`shouldRequest`) triggers a continuation check when a goal is active and the last turn ended normally without tool calls; it also detects repeated actions and asks for a blocker report. This is an **autonomous keep-working loop inside one session**, driven by completion, not by a clock. It has no start time, delay or recurrence. It does overlap with "unattended long-running work".
2. **Follow-up queue/steer** (`settings.tsx`, `prompt-input/submit.ts`, `pages/session.tsx`): messages typed while the agent is busy are held and sent afterwards. The `"queue"` value is currently coerced to `"steer"` (`settings.tsx:354-377`), so a visible queue mode is being retired. Per-session, immediate, no timing.
3. **GitHub agent on `schedule` events**: the app can act as the worker when the user's own GitHub Actions cron fires (see section 3). Scheduling is delegated to GitHub.
4. **Provider retry/backoff** (`packages/opencode/src/session/retry.ts`): retries failed model calls automatically; "free tier limit" and "account rate limit" are surfaced via the usage-exceeded dialog (`GO_UPSELL_*`, an opencode.ai upsell). This is **not a user budget or cost cap**.
5. **`agent.steps`** caps the number of agent steps per run (`MAX_STEPS_PROMPT`). It is a step limit, not a cost or time budget.
6. **`experimental.batch_tool`**: runs multiple tool calls together within a turn. Not job batching.
7. **Subagents** (`subagent_depth`, parallel-agents skill): concurrent in-session work.
8. **Auto-updater** (`updater.ts`): checks for app updates. Not user-schedulable.
9. **No power/idle hooks:** `powerMonitor` / `powerSaveBlocker` do not appear anywhere in `packages/desktop/src`, so there is no wake-on-schedule or keep-awake handling for long runs. (CONTRIBUTING.md mentions "power monitor integration", but no code uses it at this commit.)

### 7. Extension / plugin system

**Yes. A third party can add a scheduler without changing the core app.**
- `@opencode-ai/plugin` (`packages/plugin`, v1.18.31) exports plugin APIs (`./tool`, `./tui`, `./v2/effect/*`, `./v2/promise`). Plugins are listed in the `plugin` config key and can register tools; the loader is tested in `packages/opencode/test/plugin/loader-shared.test.ts`. Example plugin: `.opencode/plugins/tui-smoke.tsx`.
- Custom slash commands (`command` config / `.opencode/command/*.md`), custom agents (`agent`), skills, and **MCP servers** (`mcp`) are all user-extensible.
- The desktop UI also has a **marketplace** settings page (`settings-marketplace.tsx`, `marketplace-controller.ts`) for enabling "capabilities".
- Outside the app: the bundled CLI offers `serve`/`web` (headless server) and `run "<message>"`, so an external scheduler (OS task scheduler, GitHub Actions) can drive it.
- Caveat: I did not verify whether plugins can run long-lived background timers or add UI panels in the desktop renderer. TUI plugin hooks exist; desktop-renderer plugin hooks were not confirmed.

### 8. Verdict

BharatCode Desktop (commit f8c7feacae8a8cf89d9b9cb05a780e306f845d7d, dated 2026-10-29) exposes no scheduling, queueing, or run-later feature. Nearest features are: Goal Mode (`/goal`, an in-session autonomous keep-working loop with no time trigger), a per-session follow-up message queue/steer setting, and the GitHub agent's ability to run on GitHub Actions `schedule` events (timing owned by GitHub, not the app). Searched: README, docs, settings schema, UI/commands, and a full-text grep of the keywords above.

**Caveats to keep in the README:** the high-volume keywords were counted and targeted-reviewed, not read line by line; in-app release notes and the closed-source parts of the sidecar binary were not inspected; and the repo is an OpenCode fork, so upstream OpenCode scheduling work could land in a later catch-up merge.

**Overlap with Night Shift:** Goal Mode is the only feature that overlaps in spirit (long unattended work). It has no timing, no start-later and no off-peak logic, and its continuation is driven by the agent finishing a turn. A time-based Night Shift (start at a chosen time, run overnight, queue several jobs) would be additive, not duplicated.
