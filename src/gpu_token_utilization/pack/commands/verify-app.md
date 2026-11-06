---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Check a running app in a real browser, with drivers on the standard model doing the clicking and a judge on the heavy model deciding what passed (needs the Playwright MCP from the marketplace)
agent: verify-lead
---
Request, between the markers:
<<<$ARGUMENTS>>>

It should start with the URL of the running app, then say what to check (one or more checks, in
plain words). If there is no URL, the app isn't running: say how to start it and stop. Carry out
the verify-lead procedure from its first step. Nothing here changes the app's code.
