# Bounded finish review experiment — 2026-09-12

> The code behind this experiment (the runner flag, its prompts and tests) was removed from the product after this report; it is complete at commit 8dd6ea1 of this repository.

The review did not correct an observed defect in this experiment. It was requested on three of four candidate tasks, adding six model calls and 88,627 tokens after the first finish requests. No candidate changed repository source during review. The feature remains opt-in; these results do not justify enabling it by default.

## Method

Both arms ran the same frozen source based on `18dbb5d`, including the review implementation. Baseline left the new flag disabled; candidate alone supplied `--review-before-finish`. This is a same-source toggle comparison, not a direct run of the historical commit. Initial system instructions, tool schemas, model settings, dependency versions and dataset hashes matched. The runner and source were frozen before either arm started.

The mechanism defers the first eligible finish after an edit tracked by `ToolBox.repo_edited`. It requires at least two steps remaining and skips finishes already forced by a stall or the step cap. It permits at most five further turns that call tools, requests finish on the last, and stays within the original 50-step cap. Review can end sooner. It is a continuation with the same model and conversation, not an independent reviewer or proof that a check ran. Shell-only edits are not detected by the existing edit tracker. Malformed finish handling was not changed.

`django__django-12308` is a development case: its previous failure informed the review experiment. Three fresh tasks were selected before execution, excluding 37 previously evaluated IDs, including the previous report that was still untracked. With seed `20260912`, a sorted/shuffled non-Django pool supplied one task rated `<15 min fix`, then two rated `15 min - 1 hour`, retaining distinct repositories. The fresh tasks were scikit-learn-13779, Sphinx-8551 and Astropy-13033. Gold patches and test patches were not used for selection or agent feedback. Fresh means unseen in this repository's recorded runs, not necessarily absent from model training.

Settings: `nvidia/nemotron-3-super-120b-a12b` on Nebius Token Factory, temperature 1.0, top-p 0.95, low effort, 16,384 completion-token cap, 80,000 context budget, 50 steps, command/import timeouts 300/900 seconds, no investigation model. There was one stochastic sample per task per arm, with two tasks per arm in parallel. Each arm had a clean config directory and separate outputs. Official grading happened after the agent exported its patch, in a fresh sandbox.

## Results

| Task | Cohort | Baseline resolved | Review resolved | Baseline steps | Review steps | Baseline tokens | Review tokens | Review requested |
|---|---|---|---|---:|---:|---:|---:|---|
| django__django-12308 | development | no | no | 50 | 50 | 740,581 | 688,058 | step 48 |
| scikit-learn__scikit-learn-13779 | fresh | yes | yes | 9 | 20 | 108,111 | 220,572 | step 17 |
| sphinx-doc__sphinx-8551 | fresh | no | no | 46 | 50 | 624,247 | 713,275 | no |
| astropy__astropy-13033 | fresh | no | no | 19 | 26 | 263,780 | 382,630 | step 25 |

| Cohort | Baseline resolved | Review resolved | Baseline tokens | Review tokens | Token change |
|---|---|---|---:|---:|---:|
| Three fresh tasks | 1/3 | 1/3 | 996,138 | 1,316,477 | +32.2% |
| Django development case | 0/1 | 0/1 | 740,581 | 688,058 | −7.1% |
| All four, descriptive only | 1/4 | 1/4 | 1,736,719 | 2,004,535 | +15.4% |

Total model calls/steps were 124 versus 146; review itself accounts for six additional calls and 88,627 tokens. The rest of the complete-run differences include different trajectories before review. Two fresh cases have a higher difficulty rating than the previous six-task prompt comparison, so these scores are not directly comparable to that earlier 5/6 result.

Baseline Sphinx finished its model run, then patch export raised `UnicodeDecodeError`. Recovery exported the exact bytes from its saved final checkpoint and graded those bytes against the original task image with the same official evaluation script/parser. No new model attempt ran, no files were filtered, and the original failed result and aggregate remain untouched. The recovered patch includes 964 file diffs and 7,904,804 bytes, including generated documentation and a copy of an intentionally non-UTF-8 test fixture. Raw patch and grading output bytes are preserved; only parser/display logs use replacement for undecodable bytes. Recovery graded 0/1 target and 32/32 regression tests, failing on missing `py:module` metadata. Its 46 steps and 624,247 tokens above are reconstructed from the original event log. Recovery cost was 0.04149121 in the sandbox SDK's cost units, separate from the original run. All eight submitted patches were ultimately graded once; no task received a second model attempt.

## What review actually did

- **Django:** Requested at step 48, with deadline 50. The agent searched for `prepare_value` in the model JSON field, found no match, then sent file-path arguments to forced `finish`. No fix or check followed. Both arms graded 0/2 target and 20/20 regression tests, failing serialization of a dictionary with a tuple key. Both final summaries were missing. Review added two calls and 30,994 tokens.
- **Scikit-learn:** Both arms passed 2/2 target and 18/18 regression tests. Review started at step 17, wrote a scratch check using `n_jobs=2`, never executed it, reran the already-passing 18-test suite, then repeated the initial summary at step 20. It made no source changes and added three calls and 34,630 tokens. The candidate's extra helper guard was introduced before review; baseline submitted a root reproduction script, while candidate kept scratch files outside the repository.
- **Astropy:** Both arms failed the target test on the required error-message wording while passing 20/20 regression tests. Review started at step 25; the next turn repeated the same finish summary. It added one call and 23,003 tokens, with no intervening tool action or check. Both patches contained source changes only. Broader local leap-second failures were called pre-existing without baseline evidence.
- **Sphinx candidate:** Review never triggered because the run reached its 50-step limit. Its patch suppressed broad cross-reference lookup but did not supply required `py:module` metadata, grading 0/1 target and 32/32 regression tests. Repeated scratch builds and invalid final test targets consumed steps; forced finish carried command arguments instead of a summary. Its final patch contained no extra repository files.

## Interpretation and limits

The review instructions alone did not make the model execute a new check or fix a missed condition. Triggering only at finish can leave too little time to investigate, as in Django, and tasks that consume their full budget may never receive review. These observations motivate testing an earlier review point or a mechanism that executes a proposed check; neither improvement is demonstrated here.

The arms already differed before review. On scikit-learn, candidate used 185,942 tokens before review versus baseline's completed 108,111; on Astropy, candidate used 359,627 before review versus baseline's completed 263,780. Those differences cannot be attributed to the review prompt. Outcome differences alone likewise do not establish causation in single stochastic runs. Report the three fresh tasks separately from Django, and do not generalize this small sample to the full benchmark.

This direct-Agent runner does not exercise Session's goal loop or scheduled verification. Grading success does not establish that all broader checks passed or that the submitted patch is clean. The existing network-grading retry policy may overwrite earlier logs and retain only the final evaluation cost; attempt counts are recorded.

## Evidence and reproduction

The [companion JSON](20260912-review-comparison.json) records selection, effective settings, source hashes, per-task results, review activation and overhead. Full frozen source, initial prompt/tool snapshots, raw events, patches, grading logs and the preserved infrastructure error are retained locally under `scratch/20260912-review-comparison/` (ignored by Git).

Run both commands from the same revision and isolated settings, using different output directories:

```bash
uv run python scripts/swe_bench.py run --ids django__django-12308,scikit-learn__scikit-learn-13779,sphinx-doc__sphinx-8551,astropy__astropy-13033 --max-steps 50 --parallel 2 --out /tmp/prax-review-baseline
uv run python scripts/swe_bench.py run --ids django__django-12308,scikit-learn__scikit-learn-13779,sphinx-doc__sphinx-8551,astropy__astropy-13033 --max-steps 50 --parallel 2 --review-before-finish --out /tmp/prax-review-candidate
```

Implementation validation: all 203 unit tests passed, Ruff lint/format checks passed, independent code review found no blocking issue, and the built wheel contains the new review prompt. No default CLI/session behavior was changed.
