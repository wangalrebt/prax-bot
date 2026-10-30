# Install and test Prax Bot

Use the [public repository](https://github.com/wangalrebt/prax-bot) or the [v0.1.2 release](https://github.com/wangalrebt/prax-bot/releases/tag/v0.1.2). Its [complete repository-source ZIP](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.2/prax-bot-submission-source.zip) includes the demo script, tests, lockfile, docs, and benchmark reports; the release also provides a wheel, source distribution, and checksums.

Version 0.1.2 switches the default to `nvidia/Nemotron-3.5-Super-VL` because the previous Serverless model retires November 9, 2026. Use v0.1.2 for the updated workflow below. The preserved v0.1.1 downloads still select the retiring model, and their demo script hard-codes it. The existing video and earlier live validation used the previous Nemotron 3 Super model.

## Requirements and access

Use macOS or Linux, Python 3.12+, Git, and uv. Windows requires a Linux environment such as WSL; the runtime uses Unix file locks and does not support native Windows. Running the agent requires a Nebius Token Factory API key and a project with sandbox access. Set `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` through your shell, or use the ConTree SDK's active `auth.ini` profile as described in the [README](../README.md#requirements).

Do not put credentials in the repository or recorded video. The submitter still needs to arrange usable, free judging access to Token Factory and Sandboxes. These local installation instructions and the public test build do not by themselves establish that provider access. Prax is a localhost developer application; this package does not deploy a public multi-user service.

## Source installation

From the extracted v0.1.2 repository root:

```bash
uv sync --locked
uv run prax --help
uv run prax doctor
```

`doctor` calls the model and sandbox and reports connectivity and tool-call results. A sandbox 403 can indicate missing per-project access even when the API key works for inference.

Alternatively, install the v0.1.2 wheel into a Python 3.12 virtual environment:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python ./prax_bot-0.1.2-py3-none-any.whl
.venv/bin/prax --help
```

Use the complete source ZIP for the demo script below; the wheel installs the application without the repository scripts and tests.

## Reproduce the sandbox workflow

1. Run `uv run python scripts/submission_demo.py`. It creates a new disposable Git repository and prints its location and launch command. Run that printed command. It uses the configured model (default `nvidia/Nemotron-3.5-Super-VL`), the Contree backend, a 12-step budget, and standard-library unit tests. The test file is protected from model edits. To choose another model, export `PRAXBOT_MODEL` before running the printed launch command; the script no longer hard-codes a model flag. Its fresh `PRAXBOT_HOME` isolates the demo from your saved user configuration.

   The launch command uses `--effort low`. For Nemotron 3.5 Super VL this requests `enable_thinking=true, medium_effort=true`, NVIDIA's documented shorter reasoning mode, instead of the older `low_effort` flag. See the [model migration notes](../README.md#model-migration-in-v012) and [effort settings](../README.md#use). Live tool compatibility and a local-backend coding fixture passed; the full sandbox/browser workflow below has not been rerun with the replacement.
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

The model migration passed **393 automated tests**, lint, formatting, type checking, JavaScript syntax validation, and source/wheel builds. The [offline migration validation record](validation/model-migration-20261030.json) preserves these checks, including a mocked SDK tool round trip.

The separate [live migration validation](validation/live-model-migration-20261030.json) made **10 actual API calls** with SDK retries disabled: six compatibility calls covered automatic and forced tool calls at `low` effort, assistant/tool-result follow-ups, and no-tool replies at `off` and `high`; four calls ran the real Prax agent on a disposable **local-backend** fixture. The agent read, edited, ran tests, and finished. Independent checks failed on the baseline and passed on the patch. The host stayed unchanged before explicit Apply; afterward only `calc.py` changed, host tests passed, and `multiply` remained untouched. Usage was 15,436 input and 621 output tokens, with an estimated inference cost of **US$0.00519**. The verification harness needed two assertion corrections; verification then used the same saved run without another model attempt or product change. This did not rerun the paid sandbox or full browser workflow with the replacement.

The v0.1.1 application test run passed 383 tests, with lint, formatting, type, and JavaScript syntax checks passing. That is the pre-migration result; it does not establish replacement-model compatibility. The [browser validation](validation/browser-20261030.json) records the completed workflow and visual review: Main delegation without edits, independent baseline/patch checks, two passing candidates, selection without host changes, explicit Apply with a passing host test, and rename/close/reopen recovery. The successful attempt used 19 calls and 73,145 tokens; all three attempts, including two unsuccessful coordination attempts, used 49 calls and 231,692 tokens. The [2:42 YouTube demo](https://youtu.be/Ci8Vz0iyZGk) is public with English captions; an [MP4 download](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.mp4) and [video checks](validation/video-20261030.json) are also available.

## Evidence and evaluation scope

- [README](../README.md): implementation, behavior, limitations, and benchmark history.
- [Live model migration](validation/live-model-migration-20261030.json): replacement-model API compatibility and the local-backend coding fixture, including usage and verification-harness corrections.
- [Browser validation](validation/browser-20261030.json): all three recording attempts, including the no-edit guard failure, post-fix polling failure, and successful explicitly waiting prompt; comparison, Apply, reopen, usage, and visual-review evidence.
- [Earlier integration validation](validation/live-20261029.json): the separate 29-call sandbox exercise, including selected patches/check output and recovery/cancellation assertions.
- [Historical comparison correction](../bench/swebench/20260914-published-comparison.md): corrected token accounting and limits of the 31-task development sample.
- [Six-task exploratory comparison](../bench/swebench/20261025-reference-comparison.md): Prax resolved 2/6 and mini-SWE-agent 3/6; this is not a full benchmark or general ranking.
