# Executable acceptance-check comparison — 2026-09-13

> The code behind this experiment (the runner flag, its prompts and tests) was removed from the product after this report; it is complete at commit 8dd6ea1 of this repository.

The acceptance workflow did **not improve this sample**. Baseline resolved **1/3 fresh tasks**, versus **0/3** with acceptance checks. Both resolved **1/2 development cases**, on different tasks. Four checks were accepted and executed after solving and repair; none finished passing. The candidate's only solved task, Django, had no accepted check. Keep this experiment disabled by default.

## Implementation and method

The opt-in `--acceptance-check` runner mode gives an author its own sandbox fork to inspect the issue and propose Python setup code plus boolean assertions. A separate, untouched baseline executes each proposal. Only an observed false assertion establishes a reproduction; syntax, import, setup, evaluation and timeout errors do not. The accepted check is frozen into an inline command and reused exactly after solving and, when needed, one repair phase. The solver starts from the original repository; author edits and scratch files are isolated. Official grading remains separate.

At 50 total model turns that call tools, authoring receives at most 12 turns and three evaluated proposals. Tool-schema rejections consume turns but do not count toward those three proposals; Python syntax errors do. Eight turns are reserved for repair. The solver receives the remaining allocation after actual author usage; if no check is accepted, the repair reserve is released. Repair starts a new transcript with the task, frozen check, previous summary and actual failure output. Runtime checks add sandbox work outside the model-turn cap; command and SDK cost totals include all three workspaces. This experiment uses the existing Agent and verification APIs and does not alter normal CLI/session behavior.

Both arms ran the same frozen working source based on `18dbb5d`; only candidate enabled the acceptance flag. Finish review was disabled in both. All 130 frozen file hashes were verified after execution. Common tool schemas and initial system instructions matched; the author additionally had the submission tool and its phase instructions. The frozen Python wrapper hash and exact baseline/solver/repair command equality were verified for all four accepted checks.

Settings: Nemotron `nvidia/nemotron-3-super-120b-a12b` on Nebius Token Factory, temperature 1.0, top-p 0.95, low effort, completion cap 16,384, context budget 80,000, command/import timeouts 300/900 seconds, no investigation model. Each arm used a clean config directory and one stochastic model attempt per task. There were at most four tasks in flight, two per arm.

Django-12308 and Sphinx-8551 are explicit development cases whose earlier failures informed this work. Three fresh tasks were selected before execution with seed `20260913`, excluding 40 previously evaluated IDs and the two development repositories. One shuffled easy-task pool supplied Matplotlib; a medium-task pool supplied xarray and SymPy from distinct repositories. Gold patches and test patches were not used for selection or model feedback. “Fresh” means unseen in this repository's recorded runs, not necessarily absent from training data.

## Results

| Task | Cohort | Baseline resolved | Acceptance resolved | Baseline steps | Acceptance steps | Baseline tokens | Acceptance tokens | Final authored check |
|---|---|---|---|---:|---:|---:|---:|---|
| django__django-12308 | development | no | yes | 34 | 43 | 471,441 | 586,475 | unavailable |
| sphinx-doc__sphinx-8551 | development | yes | no | 42 | 50 | 603,889 | 581,229 | failed |
| matplotlib__matplotlib-20859 | fresh | yes | no | 34 | 50 | 382,896 | 474,207 | error |
| pydata__xarray-4687 | fresh | no | no | 50 | 50 | 1,137,659 | 964,721 | error |
| sympy__sympy-19040 | fresh | no | no | 50 | 50 | 814,423 | 700,301 | failed |

| Cohort | Baseline resolved | Acceptance resolved | Baseline tokens | Acceptance tokens | Token change |
|---|---|---|---:|---:|---:|
| Three fresh tasks | 1/3 | 0/3 | 2,334,978 | 2,139,229 | −8.4% |
| Two development cases | 1/2 | 1/2 | 1,075,330 | 1,167,704 | +8.6% |
| All five, descriptive only | 2/5 | 1/5 | 3,410,308 | 3,306,933 | −3.0% |

Candidate used 243 model calls versus baseline's 210. Its author phases consumed 46 calls / 485,259 tokens; repair consumed 32 calls / 332,320 tokens. These are measured phase costs, not estimates of causal overhead: the complete solver trajectories also differ. There were 14 runtime check executions, including two rejected Sphinx setup attempts. Four repair phases ran; none produced a passing final check. Lower total tokens here are not evidence of better efficiency at solving tasks.

Seven nonempty patches were officially graded once each; three no-diff attempts were unresolved without grading. Both initial Matplotlib attempts failed while polling a sandbox operation during pre-agent preparation. Their event logs are empty and contain zero model calls. Each was retried once with the same frozen source/settings in a separate output directory. The successful startup retries above are the first model attempts, not replacement solutions after failed grading. Original errors and outputs remain preserved. No task received another model attempt.

## What happened

- **Django:** Author used all 12 turns. Its one submission put assignments in the assertions array and was rejected for syntax; it never corrected it. Solver then passed 2/2 target and 20/20 regression tests, versus baseline's 0/2 and 20/20. Candidate used JSON serialization with a fallback on exceptional values. This win provides no evidence that executing an acceptance check helped, because none was accepted or run.
- **Sphinx:** After two setup errors, author obtained a failing check for the ambiguity warning. It did not assert the required link destination, and ignored the build return code, so an unrelated build failure without that warning could satisfy it. Candidate spent 10/32/8 turns in author/solver/repair. Solver and repair only navigated source and finished; neither attempted an edit or command. Baseline added the reference-context metadata and passed 1/1 target plus 32/32 regression tests. Candidate produced no diff.
- **Matplotlib:** The accepted check tested that `SubFigure.legend()` avoided `TypeError`, without checking the returned legend or rendering. After acceptance at turn 2, author continued until turn 12, including edits in its isolated fork. Baseline changed the existing constructor-local import from `Figure` to `FigureBase` and passed 1/1 target plus 88/88 regression tests. Candidate instead added a module-level import, creating a circular import. Solver had already observed this error in its own commands; the runtime check confirmed it. Eight repair turns made no edit. Candidate graded 0/1 and 0/88.
- **xarray:** The frozen check inferred unsupported requirements: default attribute preservation and preservation of the original `int8` dtype for `xr.where(data == 1, 5, 0)`. Here the value inputs are Python integers; the original array is not a value input. The report allowed an opt-in `keep_attrs` API and described dtype separately. Both patches omitted that API and failed the target test with `TypeError`. Candidate repair added an attributes-copy loop using an unimported `DataArray`; its own final command and the frozen check reported `NameError`. It graded 0/1 target and 1696/1717 regression tests, versus baseline's 0/1 and 1717/1717. Both diffs contained only the source module.
- **SymPy:** The accepted check directly exercised the reported factorization example, though it covered only one expression/extension form. Baseline spent its budget on navigation and scratch probes without attempting a source edit. Candidate read `sympy/polys/factortools.py`, then attempted its only edit as `/testbed/sympy/polys/factortools.py`. The read-before-edit guard keys observations by raw path and rejected the same file as unread. After a correcting read, forced finish received edit arguments, so no edit ran. Repair only navigated. Both produced no diff. The rejected proposed patch's correctness is unproven.

## Implications and limits

Execution and isolation worked: invalid setup did not count as reproduction, the accepted check stayed fixed, and unresolved failures reached repair. The gaps are the quality of the expected behavior and the model's ability to act on feedback within its budget. An explicit false result can still encode a mistaken requirement, as in xarray. A separate author can also consume turns after the check is already accepted, while a fresh repair transcript loses context when the previous finish summary is malformed.

The next narrow runtime fix supported by this evidence is consistent file identity for read-before-edit checks: relative and absolute names of the same workspace file must match without bypassing freshness checks. Ending authoring immediately after acceptance is another bounded experiment supported by Matplotlib's wasted turns. Neither change was applied to this frozen run. More review instructions alone would not address the observed path rejection or ignored errors.

This is a small, single-sample comparison. It establishes neither a reliable general regression nor an improvement, and its mixed cohort must not be compared directly with earlier 5/6 results on different tasks. The proposed SymPy edit and alternative phase budgets have not been validated. SDK sandbox costs exclude model billing; initial failed preparation costs are unavailable. Sums of per-task times are not experiment wall time. Existing grading retries can overwrite earlier logs/costs, although all seven grading attempts here completed without retry.

There is also a known runner accounting limitation: an exception escaping an Agent phase can omit partial usage and acceptance metadata from `result.json`, despite evidence in the event/acceptance logs. This did not occur in the ten completed model attempts. Forced finish can still accept malformed arguments and lose the summary. These limitations are recorded separately from acceptance-check success.

## Evidence and validation

The [companion JSON](20260913-acceptance-comparison.json) retains cohort selection, effective settings, the full frozen source hash inventory, phase usage, check specifications/results, grading counts, artifact hashes and original startup-error provenance. Full frozen source, raw events, patches, grading logs, commands and independent audits are retained locally under `scratch/20260913-acceptance-comparison/`, which is ignored by Git. No benchmark code, prompts or selected tasks changed in response to outcomes.

Implementation validation: **221 tests passed**, including 18 new acceptance tests; Ruff lint and format checks passed; the built wheel contains all four new prompt templates. Tests cover isolation, immutable specifications, explicit false versus execution error, syntax/protocol failures, unavailable checks, repeated execution, bounded repair and shared model-turn budgets. Independent reviews checked code, frozen hashes, task traces and result accounting.

To reproduce the comparison, use the same source and isolated settings with separate output directories:

```bash
uv run python scripts/swe_bench.py run --ids django__django-12308,sphinx-doc__sphinx-8551,matplotlib__matplotlib-20859,pydata__xarray-4687,sympy__sympy-19040 --max-steps 50 --parallel 2 --out /tmp/prax-acceptance-baseline
uv run python scripts/swe_bench.py run --ids django__django-12308,sphinx-doc__sphinx-8551,matplotlib__matplotlib-20859,pydata__xarray-4687,sympy__sympy-19040 --max-steps 50 --parallel 2 --acceptance-check --out /tmp/prax-acceptance-candidate
```
