"""Run Prax Bot on SWE-bench Verified tasks and grade with the official harness.

SWE-bench publishes one environment image per task with the repository at the base commit
and its dependencies installed. The sandbox imports those images directly, so no
per-repository setup is needed. Each task runs the agent in a fork of that image, exports a
diff, and grades it in a fresh fork with the dataset's own evaluation script and the
official log parsers.

    uv run --group bench python scripts/swe_bench.py list [--difficulty "<15 min fix"] [--per-repo 2] [--n 20] [--seed 0]
    uv run --group bench python scripts/swe_bench.py run --ids psf__requests-2317,pallets__flask-4045 [--max-steps 50] [--parallel 2]
    uv run --group bench python scripts/swe_bench.py reeval --ids ...      # grade saved patches again

Results go to bench/swebench/<stamp>.{json,md}; per-task logs and patches under bench/swebench/scratch/.
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prax_bot.core.agent import Agent  # noqa: E402
from prax_bot.core.llm import LLM  # noqa: E402
from prax_bot.core.tools import ToolBox  # noqa: E402
from prax_bot.settings import Settings  # noqa: E402
from prax_bot.workspace.repository import DIFF_OUTPUT_LIMIT, git_exclude_command  # noqa: E402
from prax_bot.workspace.sandbox import SandboxWorkspace as Workspace  # noqa: E402
from prax_bot.workspace.sandbox import make_client  # noqa: E402

TESTBED = "/testbed"
ENV_PREFIX = "export HOME=${HOME:-/root}; "
ACTIVATE = ENV_PREFIX + "source /opt/miniconda3/bin/activate testbed >/dev/null 2>&1"
DATASET = "SWE-bench/SWE-bench_Verified"


def require_bench_packages() -> None:
    """The dataset and grading packages live in the `bench` dependency group, not in a plain `uv sync`."""
    try:
        import pandas  # noqa: F401
        import swebench  # noqa: F401
    except ImportError as e:
        sys.exit(
            f"{e.name} is not installed: run this script with `uv run --group bench python scripts/swe_bench.py ...`"
        )


def load_dataset(cache: Path):
    import pandas as pd

    if cache.exists():
        return pd.read_pickle(cache)
    import pyarrow.parquet as pq
    from huggingface_hub import HfApi, hf_hub_download

    files = [f for f in HfApi().list_repo_files(DATASET, repo_type="dataset") if f.endswith(".parquet")]
    path = hf_hub_download(DATASET, repo_type="dataset", filename=files[0])
    table = pq.read_table(path).to_pandas()
    cache.parent.mkdir(parents=True, exist_ok=True)
    table.to_pickle(cache)
    return table


class TestbedWorkspace(Workspace):
    """The SWE-bench image is the workspace: every command runs inside the conda env at /testbed."""

    def run(self, command: str, *, timeout=None, mutate=True, truncate_output_at=None):
        wrapped = f"bash -c {shlex.quote(f'{ACTIVATE}; cd {TESTBED}; ' + command)}"
        return super().run(wrapped, timeout=timeout, mutate=mutate, truncate_output_at=truncate_output_at)


def task_text(row) -> str:
    return (
        f"Fix the following issue in the repository at {TESTBED}, which is the working directory.\n\n"
        f"Repository: {row['repo']}\n\n{row['problem_statement'].strip()}\n\n"
        "Reproduce the problem first if you can, fix the library code, and verify with the project's tests "
        "(most projects use `python -m pytest <path>`; Django uses `python tests/runtests.py <module>`). "
        "Run the relevant tests once before editing: some tests in this environment fail for reasons unrelated "
        "to the issue, such as network access, and those are not caused by your change. Do not modify existing "
        "tests. Call finish when the fix is verified."
    )


def prepare_baseline(client, row, settings: Settings):
    image = client.images.oci(f"docker://docker.io/{row['image']}", timeout=settings.import_timeout)
    baseline = image.run(
        shell=f"{ENV_PREFIX}cd {TESTBED} && git config --global --add safe.directory {TESTBED} && {git_exclude_command()} && git status --short | head -5",
        disposable=False,
        timeout=120,
    ).wait()
    if baseline.exit_code != 0:
        raise RuntimeError(f"baseline setup failed: {str(baseline.stderr or '')[-300:]}")
    return image, baseline


NETWORK_ERROR = "Network is unreachable"
GRADING_ATTEMPTS = 3


def grade(image, row, diff: str, scratch: Path, timeout: int = 1800) -> dict:
    """Apply the diff in a fresh fork of the task image and run the dataset's evaluation script.

    Some sandbox hosts have no network route, and a few regression tests (the connect-timeout tests in
    requests, for example) then fail with "Network is unreachable" instead of timing out. A grading that
    is not resolved and shows that error is run again, up to GRADING_ATTEMPTS times in all.
    """
    for attempt in range(1, GRADING_ATTEMPTS + 1):
        verdict = _grade_once(image, row, diff, scratch, timeout)
        network_error = verdict.pop("network_error", False)
        verdict["grading_attempts"] = attempt
        if verdict["resolved"] or not network_error:
            return verdict
        if attempt < GRADING_ATTEMPTS:
            print(
                f"   {row['instance_id']}: a test failed on network access in a sandbox without a route; "
                f"grading again ({attempt + 1} of {GRADING_ATTEMPTS})",
                flush=True,
            )
    verdict["eval"] = "not resolved; a test failed on network access in every grading"
    return verdict


def _grade_once(image, row, diff: str, scratch: Path, timeout: int) -> dict:
    """One grading run: apply the patch, run the evaluation script, parse the report."""
    from swebench.harness.grading import get_eval_report
    from swebench.harness.utils import make_test_spec

    spec = make_test_spec(row)
    script = (
        f"{ENV_PREFIX}cd {TESTBED} && git config --global --add safe.directory {TESTBED} && "
        "(git apply -v /tmp/model.patch || patch --batch --fuzz=5 -p1 -i /tmp/model.patch) && "
        "echo PRAXBOT_PATCH_APPLIED && bash /tmp/eval.sh"
    )
    from prax_bot.workspace.sandbox import with_retries

    # the sandbox API drops a connection now and then; a grading run is expensive to lose to that
    result = with_retries(
        lambda: image.run(
            shell=script,
            files={"/tmp/model.patch": diff.encode(), "/tmp/eval.sh": spec.eval_script.encode()},
            disposable=True,
            timeout=timeout,
            truncate_output_at=DIFF_OUTPUT_LIMIT,
        ).wait(),
        label="grading",
        emit=lambda kind, **payload: print(f"   retrying grading: {payload.get('error')}", flush=True),
    )
    log = str(result.stdout or "") + "\n" + str(result.stderr or "")
    log_path = scratch / "eval.log"
    log_path.write_text(log)
    applied = "PRAXBOT_PATCH_APPLIED" in log
    if not applied:
        return {
            "resolved": False,
            "patch_applied": False,
            "eval": "patch did not apply",
            "eval_cost": result.result.cost,
        }
    prediction = {"instance_id": row["instance_id"], "model_name_or_path": "praxbot", "model_patch": diff}
    report = get_eval_report(spec, prediction, str(log_path), include_tests_status=True)[row["instance_id"]]
    status = report.get("tests_status", {})
    f2p = status.get("FAIL_TO_PASS", {})
    p2p = status.get("PASS_TO_PASS", {})
    return {
        "resolved": bool(report.get("resolved")),
        "patch_applied": True,
        "f2p": f"{len(f2p.get('success', []))}/{len(f2p.get('success', [])) + len(f2p.get('failure', []))}",
        "p2p": f"{len(p2p.get('success', []))}/{len(p2p.get('success', [])) + len(p2p.get('failure', []))}",
        "eval": "resolved" if report.get("resolved") else "not resolved",
        "eval_cost": result.result.cost,
        "network_error": NETWORK_ERROR in log,
    }


def run_instance(row, args, scratch: Path) -> dict:
    iid = row["instance_id"]
    out = scratch / iid
    out.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env(investigate_model=args.investigate_model, max_steps=args.max_steps)
    events = (out / "events.jsonl").open("w", encoding="utf-8")

    def on_event(kind, **payload):
        rec = {"t": time.time(), "kind": kind}
        for k, v in payload.items():
            rec[k] = v if isinstance(v, (str, int, float, bool, type(None), dict, list)) else str(v)
        events.write(json.dumps(rec, default=str) + "\n")
        events.flush()

    started = time.monotonic()
    result = {"instance_id": iid, "repo": row["repo"], "difficulty": row["difficulty"]}
    try:
        client = make_client()
        image, baseline = prepare_baseline(client, row, settings)
        ws = TestbedWorkspace(
            client, baseline, workdir=TESTBED, command_timeout=settings.command_timeout, on_event=on_event
        )
        llm = LLM(settings)
        protected = (
            "tests/*",
            "*/tests/*",
            "*/test_*.py",
            "test_*.py",
            "*_test.py",
            "*/conftest.py",
            "conftest.py",
            "testing/*",
            "*/testing/*",
        )
        agent = Agent(
            llm,
            ToolBox(ws, protected=protected),
            max_steps=settings.max_steps,
            on_event=on_event,
            investigate_model=settings.investigate_model,
            investigate_steps=settings.investigate_steps,
            context_budget=settings.context_budget,
        )
        agent_result = agent.run(task_text(row))
        # Keep model usage even if exporting a patch fails (e.g. non-UTF-8 generated files).
        result.update(
            {
                "finished": agent_result.finished,
                "reason": agent_result.reason,
                "steps": agent_result.steps,
                "tokens": llm.usage.total_tokens,
                "usage_by_model": llm.usage.by_model,
                "summary": agent_result.summary[:300],
            }
        )
        try:
            diff = ws.diff()
            (out / "patch.diff").write_text(diff)
        finally:
            result.update(
                sandbox_commands=ws.commands, sandbox_cost=ws.total_cost, agent_elapsed=time.monotonic() - started
            )
        result["diff_lines"] = len(diff.splitlines())
        if not diff.strip():
            result.update({"resolved": False, "patch_applied": False, "eval": "no diff produced"})
        else:
            result.update(grade(image, row, diff, out))
    except Exception as e:
        result.update({"resolved": False, "eval": f"error: {type(e).__name__}: {str(e)[:200]}"})
    finally:
        events.close()
    result["wall"] = time.monotonic() - started
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def reeval_instance(row, _args, scratch: Path) -> dict:  # same shape as run_instance; nothing to configure
    iid = row["instance_id"]
    out = scratch / iid
    patch = out / "patch.diff"
    result = {"instance_id": iid, "repo": row["repo"], "difficulty": row["difficulty"]}
    prev = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else {}
    # An empty submission still consumed agent work. Keep recorded metadata, including
    # genuine zeros, without inventing usage or carrying a previous grading verdict.
    result.update(
        {
            k: prev[k]
            for k in (
                "finished",
                "reason",
                "exit_status",
                "steps",
                "tokens",
                "usage_by_model",
                "summary",
                "diff_lines",
                "agent_elapsed",
                "sandbox_commands",
                "sandbox_cost",
            )
            if k in prev
        }
    )
    if not patch.exists() or not patch.read_text().strip():
        return result | {"resolved": False, "patch_applied": False, "eval": "no saved patch"}
    try:
        client = make_client()
        settings = Settings.from_env()
        image = client.images.oci(f"docker://docker.io/{row['image']}", timeout=settings.import_timeout)
        result.update(grade(image, row, patch.read_text(), out))
    except Exception as e:
        result.update({"resolved": False, "eval": f"error: {type(e).__name__}: {str(e)[:200]}"})
    return result


def stage_predictions(preds_path: Path, scratch: Path, traj_dir: Path | None) -> list[str]:
    """Write another harness's predictions into the layout reeval grades: scratch/<id>/patch.diff (+ result.json).

    preds_path is the SWE-bench predictions format, {instance_id: {"model_patch": ...}} or a list of such
    records. With traj_dir (mini-SWE-agent's output directory), the model call count is taken from each
    trajectory so the comparison table can show steps.
    """
    data = json.loads(preds_path.read_text())
    records = list(data.values()) if isinstance(data, dict) else data
    ids: list[str] = []
    for rec in records:
        iid = rec["instance_id"]
        out = scratch / iid
        out.mkdir(parents=True, exist_ok=True)
        (out / "patch.diff").write_text(rec.get("model_patch") or "")
        info: dict = {}
        traj = traj_dir / iid / f"{iid}.traj.json" if traj_dir else None
        if traj and traj.exists():
            t = json.loads(traj.read_text())
            turns = [m for m in t.get("messages", []) if m.get("role") == "assistant"]
            calls = t.get("info", {}).get("model_stats", {}).get("api_calls") or len(turns)
            tokens = 0
            for m in turns:
                response = (m.get("extra") or {}).get("response") or {}
                usage = response.get("usage") if isinstance(response, dict) else None
                tokens += int((usage or {}).get("total_tokens") or 0)
            info = {"steps": calls, "tokens": tokens, "exit_status": t.get("info", {}).get("exit_status")}
        (out / "result.json").write_text(json.dumps(info))
        ids.append(iid)
    return ids


def markdown(results: list[dict], args) -> str:
    rows = [
        "| instance | repo | difficulty | resolved | F2P | P2P | steps | tokens | evaluation |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        steps = r.get("steps")
        tokens = r.get("tokens")
        rows.append(
            f"| {r['instance_id']} | {r['repo']} | {r.get('difficulty', '')} | {'yes' if r.get('resolved') else 'no'} | "
            f"{r.get('f2p', '-')} | {r.get('p2p', '-')} | {steps if steps is not None else '-'} | "
            f"{f'{tokens:,}' if tokens is not None else '-'} | {r.get('eval', '')} |"
        )
    n, ok = len(results), sum(1 for r in results if r.get("resolved"))
    head = (
        f"SWE-bench Verified subset: {ok} of {n} resolved. Model {Settings.from_env().model}"
        + (f", investigation model {args.investigate_model}" if getattr(args, "investigate_model", None) else "")
        + (f", graded from {args.preds}" if getattr(args, "preds", None) else "")
        + f", max steps {getattr(args, 'max_steps', '-')}. Graded with the official swebench harness in a fresh sandbox per task."
    )
    return head + "\n\n" + "\n".join(rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--difficulty", default="<15 min fix")
    ls.add_argument("--per-repo", type=int, default=2)
    ls.add_argument("--n", type=int, default=20)
    ls.add_argument("--seed", type=int, default=0)
    ls.add_argument("--exclude-repos", default="")
    for name in ("run", "reeval", "grade"):
        p = sub.add_parser(name)
        p.add_argument("--ids", required=name != "grade", default="", help="comma-separated instance ids")
        if name == "grade":
            p.add_argument("--preds", type=Path, required=True, help="another harness's predictions JSON")
            p.add_argument("--traj-dir", type=Path, default=None, help="mini-SWE-agent output dir, for step counts")
        p.add_argument("--max-steps", type=int, default=50)
        p.add_argument("--investigate-model", default=None)
        p.add_argument("--parallel", type=int, default=1)
        p.add_argument("--out", type=Path, default=ROOT / "bench" / "swebench")
    args = ap.parse_args()

    require_bench_packages()
    cache = ROOT / "bench" / "swebench" / "scratch" / "verified.pkl"
    table = load_dataset(cache)

    if args.cmd == "list":
        rng = random.Random(args.seed)
        excluded = {r for r in args.exclude_repos.split(",") if r}
        pool = table[table.difficulty == args.difficulty] if args.difficulty else table
        chosen: list[str] = []
        by_repo: dict[str, int] = {}
        ids = list(pool.instance_id)
        rng.shuffle(ids)
        for iid in ids:
            repo = pool[pool.instance_id == iid].iloc[0].repo
            if repo in excluded or by_repo.get(repo, 0) >= args.per_repo:
                continue
            chosen.append(iid)
            by_repo[repo] = by_repo.get(repo, 0) + 1
            if len(chosen) >= args.n:
                break
        print(",".join(chosen))
        for iid in chosen:
            r = pool[pool.instance_id == iid].iloc[0]
            print(f"  {iid:<40} {r.repo:<28} {r.difficulty}")
        return 0

    scratch = args.out / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    ids = [i for i in args.ids.split(",") if i]
    if args.cmd == "grade":
        staged = stage_predictions(args.preds, scratch, args.traj_dir)
        ids = [i for i in staged if i in ids] if ids else staged
    missing = [i for i in ids if i not in set(table.instance_id)]
    if missing:
        ap.error(f"unknown instance ids: {missing}")
    rows = [table[table.instance_id == i].iloc[0].to_dict() for i in ids]
    worker = run_instance if args.cmd == "run" else reeval_instance

    results: list[dict] = []
    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for r in pool.map(lambda row: worker(row, args, scratch), rows):
                results.append(r)
                print(
                    f"== {r['instance_id']}: resolved={r.get('resolved')} steps={r.get('steps')} tokens={r.get('tokens')} eval={r.get('eval')}",
                    flush=True,
                )
    else:
        for row in rows:
            print(f"== {row['instance_id']}", flush=True)
            r = worker(row, args, scratch)
            results.append(r)
            print(
                f"   resolved={r.get('resolved')} steps={r.get('steps')} tokens={r.get('tokens')} eval={r.get('eval')}",
                flush=True,
            )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (args.out / f"{stamp}-{args.cmd}.json").write_text(
        json.dumps({"args": {k: str(v) for k, v in vars(args).items()}, "results": results}, indent=2, default=str)
    )
    md = markdown(results, args)
    (args.out / f"{stamp}-{args.cmd}.md").write_text(md)
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
