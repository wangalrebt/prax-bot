# The rewritten prompts on the twelve-task regression set — 2026-09-13

Commit 18dbb5d rewrote every prompt the model sees and changed how the system message is assembled: core rules, code style, an environment section per backend, and a verification plan. Its own comparison was six held-out tasks, one sample each (5 of 6 both ways, 11% fewer tokens). This note runs it on the twelve-task set that every earlier prompt change was judged on, and isolates which part of the new text costs what.

| prompt state | resolved | steps | tokens | run file |
|---|---|---|---|---|
| before the rewrite: measured rules, symbol read, halfway nudge (Sep 10) | 8 of 12 | 350 | 4.36M | 20260910T052238Z |
| the rewrite as committed | 8 of 12 | 433 | 6.09M | 20260913T070324Z |
| hybrid: the measured rules with the rewrite's environment and verification sections | 8 of 12 | 435 | 6.12M | 20260913T072020Z |
| the rewrite with a two-sentence verification plan | 9 of 12 | 394 | 5.37M | 20260913T191005Z |

Each run is one sample per task at the default settings (Nemotron 3 Super, temperature 1.0, top-p 0.95, effort low, 50 steps), graded with the official harness; the second and fourth ran from a worktree frozen at 18dbb5d. On this set one task either way is noise: the third miss has rotated among five tasks over nine runs.

The rewritten rules and the old rules cost the same when they carry the same appended sections (6.09M against 6.12M), so the rules text is not what changed. The appended verification plan is. With no test or lint command configured the plan said: "Find the relevant commands in project configuration, CI or documentation and run the checks needed for this task before finish." Django-11880 then ran 24 commands, every one a test invocation, against 12 with the shorter plan; pytest-5809 ran 16 against 3. The two-sentence plan ("No commands are scheduled by the runtime. Run the checks relevant to your change before finish and report their results.") recovers most of the cost and is what ships. The remaining gap to the Sep 10 figure is inside the noise of this set, but it is on the wrong side of it, and the next prompt change should be measured here before it is kept.

Also kept from the rewrite: the stronger stale-test rule, which requires an explicit conflicting requirement before a test is called obsolete, addresses the django-16899 misuse seen on Sep 9. Lost from the earlier text and worth watching: the one-script cap on reproductions (135 of 894 steps across the 31 traces were scratch-file writes) and the instruction to make the most likely edit when the budget is nearly gone, which the rewrite replaced with a warning against speculative edits.
