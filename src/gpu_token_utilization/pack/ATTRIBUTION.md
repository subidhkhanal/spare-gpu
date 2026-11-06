# Attribution

The pack is written for BharatCode Desktop and is not affiliated with BharatCode or Anthropic.
It re-implements behaviour described in public documentation and reuses only the sources below,
under their own licences. Nothing in it is copied from `anthropics/claude-code` (all rights
reserved).

## anthropics/claude-code-security-review (MIT)

Copyright (c) 2025 Anthropic. Licence text: `licenses/claude-code-security-review.LICENSE`.

Used for `/security-review`:
- the vulnerability categories the finders look for (input validation and injection,
  authentication and authorisation, crypto and secrets, code execution and deserialisation,
  data exposure), its severity and confidence scales, and its finding fields;
- the method of a deterministic false-positive filter before a model judges the candidates
  (`security.py` is a new implementation of the same rules: denial of service, rate limiting,
  resource leaks, open redirects, regex injection, memory safety outside C and C++, SSRF in HTML,
  documentation files), extended with test-only files, log spoofing, audit logs, outdated
  dependencies, missing hardening, and attacks that rely on environment variables;
- the verification precedents the verifier applies (trusted environment variables and flags,
  client-side checks, frameworks that escape by default, notebooks and workflows, logging of
  non-sensitive data), restated in the verifier's own words;
- the idea of an evaluation on seeded vulnerabilities (research/security-review/benchmark.md).

## OpenCode (MIT) and BharatCode Desktop

The pack uses only their public extension points: Markdown commands and agents, config keys,
MCP servers. No code is copied.
