# Draft: proposal to BharatCode (not posted)

BharatCode asks for an issue before a pull request, and the plan says to open it only once the
numbers exist (plan §9, item 9). This is the text to post then, with the measured figures filled
in. It is a draft; nothing has been sent.

---

**Title:** Proposal: a community capability for spare-gpu's command pack (DeepSeek plans, Qwen does the parallel work)

**What it is.** spare-gpu (hackathon entry, MIT) adds commands to BharatCode Desktop that split
each job between the two models: DeepSeek plans, judges and verifies; Qwen does the many quick,
parallel, self-contained steps. `/deep-research` (cited reports with every reference checked
against OpenAlex, Crossref and arXiv), `/code-review` (parallel finders, a verifier, optional
`--fix` kept only if tests pass), `/security-review` (the method of Anthropic's MIT-licensed
security reviewer, read-only), `/simplify`, `/context-report` and `/verify-app`.

**Why it may interest BharatCode.** It uses only the extension points Desktop already ships
(Markdown commands and agents, MCP, config keys) and it is built to spend BharatCode's capacity
carefully: the heavy tier gets a few short calls per command; the standard tier gets the parallel
work; helpers and tools are hidden from every other agent, so a normal chat pays nothing for the
pack (measured: __ tokens per step added, budget 1,500). Every command reports the model per
phase, tokens per model from `bharatcode stats`, and the queue time saved.

**Numbers.** (fill in from research/*/results.md)
- /deep-research: __% of references resolve; __% of sampled claims supported; coverage within __ points of DeepSeek-only.
- /code-review: recall __ vs built-in /review __; precision __%.
- /security-review: precision __ vs /review __; no exploit code executed.
- /simplify: tests pass in 100% of applied runs; __% of changes accepted by reviewers.
- DeepSeek calls saved per command: __.

**The ask.** Would a curated marketplace entry be considered? `pack/capability.json` is the pack
in the catalog's format. The marketplace supports `skill` and `mcp` modules today; the pack's
commands and agents would need `command` and `agent` module types (or a way for a capability to
ship a config folder). Until then spare-gpu installs them with `gtu integrate --pack`.

**Not affiliated** with BharatCode or Anthropic. Licences and attribution: `pack/ATTRIBUTION.md`.
