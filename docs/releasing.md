# Releasing spare-gpu

The installer (`install.ps1`) installs the newest **tagged release**, found through GitHub's
`releases/latest` API, so users get a known version rather than whatever `master` holds at that
minute. Until a release exists it falls back to `master` and says so in yellow.

To publish a release:

1. Set the version in `src/gpu_token_utilization/__init__.py` (`__version__`), commit.
2. Tag it with a `v` prefix and push the tag: `git tag v0.1.0 && git push origin v0.1.0`.
3. On GitHub, create a release from that tag (the API only lists releases, not bare tags).
4. Check: `irm https://raw.githubusercontent.com/subidhkhanal/spare-gpu/master/install.ps1 | iex`
   prints `installing release v0.1.0` and ends with `spare-gpu 0.1.0 is installed.`

`$env:SPARE_GPU_VERSION = '0.1.0'` pins a version; `$env:SPARE_GPU_SOURCE` installs anything pip
accepts (a branch zip, a local folder) and is for development. A fork installs from itself with
`$env:SPARE_GPU_REPO = 'owner/name'` (its releases are then the ones the installer looks up).

## Before the first release of the pack

1. Run the Windows checks in `research/*/manual-check.md` and record the outcomes.
2. Record real registry fixtures: `python scripts/record_fixtures.py`, then `pytest`.
3. Measure the overhead budget: `scripts/check_overhead_budget.py` (research/context-report/manual-check.md, check 5) and commit `research/pack-install/overhead.json`.
4. Run the benchmarks (`scripts/eval_research.py`, `scripts/eval_review.py`) and fill the results tables; only commands that meet their ship rule lose the "experimental" label in the README.
5. Tag and publish the release, then post the marketplace proposal drafted in `research/bharatcode-issue-draft.md`.
