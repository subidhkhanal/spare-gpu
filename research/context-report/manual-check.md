# Manual check on Windows: /context-report and the overhead budget

Run after `gtu integrate --pack` and a full restart of BharatCode Desktop. Record the outcome of
each check. Nothing here was run by the implementer.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | The numbers match a manual measurement | In a project, `gtu context-report --measure`. Compare its estimate and its measured total with `python scripts/measure_overhead.py <project> <empty-folder> empty-mcp`. | The measured totals agree within a few hundred tokens; the estimate is within about 20% of the measurement (the per-item costs date from 31 Oct). Record all three numbers. | |
| 2 | /context-report in Desktop | Type `/context-report`. | The `context-lead` session runs on Qwen, calls `context_report` once, and the summary's numbers are exactly the tool's (compare with the terminal output). | |
| 3 | A switch applies with a backup | `gtu context-report --apply N` for an MCP server you don't need. | It asks first; after yes, the server has `"enabled": false`, a `<file>.<timestamp>.spare-gpu-backup` exists, and after a Desktop restart the server is off. | |
| 4 | Usage | With BharatCode's CLI installed and signed in, `gtu context-report --days 3`. | The `bharatcode stats` figures appear under "Usage". | |
| 5 | Overhead budget | `python scripts/measure_overhead.py <project> <empty> empty-mcp > before.jsonl` with the pack uninstalled; `gtu integrate --pack`, restart Desktop, measure again into after.jsonl; `python scripts/check_overhead_budget.py before.jsonl after.jsonl`. | It writes research/pack-install/overhead.json with `added` at most 1,500 and exits 0; `pytest tests/test_overhead_budget.py` passes. Commit the JSON. | |
