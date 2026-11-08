# /deep-research benchmark (plan §8)

**Status: designed, not run.** The runs need BharatCode Desktop on Windows and the live registries.
No number below is a result.

## Ship rule

- At least 95% of the references in the final reports resolve (`verify_refs` verdicts, after the
  checker's pass).
- At least 90% of a random sample of 30 cited claims are supported by the cited source (a person
  reads the source).
- Coverage of the key papers in each question's reference list is within 10 percentage points of
  the DeepSeek-only setup.

Until these hold, the command stays documented as experimental in the README.

## Setups

Each question runs three times: **DeepSeek-only** (`gtu integrate --pack --only research` with
`GTU_MODELS=deepseek-v4.1-flash,deepseek-v4.1-flash`), **split** (the default), **Qwen-only**
(`--lite`). Record per run: minutes, tokens per model (Desktop's session detail), queue waits
(`spare_waits` at the start and research/livestats-endpoints.md's method), references kept and
dropped, and DeepSeek calls saved versus DeepSeek-only.

## Questions (10) and the key papers to cover

Fill the key-paper lists before running; 5 to 8 papers each, chosen by someone who knows the field.

| # | Field | Question |
|---|---|---|
| 1 | CS | What is known about quantum-inspired particle swarm optimisation for task offloading in vehicular networks? |
| 2 | CS | How do retrieval-augmented language models reduce hallucination, and what are the limits? |
| 3 | CS | What do measured studies say about the energy cost of training versus serving large language models? |
| 4 | Medicine | What is the evidence on GLP-1 receptor agonists for weight loss in people without diabetes? |
| 5 | Medicine | What does the evidence say about screen time and sleep in adolescents? |
| 6 | Economics | What are the measured effects of minimum wage increases on employment in the 2010s? |
| 7 | Economics | How do carbon taxes affect emissions and competitiveness, by the evidence from British Columbia and the EU? |
| 8 | Policy | What is known about the effect of four-day work weeks on productivity and wellbeing? |
| 9 | Policy | What does the research say about automated content moderation and over-removal of lawful speech? |
| 10 | Cross | What is the evidence for and against open-weight model releases on safety? |

## Scripts

`python scripts/eval_research.py research/deep-research/tasks.json --setup <setup>` runs every
question as a BharatCode Desktop session on the research lead, tallies `sources.json` verdicts and
key-paper coverage, records the exact tokens per model from Desktop's own record of the replies,
and adds the setup's rows to `results-<date>.json` and `results.md` here. Run it from Desktop's
terminal once per setup (deepseek, split, lite, and split-standard-roles for the checker's
switch-safety): install the pack for the setup (the script prints the command) and restart
Desktop before each. Fill the
`key_papers` lists in tasks.json first (DOIs or arXiv ids). `--write-policy` records the
research-checker verdict in routing_policy.json. Claims support is a column to fill by hand.
