# Manual check on Windows: `gtu integrate --pack` and the model split (plan §7)

These checks need BharatCode Desktop on Windows, so they are run by hand. Record the outcome of
each in the last column (date, pass/fail, what you saw). Nothing here was run by the implementer.

Setup: `pip install -e .` from this checkout, then `gtu integrate --pack`, then fully close and
reopen BharatCode Desktop. `gtu integrate --pack --uninstall` undoes it.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Config accepted | Run `gtu integrate --pack`. Open Desktop. | Desktop starts with no policy error and no config error. `gtu integrate --pack` printed `Wrote the pack's settings in ...\config.json` (or `opencode.json` if `config.json` was already yours). | |
| 2 | Your own file untouched | Compare `opencode.jsonc` before and after. | Only the `spare-gpu` MCP entry (added by the plain `gtu integrate`) differs; nothing from the pack is in it. | |
| 3 | Split really used: titles | Start a new chat on DeepSeek, send one message, wait for the title. Then `bharatcode stats --days 1 --models` (or the session's model breakdown in Desktop). | The title request ran on Qwen 3.8 27B (`small_model`). | |
| 4 | Split really used: explore | In a chat on DeepSeek ask: "use the explore subagent to find where config files are loaded". | The explore child session shows Qwen 3.8 27B as its model. | |
| 5 | Model picker doesn't override agent models | With the pack installed, pick Qwen in Desktop's model picker, then run a pack command whose lead is on DeepSeek (once /deep-research exists). | The lead's session runs on the model its agent file names, not the picker's. If the picker wins, record it: the plan's assumption is wrong and the lead must say its model another way. | |
| 6 | Helpers hidden from normal sessions | In a plain `build` chat, send any message, then open the request (Desktop's session detail, or `bharatcode debug` of the system prompt) and look at the task tool's list of agent types. | None of the pack's helper agents (research-scout, research-writer, research-checker, review-finder, ...) is listed; `general` and `explore` still are. | |
| 7 | Pack-only MCP tools hidden from normal sessions | Same request as 6: look at the tool list. | No `spare-gpu_scholar_search`, `spare-gpu_verify_refs`, `spare-gpu_security_filter` or `spare-gpu_pack_cost` tool is in the request (the five /auto tools are). | |
| 8 | Overhead within budget | `python scripts/measure_overhead.py` before and after `gtu integrate --pack` (see research/token-overhead.md for the method). | The pack adds at most 1.5k tokens per step to a normal session. Record both numbers. | |
| 9 | Exa remote MCP keyless | With the research component installed, open Desktop's MCP list. | `exa` connects without a key and one web search works from a chat that may use it. If it needs a key, record the error: the fallback is the `OPENCODE_ENABLE_EXA` route. | |
| 10 | Uninstall is exact | `gtu integrate --pack --uninstall`, then diff the config folder. | `config.json` (the pack's) and the pack's `agent/*.md` and `command/*.md` are gone; `opencode.jsonc`, `command/auto.md` and your own files are as they were. | |

| 11 | Overhead budget, measured | `python scripts/measure_overhead.py <project> <empty> empty-mcp > before.jsonl` with the pack uninstalled; `gtu integrate --pack`, restart Desktop, measure again into after.jsonl; `python scripts/check_overhead_budget.py before.jsonl after.jsonl`. | It writes research/pack-install/overhead.json with `added` at most 1,500 and exits 0; `pytest tests/test_overhead_budget.py` passes. Commit the JSON. | |

Checks for each command (lead as primary, parallel scouts, tool limits) are in that command's own
manual-check.md.
