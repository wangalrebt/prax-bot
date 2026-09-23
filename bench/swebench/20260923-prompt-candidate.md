# A system prompt candidate built from the logs, measured and reverted — 2026-09-23

The September 18 twelve-task run is the only one made with the current prompt and context handling. Its logs
show four things the prompt governs:

- Two of twelve runs threw their own work away with git to compare against the original code. sympy-20916 ran
  `git stash` at step 39, never popped it, and ended with no diff. requests-1142 was refused three restores to
  the baseline, ran `git checkout -- .`, and had to redo its fix, which landed at step 49 of 50.
- 23 of 380 tool calls failed avoidably: guessed test ids, scratch files written outside the scratch
  directory, refused overwrites of a scratch file, tools named `ls` or `find`, refused restores.
- 29 of 114 file reads read again a region already read with no edit since, only two of them right after a
  command.
- One run downgraded numpy to make its own test pass.

Commit 8e46105 answered each: an `original` option on `run` that runs a command against the repository without
the agent's changes, in a throwaway copy with the same scratch files and packages; `write_file` allowed to
replace a scratch file; and system-prompt rules against discarding work with git, against rereading unchanged
regions, for one rewritable reproduction script, against guessed test ids, and against changing installed
package versions. The rule, written into the commit before the run: keep at 9 or more of 12 with total tokens
not above the baseline's 3.76M, otherwise revert.

| run | resolved | steps | tokens | tokens per step |
|---|---|---|---|---|
| baseline, 20260918T070943Z | 10 of 12 | 392 | 3.76M | 9.6k |
| candidate, 20260923T044305Z | 7 of 12 | 369 | 3.82M | 10.4k |

It failed the rule and was reverted in 3ef17ad. The logs, counted the same way on both runs:

| diagnostic | baseline | candidate |
|---|---|---|
| runs that discarded their own work with git | 2 | 0 |
| runs with the original option | none existed | 0 |
| rereads of unchanged regions, of all reads | 29 of 114 | 29 of 121 |
| scratch files written | 32 | 37 |
| test runs that did not start, of all test runs | 11 of 34 | 14 of 39 |
| guessed test ids or paths | 6 | 9 |
| scratch files written outside the scratch directory | 6 | 5 |
| refused overwrites of a scratch file | 4 | 0 |
| calls to tools named `ls` or `find` | 4 | 1 |
| refused restores | 3 | 1 |

The new misses do not trace to the new text. pytest-5809 had the right edit at step 2, then reworked it until
the protected old test that asserts the old lexer passed, the stale-test trap already seen on django-16899;
sympy-15017 fixed a different pair of files than in the baseline run and missed a case. What the logs do show
is that the prompt rules changed nothing they targeted: rereads, scratch churn and guessed test ids are the
same or worse, and the model never once used the new option. The parts that moved were mechanical: a refusal
the tool no longer issues, and a tool description naming `ls` and `find`. That matches every earlier prompt
measurement on this set: wording the model is asked to follow has not changed its behaviour, while changes to
what the tools accept or return have.

Two bugs found while building the candidate are fixed independently in 528b915 and stay: a command that
called `exit` lost all its output, and on macOS the local backend's padded byte count leaked into results.
