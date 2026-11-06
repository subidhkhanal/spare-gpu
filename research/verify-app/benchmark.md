# /verify-app benchmark

**Status: designed, not run.** A stretch command (plan §5, item 7), run only after the five main
commands have their numbers.

## Ship rule

On 20 checks across 4 small web apps (10 that should pass, 10 seeded to fail: a broken link, a
missing title, a form that doesn't submit, a wrong redirect), the judge's verdicts agree with a
person's on at least 18, and no "pass" is given to a seeded failure.

## Method

Run `/verify-app <url> <check>` for each check in the split setup and in `--lite`; record the
verdict, the evidence quoted, minutes and tokens per model. Compare with a person's verdict.

## Results

| Setup | Agreement (of 20) | False passes | Minutes (median) |
|---|---|---|---|
| split | | | |
| lite | | | |
