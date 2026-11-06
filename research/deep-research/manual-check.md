# Manual check on Windows: /deep-research

Run after `gtu integrate --pack --only split,research` and a full restart of BharatCode Desktop.
Record the outcome of each check in the last column. Nothing here was run by the implementer.

| # | Check | Steps | Pass condition | Result |
|---|---|---|---|---|
| 1 | Lead as primary at depth 0 | Type `/deep-research what is known about quantum-inspired particle swarm optimisation for vehicular networks --depth quick`. | The main session runs the `research-lead` agent (DeepSeek), not a child task. `research/<slug>/plan.md` appears. | |
| 2 | Several scouts at once | Watch the session while step 5 runs. | Three child sessions (`research-scout`) start from one message and run in parallel, on Qwen. | |
| 3 | Scouts can't run commands or write elsewhere | Open a scout's session: its tool list. | No bash, no edit outside `research/<slug>/notes/`. Its writes went only to its own notes file. | |
| 4 | Exa works keyless | In a scout's session look for a web search call. | The `exa` tools answer without a key. If they don't, record the error; the fallback is the `OPENCODE_ENABLE_EXA` route and the built-in `websearch` tool. | |
| 5 | scholar_search is used | Same session. | At least one `scholar_search` call with results carrying DOIs or arXiv ids. | |
| 6 | Writer writes one section per call | Watch step 8. | One `research-writer` run per section; `report.md` grows section by section. | |
| 7 | A fake citation is removed | Before the checker runs (or by editing sources.json in a second run), add a reference with DOI `10.1000/spare-gpu-fake` to sources.json. | `verify_refs` marks it DROP; `review.md` lists it; the final report no longer cites it. | |
| 8 | Output files | After the run. | `research/<slug>/` holds plan.md, outline.md, notes/*.md, sources.json (with `verified` fields), report.md and review.md. | |
| 9 | Cost summary | The lead's final reply. | Names the model per phase, the number of helper runs, and the queue time saved from `spare_waits`, or says the data was too thin. | |
| 10 | Overhead in a normal chat | In a plain `build` chat, look at the request's tools and agent list. | No research helper or `scholar_search`/`verify_refs` tool is present (see research/pack-install/manual-check.md, checks 6 to 8). | |

If check 5 (model picker) in research/pack-install/manual-check.md failed, the lead's model may
be the picker's: note which model each session really used.
