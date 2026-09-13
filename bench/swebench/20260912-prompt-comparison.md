# Paired prompt benchmark — 2026-09-12

Both revisions resolved **5 of 6 issues**. The revised commit used **2,407,989 tokens versus 2,693,196**, a **10.6% reduction** in this run, and **167 versus 187 steps**. The same Django issue failed in both. No new solve or solve regression appeared, and no infrastructure errors or grading retries occurred.

## Method

Compared baseline `82d7a22` with candidate `18dbb5d` in separate, frozen worktrees. The benchmark runner, dependency lock and cached dataset were identical. Both used `nvidia/nemotron-3-super-120b-a12b`, temperature 1.0, top-p 0.95, low effort, a 50-step cap, and two concurrent tasks per revision. Each patch was graded by the existing official SWE-bench test scripts and log parsers in a fresh sandbox.

Selection was fixed before execution: exclude the 31 instance IDs already present in tracked benchmark results; retain the `<15 min fix` tier; sort IDs, shuffle with seed `20260912`, then take the first six distinct repositories. The eligible pool had 163 issues across nine repositories. Each revision ran once on each selected task. The seed selects tasks; it does not seed model generation.

## Results

| Issue | Baseline resolved | Revised resolved | Baseline steps | Revised steps | Baseline tokens | Revised tokens | Token change |
|---|---|---|---:|---:|---:|---:|---:|
| astropy__astropy-7336 | yes | yes | 25 | 10 | 333,031 | 106,347 | -68.1% |
| django__django-12308 | no | no | 39 | 39 | 522,977 | 522,480 | -0.1% |
| sympy__sympy-24539 | yes | yes | 11 | 14 | 98,242 | 121,721 | +23.9% |
| matplotlib__matplotlib-25287 | yes | yes | 30 | 25 | 430,085 | 291,345 | -32.3% |
| sphinx-doc__sphinx-7440 | yes | yes | 50 | 50 | 943,275 | 975,164 | +3.4% |
| pytest-dev__pytest-6202 | yes | yes | 32 | 29 | 365,586 | 390,932 | +6.9% |
| **Total** | **5/6** | **5/6** | **187** | **167** | **2,693,196** | **2,407,989** | **-10.6%** |

The revised run used fewer tokens on three tasks and more on three. Most savings came from Astropy and Matplotlib. Wall time is recorded in the JSON, but imports, concurrent work and shared-service load make it unsuitable for a clean latency comparison here.

## Trace findings

- **Django remains a real coding miss.** Both patches serialize ordinary JSON values but raise `TypeError` on the tuple-key dictionary exercised by grading. Both pass 20 existing checks and fail both target checks.
- **Temporary-file hygiene remains weak.** The candidate submits `test_reproducer.py` in Astropy and an intentionally failing `bug.py` in pytest. The baseline submits an empty `=0.12,` file in pytest after an unquoted pip requirement is interpreted as shell redirection. These files do not prevent the selected official tests from passing.
- **The finish contract fails at the step cap.** Both Sphinx runs supply `{"command": "...pytest..."}` to forced `finish`. The runtime accepts the call and substitutes `(no summary given)`; that final command never runs. Both had unresolved local checks. The revised run also observed two failing internationalized-glossary tests, without establishing baseline status.
- **Fewer tokens does not mean stronger workflow compliance.** Several runs edit before reproducing. Both versions repeatedly guess test targets or rerun already-passing subsets. The candidate’s Astropy run attempts to restore the baseline after its tests pass; the existing guard prevents losing the fix.
- **Most successful source fixes agree.** SymPy, Matplotlib and Sphinx patches are byte-identical. Pytest has the same source fix with different extra files; Astropy fixes are behaviorally equivalent.

## Interpretation

This run shows equal resolution with lower aggregate token use for the revised commit. Six issues and one stochastic sample per version cannot establish a reliable benchmark-wide improvement. No prompts were changed during the run. These issues were held out from this repository’s tracked evaluations; their model-training exposure is unknown.

The comparison covers the whole commit as exercised by the direct-Agent SWE-bench runner: core/style/environment instructions, tool descriptions, protected-file guidance and budget reminders. It does not isolate `system.md` or test local execution, Session-scheduled verification, read-only tasks, features or documentation. Passing the selected official tests also does not establish that all broader checks passed.

The traces support two concrete follow-up investigations: validate forced `finish` arguments, and keep reproduction/installation artifacts out of submitted patches. Reliable discovery of test commands remains another source of wasted work.

## Evidence

- [Manifest, settings, hashes, per-task results and trace findings](20260912-prompt-comparison.json).
- Full local logs, patches, grading output and prompt snapshots are retained under `scratch/20260912-prompt-comparison/{baseline,candidate}/`; these scratch artifacts are ignored by Git.
- The existing network-grading retry policy would overwrite earlier grading logs and retain only the last evaluation cost. Every task in this comparison graded on its first attempt.
