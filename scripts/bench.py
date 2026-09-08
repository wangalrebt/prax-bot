"""Benchmark Prax Bot on seeded regressions in real repositories.

Each task in bench/tasks.json names a repository, a commit, a bug patch that makes some
tests fail, the setup command, the test command the agent is told about, and a broader
evaluation command. Success is judged independently: the agent's diff is applied to a
fresh copy of the bugged repository and the evaluation command is run, by default inside
a fresh Token Factory Sandbox.

    uv run python scripts/bench.py --src DIR --validate          # HEAD passes, bug fails (local venvs)
    uv run python scripts/bench.py --src DIR [--backend contree] [--eval contree|local] [--tasks a,b]

DIR holds one clone per repository, named as in tasks.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = json.loads((ROOT / "bench" / "tasks.json").read_text())


def git(*args: str, cwd: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=kw.pop("check", False), **kw)


def prepare_repo(task: dict, src: Path, work: Path, *, bugged: bool) -> Path:
    """Fresh clone at the task commit, optionally with the seeded bug committed on top."""
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(src / task["repo"]), str(work)], check=True, capture_output=True)
    git("checkout", "-q", task["commit"], cwd=work, check=True)
    if bugged:
        git("apply", str(ROOT / task["bug_patch"]), cwd=work, check=True)
        git(
            "-c",
            "user.email=bench@local",
            "-c",
            "user.name=bench",
            "commit",
            "-qam",
            "seed regression",
            cwd=work,
            check=True,
        )
    return work


def venv_env(venv: Path) -> dict[str, str]:
    return {
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "VIRTUAL_ENV": str(venv),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def ensure_venv(path: Path) -> Path:
    if not (path / "bin" / "python").exists():
        subprocess.run(["uv", "venv", "-q", "--seed", "--python", "3.12", str(path)], check=True)
    return path


def local_run(cmd: str, cwd: Path, venv: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", cmd], cwd=cwd, env=venv_env(venv), capture_output=True, text=True, timeout=timeout, check=False
    )


def validate(tasks: list[dict], src: Path, out: Path) -> int:
    """Check every task locally: tests pass at HEAD and fail with the bug applied."""
    bad = 0
    print(f"{'task':<26} {'head test':>9} {'head eval':>9} {'bug test':>8} {'bug eval':>8}")
    for t in tasks:
        work = prepare_repo(t, src, out / "validate" / t["name"], bugged=False)
        venv = ensure_venv(out / "venvs" / t["name"])
        setup = local_run(t["setup"], work, venv)
        if setup.returncode != 0:
            print(f"{t['name']:<26} setup failed: {setup.stderr.strip()[-200:]}")
            bad += 1
            continue
        head_test = local_run(t["test_cmd"], work, venv).returncode == 0
        head_eval = local_run(t["eval_cmd"], work, venv).returncode == 0
        git("apply", str(ROOT / t["bug_patch"]), cwd=work, check=True)
        bug_test = local_run(t["test_cmd"], work, venv).returncode == 0
        bug_eval = local_run(t["eval_cmd"], work, venv).returncode == 0
        ok = head_test and head_eval and not bug_test and not bug_eval
        bad += not ok
        mark = lambda b: "pass" if b else "fail"  # noqa: E731
        print(
            f"{t['name']:<26} {mark(head_test):>9} {mark(head_eval):>9} {mark(bug_test):>8} {mark(bug_eval):>8}  {'ok' if ok else 'PROBLEM'}"
        )
    return bad


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def last_line(output: str) -> str:
    lines = ANSI.sub("", output).strip().splitlines()
    return (lines[-1] if lines else "").strip("= ")[:200]


def touched_tests(diff: str) -> list[str]:
    paths = re.findall(r"^\+\+\+ b/(.*)$", diff, re.MULTILINE)
    return [p for p in paths if re.search(r"(^|/)tests?(/|_)|_test\.py$|(^|/)test_", p)]


def evaluate(task: dict, src: Path, diff: str, args: argparse.Namespace, out: Path) -> dict:
    work = prepare_repo(task, src, out / "eval" / task["name"], bugged=True)
    check = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=work, input=diff, capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        return {"passed": False, "eval": f"diff does not apply: {check.stderr.strip()[:200]}"}
    subprocess.run(["git", "apply", "-"], cwd=work, input=diff, check=True, capture_output=True, text=True)

    if args.eval == "local":
        venv = ensure_venv(out / "venvs" / task["name"])
        setup = local_run(task["setup"], work, venv)
        if setup.returncode != 0:
            return {"passed": False, "eval": f"setup failed locally: {setup.stderr.strip()[-200:]}"}
        r = local_run(task["eval_cmd"], work, venv)
        return {"passed": r.returncode == 0, "eval": last_line(r.stdout + r.stderr), "eval_where": "local"}

    from contree_sdk.sdk.exceptions import ContreeError

    from prax_bot.sandbox import create_workspace, make_client
    from prax_bot.settings import Settings

    settings = Settings.from_env(setup=task["setup"])
    last_error: Exception | None = None
    for attempt in range(3):  # sandbox calls occasionally time out; that is not a verdict on the diff
        try:
            ws = create_workspace(make_client(), settings, work)
            r = ws.run(task["eval_cmd"], timeout=900)
            return {
                "passed": r.exit_code == 0,
                "eval": last_line(r.stdout + r.stderr),
                "eval_where": f"sandbox {ws.image_id}",
                "eval_cost": ws.total_cost,
                "eval_attempts": attempt + 1,
            }
        except ContreeError as e:
            last_error = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"sandbox evaluation failed three times: {type(last_error).__name__}: {str(last_error)[:160]}")


def run_task(task: dict, src: Path, args: argparse.Namespace, out: Path) -> dict:
    work = prepare_repo(task, src, out / "work" / task["name"], bugged=True)
    home = out / "home" / task["name"]
    home.mkdir(parents=True, exist_ok=True)
    diff_path = out / "diffs" / f"{task['name']}.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.unlink(missing_ok=True)
    cmd = [
        "uv",
        "run",
        "praxbot",
        "run",
        task["task"],
        "--repo",
        str(work),
        "--backend",
        args.backend,
        "--setup",
        task["setup"],
        "--export",
        str(diff_path),
        "--max-steps",
        str(args.max_steps),
    ]
    env = {**os.environ, "PRAXBOT_HOME": str(home), "COLUMNS": "120", "TERM": "dumb"}
    started = time.monotonic()
    p = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        check=False,
    )
    wall = time.monotonic() - started
    (out / "logs").mkdir(exist_ok=True)
    (out / "logs" / f"{task['name']}.log").write_text(p.stdout + p.stderr)

    runs = sorted((home / "runs").glob("*"))
    run_json = {}
    if runs and (runs[-1] / "run.json").exists():
        run_json = json.loads((runs[-1] / "run.json").read_text())
    diff = diff_path.read_text() if diff_path.exists() else ""
    result = {
        "name": task["name"],
        "exit": p.returncode,
        "finished": run_json.get("finished"),
        "reason": run_json.get("reason"),
        "steps": run_json.get("steps"),
        "tokens": (run_json.get("prompt_tokens") or 0) + (run_json.get("completion_tokens") or 0),
        "sandbox_commands": run_json.get("sandbox_commands"),
        "sandbox_cost": run_json.get("sandbox_cost"),
        "agent_elapsed": run_json.get("elapsed_s"),
        "wall": wall,
        "diff_lines": len(diff.splitlines()),
        "touched_tests": touched_tests(diff),
        "summary": (run_json.get("summary") or "")[:300],
    }
    if not diff.strip():
        result.update({"passed": False, "eval": "no diff produced"})
    else:
        try:
            result.update(evaluate(task, src, diff, args, out))
        except Exception as e:
            result.update({"passed": False, "eval": f"evaluation error: {type(e).__name__}: {str(e)[:200]}"})
    if result["touched_tests"]:
        result["passed"] = False
        result["eval"] = f"modified test files: {', '.join(result['touched_tests'])}; " + result.get("eval", "")
    return result


def markdown(results: list[dict], args: argparse.Namespace) -> str:
    rows = [
        "| task | passed | steps | tokens | agent time | diff lines | evaluation |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r['name']} | {'yes' if r.get('passed') else 'no'} | {r.get('steps') or '-'} | "
            f"{(r.get('tokens') or 0):,} | {int(r.get('agent_elapsed') or 0)} s | {r.get('diff_lines') or 0} | {r.get('eval', '')} |"
        )
    n = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    head = (
        f"Backend: {args.backend}, evaluation: {args.eval}, model: {os.environ.get('PRAXBOT_MODEL', 'default')}, "
        f"max steps: {args.max_steps}. Passed {passed} of {n}."
    )
    return head + "\n\n" + "\n".join(rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="directory with one clone per repository")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results", help="results and scratch directory")
    ap.add_argument("--tasks", help="comma-separated task names (default: all)")
    ap.add_argument("--backend", default="contree", choices=["contree", "local"])
    ap.add_argument("--eval", default="contree", choices=["contree", "local"], help="where to run the evaluation tests")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per task for the agent run")
    ap.add_argument("--validate", action="store_true", help="only check that HEAD passes and the bug fails, locally")
    ap.add_argument(
        "--reeval", action="store_true", help="re-judge the diffs saved by a previous run instead of running the agent"
    )
    args = ap.parse_args()

    tasks = TASKS
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in TASKS if t["name"] in wanted]
        missing = wanted - {t["name"] for t in tasks}
        if missing:
            ap.error(f"unknown tasks: {', '.join(sorted(missing))}")
    scratch = args.out / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    if args.validate:
        bad = validate(tasks, args.src, scratch)
        print("all tasks valid" if not bad else f"{bad} task(s) need attention")
        return 1 if bad else 0

    results = []
    for t in tasks:
        print(f"== {t['name']}", flush=True)
        if args.reeval:
            diff_path = scratch / "diffs" / f"{t['name']}.patch"
            diff = diff_path.read_text() if diff_path.exists() else ""
            r = {"name": t["name"], "diff_lines": len(diff.splitlines()), "touched_tests": touched_tests(diff)}
            if not diff.strip():
                r.update({"passed": False, "eval": "no saved diff"})
            else:
                try:
                    r.update(evaluate(t, args.src, diff, args, scratch))
                except Exception as e:
                    r.update({"passed": False, "eval": f"evaluation error: {type(e).__name__}: {str(e)[:200]}"})
            if r["touched_tests"]:
                r["passed"] = False
                r["eval"] = f"modified test files: {', '.join(r['touched_tests'])}; " + r.get("eval", "")
            results.append(r)
            print(f"   passed={r.get('passed')} eval={r.get('eval')}", flush=True)
            continue
        r = run_task(t, args.src, args, scratch)
        results.append(r)
        print(
            f"   passed={r.get('passed')} steps={r.get('steps')} tokens={r.get('tokens'):,} eval={r.get('eval')}",
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"{args.backend}-reeval" if args.reeval else args.backend
    (args.out / f"{stamp}-{suffix}.json").write_text(
        json.dumps(
            {"args": vars(args) | {"src": str(args.src), "out": str(args.out)}, "results": results},
            indent=2,
            default=str,
        )
    )
    md = markdown(results, args)
    (args.out / f"{stamp}-{suffix}.md").write_text(md)
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
