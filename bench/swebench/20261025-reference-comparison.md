# Prax Bot and mini-SWE-agent: six stratified tasks — 2026-10-25

**Prax resolved 2/6; mini-SWE-agent resolved 3/6.** Mini used **25.6% fewer recorded tokens** on these six tasks. Both solved Pytest and Requests; mini additionally solved Django. There are no ungraded primary outcomes. This is one exploratory sample per harness, not a full-Verified score or evidence of a general ranking.

| Harness | Resolved | Steps / recorded completions | Input tokens | Output tokens | Total tokens |
|---|---:|---:|---:|---:|---:|
| Prax Bot | 2/6 | 194 / 194 | 1,998,068 | 102,516 | 2,100,584 |
| mini-SWE-agent 2.4.6 | 3/6 | 155 / 155 | 1,536,370 | 26,422 | 1,562,792 |

| Task | Annotated difficulty | Prax: F2P / P2P | Mini: F2P / P2P | Resolved by |
|---|---|---|---|---|
| sphinx-doc__sphinx-11510 | 1-4 hours | 0/2 / 7/7 | 0/2 / 7/7 | Neither |
| django__django-13837 | 1-4 hours | 0/1 / 57/57 | 1/1 / 57/57 | mini |
| sympy__sympy-15599 | 15 min - 1 hour | 0/1 / 80/80 | 0/1 / 80/80 | Neither |
| astropy__astropy-14182 | 15 min - 1 hour | 0/1 / 9/9 | 0/1 / 9/9 | Neither |
| pytest-dev__pytest-7982 | <15 min fix | 1/1 / 78/78 | 1/1 / 78/78 | Prax, mini |
| psf__requests-1766 | <15 min fix | 6/6 / 79/79 | 6/6 / 79/79 | Prax, mini |

F2P counts issue-specific tests that now pass; P2P counts retained regression-test passes. By stratum, Prax/mini resolved **0/2 versus 1/2** on 1–4-hour tasks, **0/2 each** on 15–60-minute tasks, and **2/2 each** on under-15-minute tasks.

**Selection and controls.** Seed `20261025` selected two tasks from each of those three strata, with one task per repository and 43 previously exposed local task IDs excluded. Selection used metadata before any solver call. The three tasks annotated above four hours were outside this design. “Fresh” means no recorded prior local attempt or development exposure was found; it does not establish training-data novelty. The sample is deliberately stratified, not representative of the full dataset.

Both arms used `nvidia/nemotron-3-super-120b-a12b` through Token Factory: temperature 1, top-p 0.95, low reasoning effort, maximum completion 16,384 tokens, and 50 agent steps. There was one actual solver attempt per task per arm, with two workers per arm. Prax auxiliary model usage would count toward totals; none added calls beyond its 194 steps here. Native prompts, tools and work per step differ: mini permits multiple shell calls in a turn and forbids configuration-file edits; Prax uses typed tools and a context budget of 80,000. The same step cap does not equalize compute or functionality.

Prax ran from a frozen 128-file source tree based on `9183679`, including the reviewed path and accounting fixes. Mini was pinned to 2.4.6, with 98 dependency versions and 70 source hashes retained. The official `swebench` parser was pinned to 5.0.2. Source, selection, prompts and model settings were frozen before solver work; the subsequent integrity audit passed. No hidden-test feedback entered a solver.

**Patch policy and recovery.** The primary result grades the entire final worktree against the original task-image HEAD, including nonignored untracked files and step-limit exits. Mini’s five nonempty native submissions were also graded separately, and Sphinx’s empty submission counted as no submission. These secondary outcomes agree with every primary outcome, giving mini **3/6** under either policy in this run. The primary policy differs from the historical submission-only mini comparison and is not an upstream leaderboard submission protocol.

The evaluation adapter initially assumed image HEAD equaled the dataset base commit. Read-only checks showed that every image contains a pre-existing setup commit; Sphinx and Astropy also have pre-existing configuration changes. All six initial mini preparations stopped with **zero model calls**. Their first actual solver attempts then used the audited image HEAD. Prax solver work continued unchanged: four grades were reused only because corrected patch bytes and image matched exactly; Sphinx and Astropy were regraded from the same final checkpoints against the correct baseline. All original patches, grades, failures and returned costs remain retained.

Mini’s Sphinx export exceeded the 8 MiB stdout limit because it contained generated `_build/` documentation. The partial export was preserved, then the complete **11,890,345-byte** patch was recovered from the same final checkpoint under a fixed 64 MiB download ceiling, with exact size/SHA256 verification. No files were filtered and no solver was rerun.

**Accounting.** All 12 model runs, six zero-model preparation failures, original grades, corrected grades, secondary native grades and recovery work are represented in the [machine-readable report](20261025-reference-comparison.json). Returned usage reconciles to 194 Prax and 155 mini completions; all mini responses have usage and none of its observed completion invocations failed. Provider-internal HTTP retries and charges remain unknown.

Observed sandbox SDK units total **1.26285724 for Prax** and **2.00275324 for mini**, plus **0.02615005** in unallocated shared diagnostics. Per resolved task, the arm totals are 0.63142862 and 0.66758441. These include experimental overhead: mini has extra preparation recovery and separate native-submission grading. They are **not USD prices or a solver-cost comparison**. Import/preparation gaps, SDK file-read billing and costs hidden by internal retries are explicitly unknown, rather than zero. Wall-time records retain their original scopes: Prax’s original job includes grading while mini’s does not, so those durations are not comparable latency measurements.

**What the traces show.** Prax’s Django patch targeted `-m django` and passed the existing autoreload tests, but missed the requested arbitrary-package behavior. Mini’s patch handled that behavior and passed grading. On SymPy, Prax used 32 read calls and encountered protected-test, missing-pytest, edit and restore failures; mini submitted after 14 steps using a proposal already included in the issue, without running tests. Both still failed grading. These observations identify places to investigate; they do not isolate a causal effect of guardrails, tools or prompts. Mini’s Sphinx run repeatedly assumed shell directory changes persisted and exhausted its budget. Its native instructions explicitly state that shells are fresh.

**Repository fixes and validation.** Path aliases now share observation/protection/definition-cache identity, including absolute paths and dotfiles; failed symbol/range reads no longer grant edit freshness. Benchmark reevaluation preserves usage for empty or missing patches, and missing usage displays as unknown. The historical 31-task mini total was corrected to **11,087,598 tokens / 1,059 steps**: 9.2% fewer tokens than Prax, not 44%. Historical resolution counts remain 20/31 versus 23/31; the unsupported full-500 extrapolation was removed. See the [historical correction](20260914-published-comparison.md).

Validation before source freeze: **260 tests passed**, Ruff lint/format passed, and Pyright reported zero errors/warnings. The evaluation recovery adapter separately passed **17 socket-blocked tests** for exact-byte export/upload, patch selection, grade reuse, retry accounting and unknown usage.

The [JSON report](20261025-reference-comparison.json) retains selection, configurations, source/dependency hashes, per-task grades, patch hashes, accounting and protocol corrections. Raw trajectories, patches and individual grading logs remain locally under `bench/swebench/scratch/20261025-reference-comparison/`, which is Git-ignored. The report does not claim a public archive of those raw files.
