# Install and test Prax Bot

Use the [public repository](https://github.com/wangalrebt/prax-bot) or the [complete repository-source ZIP](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-submission-source.zip). The [v0.1.1 release](https://github.com/wangalrebt/prax-bot/releases/tag/v0.1.1) also provides an installable Python wheel, source distribution, and checksums. The complete source ZIP includes the demo script, tests, lockfile, docs, and benchmark reports.

## Requirements and access

Use macOS or Linux, Python 3.12+, Git, and uv. Windows requires a Linux environment such as WSL; the runtime uses Unix file locks and does not support native Windows. Running the agent requires a Nebius Token Factory API key and a project with sandbox access. Set `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` through your shell, or use the ConTree SDK's active `auth.ini` profile as described in the [README](../README.md#requirements).

Do not put credentials in the repository or recorded video. The submitter still needs to arrange usable, free judging access to Token Factory and Sandboxes. These local installation instructions and the public test build do not by themselves establish that provider access. Prax is a localhost developer application; this package does not deploy a public multi-user service.

## Source installation

From the extracted repository root:

```bash
uv sync --locked
uv run prax --help
uv run prax doctor
```

`doctor` calls the model and sandbox and reports connectivity and tool-call results. A sandbox 403 can indicate missing per-project access even when the API key works for inference.

Alternatively, install the wheel from the published test build into a Python 3.12 virtual environment:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python ./prax_bot-0.1.1-py3-none-any.whl
.venv/bin/prax --help
```

The source route below is the simplest way to reproduce the demo.

## Reproduce the sandbox workflow

1. Run `uv run python scripts/submission_demo.py`. It creates a new disposable Git repository and prints its location and launch command. Run that printed command. It uses Nemotron 3 Super, the Contree backend, a 12-step budget, and standard-library unit tests. The test file is protected from model edits.
2. In Main, send:

   ```text
   Start exactly one work session named Arithmetic fix. Ask it to fix calc.add so it adds a and b, changing only calc.py and leaving multiply untouched. Run python -B -m unittest -v. Use session_read with wait_seconds=30 until the child is idle and its independent checks have finished. Do not send duplicate follow-ups or stop the child. Report its changed file and actual check results. Do not edit Main's workspace; this task is coordination only.
   ```

3. Open Arithmetic fix. Wait until its work and independent checks finish. Inspect the changed line, the failing baseline, and the passing patch check. Run the printed host check: it should still fail before Apply.
4. Create another work session named Compare fixes with no initial task. In Compare, use objective `Fix calc.add to add its arguments. Change only calc.py; preserve multiply and all tests.` Set approach A to `Use a + b` and approach B to `Use sum((a, b))`.
5. Wait for both candidates. Review their individual diffs and checks. Choose A. The host should still have no diff. Apply only `calc.py`, then run the printed host test command again: it should pass.
6. Rename and close Arithmetic fix, then reopen it. Its pending patch and prior check evidence should remain. Stop the server with Ctrl+C, relaunch the same printed command, and reopen the sessions. Names and saved evidence should return; no interrupted instruction should start automatically.

Model output and provider timing vary. If an attempt fails, preserve that result, inspect its logs, and describe a retry accurately. Passing this fixture validates the workflow; it does not measure broad coding ability. Check results cover the configured commands. Applying advances the baseline, so prior current-workspace checks become stale until verification runs again; candidate evidence remains available.

## Automated checks

Node.js is needed only for the JavaScript syntax command below; the application itself does not require Node.js.

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
node --check src/prax_bot/ui/assets/app.js
```

The completed application test run passed 383 tests, with lint, formatting, type, and JavaScript syntax checks passing. The [browser validation](validation/browser-20261030.json) records the completed workflow and visual review: Main delegation without edits, independent baseline/patch checks, two passing candidates, selection without host changes, explicit Apply with a passing host test, and rename/close/reopen recovery. The successful attempt used 19 calls and 73,145 tokens; all three attempts, including two unsuccessful coordination attempts, used 49 calls and 231,692 tokens. The final edited video and public upload remain pending.

## Evidence and evaluation scope

- [README](../README.md): implementation, behavior, limitations, and benchmark history.
- [Browser validation](validation/browser-20261030.json): all three recording attempts, including the no-edit guard failure, post-fix polling failure, and successful explicitly waiting prompt; comparison, Apply, reopen, usage, and visual-review evidence.
- [Earlier integration validation](validation/live-20261029.json): the separate 29-call sandbox exercise, including selected patches/check output and recovery/cancellation assertions.
- [Historical comparison correction](../bench/swebench/20260914-published-comparison.md): corrected token accounting and limits of the 31-task development sample.
- [Six-task exploratory comparison](../bench/swebench/20261025-reference-comparison.md): Prax resolved 2/6 and mini-SWE-agent 3/6; this is not a full benchmark or general ranking.
