# Recorded registry answers for the scholar and refs tests

These files stand in for OpenAlex, Crossref and arXiv in the tests, which never reach the network.

They were **written by hand in the shapes the three APIs document** (OpenAlex works objects with
`abstract_inverted_index`, Crossref `message.items`, the arXiv Atom feed) because the machine the
pack was built on had no route to those hosts. To replace them with real recordings, run on a
machine with internet access:

    python scripts/record_fixtures.py

It fetches the same queries and ids and overwrites these files; the tests then run against what
the registries really answer. Re-record when a registry changes its format.
