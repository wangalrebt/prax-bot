# Prax Bot's 31 tasks against the published SWE-bench Verified results — 2026-09-14

Corrected on **2026-10-25**: seven empty mini-SWE-agent submissions had recorded usage that the grading report omitted. The corrected total is **11,087,598 tokens and 1,059 steps**, not 6.8M tokens and 709 steps. The [correction artifact](20260914-published-comparison-correction.json) retains all 31 corrected rows, the seven recovered metadata records, source hashes and paired outcomes. Original result files are unchanged; no model or grading run was repeated.

This report describes the **September 14 leaderboard snapshot and historical 31-task comparison**, not the current leaderboard or every task subsequently evaluated in this repository. Scoring published resolved-instance lists on those 31 tasks puts the runs alongside one another without rerunning them. The data comes from the `SWE-bench/experiments` repository (`scripts/leaderboard.py`, output in `20260914-leaderboard.json`); 174 of 182 submissions carried a usable list.

## What the leaderboard looks like by tier

Verified's 500 tasks are tiered by the annotators' estimate of the fix: 194 under 15 minutes, 261 between 15 minutes and an hour, 42 between one and four hours, 3 longer. All 31 tasks in this historical comparison are in the first tier.

| | overall | under-15-minute tier | 15-minute-to-hour tier | our 31 |
|---|---|---|---|---|
| top 30 submissions, mean | 75.2% | 87% | 73% | 82% (25.5 of 31) |
| all 174 submissions, mean | | | | 64% |
| best single submission | 79.2% (two, both Claude Opus 4.5) | 90% | 79% | 28 of 31 (Claude Opus 4.6, bash only) |

The top 30 submissions resolve 82% of these 31 tasks against 87% of the easy tier. Four tasks had low resolve rates in this snapshot: matplotlib-23476 at 1% of submissions, sympy-20916 at 7%, matplotlib-20676 at 15%, and pylint-4970 at 16%. These rates describe the recorded submissions; they do not show that a task is impossible or that harness changes cannot help.

## The bash-only board, on the same 31 tasks

The bash-only leaderboard uses mini-SWE-agent across models. Its published runs, scored on these 31 tasks, are shown below. A shared harness reduces one source of variation, but model settings, budgets and run versions still matter.

| model, mini-SWE-agent | overall | under-15-minute tier | our 31 |
|---|---|---|---|
| Claude 4.6 Opus | 75.6% | 88% | 28 |
| Claude 4.5 Opus, high | 76.8% | 89% | 27 |
| Gemini 3 Flash, high | 75.8% | 88% | 27 |
| GLM-5, high | 72.8% | 85% | 25 |
| GPT-5.2, high | 72.8% | 85% | 25 |
| Claude 4.5 Sonnet, high | 71.4% | 85% | 25 |
| Kimi K2.5, high | 70.8% | 85% | 25 |
| DeepSeek V3.2, high | 70.0% | 82% | 24 |
| MiniMax M2.5, high | 75.8% | 87% | 24 |
| Gemini 3.5 Flash | 71.8% | 81% | 23 |
| Claude 4.5 Haiku, high | 66.6% | 82% | 23 |
| GPT-5.1 | 66.0% | 78% | 23 |
| GPT-5 mini | 59.8% | 77% | 23 |
| Devstral Small 2512 | 56.4% | 72% | 20 |
| Qwen3-Coder 480B | 55.4% | 70% | 18 |
| GLM-4.6 | 55.4% | 70% | 19 |
| gpt-oss-120b | 26.0% | 42% | 15 |
| **Prax Bot, Nemotron 3 Super** | not run | only the 31-task subset | **23** |

Several published models also resolved 23 of these 31, while their full-500 scores ranged from 59.8% to 71.8%. The original report extrapolated a roughly 65% Prax Bot score from a fit across 40 bash-only runs. That fit is descriptive of those runs, not a validated prediction for Prax Bot: this subset contains only easy-tier tasks and was already used for development. **Prax Bot has no measured full-500 score in this comparison.**

## Nemotron 3 Super in published harnesses

The original report compared its extrapolation with NVIDIA's full-Verified model-card results under other harnesses. Different task coverage, configurations and budgets prevent that comparison from establishing harness parity. The direct mini-SWE-agent comparison below is more relevant because it uses the same model and task IDs, but it remains one historical sample per harness.

## What this says about the next steps

- Inspect failure traces before attributing misses to the model, tools, budget or grading environment. Low solve rates across other submissions do not identify the cause.
- Use fresh, stratified tasks to assess broader performance, reporting development tasks separately. A full-500 score requires evaluating that set; an easy-tier fit cannot substitute for it.
- Compare harnesses with fixed model settings, task IDs and explicit budgets, and retain usage for every attempt. This sample cannot establish a general ordering between model changes and harness changes.

## Addendum: the reference harness run on the same model, same tasks

mini-SWE-agent 2.4.6 was run on the same 31 tasks with Nemotron 3 Super, the same reported sampling and effort settings, and a 50-step cap, in Token Factory sandboxes through its own sandbox environment. Submitted patches were graded with the same official harness (`scripts/swe_bench.py grade`; original results in [minisweagent/20260914T005753Z-grade.json](minisweagent/20260914T005753Z-grade.json)). The run also removed `parallel_tool_calls`, which the endpoint rejected. A shared step cap does not make the harnesses' tools or work per step identical.

| | resolved of 31 | steps | tokens |
|---|---|---|---|
| mini-SWE-agent, Nemotron 3 Super, 50 steps | 20 | 1,059 | 11,087,598 |
| Prax Bot, Nemotron 3 Super, 50 steps | 23 | 933 | 12,211,376 |

Eighteen tasks resolved under both. Five resolved only under Prax Bot: django-12193, where mini submitted a patch that did not resolve the issue, and four where mini exhausted 50 steps without submitting (django-16899, xarray-4094, pylint-7277, sphinx-9591). Two resolved only under mini: requests-5414 and pytest-7521. **Six** resolved under neither: matplotlib-20676, matplotlib-23476, pylint-4970, sphinx-9258, sympy-17655 and sympy-20916.

Mini submitted no patch on seven tasks. All seven saved records report `LimitsExceeded` at 50 steps, totaling **4,293,139 tokens and 350 steps**. `reeval_instance` returned before loading this metadata, and the generated Markdown displayed missing tokens as zero. The correction adds that recorded usage to the original 6,794,459 tokens and 709 steps; it leaves all resolution outcomes unchanged. The seven empty submissions were not graded. The remaining 24 submitted patches account for the original report's recorded usage.

Mini therefore used **9.2% fewer tokens for three fewer resolved tasks**, while taking **126 more steps**. These are recorded token counts, not USD costs. This comparison does not isolate why the trajectories differed. Mini's documented 250-step benchmark configuration also differs from this run's 50-step cap; the effect of increasing its budget was not measured. The result does not establish parity with full-500 submissions or a general efficiency advantage for either harness.
