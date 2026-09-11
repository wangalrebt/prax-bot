"""Standalone HTML reports for saved runs."""

from __future__ import annotations

import html

from prax_bot.runtime.runs import Run
from prax_bot.ui.render import describe_call


def render_html(run: Run) -> str:
    """A self-contained HTML report for one run."""
    info = run.info
    e = html.escape
    usage = info.get("usage_by_model") or {}
    rows = "".join(
        f"<tr class='{'err' if c.get('error') else ''}'><td>{c['step']}</td><td>{e(str(c['name']))}</td>"
        f"<td>{e(describe_call(str(c['name']), str(c['args'])))}</td><td>{e(str(c['result'])[:160])}</td></tr>"
        for c in run.tool_calls
    )
    cps = "".join(
        f"<tr><td>{c['step']}</td><td><code>{e(str(c['image']))}</code></td><td>{e(str(c['label']))}</td></tr>"
        for c in run.checkpoints
    )
    by_model = "".join(
        f"<tr><td><code>{e(m)}</code></td><td>{u.get('calls', 0)}</td><td>{u.get('prompt_tokens', 0):,}</td><td>{u.get('completion_tokens', 0):,}</td></tr>"
        for m, u in usage.items()
    )
    status = "finished" if info.get("finished") else f"stopped: {info.get('reason', '')}"
    verification = info.get("verification") or {}
    checks = "".join(
        f"<details><summary>{e(str(check.get('name', 'check')))} · exit {e(str(check.get('exit_code')))}</summary>"
        f"<p><code>{e(str(check.get('command', '')))}</code></p>"
        f"<p>Tested image: <code>{e(str(check.get('image', '')))}</code> · {check.get('elapsed_s', 0):.2f}s</p>"
        f"<pre>{e(str(check.get('error') or check.get('output') or '(no output)'))}</pre></details>"
        for check in verification.get("checks", [])
    )
    diff = run.diff
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Prax Bot run {e(run.run_id)}</title>
<style>
body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:32px;max-width:1100px;color:#1a2321;background:#f6f8f7}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}} code,pre{{font:13px/1.45 ui-monospace,Menlo,monospace}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{text-align:left;vertical-align:top;padding:5px 10px 5px 0;border-bottom:1px solid #d9dfdc}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#6b7772}} tr.err td{{color:#a33d2a}}
pre{{background:#17211e;color:#dfe6e2;padding:14px;overflow:auto;border-radius:4px}} .kv td:first-child{{color:#6b7772;width:140px}}
.summary{{background:#fff;border-left:3px solid #0f6a86;padding:10px 14px;white-space:pre-wrap}}
</style></head><body>
<h1>Prax Bot run {e(run.run_id)}</h1>
<div>{e(str(info.get("task", "")))}</div>
<h2>Run</h2>
<table class="kv">
<tr><td>result</td><td>{e(status)}</td></tr>
<tr><td>repository</td><td><code>{e(str(info.get("repo", "")))}</code></td></tr>
<tr><td>backend</td><td>{e(str(info.get("backend", "")))}{(" · image " + e(str(info.get("base_image")))) if info.get("base_image") else ""}</td></tr>
<tr><td>model</td><td><code>{e(str(info.get("model", "")))}</code>{(" · investigation " + e(str(info.get("investigate_model")))) if info.get("investigate_model") else ""}</td></tr>
<tr><td>steps</td><td>{info.get("steps", "")}</td></tr>
<tr><td>tokens</td><td>{(info.get("prompt_tokens") or 0) + (info.get("completion_tokens") or 0):,} in {info.get("model_calls", "")} calls</td></tr>
<tr><td>sandbox</td><td>{info.get("sandbox_commands", "")} commands · {len(run.checkpoints)} checkpoints · cost {info.get("sandbox_cost", 0):.4f}</td></tr>
<tr><td>images</td><td><code>{e(str(info.get("baseline_image", "")))}</code> → <code>{e(str(info.get("final_image", "")))}</code></td></tr>
<tr><td>elapsed</td><td>{int(info.get("elapsed_s") or 0)} s</td></tr>
</table>
{("<h2>Usage by model</h2><table><tr><th>model</th><th>calls</th><th>prompt</th><th>completion</th></tr>" + by_model + "</table>") if by_model else ""}
<h2>Summary</h2>
<div class="summary">{e(str(info.get("summary", "")))}</div>
<h2>Verification</h2>
<p>{e(str(verification.get("status", "not recorded")))}</p>
{checks}
<h2>Steps</h2>
<table><tr><th>step</th><th>tool</th><th>arguments</th><th>result</th></tr>{rows}</table>
<h2>Checkpoints</h2>
<p>Every command and edit produced an immutable state. Any of them can be diffed or restored with <code>praxbot inspect {e(run.run_id)} --diff N</code>.</p>
<table><tr><th>step</th><th>image</th><th>label</th></tr>{cps}</table>
<h2>Diff</h2>
<pre>{e(diff) if diff.strip() else "(no changes)"}</pre>
</body></html>
"""
