---
# Installed by spare-gpu (`gtu integrate --pack`); `gtu integrate --pack --uninstall` removes it. Edit freely:
# once this line is gone, spare-gpu leaves the file alone.
description: Research a question with parallel scouts on the standard model, then a checked, cited report
agent: research-lead
---
Research request, between the markers:
<<<$ARGUMENTS>>>

Options may appear anywhere in it and are not part of the question: `--depth quick|standard|deep`
(default standard; quick for a single fact), `--sources web|papers|both` (default: from the kind
of question) and `--plan`. Carry out the research-lead
procedure from its first step, keeping every file under research/<slug>/ in this project. If the
request is empty, ask for the question and stop.
