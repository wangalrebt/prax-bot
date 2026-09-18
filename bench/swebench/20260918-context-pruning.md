# What a run carries from call to call, measured and cut — 2026-09-18

Every model call resends the whole transcript. This note records where those tokens went, what was changed, and
two measurements on the twelve-task set, the first of which failed the rule set before it ran.

## Where the tokens went

From the event logs of the 31 latest SWE-bench runs, 915 model calls:

| measure | value |
|---|---|
| prompt tokens | 12.14M, 96% of all tokens |
| completion tokens | 0.46M |
| context at step 1, median | 5,700 tokens |
| context at step 50, median | 18,000 tokens |
| largest context in any run | 29,000 tokens |
| summarising compactions at the 80,000-token budget | 0 |
| hard-cap elisions at 160,000 characters | 0 |

Age-based pruning was already holding the context nearly flat, and the two heavier mechanisms sat beyond anything a
50-step run reaches. A replay of the logged result sizes, which reproduces the measured contexts to within 8%,
splits the prompt tokens like this:

| resent with every call | share |
|---|---|
| system message, tool specifications, issue text | 33% |
| repository map | 13% |
| tool results after pruning | 38% |
| old tool-call arguments, never pruned | 16% |

The old tiers hardly bit: they cut at 4,096 characters and the mean result is 2,800, so the last 24 results were
in effect whole. Token Factory reports no cached prompt tokens even for an identical prefix
(`cached_tokens: 0`, `prompt_cache_miss_tokens` equal to the prompt), so every resent token is billed; identical
prefixes do come back about a second faster.

## What changed

1. **Results age by kind.** In the logs 73 of 84 repository edits followed a read of that file at most five
   results back, so file reads keep the six-whole window and are then cut to 2,048 and 600 characters. Every other
   result stays whole for three, then 1,500, then 400, then a one-line digest. Under this split no more edits
   work from a cut read than before: 8 of 84, against 12 for a uniform tightening that saves the same 12%.
2. **Old call arguments become their size.** In calls older than the last eight, string arguments of 200
   characters or more, which are written files and edit texts, become a placeholder. Tools refuse arguments that
   carry the placeholder.
3. **The repository map goes** after the first repository edit and ten steps. First edits land at a median of
   step 6, and 3% of tool calls after step 10 open a file for the first time.

The rule, written into commit 15a1e4e before the first run: keep at 8 or more of 12 resolved with at least 15%
fewer tokens per step than the 13.6k of the baseline, otherwise revert.

## Two measurements

One sample per task, default settings, 50 steps, three at a time, each from a worktree frozen at its commit.

| run | commit | resolved | steps | tokens | tokens per step |
|---|---|---|---|---|---|
| baseline, 20260913T191005Z | before | 9 of 12 | 394 | 5.37M | 13.6k |
| candidate 1, 20260918-context1-assembled | 15a1e4e | 7 of 12 | 380 | 3.94M | 10.4k |
| candidate 2, 20260918T070943Z | c927d86 | 10 of 12 | 392 | 3.76M | 9.6k |

Candidate 1 failed the rule by one task. In its first run matplotlib-25122 died on a sandbox API timeout before its
first step; it was run again alone and resolved in 9 steps, which is what the assembled file holds. Two tasks
flipped to misses, requests-5414 and sphinx-8621, both from the set's flaky tail. The logs gave a cause for one.
When requests-5414 called finish, its only full test run was five results old and had been cut to 1,500
characters, and the head-and-tail cut had spent the whole tail on the harness's "code referenced above" block. The
line "8 failed, 27 passed, 7 errors" was gone, and the finish summary called the failures "unrelated to the
change (network or pre-existing)". Under the old 4,096 cut that line survived. The one-line digest of a command
result had the same flaw under the old tiers too: it kept the last three lines, which were the harness's.

Candidate 2 changes only that. A command result is cut by its structure: mostly the end of what the command
printed, then the pointer to its full output and the notes on failing tests; the referenced definitions, which
were for the step that followed, go. On the logged transcripts the summary line of an aged test run then survives
in 38 of 39 cases. It passes the rule with 29% fewer tokens per step and 30% fewer tokens for the run. Both
flipped tasks resolved again, requests-5414 in 20 steps, and sympy-15017 resolved as well.

What the logs say about the fears, old policy (20260913T070324Z logs), then candidate 1, then candidate 2:

| diagnostic | old policy | candidate 1 | candidate 2 |
|---|---|---|---|
| mean context per call | 13.4k | 9.9k | 9.2k |
| largest context | 29.1k | 20.2k | 15.5k |
| file reads that overlap an earlier read | 64% | 63% | 63% |
| file reads per 100 calls | 26 | 27 | 29 |
| failed edits | 20 | 12 | 6 |
| placeholder copied into a call, refused | none exist | 1 | 0 |
| searches, outlines and listings, share of calls after step 10 | 12% | 22% | 23% |

Pruning did not make the model read files again, and edits did not start missing. The model did copy a
placeholder once, in requests-1142, as the content of a scratch file; the tool refused it, the model moved on and
the task resolved, so the refusal stays.

The map is the weakest of the three. Once it is gone the model navigates more: searches, outlines and listings are
22% and 23% of the calls after step 10 against 12% before, about thirty more calls in a run. At ten thousand tokens
a call that gives back roughly two thirds of the 1,700 tokens a call the map cost, and those calls also use up
steps. It stays because the package passed as measured; a map reduced to file names is the variant to try next.

## What this does and does not show

Ten of twelve is the best count on this set, and it is one sample: on this set one task either way is noise, and
the difference between the two candidates is partly that. What is not noise is the cost per step, down by a
quarter or more in both runs, and the mechanism found in the requests-5414 log. A third of every call is still the
fixed prefix: 3,800 characters of system message and 7,100 of tool specifications. That is the next place to look,
and it has to be measured the same way, because those texts were tuned on this set.
