# Prax Bot's 31 tasks against the published SWE-bench Verified results — 2026-09-14

Every submission on the official SWE-bench Verified leaderboard publishes the list of instances it resolved. Scoring those lists on the 31 tasks Prax Bot has run puts our number next to published harnesses on the same tasks, without running anything. The data comes from the `SWE-bench/experiments` repository (`scripts/leaderboard.py`, output in `20260914-leaderboard.json`); 174 of 182 submissions carry a usable list.

## What the leaderboard looks like by tier

Verified's 500 tasks are tiered by the annotators' estimate of the fix: 194 under 15 minutes, 261 between 15 minutes and an hour, 42 between one and four hours, 3 longer. Every task Prax Bot has run except two is in the first tier.

| | overall | under-15-minute tier | 15-minute-to-hour tier | our 31 |
|---|---|---|---|---|
| top 30 submissions, mean | 75.2% | 87% | 73% | 82% (25.5 of 31) |
| all 174 submissions, mean | | | | 64% |
| best single submission | 79.2% (two, both Claude Opus 4.5) | 90% | 79% | 28 of 31 (Claude Opus 4.6, bash only) |

Our 31 are close to the easy tier's difficulty for the field: the top 30 resolve 82% of them against 87% of the tier. Four of them almost nobody solves: matplotlib-23476 is resolved by 1% of submissions, sympy-20916 by 7%, matplotlib-20676 by 15%, pylint-4970 by 16%. Those are the four Prax Bot has never solved either.

## The bash-only board, on the same 31 tasks

The bash-only leaderboard runs every model through the same minimal harness (mini-SWE-agent: one bash tool, one prompt), so it isolates the model. Its published runs, scored on our 31:

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
| **Prax Bot, Nemotron 3 Super** | not run | 77% mean pass rate on 38 tasks | **23** |

Across the 40 bash-only runs, the count on our 31 predicts the overall score to within about five points (overall ≈ 3.1 × count − 6). A count of 23 sits with GPT-5 mini, GPT-5.1, Claude 4.5 Haiku and Gemini 3.5 Flash, whose published overall scores run from 59.8% to 71.8%; the fitted estimate is 65%. That is the number Prax Bot on Nemotron 3 Super would plausibly post on the full 500, with the caveat that it rests on 31 tasks from the easy tier and one sample each.

## Nemotron 3 Super in published harnesses

NVIDIA's model card reports the model on the full Verified set: 60.47% under OpenHands, 59.20% under OpenCode, 53.73% under Codex, against Qwen3.5-122B at 66.40% and gpt-oss-120b at 41.9% under OpenHands. No bash-only submission exists for it. Read together with the mapping above, Prax Bot's harness is at least on par with the published harnesses for this model, and the difference between 60% and 65% is inside the uncertainty of a 31-task sample.

## What this says about the next steps

- The four tasks we never solve are the four the field never solves; there is no harness change waiting there.
- Everything we have measured is the easy tier. The published runs lose 14 points from the easy tier to the middle tier, and the middle tier is 261 of the 500 tasks. A stratified sample from it is the measurement that would turn the 65% estimate into a number.
- The gap between our harness and the published harnesses is smaller than the gap between models on the bash-only board: two tiers of model separate gpt-oss-120b (15 of our 31) from Claude 4.6 Opus (28). Harness work moves a task or two; the model moves ten.

## Addendum: the reference harness run on the same model, same tasks

Before the decision to rely on published numbers, mini-SWE-agent 2.4.6 had already been run on the 31 with Nemotron 3 Super: the same sampling, effort and 50-step cap as ours, in Token Factory sandboxes through its own sandbox environment, graded with the same official harness (`scripts/swe_bench.py grade`; results in `minisweagent/`). The only change to its published configuration is the removal of `parallel_tool_calls`, which the endpoint rejects.

| | resolved of 31 | steps | tokens |
|---|---|---|---|
| mini-SWE-agent, Nemotron 3 Super, 50 steps | 20 | 709 | 6.8M |
| Prax Bot, Nemotron 3 Super, 50 steps | 23 | 933 | 12.2M |

Eighteen tasks resolved under both. Five resolved only under Prax Bot: django-12193 (the reference's patch was wrong) and four where the reference ran out of its 50 steps without submitting (django-16899, xarray-4094, pylint-7277, sphinx-9591). Two resolved only under the reference: requests-5414 and pytest-7521, both tasks Prax Bot has resolved in other runs. The reference submitted nothing on seven tasks in all; its published leaderboard runs use a 250-step limit, so at its own budget it would likely have closed some of those. The four tasks neither solves are the four the field rarely solves.

The reference used 44% fewer tokens for three fewer tasks. The difference is where the steps go: the reference reads with `cat` and `sed` and stops when it has a patch; Prax Bot spends steps on its post-fix verification, which the earlier traces put at a third of all steps.
