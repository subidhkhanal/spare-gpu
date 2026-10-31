# BharatCode CLI: fixed token overhead

- **Date:** 2026-10-31, 00:32–00:35 IST
- **Tool:** BharatCode CLI 1.15.46 (`bharatcode run`), model `bharatcode/qwen-3.8-27b`, Windows 11
- **Script:** [scripts/measure_overhead.py](../scripts/measure_overhead.py)
- **Cost of the whole measurement:** about 50k tokens (4 runs, plus 1 whose result was lost to a script bug)

## Question

How many tokens does BharatCode's CLI send before the model reads the user's request, and where
do they come from? That fixed cost is paid again on every agent step, so it matters more than any
single prompt.

## Method

One trivial prompt, `Reply with the single word OK.`, run once per variant with
`bharatcode run --format json`. The CLI's own `step_finish` event reports the tokens it sent
(`input`, plus `cache.read` and `cache.write`) and received (`output`, `reasoning`). Every run
finished in one step with the reply "OK", so the context sent is the overhead plus our 7 words.

| Variant | Folder | Change from the baseline |
|---|---|---|
| empty | a new, empty folder (not a git repo) | spare-gpu's MCP server switched off for the run |
| empty-mcp | same | spare-gpu's MCP server on (7 tools, as registered by `gtu integrate`) |
| project | this repository | MCP off |
| empty-notools | empty folder | MCP off and `"tools": {"*": false}`: no tool schemas |

Variants were applied with an extra config file passed through `OPENCODE_CONFIG`, so the user's
own global config was never edited. `bharatcode mcp list` confirmed the switch:
`○ spare-gpu disabled`.

To reproduce: `python scripts/measure_overhead.py <a-project-folder> <an-empty-folder>`.

## Results

| Variant | Context sent (input + cache) | Output | Reading |
|---|---|---|---|
| empty | **11,504** | 19 | The harness floor: system prompt plus built-in tool schemas. |
| empty-mcp | **13,833** | 26 | spare-gpu's 7 MCP tools add **2,329** tokens to every request. |
| project | **11,193** (57 new + 11,136 read from cache) | 2 | No measurable project context for this repo (no AGENTS.md); within noise of the floor. |
| empty-notools | **2,025** | 110 | System prompt alone. Built-in tool schemas are therefore about **9,480**. |

**So, per step, before the user's request:** about 2.0k of system prompt, about 9.5k of tool
schemas, plus about 2.3k for every MCP server like spare-gpu's, plus project context where a
repo has instruction files. A user's bare "hi" with spare-gpu registered measured 14,407, which
fits: 11.5k + 2.3k + a few hundred.

## Follow-up: skills and instruction files (2026-10-31, 14:30 IST)

Three more one-step runs, same prompt, spare-gpu's MCP server off:

| Variant | Context sent | Reading |
|---|---|---|
| empty (repeat) | **11,536** | Matches the first run (11,504) within noise. |
| empty, `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` | **8,032** | The CLI lists every skill it finds in `~/.claude/skills` and `~/.agents/skills` (16 Claude Code skills on this machine) in each request: **3,504 tokens**. |
| a folder containing only an `AGENTS.md` (this README, 5,188 bytes) | **12,959** | The file goes in whole: **+1,423 tokens**, about 3.6 bytes per token. |

**Corrected breakdown** of the 14,407 tokens a bare "hi" cost with spare-gpu registered, in this repo (no `AGENTS.md`):

| Part | Tokens |
|---|---|
| System prompt (`default.txt` plus the environment block) | ~2.0k |
| Built-in tool schemas | ~6.0k (not 9.5k as first estimated) |
| The user's own Claude Code skills, picked up from `~/.claude/skills` | ~3.5k (depends on the machine) |
| spare-gpu's 7 MCP tools | ~2.3k |
| Project context | ~0 here (no instruction file); an `AGENTS.md` adds its full size |
| The prompt and run-to-run noise | ~0.6k |

The source (BharatCode CLI 1.15.46, `packages/opencode/src/session/system.ts`) confirms that the environment block has no file tree or repo map. It has only the working directory, whether it's a git repo, the platform and the date.

## What this changed in spare-gpu

- `spare_token_cost` / `gtu cost` now count the measured 11,500-token floor of each BharatCode CLI
  step as overhead and report the overhead multiplier (total / task). Set
  `GTU_CLI_OVERHEAD_TOKENS` to use your own measurement.
- spare-gpu's tests and docs jobs run BharatCode's CLI with spare-gpu's own MCP server switched
  off. Before this, every job step paid the extra ~2.3k for tools the job never needed.

## Limits

- One run per variant. Run-to-run variation looked like a few hundred tokens (project vs empty).
- The "project" run read 11,136 tokens from BharatCode's prompt cache. Whether cached tokens
  count against the daily allowance the same way isn't documented; we count them.
- `"tools": {"*": false}` is OpenCode's configuration; the 2,025 result suggests BharatCode honours
  it, but that's inferred from the drop, not documented.
- Published comparison figures for other harnesses (see the README) are other people's
  measurements, made with their own method; ours are only the numbers on this page.
