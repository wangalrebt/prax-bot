# Prax Bot

A coding agent that works inside a [Nebius Token Factory Sandbox](https://docs.tokenfactory.nebius.com/sandboxes/overview), powered by [NVIDIA Nemotron](https://nebius.com/services/token-factory/nemotron) on Token Factory.

Give it a task in a git repository. It uploads the repository into an isolated sandbox, then reads, edits, and runs commands there until the task is done and tested. Nothing executes on your machine. At the end you see a diff and decide whether to apply it.

Built for the Nebius x NVIDIA Global AI Hackathon, Track 1: agents that write, run, and test code in Token Factory Sandboxes.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- A Nebius account with Token Factory access
- `NEBIUS_API_KEY`: your Token Factory API key. It also authenticates Sandboxes.
- `NEBIUS_PROJECT_ID`: your Nebius project id. Sandboxes require it.

## Install

```bash
uv sync
uv run praxbot doctor
```

`doctor` checks the credentials, lists the `nvidia/` models your key can reach, proves a tool-call round trip with the configured model, and runs a command in a sandbox, including a fork-isolation check. Add `--skip-sandbox` to test only the model.

Credentials can also live in the sandbox SDK's own file, `~/.config/contree/auth.ini`, which `contree auth -P <project id>` writes for you (`uv tool install contree-cli`). Prax Bot reads the `token` and `project` lines from the active profile.

Sandboxes are in beta and access is granted per project. If `doctor` reports a 403, request access at https://tokenfactory.nebius.com/sandboxes/about and use `--backend local` in the meantime.

## Use

Inside any git repository:

```bash
uv run praxbot run "fix the failing test in tests/test_cache.py" --setup "pip install -e . pytest"
```

What happens:

1. The working tree (tracked and untracked files, respecting `.gitignore`) is uploaded into a fresh sandbox built from `python:3.12` and committed there as the baseline.
2. The optional `--setup` command runs once, for dependencies.
3. Nemotron works through a small tool set: `list_files`, `search`, `read_file`, `outline`, `edit_file`, `write_file`, `run`, `restore`, `finish`. Every `run` and every edit produces a new immutable sandbox image, which is a checkpoint the model can `restore`.
4. When the model calls `finish`, or the step budget ends, the diff against the baseline is pulled out of the sandbox and shown.
5. You can type a follow-up instruction ("also handle the negative case"). The agent continues in the same sandbox, with the original task, its previous summary and the diff so far as context, and shows the new diff. Press Enter to stop.
6. You choose whether to apply the diff. Applying changes the working tree only; nothing is committed.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `nvidia/nemotron-3-super-120b-a12b` | Any model id on Token Factory |
| `--base-image` | `python:3.12` | OCI image for the sandbox; it needs `git` and `tar` |
| `--setup` | none | Shell command run once after upload |
| `--max-steps` | 40 | Model turns before the run stops |
| `--backend` | `contree` | `contree` runs in a Token Factory Sandbox. `local` runs on this machine in a private virtualenv, for development only |
| `--investigate-model` | none | A second model for the first investigation turns, for example Nemotron 3 Ultra, before switching to `--model` for edits |
| `--temperature`, `--top-p` | 0.2, unset | Sampling. NVIDIA recommends 1.0 and 0.95 for Nemotron 3 |
| `--effort` | high | How much the model reasons per step: off, low, high (the model's default), xhigh (a 32k completion cap) |
| `--yes` | off | Apply without asking |
| `--export PATH` | none | Also write the diff to a file |
| `--verbose` | off | Full tool outputs and checkpoint ids |

Environment variables `PRAXBOT_MODEL`, `PRAXBOT_TEMPERATURE`, `PRAXBOT_TOP_P`, `PRAXBOT_EFFORT`, `PRAXBOT_BASE_IMAGE`, `PRAXBOT_SETUP`, `PRAXBOT_MAX_STEPS`, `PRAXBOT_COMMAND_TIMEOUT`, `PRAXBOT_SETUP_TIMEOUT`, `PRAXBOT_IMPORT_TIMEOUT`, and `PRAXBOT_HOME` set the same things.

Every run is logged under `~/.praxbot/runs/<run id>/`: `events.jsonl` (every tool call and result), `diff.patch`, and `run.json` (model, tokens, sandbox commands, checkpoints with image ids, cost, timing).

Past runs are first-class:

```bash
uv run praxbot status                         # what is running now, recent runs and their verdicts, setup state
uv run praxbot inspect --list                 # recent runs
uv run praxbot inspect <run-id>               # steps and the checkpoint chain
uv run praxbot inspect <run-id> --diff 3      # the repository as it stood at checkpoint 3, as a diff
uv run praxbot inspect <run-id> --apply 3     # put that state into your working tree
uv run praxbot report <run-id> --open         # a self-contained HTML report
uv run praxbot resume                         # continue the latest run from its last checkpoint
uv run praxbot resume <run-id> "add a test for the empty case"   # continue with a new instruction
uv run praxbot resume <run-id> --from 3       # continue from checkpoint 3 instead of the last one
```

A goal keeps the agent working in rounds until something verifiable is true:

```bash
uv run praxbot goal "all tests pass and mypy is clean" --check "pytest -q && mypy src" --rounds 5 --setup "pip install -e . pytest mypy"
uv run praxbot goal "the CLI has a --json flag documented in the README"        # no check: the model reports when the goal is met
```

Each round is a fresh conversation on the same workspace, seeded with the goal, the previous round's summary and the diff so far. After every round the check command runs in the sandbox without creating a checkpoint; when it exits 0 the goal is met and the diff is shown. Without `--check`, the model ends a round with `finish(goal_met=true)` when it judges the goal achieved. The run stops early after the round cap, after two rounds that changed nothing, or on an error, and says which. The record in `run.json` keeps the objective, the rounds used and the verdict.

Every command and edit the agent made is an immutable sandbox image, so any intermediate state can be diffed, restored, or resumed after the fact. `resume` starts a new sandbox from the chosen image, so the installed dependencies and the work so far are already there, and writes a new run that records which run and checkpoint it came from. If the repository has an `AGENTS.md` or `CLAUDE.md`, its contents are given to the model at the start of the run.

## How the sandbox is used

Token Factory Sandboxes are immutable images. Running a command on an image produces a new image. Prax Bot keeps the current image as the workspace state: mutating commands and file writes advance it, read-only commands run disposably and leave it alone. That gives free checkpoints, an exact record of every state the agent passed through, and a diff that is computed inside the sandbox with git. Build artifacts such as `*.egg-info/`, `__pycache__/`, and `.pytest_cache/` are kept out of the diff through the workspace's `.git/info/exclude`.

The local backend implements the same five operations over a temporary directory: commands run with a scrubbed environment inside a private Python 3.12 virtualenv, and checkpoints are directory clones. It is convenient for development and it is not isolation: model-generated commands execute on your machine.

## A first run

Against a small package with two seeded bugs and four tests, Nemotron 3 Super listed the files, read the code and the tests, ran pytest, made two edits, re-ran pytest until it passed, and called finish. The diff touched one file. Numbers from runs of that task:

| | Token Factory Sandbox | local backend |
|---|---|---|
| model turns | 11 | 14 |
| tokens | 38k | 67k |
| commands and checkpoints | 7 and 7 | 6 and 7 |
| baseline ready after | 2.2 s plus a 3.4 s `pip install` | 0.1 s plus 3.5 s |
| wall time | 93 s | 57 s |

Every sandbox command is one immutable image; the run above went from baseline image `27269190…` to final image `d6afd5b8…` through seven checkpoints, and the diff was computed by git inside the sandbox.

## Limits

- One project type is tested: Python repositories using pytest. Other languages work if the base image has their toolchain.
- Commands are stateless between calls. A background server started in one `run` is gone by the next.
- The sandbox has network access, so a prompt injection inside a repository could exfiltrate repository contents. No credentials are ever placed in the sandbox.
- The model verifies its own work by running tests. There are no independent gates yet.

## Benchmark

`bench/` holds five seeded regressions in real Python libraries, each verified to fail at the bugged commit and pass at HEAD. `scripts/bench.py` runs the agent on each one in a sandbox and judges the exported diff independently: it is applied to a fresh copy of the bugged repository in a new sandbox, a broader test set than the one the agent was told about is run, and any change to a test file counts as a failure.

```bash
uv run python scripts/bench.py --src /path/to/clones --validate   # HEAD passes, bug fails
uv run python scripts/bench.py --src /path/to/clones              # run and judge
```

Four runs with Nemotron 3 Super, 40-step budget, the harness improving between runs as the failures were diagnosed:

| task | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|---|
| toolz-sliding-window | fail, 40 steps | pass, 12 | pass, 15 | pass, 17 | pass, 8 |
| more-itertools-tail | pass, 9 | pass, 8 | pass, 8 (judged on retry) | pass, 12 | pass, 6 |
| tabulate-padding | pass, 24 | fail, 40 | fail, 40 | fail, 35 | pass, 25 |
| humanize-naturalsize | pass, 13 | pass, 7 | pass, 11 | pass, 10 | pass, 12 |
| jmespath-prefix-suffix | pass, 10 | pass, 7 | pass, 7 | pass, 6 | pass, 5 |
| tokens for the run | 1.47M | 1.26M | 1.05M | 1.12M | 0.63M |

What the runs taught the harness, in order: exact-match editing rejected a one-space indentation difference and cost a whole run, so edits now tolerate whitespace and show the closest region on a miss (run 2); replies without a tool call no longer consume the step budget, invalid regexes fall back to literal search, and old tool output is compacted sooner (run 3); there is an `outline` tool for large files and repeated identical calls are caught even when interleaved (run 4); and every run now starts with a repository map of definitions and line numbers, ranked toward the files the task mentions, while compaction keeps a short summary of old tool output instead of dropping it (run 5). The fifth task, a padding bug in a 3,000-line module, failed three times in a row on navigation and passed in run 5 at half the cost of its only earlier pass.

The benchmark then grew to ten tasks, adding click, jinja, packaging, pyparsing, and tomli, which are larger codebases of 1,100 to 35,000 lines:

| task | run 6 | run 7 | run 8 | run 9 | run 10 |
|---|---|---|---|---|
| toolz-sliding-window | pass, 16 steps | pass, 10 steps | pass, 12 steps | pass, 12 steps | pass, 20 steps |
| more-itertools-tail | pass, 8 steps | pass, 10 steps | pass, 9 steps | pass, 7 steps | pass, 8 steps |
| tabulate-padding | fail, 33 steps | pass, 35 steps | fail, 40 steps | pass, 25 steps | fail, 40 steps |
| humanize-naturalsize | pass, 5 steps | fail, 40 steps | pass, 8 steps | pass, 7 steps | pass, 11 steps |
| jmespath-prefix-suffix | pass, 5 steps | pass, 6 steps | pass, 6 steps | pass, 6 steps | pass, 7 steps |
| click-intrange-clamp | pass, 13 steps | pass, 12 steps | pass, 9 steps | pass, 9 steps | pass, 10 steps |
| jinja-batch-filter | pass, 10 steps | pass, 22 steps | pass, 8 steps | pass, 8 steps | pass, 7 steps |
| packaging-canonicalize | pass, 8 steps | pass, 7 steps | pass, 7 steps | pass, 8 steps | pass, 9 steps |
| pyparsing-lineno | fail, - steps | pass, 8 steps | pass, 13 steps | pass, 9 steps | pass, 8 steps |
| tomli-unicode-escape | fail, 7 steps | pass, 7 steps | pass, 5 steps | pass, 7 steps | pass, 8 steps |
| passed | 7/10 | 9/10 | 9/10 | 10/10 | 9/10 |
| tokens for the run | 1.18M | 2.22M | 1.45M | 1.05M | 1.52M |

Run 6's misses: tabulate again, a 14 MB upload that hit the SDK's ten-second request timeout (fixed: 180 s), and a correct tomli fix whose diff extraction hit a transient API error (fixed: retries; the recovered diff passes). Run 7's single miss was a bug in the whitespace-tolerant editor that re-indented an inserted line by the wrong offset and broke the module (fixed, with a regression test). Run 8, on the fixed editor, missed only tabulate. Run 9 added ideas taken from DeepSeek Harness, full command output saved to a file with the tail returned, a read-before-edit guard, a todo tool, and a step counter on every result, and passed all ten, though tabulate has flipped before and stands at four passes in nine attempts with Nemotron 3 Super. Run 10, after the changes made for SWE-bench (file protection only for existing files, staleness by content, a 16k completion cap), passed nine; tabulate flipped back to a miss at 40 steps with no diff, and the nine passes together cost 0.89M tokens.

One attempt of the tabulate task with Nemotron 3 Ultra, before the repository map existed, passed in 35 steps and 836k tokens. `--investigate-model` routes the first investigation turns to one model and the rest to another; the effect is not yet measured.

These are synthetic bugs chosen by us. The numbers are a check on the harness, not a score comparable to SWE-bench.

### Sampling and effort

NVIDIA's guidance for Nemotron 3 Super is temperature 1.0 and top_p 0.95 for every task; runs 1 to 10 used 0.2. The model also reasons before every reply by default, and Token Factory honours the flags that turn reasoning off or shorten it (its reasoning budget is accepted and ignored, so there is no medium level). The suite was run once at the recommended sampling settings for each effort level, against run 10 at the old settings:

| task | run 10 (0.2, effort high) | 1.0/0.95, effort high | 1.0/0.95, effort low | 1.0/0.95, effort off |
|---|---|---|---|---|
| toolz-sliding-window | pass, 20 steps | pass, 13 steps | pass, 8 steps | pass, 14 steps |
| more-itertools-tail | pass, 8 steps | pass, 9 steps | pass, 7 steps | pass, 8 steps |
| tabulate-padding | fail, 40 steps | fail, 40 steps | fail, 40 steps | pass, 21 steps |
| humanize-naturalsize | pass, 11 steps | pass, 6 steps | pass, 5 steps | pass, 10 steps |
| jmespath-prefix-suffix | pass, 7 steps | pass, 5 steps | pass, 5 steps | pass, 10 steps |
| click-intrange-clamp | pass, 10 steps | pass, 13 steps | pass, 8 steps | pass, 14 steps |
| jinja-batch-filter | pass, 7 steps | pass, 8 steps | pass, 5 steps | pass, 9 steps |
| packaging-canonicalize | pass, 9 steps | pass, 7 steps | pass, 7 steps | pass, 9 steps |
| pyparsing-lineno | pass, 8 steps | pass, 22 steps | pass, 10 steps | pass, 28 steps |
| tomli-unicode-escape | pass, 8 steps | pass, 8 steps | pass, 5 steps | pass, 6 steps |
| passed | 9/10 | 9/10 | 9/10 | 10/10 |
| tokens for the run | 1.52M | 1.84M | 1.33M | 1.52M |
| tokens for the passes | 0.89M | 0.98M | 0.52M | 1.52M |

At the recommended sampling settings every level matched or beat run 10, and the tabulate miss is the same task as always: it took 40 steps and about 0.8M tokens at both high and low, and passed at off in 21. Effort off's jinja run first died on a 504 from the sandbox gateway before the agent's first step (which is why workspace creation now retries); the re-run passed in 9 steps and is what the table shows. Low is the cheapest by a wide margin, five to ten steps per pass, and off solved everything but spent up to 28 steps on trial and error where reasoning takes eight. One run each, so the levels are within a task of each other; the cost difference is not.

## SWE-bench Verified subset

`scripts/swe_bench.py` runs the agent on SWE-bench Verified tasks. SWE-bench publishes one environment image per task with the repository at the issue's base commit and its dependencies installed; the sandbox imports those images directly, so no per-repository setup is needed. The agent sees only the issue text, works in a fork of the task image, and its diff is graded in a fresh fork with the dataset's own evaluation script and the official `swebench` harness, hidden tests included.

```bash
uv run python scripts/swe_bench.py list --difficulty "<15 min fix" --per-repo 2 --n 12
uv run python scripts/swe_bench.py run --ids <comma-separated instance ids> --max-steps 50 --parallel 2
```

Twelve tasks rated "under 15 minutes", at most two per repository, chosen with a fixed seed, Nemotron 3 Super, 50-step budget:

| instance | repo | resolved | steps | tokens |
|---|---|---|---|---|
| django__django-11880 | django/django | yes | 21 | 261k |
| django__django-16899 | django/django | yes | 20 | 211k |
| matplotlib__matplotlib-25122 | matplotlib/matplotlib | yes | 50 | 753k |
| psf__requests-1142 | psf/requests | yes | 40 | 554k |
| psf__requests-5414 | psf/requests | yes | 36 | 651k |
| pydata__xarray-4075 | pydata/xarray | yes | 20 | 312k |
| pytest-dev__pytest-5809 | pytest-dev/pytest | yes | 13 | 190k |
| scikit-learn__scikit-learn-12585 | scikit-learn/scikit-learn | yes | 22 | 313k |
| matplotlib__matplotlib-23476 | matplotlib/matplotlib | no | 48 | 1274k |
| sphinx-doc__sphinx-8621 | sphinx-doc/sphinx | no (no diff) | 12 | 166k |
| sympy__sympy-15017 | sympy/sympy | no | 37 | 799k |
| sympy__sympy-20916 | sympy/sympy | no | 28 | 633k |
| **8 of 12 resolved** | | | | 6.1M total |

Before the subset, one pipeline trial on requests-2317 ran on an earlier harness and failed: the model made the correct edit three times but kept restoring to the baseline after seeing unrelated, network-dependent test failures. That trial led to the restore cap and the pre-existing-failure notes.

The four failures were then traced. Two were the harness: leftover reproduction scripts had gone into the diff, and a test file had been edited despite the instruction not to; both are now prevented (scratch files live outside the repository and are excluded from the diff, and test paths can be protected). A third was the harness stalling a run after two blank replies; blank replies are now retried and `finish` is forced only after three stalls. Retried on the fixed harness, sympy-15017 and sphinx-8621 resolved. sympy-20916 produced the same one-line fix as the gold patch except `[0-9]` where the hidden test needs `\d`, and matplotlib-23476 was diagnosed in the wrong file both times; those two are the model's.

The same twelve tasks were then run twice more from scratch, with no retries, to measure the harness rather than the best of several attempts:

| instance | first pass | clean run, round-10 harness | clean run, round-11 harness | clean run, round-12 harness |
|---|---|---|---|---|
| django__django-11880 | yes, 21 | yes, 46 | yes, 22 | yes, 40 |
| django__django-16899 | yes, 20 | yes, 50 | yes, 46 | no (no diff), 31 |
| matplotlib__matplotlib-23476 | no, 48 | no, 50 | no, 50 | no, 50 |
| matplotlib__matplotlib-25122 | yes, 50 | yes, 39 | yes, 49 | yes, 47 |
| psf__requests-1142 | yes, 40 | yes, 50 | no, 50 | yes, 50 |
| psf__requests-5414 | yes, 36 | no, 50 | no, 50 (no diff) | no, 50 |
| pydata__xarray-4075 | yes, 20 | yes, 22 | yes, 18 | yes, 22 |
| pytest-dev__pytest-5809 | yes, 13 | yes, 45 | no, 50 | yes, 16 |
| scikit-learn__scikit-learn-12585 | yes, 22 | yes, 20 | yes, 24 | yes, 22 |
| sphinx-doc__sphinx-8621 | no (no diff), 12 | no, 50 | yes, 24 | yes, 50 |
| sympy__sympy-15017 | no, 37 | yes, 38 | no, 50 (no diff) | yes, 50 |
| sympy__sympy-20916 | no, 28 | no, 50 | no, 50 (no diff) | no (no diff), 50 |
| **resolved** | **8 of 12** | **8 of 12** | **6 of 12** | **8 of 12** |
| tokens | 6.1M | 10.0M | 10.1M | 10.3M |

The round-11 changes (files protected only when they already exist, staleness checked by content, a 16k completion cap, a nudge on cut-off replies) did not move the score, and the two-task drop is within the swing of tasks that sit at the edge of the 50-step budget: every miss in both clean runs used all 50 steps. Reading the logs of the round-11 misses shows one harness problem, not a model one. In sympy-15017 the model had the correct two-line fix in place by step 20, was refused twice when it tried to edit a protected test file, then called `restore` back to checkpoint 1 and lost its own work; requests-5414 did the same with a `restore` to the baseline at step 48. Both ended with no diff. sympy-20916 spent its 50 steps on 17 searches and 10 scratch scripts and never edited a repository file. Round 12 made those two changes: a `restore` that would discard every repository edit is refused, and in the last fifth of the budget every tool result says to make the edit or finish. Run again, the same twelve tasks came back to 8 of 12. The refusal fired once, on requests-5414, and the edit it saved was submitted but wrong; sympy-15017 passed without ever calling `restore`. The nudge changed nothing on sympy-20916, which ignored it ten times and kept writing scratch scripts. And django-16899, resolved in both earlier clean runs, ended with no diff after 31 steps: the model made the correct fix, was refused on the protected test file, then reverted its own change so the old assertions would pass. The protection message now says to keep the fix when an existing test asserts the old behaviour. That is the harness as frozen for the larger sample below.

With the harness frozen at that point, every task in the "under 15 minutes" tier at up to three per repository, 31 in all, was run once, three at a time, with no retries:

| instance | repo | resolved | steps | tokens |
|---|---|---|---|---|
| astropy__astropy-14309 | astropy/astropy | yes | 17 | 327k |
| astropy__astropy-14995 | astropy/astropy | yes | 19 | 285k |
| astropy__astropy-7166 | astropy/astropy | yes | 29 | 329k |
| django__django-11880 | django/django | yes | 43 | 956k |
| django__django-16899 | django/django | yes | 38 | 944k |
| matplotlib__matplotlib-25122 | matplotlib/matplotlib | yes | 50 | 1021k |
| pallets__flask-5014 | pallets/flask | yes | 15 | 266k |
| psf__requests-1142 | psf/requests | yes | 50 | 1084k |
| psf__requests-2317 | psf/requests | yes | 50 | 974k |
| pydata__xarray-4075 | pydata/xarray | yes | 22 | 215k |
| pydata__xarray-4094 | pydata/xarray | yes | 46 | 1502k |
| pydata__xarray-4629 | pydata/xarray | yes | 14 | 147k |
| pylint-dev__pylint-6903 | pylint-dev/pylint | yes | 50 | 1486k |
| pylint-dev__pylint-7277 | pylint-dev/pylint | yes | 19 | 286k |
| pytest-dev__pytest-5262 | pytest-dev/pytest | yes | 33 | 545k |
| pytest-dev__pytest-5809 | pytest-dev/pytest | yes | 14 | 168k |
| pytest-dev__pytest-7521 | pytest-dev/pytest | yes | 23 | 342k |
| scikit-learn__scikit-learn-12585 | scikit-learn/scikit-learn | yes | 19 | 210k |
| scikit-learn__scikit-learn-13328 | scikit-learn/scikit-learn | yes | 17 | 237k |
| scikit-learn__scikit-learn-25232 | scikit-learn/scikit-learn | yes | 31 | 542k |
| sphinx-doc__sphinx-8621 | sphinx-doc/sphinx | yes | 26 | 398k |
| sympy__sympy-15017 | sympy/sympy | yes | 19 | 325k |
| django__django-12193 | django/django | no | 32 | 544k |
| matplotlib__matplotlib-20676 | matplotlib/matplotlib | no | 20 | 331k |
| matplotlib__matplotlib-23476 | matplotlib/matplotlib | no | 50 | 1595k |
| psf__requests-5414 | psf/requests | no (no diff) | 50 | 813k |
| pylint-dev__pylint-4970 | pylint-dev/pylint | no | 50 | 1119k |
| sphinx-doc__sphinx-9258 | sphinx-doc/sphinx | no | 41 | 861k |
| sphinx-doc__sphinx-9591 | sphinx-doc/sphinx | no | 18 | 264k |
| sympy__sympy-17655 | sympy/sympy | no (no diff) | 50 | 943k |
| sympy__sympy-20916 | sympy/sympy | no (no diff) | 50 | 1124k |
| **22 of 31 resolved** | | | | 20.2M total |

The original twelve went 9 of 12 inside this run, one better than the re-run because django-16899 kept its fix this time; the nineteen new tasks went 13 of 19. Two evaluations, pylint-7277 and astropy-14309, failed on a transient error from the sandbox API after the agent had finished and its patch was saved; they were graded again from the saved patch without running the agent again, and both resolved.

The nine misses, from their logs, fall into three groups. Four finished early and confident with a plausible fix in the right file that the hidden tests reject (django-12193, matplotlib-20676, sphinx-9591, sphinx-9258): one edit each, tests run, a clear summary, wrong. Three ended with no diff: sympy-20916 never localised the bug in five attempts now, while sympy-17655 and requests-5414 made an edit, judged it wrong, and undid it with `edit_file`; the `restore` refusal fired twice on requests-5414 and held, but nothing stops the model from editing its own change back out, and nothing should. Two used the whole budget on a wrong diff (matplotlib-23476, pylint-4970). None of the nine lost work to the harness.

Thirty-one tasks is still a small sample and these are the easier tier of Verified, so this is not comparable to leaderboard numbers on the full 500. It is a genuine measurement on real GitHub issues with hidden tests, run once, with the harness frozen before the run.

## Development

```bash
uv run pytest
```

## License

MIT
