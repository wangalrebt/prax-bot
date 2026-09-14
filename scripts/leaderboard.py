"""Score every official SWE-bench Verified submission on the tasks Prax Bot has run.

Reads the SWE-bench/experiments repository through the GitHub API (gh must be logged in), takes each
submission's resolved instance list, and writes a table with each run's score on the full 500, on the
"<15 min fix" tier, on the "15 min - 1 hour" tier, and on the task ids given, so a small sample of ours
can be read against published numbers on the same tasks.

    uv run python scripts/leaderboard.py --ids bench/swebench/ids-31.txt --out bench/swebench/leaderboard.json
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

REPO = "SWE-bench/experiments"


def gh_json(path: str) -> str | None:
    command = ["gh", "api", f"repos/{REPO}/contents/{path}", "--jq", ".content"]
    r = subprocess.run(command, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return base64.b64decode(r.stdout.strip()).decode("utf-8", "replace")


def resolved_ids(entry: str) -> set[str] | None:
    """A submission's resolved instances: newer entries keep them in results/results.json, older ones per instance."""
    text = gh_json(f"evaluation/verified/{entry}/results/results.json")
    if text:
        return set(json.loads(text).get("resolved", []))
    text = gh_json(f"evaluation/verified/{entry}/per_instance_details.json")
    if text:
        details = json.loads(text)
        return {iid for iid, v in details.items() if isinstance(v, dict) and v.get("resolved") is True}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", type=Path, required=True, help="file with one instance id per line")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "swebench" / "leaderboard.json")
    args = ap.parse_args()
    from swe_bench import load_dataset

    table = load_dataset(ROOT / "bench" / "swebench" / "scratch" / "verified.pkl")
    tier = dict(zip(table.instance_id, table.difficulty, strict=True))
    easy = {i for i, d in tier.items() if d == "<15 min fix"}
    mid = {i for i, d in tier.items() if d == "15 min - 1 hour"}
    ours = {line.strip() for line in args.ids.read_text().splitlines() if line.strip()}
    listing = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/evaluation/verified", "--paginate", "--jq", ".[].name"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    with ThreadPoolExecutor(8) as pool:
        found = list(zip(listing, pool.map(resolved_ids, listing), strict=True))
    rows = []
    for entry, resolved in found:
        if not resolved:
            continue
        rows.append(
            {
                "entry": entry,
                "bash_only": "_mini-" in entry,
                "resolved": len(resolved),
                "easy_tier": len(resolved & easy),
                "mid_tier": len(resolved & mid),
                "ours": len(resolved & ours),
            }
        )
    rows.sort(key=lambda r: -r["resolved"])
    args.out.write_text(
        json.dumps({"ids": sorted(ours), "tiers": {"easy": len(easy), "mid": len(mid)}, "rows": rows}, indent=1)
    )
    print(f"{len(rows)} submissions scored; written to {args.out}")
    for r in rows[:10]:
        print(f"  {r['entry'][:48]:48} {r['resolved']:>4} ({r['resolved'] / 5:.1f}%)  ours {r['ours']}/{len(ours)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
