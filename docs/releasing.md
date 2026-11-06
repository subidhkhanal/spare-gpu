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
accepts (a branch zip, a local folder) and is for development.
