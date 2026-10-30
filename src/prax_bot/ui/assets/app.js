const TOKEN = window.PRAX_TOKEN;
const $ = (s) => document.querySelector(s);
const COMMANDS = [
  ["/apply", "apply all pending files, or /apply PATH ..."],
  ["/undo", "undo the last instruction"],
  ["/diff", "show the pending changes"],
  ["/goal", "work in rounds: /goal [--check CMD] [--rounds N] OBJECTIVE"],
  ["/compare", "compare two fixes: /compare [--a APPROACH] [--b APPROACH] OBJECTIVE"],
  ["/choose", "select a comparison patch: /choose A|B"],
  ["/resume", "swap in a past run's checkpoint: /resume RUN-ID [--from N]"],
  ["/effort", "off, low, high or xhigh for the next instructions"],
  ["/verify", "run the configured test and lint commands"],
  ["/stop", "stop after the current call"],
  ["/status", "steps, tokens, checkpoints, pending changes"],
  ["/help", "list the commands"],
  ["/quit", "close this work session; Main stays available"],
];
let state = {},
  turn = null,
  group = null,
  callRows = {},
  roundStepsStart = 0,
  lastSeq = 0,
  requestingComparison = false,
  choosingCandidate = false,
  activeSessionId = null,
  sessionEpoch = 0,
  eventStream = null,
  reconnectTimer = null,
  sessionPollTimer = null,
  serverReachable = true,
  serverShutdown = false,
  sessions = [],
  savedRuns = [],
  mainSessionId = null;
const drafts = new Map();
const selectedFiles = new Set(),
  knownFiles = new Set();
const chat = $("#chat"),
  traj = $("#traj"),
  input = $("#input"),
  menu = $("#menu");
function el(t, c, x) {
  const e = document.createElement(t);
  if (c) e.className = c;
  if (x !== undefined) e.textContent = x;
  return e;
}
function describe(name, a) {
  try {
    const o = typeof a === "string" ? JSON.parse(a || "{}") : a || {};
    if (o.command) return o.command;
    if (o.path) {
      let s = o.path;
      if (o.start_line)
        s += ":" + o.start_line + (o.end_line ? "-" + o.end_line : "");
      return s;
    }
    if (o.pattern) return o.pattern + (o.path ? " in " + o.path : "");
    if (o.name) return o.name;
    if (o.summary) return o.summary;
    if (o.step !== undefined) return "→ step " + o.step;
    return JSON.stringify(o).slice(0, 120);
  } catch (e) {
    return String(a).slice(0, 120);
  }
}
function newTurn(text, historical = false) {
  $("#empty").style.display = "none";
  turn = el("div", "turn");
  turn.dataset.label = text.slice(0, 34);
  turn.dataset.historical = String(historical);
  turn.appendChild(el("div", "user", text));
  group = null;
  chat.appendChild(turn);
  turn.scrollIntoView({ block: "end", behavior: "smooth" });
}
function ensureGroup(historical = false) {
  if (!turn) newTurn("(continued)", historical);
  if (!group) {
    group = el("div", "group open");
    const b = el("button", "ghead");
    b.innerHTML =
      '<span class="chev">▸</span><span class="gcount">0 tool calls</span>';
    const activeGroup = group;
    b.onclick = () => activeGroup.classList.toggle("open");
    const box = el("div", "rows");
    group.appendChild(b);
    group.appendChild(box);
    turn.appendChild(group);
  }
  return group;
}
function addCall(e) {
  const g = ensureGroup(e.historical);
  const r = el("div", "row");
  r.innerHTML =
    '<span class="name"></span> <span class="args"></span> <span class="res"></span><pre></pre>';
  r.querySelector(".name").textContent = e.name;
  r.querySelector(".args").textContent = describe(e.name, e.args);
  r.onclick = () => r.classList.toggle("open");
  g.querySelector(".rows").appendChild(r);
  callRows[e.step + ":" + e.name] = r;
  const n = g.querySelectorAll(".row").length;
  g.querySelector(".gcount").textContent =
    n + " tool call" + (n > 1 ? "s" : "");
  chat.scrollTop = chat.scrollHeight;
}
function addResult(e) {
  const r = callRows[e.step + ":" + e.name];
  if (!r) return;
  const out = String(e.output || "");
  r.querySelector(".res").textContent = "→ " + out.split("\n")[0].slice(0, 120);
  if (e.error ?? out.startsWith("error:")) r.classList.add("err"); // older event logs lack the flag
  r.querySelector("pre").textContent = out;
}
function addSummary(e, finished) {
  if (!turn) newTurn("(continued)", e.historical);
  turn.appendChild(
    el(
      "div",
      "summary" + (finished ? "" : " stopped"),
      (finished ? "Agent finished: " : "Stopped: ") +
        (e.summary || "(no summary)"),
    ),
  );
  if (group) group.classList.remove("open");
  chat.scrollTop = chat.scrollHeight;
}
function addStats(s, rs) {
  if (!turn) return;
  const w = el("div", "stats");
  [
    rs + " steps this round",
    s.steps + " steps total",
    s.tokens.toLocaleString() + " tokens",
    s.checkpoints + " checkpoints",
    "cost " + s.cost.toFixed(4),
  ].forEach((x) => w.appendChild(el("span", "", x)));
  turn.appendChild(w);
}
function diffView(diff) {
  const files = diff
    .split("\n")
    .filter((l) => l.startsWith("diff --git")).length;
  const d = el("div", "diff");
  const b = el("button", "diffhead");
  b.innerHTML =
    '<span class="chev">▸</span> ' +
    files +
    " file" +
    (files > 1 ? "s" : "") +
    " changed";
  b.onclick = () => d.classList.toggle("open");
  const box = el("div", "difflines");
  diff.split("\n").forEach((l) => {
    let c = "ctx";
    if (
      l.startsWith("+++") ||
      l.startsWith("---") ||
      l.startsWith("diff ") ||
      l.startsWith("index ")
    )
      c = "meta";
    else if (l.startsWith("@@")) c = "hunk";
    else if (l.startsWith("+")) c = "add";
    else if (l.startsWith("-")) c = "del";
    box.appendChild(el("div", "dl " + c, l || " "));
  });
  d.appendChild(b);
  d.appendChild(box);
  return d;
}
function addDiff(diff) {
  if (turn && diff) turn.appendChild(diffView(diff));
}
function checkOutcome(check) {
  if (!check) return { label: "Unavailable", tone: "unknown" };
  if (check.timed_out) return { label: "Unknown · timed out", tone: "unknown" };
  if (check.error) return { label: "Unknown · check error", tone: "unknown" };
  if (check.exit_code === null || check.exit_code === undefined)
    return { label: "Unavailable", tone: "unknown" };
  return check.exit_code === 0
    ? { label: "Passed", tone: "passed" }
    : { label: "Failed", tone: "failed" };
}
function checkCard(check, before, key) {
  const card = el("div", "check-card"),
    outcome = checkOutcome(check),
    heading = el("div", "check-card-heading");
  heading.append(
    el("h4", "", before ? "Before · baseline" : "After · patch"),
    el("span", "outcome " + outcome.tone, outcome.label),
  );
  card.appendChild(heading);
  if (!check) {
    card.appendChild(el("p", "muted", "No baseline result is available. Whether this check changed is unknown."));
    return card;
  }
  card.appendChild(el("code", "check-command", check.command));
  const location = check.where === "fork"
    ? before ? "Clean image fork, without the patch" : "Clean image fork, with the patch applied"
    : check.where === "workspace" ? "Workspace" : check.where || "Unavailable";
  card.append(
    el("div", "check-location", "Test location: " + location),
    el("div", "check-image", "Image: " + (check.image || "unavailable")),
    el("div", "check-meta", "Exit " + (check.exit_code ?? "unavailable") + " · " + Number(check.elapsed_s || 0).toFixed(2) + "s"),
  );
  if (outcome.tone === "unknown")
    card.appendChild(el("p", "check-unknown", "This result does not establish whether the check passes or fails."));
  const details = el("details", "check-details");
  details.dataset.key = key;
  details.appendChild(el("summary", "", "Logs & details"));
  if (check.error) details.appendChild(el("pre", "check-output bad", "Error: " + check.error));
  if (check.timed_out) details.appendChild(el("p", "check-unknown", "The check timed out."));
  details.appendChild(el("pre", "check-output", check.output || "No output captured."));
  if (check.failures && check.failures.length)
    details.appendChild(el("pre", "check-output", "Reported failing tests:\n" + check.failures.join("\n")));
  card.appendChild(details);
  return card;
}
function checkPair(check, key) {
  const pair = el("div", "check-pair"),
    cards = el("div", "check-pair-cards");
  pair.appendChild(el("h3", "", check.name));
  if (check.attribution) pair.appendChild(el("p", "check-attribution", check.attribution));
  cards.append(checkCard(check.baseline, true, key + "-before"), checkCard(check, false, key + "-after"));
  pair.appendChild(cards);
  return pair;
}
function preserveOpenDetails(container, render) {
  const open = new Set([...container.querySelectorAll("details[open], .diff.open")].map((node) => node.dataset.key));
  render();
  container.querySelectorAll("details, .diff").forEach((node) => {
    if (!open.has(node.dataset.key)) return;
    if (node.tagName === "DETAILS") node.open = true;
    else node.classList.add("open");
  });
}
function renderChecks(s) {
  const sidebar = $("#schecks"),
    checks = s.checks || [],
    full = $("#verification-checks");
  sidebar.replaceChildren(el("div", "path", s.verification || "not run"));
  checks.forEach((check) => {
    const item = el("div", "check-mini");
    item.append(
      el("strong", "", check.name),
      el("span", "", "Before: " + checkOutcome(check.baseline).label),
      el("span", "", "After: " + checkOutcome(check).label),
    );
    sidebar.appendChild(item);
  });
  $("#verification-status").textContent = "Current verification: " + (s.verification || "not run") +
    (s.verification === "stale" ? ". These results are from an earlier version of the changes. Run checks again for current evidence." : "");
  preserveOpenDetails(full, () => {
    full.replaceChildren();
    checks.forEach((check, i) => full.appendChild(checkPair(check, "current-" + i)));
    if (!checks.length) full.appendChild(el("div", "empty-panel", s.verification === "not configured"
      ? "No checks are configured for this session."
      : "Run checks to see the baseline and the proposed change together. Missing or interrupted checks stay marked as unknown."));
  });
}
function comparisonDisabledReason() {
  if (state.closed) return "The session has ended.";
  if (state.pending_apply) return "Finish applying the pending change before starting a comparison.";
  if (state.status !== "idle") return "Available when the session is idle.";
  if (requestingComparison) return "Starting comparison…";
  return "Choose a candidate when both runs have stopped. Applying is a separate step.";
}
function updateCompareControls() {
  const disabled = !!state.closed || state.status !== "idle" || !!state.pending_apply || requestingComparison;
  $("#compare-start").disabled = disabled;
  $("#compare-availability").textContent = comparisonDisabledReason();
  ["#compare-objective", "#compare-approach-a", "#compare-approach-b"].forEach((id) => {
    $(id).disabled = disabled;
  });
}
function candidateView(candidate, comparison) {
  const card = el("article", "candidate" + (comparison.selected === candidate.label ? " selected" : "")),
    heading = el("div", "candidate-heading"),
    label = candidate.label;
  card.id = "candidate-" + label.toLowerCase();
  heading.append(el("h3", "", "Approach " + label), el("span", "outcome", candidate.status));
  card.append(heading, el("p", "candidate-approach", candidate.approach || "Approach left open"));
  if (candidate.run_id) card.appendChild(el("div", "candidate-run", "Run " + candidate.run_id));
  const usage = el("div", "stats candidate-usage");
  [
    Number(candidate.steps || 0).toLocaleString() + " steps",
    Number(candidate.tokens || 0).toLocaleString() + " tokens",
    Number(candidate.calls || 0).toLocaleString() + " calls",
    "cost " + Number(candidate.cost || 0).toFixed(4),
  ].forEach((value) => usage.appendChild(el("span", "", value)));
  card.appendChild(usage);
  card.appendChild(el("p", "candidate-summary", candidate.summary || (candidate.status === "working" || candidate.status === "starting" ? "Working on this approach…" : "No summary available.")));
  if (candidate.error) card.appendChild(el("pre", "check-output bad", candidate.error));
  card.appendChild(el("div", "candidate-verification", "Verification: " + (candidate.verification || "not run")));
  if (candidate.verification === "stale") card.appendChild(el("p", "check-unknown", "Checks are from an earlier version of this candidate."));
  const checks = el("div", "candidate-checks");
  checks.id = "candidate-" + label.toLowerCase() + "-checks";
  (candidate.checks || []).forEach((check, i) => checks.appendChild(checkPair(check, label + "-check-" + i)));
  if (!candidate.checks || !candidate.checks.length) checks.appendChild(el("div", "dim", "No check results available."));
  card.appendChild(checks);
  if (candidate.diff_error) card.appendChild(el("pre", "check-output bad", "Diff unavailable: " + candidate.diff_error));
  if (candidate.diff) {
    const diff = diffView(candidate.diff);
    diff.id = "candidate-" + label.toLowerCase() + "-diff";
    diff.dataset.key = label + "-diff";
    card.appendChild(diff);
  } else if (!candidate.diff_error) card.appendChild(el("div", "empty-diff", "No patch captured."));
  const logs = el("details", "candidate-logs");
  logs.id = "candidate-" + label.toLowerCase() + "-logs";
  logs.dataset.key = label + "-logs";
  logs.appendChild(el("summary", "", "Tool logs · " + (candidate.trajectory || []).length + " calls"));
  (candidate.trajectory || []).forEach((entry, i) => {
    const details = el("details", "candidate-call");
    details.dataset.key = label + "-call-" + i;
    details.appendChild(el("summary", entry.error ? "bad" : "", (entry.step ?? i + 1) + " · " + entry.name + " · " + describe(entry.name, entry.args)));
    const output = entry.result === null || entry.result === undefined ? "Waiting for result…" : typeof entry.result === "string" ? entry.result : JSON.stringify(entry.result, null, 2);
    details.append(el("pre", "check-output", "Arguments:\n" + (typeof entry.args === "string" ? entry.args : JSON.stringify(entry.args || {}, null, 2))), el("pre", "check-output" + (entry.error ? " bad" : ""), output));
    logs.appendChild(details);
  });
  if (!candidate.trajectory || !candidate.trajectory.length) logs.appendChild(el("p", "muted", "No tool calls recorded."));
  card.appendChild(logs);
  const choose = el("button", "btn candidate-choose" + (comparison.selected === label ? " primary" : ""), comparison.selected === label ? label + " selected" : "Choose " + label + " for review");
  choose.id = "choose-" + label.toLowerCase();
  choose.disabled = !!state.closed || state.status !== "idle" || !!state.pending_apply || comparison.stale || !!comparison.selected || choosingCandidate || !["complete", "stopped"].includes(candidate.status) || !!candidate.diff_error || !candidate.run_id || candidate.checkpoint === undefined || candidate.checkpoint === null;
  choose.onclick = () => chooseCandidate(comparison.id, label);
  card.appendChild(choose);
  return card;
}
function renderComparison(comparison) {
  updateCompareControls();
  $("#comparison-result").hidden = !comparison;
  $("#comparison-empty").hidden = !!comparison;
  if (!comparison) {
    $("#comparison-objective").textContent = "";
    $("#comparison-status").textContent = "";
    $("#comparison-notice").textContent = "";
    $("#comparison-candidates").replaceChildren();
    return;
  }
  $("#comparison-objective").textContent = comparison.objective;
  $("#comparison-status").textContent = comparison.status;
  const notice = $("#comparison-notice");
  notice.className = "notice" + (comparison.stale && !comparison.selected ? " warning" : "");
  notice.textContent = comparison.selected
    ? "Approach " + comparison.selected + " is selected. Review its pending files and use Apply selected to apply the patch. Both runs remain available below."
    : comparison.stale
      ? "The workspace changed after this comparison. These candidates can still be reviewed, but cannot be selected. Start a new comparison for the current workspace."
      : comparison.status === "running"
        ? "Both candidates are kept separate. Stop is available below; choose a candidate once the session is idle."
        : "Review both candidates, then choose A or B. A stopped run may still have a usable patch. No candidate is selected automatically.";
  const candidates = $("#comparison-candidates");
  preserveOpenDetails(candidates, () => {
    candidates.replaceChildren();
    (comparison.candidates || []).forEach((candidate) => candidates.appendChild(candidateView(candidate, comparison)));
  });
}
function renderState(s) {
  const previousComparison = state.comparison;
  state = s;
  $("#hrepo").textContent = s.name || "";
  $("#hbackend").textContent = s.backend || "";
  $("#hmodel").textContent = s.model || "";
  $("#session-title").textContent = s.session_name || sessions.find((item) => item.id === activeSessionId)?.name || "Loading…";
  $("#session-rename").disabled = !activeSessionId || !s.session_id;
  $("#session-close").hidden = !s.session_id || !!s.is_main;
  $("#session-close").disabled = !!s.closed || s.status === "stopping";
  $("#session-main-note").hidden = !s.is_main;
  $("#empty-title").textContent = s.is_main ? "One place to guide your work" : "Ready when you are";
  $("#empty-description").textContent = s.is_main
    ? "Your main session can start and monitor work sessions. Work continues while you browse elsewhere."
    : "Describe a change and press Enter. Prax Bot works in the workspace and shows you the diff.";
  document.querySelectorAll("#effort button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === s.effort);
    b.disabled = s.closed || s.status !== "idle";
  });
  $("#sinfo").textContent = s.repo || "";
  const files = s.files || [],
    sf = $("#sfiles"),
    flagged = new Map();
  (s.scratch || []).forEach((f) => flagged.set(f, "scratch?"));
  (s.binary || []).forEach((f) => flagged.set(f, "binary"));
  files.forEach((f) => {
    if (!knownFiles.has(f) && !flagged.has(f)) selectedFiles.add(f);
  });
  [...knownFiles].forEach((f) => {
    if (!files.includes(f)) selectedFiles.delete(f);
  });
  knownFiles.clear();
  files.forEach((f) => knownFiles.add(f));
  sf.replaceChildren();
  files.forEach((f) => {
    const row = el("label", "frow"),
      checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedFiles.has(f);
    checkbox.disabled = s.closed || s.status !== "idle";
    checkbox.onchange = () => {
      if (checkbox.checked) selectedFiles.add(f);
      else selectedFiles.delete(f);
      updateApplyButton();
    };
    row.append(checkbox, document.createTextNode(f));
    if (flagged.has(f)) {
      row.title =
        flagged.get(f) === "binary"
          ? "a binary file the run created; not applied unless selected"
          : "named like a reproduction script; not applied unless selected";
      row.appendChild(el("span", "tag", flagged.get(f)));
    }
    sf.appendChild(row);
  });
  if (!files.length)
    sf.appendChild(
      el("div", "dim", s.diff_error ? "diff unavailable" : "none pending"),
    );
  if (s.diff_error) sf.appendChild(el("div", "log bad", s.diff_error));
  $("#sexcluded").textContent = (s.excluded || []).join("\n") || "none";
  renderChecks(s);
  renderComparison(s.comparison);
  if (s.comparison && s.comparison.id !== previousComparison?.id) showTab("compare");
  const st = $("#sstatus"),
    dot = $("#dot");
  st.textContent = s.stopping
    ? "stop requested · waiting for current call"
    : s.status === "working"
      ? "working" + (s.step ? " · " + s.step : "")
      : s.status;
  dot.className =
    "dot" +
    (s.status === "working"
      ? " working"
      : s.status === "failed"
        ? " failed"
        : "");
  $("#sround").textContent = (s.rounds || 0) + " rounds · " + (s.steps || 0) + " steps";
  $("#stoks").textContent =
    Number(s.tokens || 0).toLocaleString() +
    " tokens · " +
    (s.calls || 0) +
    " calls · context " +
    (s.context || 0).toLocaleString();
  $("#spend").textContent = files.length
    ? files.length + " files pending"
    : "nothing pending";
  $("#squeue").textContent = s.queued ? s.queued + " queued" : "";
  $("#send").disabled =
    s.closed || !s.run_id || !["idle", "working"].includes(s.status);
  $("#undo").disabled = s.closed || s.status !== "idle" || !s.can_undo;
  $("#verify").disabled =
    s.closed || s.status !== "idle" || s.verification === "not configured";
  $("#verify-checks").disabled = $("#verify").disabled;
  $("#stop").disabled = s.closed || s.status !== "working" || s.stopping;
  updateApplyButton();
  input.disabled = !!s.closed || !s.run_id || !["idle", "working"].includes(s.status);
  if (s.closed) {
    st.textContent = "session ended";
  }
  if (s.session_id) {
    const item = sessions.find((session) => session.id === s.session_id);
    if (item) Object.assign(item, {
      name: s.session_name,
      status: s.closed ? "closed" : s.stopping ? "stopping" : s.status,
      verification: s.verification,
      files_changed: files.length,
      tokens: s.tokens,
    });
    renderSessions();
  }
}
function updateApplyButton() {
  const button = $("#apply");
  button.disabled =
    state.closed ||
    state.status !== "idle" ||
    !!state.diff_error ||
    (!selectedFiles.size && !state.pending_apply);
  button.textContent = state.pending_apply
    ? "Finish applying"
    : "Apply selected";
}
function renderRounds() {
  const r = $("#srounds");
  r.innerHTML = "";
  const turns = [...document.querySelectorAll(".turn")];
  if (!turns.length) {
    r.innerHTML = '<div class="dim">none yet</div>';
    return;
  }
  turns.forEach((t, i) => {
    const d = el("div", "rround");
    d.innerHTML = '<span class="n">' + (i + 1) + "</span>";
    d.appendChild(document.createTextNode(t.dataset.label || ""));
    d.onclick = () => t.scrollIntoView({ block: "start", behavior: "smooth" });
    r.appendChild(d);
  });
}
function renderTodos(list) {
  const t = $("#stodos");
  t.innerHTML = "";
  if (!list || !list.length) {
    t.innerHTML = '<div class="dim">none</div>';
    return;
  }
  list.forEach((x) => {
    const d = el("div", "todo " + (x.status || ""));
    d.appendChild(
      el(
        "span",
        "tb",
        x.status === "completed" ? "✓" : x.status === "in_progress" ? "◐" : "○",
      ),
    );
    d.appendChild(document.createTextNode(x.content));
    t.appendChild(d);
  });
}
function addLog(text) {
  traj.appendChild(
    el(
      "div",
      "log" + (text.startsWith("✗") || text.includes("error") ? " bad" : ""),
      text,
    ),
  );
  if (text.startsWith("✗") || text.startsWith("could not"))
    chat.appendChild(el("div", "log bad", text));
}
function handle(e) {
  // Candidate progress belongs to the comparison cards, never the active chat turn.
  if (e.candidate || e.kind?.startsWith("comparison.")) return;
  switch (e.kind) {
    case "instruction.start":
      newTurn(e.text, e.historical);
      roundStepsStart = state.steps || 0;
      renderRounds();
      break;
    case "tool.call":
      addCall(e);
      break;
    case "tool.result":
      addResult(e);
      break;
    case "assistant":
      if (e.text && e.text.trim()) {
        ensureGroup(e.historical);
        turn.appendChild(el("div", "think", e.text.trim().slice(0, 600)));
      }
      break;
    case "finish":
      addSummary(e, true);
      break;
    case "session.round":
      if (!e.result.finished) addSummary({ ...e.result, historical: e.historical }, false);
      break;
    case "verification.result":
      addLog(
        e.name +
          ": " +
          checkOutcome(e).label.toLowerCase() +
          " · " +
          e.command +
          (e.attribution ? " · " + e.attribution : ""),
      );
      break;
    case "todo":
      renderTodos(e.todos);
      break;
    case "instruction.done":
      if (group) group.classList.remove("open");
      break;
    case "instruction.cancelled": {
      const text = "Cancelled queued instruction: " + e.text +
        (e.reason ? "\nReason: " + e.reason : "");
      $("#empty").style.display = "none";
      chat.appendChild(el("div", "log", text));
      traj.appendChild(el("div", "log", text));
      chat.scrollTop = chat.scrollHeight;
      break;
    }
    case "state": {
      if (e.historical) break;
      const pr = state.rounds || 0;
      renderState(e);
      if (e.rounds > pr && turn && turn.dataset.historical !== "true" && !turn.querySelector(".stats")) {
        addStats(e, e.steps - roundStepsStart);
        addDiff(e.diff);
      }
      break;
    }
    case "queued":
      break;
    case "log":
      addLog(e.text);
      break;
    case "goal.round":
      addLog(
        "goal round " +
          e.round +
          (e.met ? " · goal met" : e.stopped ? " · " + e.stopped : ""),
      );
      break;
    case "sandbox.retry":
      addLog("sandbox retry: " + e.error);
      break;
    default:
      if (e.kind && e.kind.startsWith("sandbox"))
        addLog(
          e.kind.replace("sandbox.", "sandbox: ") +
            (e.image ? " " + e.image : "") +
            (e.elapsed ? " · " + e.elapsed.toFixed(1) + "s" : ""),
        );
  }
}
function sessionContext() {
  return { id: activeSessionId, runId: state.run_id, epoch: sessionEpoch };
}
function isCurrent(context) {
  return context.id === activeSessionId && context.epoch === sessionEpoch;
}
function endpoint(path, context) {
  const query = new URLSearchParams({ token: TOKEN });
  if (context?.id) query.set("session", context.id);
  return path + "?" + query;
}
async function fetchJSON(path, { method = "GET", body, context } = {}) {
  const response = await fetch(endpoint(path, context), {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "Request failed");
    error.status = response.status;
    throw error;
  }
  return result;
}
function request(path, body = {}, context = sessionContext()) {
  return fetchJSON(path, {
    method: "POST",
    body: { ...body, source_run: context.runId },
    context,
  });
}
function managerRequest(path, body) {
  return fetchJSON(path, { method: "POST", body });
}
function disconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (eventStream) eventStream.close();
  eventStream = null;
}
function connect(context = sessionContext()) {
  if (!isCurrent(context) || !serverReachable || serverShutdown || state.closed) return;
  const es = new EventSource(endpoint("/events", context) + "&after=" + lastSeq);
  eventStream = es;
  es.onmessage = (message) => {
    if (!isCurrent(context) || eventStream !== es) return;
    try {
      const event = JSON.parse(message.data);
      if (event.session_id && event.session_id !== context.id) return;
      if (event.seq && event.seq <= lastSeq) return;
      if (event.seq) lastSeq = event.seq;
      handle(event);
      if (state.closed) disconnect();
    } catch (error) {
      console.error(error);
    }
  };
  es.onerror = () => {
    es.close();
    if (!isCurrent(context) || eventStream !== es) return;
    eventStream = null;
    if (!state.closed && serverReachable && !serverShutdown)
      reconnectTimer = setTimeout(() => connect(context), 1500);
  };
}
function saveDraft() {
  if (!activeSessionId) return;
  drafts.set(activeSessionId, {
    text: input.value,
    objective: $("#compare-objective").value,
    approachA: $("#compare-approach-a").value,
    approachB: $("#compare-approach-b").value,
  });
}
function resetSessionView() {
  state = {};
  turn = group = null;
  callRows = {};
  roundStepsStart = lastSeq = 0;
  requestingComparison = choosingCandidate = false;
  selectedFiles.clear();
  knownFiles.clear();
  const empty = $("#empty");
  chat.replaceChildren(empty);
  empty.style.display = "";
  traj.replaceChildren();
  renderTodos([]);
  renderRounds();
  $("#verification-checks").replaceChildren();
  $("#comparison-candidates").replaceChildren();
  $("#compare-error").textContent = "";
  $("#session-rename-form").hidden = true;
  $("#session-close").disabled = false;
  menu.classList.remove("on");
  menuSel = 0;
  const draft = drafts.get(activeSessionId) || {};
  input.value = draft.text || "";
  $("#compare-objective").value = draft.objective || "";
  $("#compare-approach-a").value = draft.approachA || "";
  $("#compare-approach-b").value = draft.approachB || "";
  renderState({ status: "starting" });
  autosize();
  showTab("chat");
}
async function switchSession(id, { reopen = true, force = false } = {}) {
  if (!id || (!force && id === activeSessionId && !state.closed)) return;
  saveDraft();
  disconnect();
  activeSessionId = id;
  sessionEpoch++;
  const context = sessionContext();
  resetSessionView();
  $("#session-error").textContent = "";
  $("#sidebar").classList.remove("mobile-open");
  $("#sidebar-toggle").setAttribute("aria-expanded", "false");
  const url = new URL(window.location.href);
  url.searchParams.set("session", id);
  window.history.replaceState(null, "", url);
  renderSessions();
  try {
    if (reopen) await managerRequest("/sessions/open", { session_id: id });
    if (!isCurrent(context)) return;
    const current = await fetchJSON("/state", { context });
    if (!isCurrent(context)) return;
    renderState(current);
    connect(context);
    if (!input.disabled) input.focus();
    refreshSessions();
  } catch (error) {
    if (!isCurrent(context)) return;
    $("#session-error").textContent = "Could not open session: " + error.message;
    $("#sstatus").textContent = "session unavailable";
    $("#session-retry").hidden = false;
  }
}
function sessionMatches(item, query) {
  return [item.name, item.task, item.summary, item.id, item.run_id, item.last_run_id, ...(item.run_ids || [])]
    .some((value) => String(value || "").toLowerCase().includes(query));
}
function sessionCard(item, saved = false) {
  const button = el("button", "session-card" + (!saved && item.id === activeSessionId ? " selected" : ""));
  button.type = "button";
  if (!saved) button.setAttribute("aria-current", item.id === activeSessionId ? "page" : "false");
  const heading = el("span", "session-card-heading"),
    isMain = item.is_main || item.id === mainSessionId,
    status = saved ? "saved" : item.status || "starting";
  heading.appendChild(el("strong", "session-card-name", item.name || item.task || item.run_id || "Untitled session"));
  heading.appendChild(el("span", "session-badge " + status, status));
  button.appendChild(heading);
  if (isMain) button.appendChild(el("span", "session-role", "Main · always available"));
  if (item.task && item.task !== item.name && !saved)
    button.appendChild(el("span", "session-task", item.task));
  if (item.error || item.summary)
    button.appendChild(el("span", "session-summary" + (item.error ? " bad" : ""), item.error || item.summary));
  const details = [];
  if (item.verification) details.push("Checks: " + item.verification);
  if (item.files_changed) details.push(item.files_changed + " files");
  if (item.tokens) details.push(Number(item.tokens).toLocaleString() + " tokens");
  if (details.length) button.appendChild(el("span", "session-meta", details.join(" · ")));
  if (saved) {
    if (item.state) button.appendChild(el("span", "session-meta", "Saved state: " + item.state));
    button.appendChild(el("span", "session-meta", item.live ? "In use by another process" : "Reopen run " + item.run_id));
    button.disabled = !!item.live;
  }
  else if (["saved", "closed", "failed"].includes(status))
    button.appendChild(el("span", "session-meta", "Open to continue"));
  const updated = typeof item.updated_at === "number"
    ? new Date(item.updated_at * 1000).toLocaleString() : item.updated_at;
  button.title = [item.name, item.task, item.summary, item.error, item.run_id, updated && "Updated " + updated].filter(Boolean).join("\n");
  button.onclick = saved ? () => importRun(item.run_id, button) : () => switchSession(item.id, { force: item.id === activeSessionId && ["saved", "closed", "failed"].includes(status) });
  return button;
}
function renderSessions() {
  const list = $("#session-list"),
    saved = $("#saved-run-list"),
    query = $("#session-search").value.trim().toLowerCase();
  list.replaceChildren();
  const main = sessions.filter((item) => item.is_main || item.id === mainSessionId),
    work = sessions.filter((item) => !item.is_main && item.id !== mainSessionId && sessionMatches(item, query));
  main.forEach((item) => list.appendChild(sessionCard(item)));
  if (work.length) list.appendChild(el("div", "session-subheading", "Work sessions"));
  work.forEach((item) => list.appendChild(sessionCard(item)));
  if (!work.length) list.appendChild(el("div", "dim", query ? "No matching work sessions" : "Create a work session to start a separate task."));
  saved.replaceChildren();
  const matches = savedRuns.filter((item) => sessionMatches(item, query));
  matches.forEach((item) => saved.appendChild(sessionCard(item, true)));
  if (!matches.length) saved.appendChild(el("div", "dim", query ? "No matching saved runs" : "No other saved runs"));
  $("#saved-runs-label").textContent = "Saved runs · " + matches.length;
}
function scheduleSessionPoll() {
  clearTimeout(sessionPollTimer);
  if (!document.hidden && serverReachable && !serverShutdown)
    sessionPollTimer = setTimeout(refreshSessions, 5000);
}
let sessionListRequest = null;
function refreshSessions() {
  if (sessionListRequest) return sessionListRequest;
  sessionListRequest = (async () => {
    try {
      const result = await fetchJSON("/sessions");
      serverReachable = true;
      serverShutdown = !!result.shutdown;
      sessions = result.sessions || [];
      savedRuns = result.saved_runs || [];
      mainSessionId = result.main_session_id;
      $("#session-retry").hidden = true;
      renderSessions();
      if (serverShutdown) {
        disconnect();
        $("#session-error").textContent = "Prax Bot has stopped. Restart the server to continue.";
        renderState({ ...state, closed: true });
        $("#sstatus").textContent = "server stopped";
      }
      return result;
    } catch (error) {
      serverReachable = false;
      disconnect();
      $("#session-error").textContent = "Cannot reach Prax Bot: " + error.message;
      $("#session-retry").hidden = false;
      renderState({ ...state, status: "disconnected" });
      $("#sstatus").textContent = "disconnected";
      return null;
    } finally {
      sessionListRequest = null;
      scheduleSessionPoll();
    }
  })();
  return sessionListRequest;
}
async function importRun(runId, button) {
  const epoch = sessionEpoch;
  button.disabled = true;
  $("#session-error").textContent = "";
  try {
    const result = await managerRequest("/sessions/import", { run_id: runId });
    await refreshSessions();
    if (epoch === sessionEpoch) await switchSession(result.session_id);
  } catch (error) {
    $("#session-error").textContent = "Could not reopen run: " + error.message;
  } finally {
    button.disabled = false;
  }
}
async function send(text) {
  if (!text.trim() || !state.run_id || state.closed) return;
  const context = sessionContext();
  saveDraft();
  try {
    await request("/send", { text }, context);
    const draft = drafts.get(context.id);
    if (draft?.text === text) draft.text = "";
    if (!isCurrent(context)) return;
    if (input.value === text) input.value = "";
    autosize();
    menu.classList.remove("on");
  } catch (error) {
    if (isCurrent(context)) addLog("✗ " + error.message);
  }
}
$("#send").onclick = () => send(input.value);
$("#apply").onclick = async () => {
  const context = sessionContext();
  try {
    if (state.pending_apply) await request("/send", { text: "/apply" }, context);
    else await request("/apply", { files: [...selectedFiles] }, context);
  } catch (error) {
    if (isCurrent(context)) addLog("✗ " + error.message);
  }
};
$("#undo").onclick = () => send("/undo");
$("#verify").onclick = $("#verify-checks").onclick = () => {
  showTab("checks");
  send("/verify");
};
$("#stop").onclick = async () => {
  const context = sessionContext();
  try {
    await request("/stop", {}, context);
  } catch (error) {
    if (isCurrent(context)) addLog("✗ " + error.message);
  }
};
$("#compare-form").onsubmit = async (event) => {
  event.preventDefault();
  const objective = $("#compare-objective").value.trim();
  if (!objective || $("#compare-start").disabled) return;
  const context = sessionContext();
  requestingComparison = true;
  $("#compare-error").textContent = "";
  updateCompareControls();
  try {
    await request("/compare", {
      objective,
      approach_a: $("#compare-approach-a").value.trim(),
      approach_b: $("#compare-approach-b").value.trim(),
    }, context);
  } catch (error) {
    if (isCurrent(context)) $("#compare-error").textContent = error.message;
  } finally {
    if (isCurrent(context)) {
      requestingComparison = false;
      updateCompareControls();
    }
  }
};
async function chooseCandidate(comparisonId, candidate) {
  const context = sessionContext();
  choosingCandidate = true;
  $("#compare-error").textContent = "";
  renderComparison(state.comparison);
  try {
    await request("/choose", { comparison_id: comparisonId, candidate }, context);
  } catch (error) {
    if (isCurrent(context)) $("#compare-error").textContent = error.message;
  } finally {
    if (isCurrent(context)) {
      choosingCandidate = false;
      renderComparison(state.comparison);
    }
  }
}
$("#session-search").oninput = renderSessions;
$("#sidebar-toggle").onclick = () => {
  const open = $("#sidebar").classList.toggle("mobile-open");
  $("#sidebar-toggle").setAttribute("aria-expanded", String(open));
};
$("#new-session-toggle").onclick = () => {
  const form = $("#new-session-form");
  form.hidden = !form.hidden;
  $("#new-session-toggle").setAttribute("aria-expanded", String(!form.hidden));
  if (!form.hidden) $("#new-session-name").focus();
};
$("#new-session-form").onsubmit = async (event) => {
  event.preventDefault();
  const name = $("#new-session-name").value.trim(),
    task = $("#new-session-task").value.trim(),
    epoch = sessionEpoch;
  if (!name || $("#new-session-create").disabled) return;
  $("#new-session-create").disabled = true;
  $("#session-error").textContent = "";
  try {
    const result = await managerRequest("/sessions/new", { name, task });
    $("#new-session-form").reset();
    $("#new-session-form").hidden = true;
    $("#new-session-toggle").setAttribute("aria-expanded", "false");
    await refreshSessions();
    if (epoch === sessionEpoch) await switchSession(result.session_id);
  } catch (error) {
    $("#session-error").textContent = "Could not create session: " + error.message;
  } finally {
    $("#new-session-create").disabled = false;
  }
};
$("#session-rename").onclick = () => {
  $("#session-rename-form").hidden = false;
  $("#session-name").value = state.session_name || "";
  $("#session-name").focus();
  $("#session-name").select();
};
$("#session-name-cancel").onclick = () => { $("#session-rename-form").hidden = true; };
$("#session-rename-form").onsubmit = async (event) => {
  event.preventDefault();
  const name = $("#session-name").value.trim(),
    context = sessionContext();
  if (!name || $("#session-name-save").disabled) return;
  $("#session-name-save").disabled = true;
  try {
    await managerRequest("/sessions/rename", { session_id: context.id, name });
    if (isCurrent(context)) {
      state.session_name = name;
      $("#session-title").textContent = name;
      $("#session-rename-form").hidden = true;
    }
    await refreshSessions();
  } catch (error) {
    if (isCurrent(context)) $("#session-error").textContent = "Could not rename session: " + error.message;
  } finally {
    $("#session-name-save").disabled = false;
  }
};
$("#session-close").onclick = async () => {
  const context = sessionContext();
  if (state.is_main || !context.id) return;
  $("#session-close").disabled = true;
  try {
    await managerRequest("/sessions/close", { session_id: context.id, source_run: context.runId });
    await refreshSessions();
    if (isCurrent(context)) await switchSession(mainSessionId);
  } catch (error) {
    if (isCurrent(context)) {
      $("#session-error").textContent = "Could not close session: " + error.message;
      $("#session-close").disabled = false;
    }
  }
};
$("#session-retry").onclick = async () => {
  $("#session-error").textContent = "";
  const epoch = sessionEpoch;
  if (await refreshSessions()) {
    if (epoch === sessionEpoch && !serverShutdown)
      await switchSession(activeSessionId || mainSessionId, { force: true });
  }
};
document.addEventListener("visibilitychange", () => {
  clearTimeout(sessionPollTimer);
  if (!document.hidden && serverReachable && !serverShutdown) refreshSessions();
});
window.addEventListener("pagehide", () => {
  saveDraft();
  disconnect();
  clearTimeout(sessionPollTimer);
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    refreshSessions();
    connect();
  }
});
document
  .querySelectorAll("#effort button")
  .forEach((b) => (b.onclick = () => send("/effort " + b.dataset.v)));
function showTab(name) {
  ["chat", "traj", "checks", "compare"].forEach((tab) => {
    $("#" + (tab === "checks" || tab === "compare" ? tab + "-view" : tab)).classList.toggle("on", tab === name);
    $("#tab" + tab).classList.toggle("on", tab === name);
  });
}
["chat", "traj", "checks", "compare"].forEach((name) => {
  $("#tab" + name).onclick = () => showTab(name);
});
$("#view-checks").onclick = () => showTab("checks");
let menuSel = 0;
function matches() {
  return COMMANDS.filter((c) =>
    c[0].slice(1).startsWith(input.value.slice(1).toLowerCase()),
  );
}
function showMenu() {
  const v = input.value;
  if (!v.startsWith("/") || v.includes(" ")) {
    menu.classList.remove("on");
    return;
  }
  const items = matches();
  menu.innerHTML = "";
  if (!items.length) {
    menu.classList.remove("on");
    return;
  }
  menuSel = Math.min(menuSel, items.length - 1);
  items.forEach((c, i) => {
    const d = el("div", i === menuSel ? "sel" : "");
    d.innerHTML = "<b></b><span></span>";
    d.querySelector("b").textContent = c[0];
    d.querySelector("span").textContent = c[1];
    d.onmousedown = (ev) => {
      ev.preventDefault();
      input.value = c[0] + " ";
      menu.classList.remove("on");
      input.focus();
    };
    menu.appendChild(d);
  });
  menu.classList.add("on");
}
input.addEventListener("input", () => {
  menuSel = 0;
  showMenu();
  autosize();
});
input.addEventListener("keydown", (ev) => {
  if (menu.classList.contains("on")) {
    const items = menu.querySelectorAll("div");
    if (ev.key === "ArrowDown") {
      menuSel = (menuSel + 1) % items.length;
      showMenu();
      ev.preventDefault();
      return;
    }
    if (ev.key === "ArrowUp") {
      menuSel = (menuSel - 1 + items.length) % items.length;
      showMenu();
      ev.preventDefault();
      return;
    }
    if ((ev.key === "Tab" || ev.key === "Enter") && items.length) {
      input.value = matches()[menuSel][0] + " ";
      menu.classList.remove("on");
      ev.preventDefault();
      return;
    }
    if (ev.key === "Escape") {
      menu.classList.remove("on");
      return;
    }
  }
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    send(input.value);
  }
});
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}
async function initialize() {
  resetSessionView();
  const result = await refreshSessions();
  if (!result || serverShutdown) return;
  const requested = new URL(window.location.href).searchParams.get("session");
  const selected = requested && sessions.some((session) => session.id === requested) ? requested : mainSessionId;
  await switchSession(selected);
  if (requested && selected !== requested)
    $("#session-error").textContent = "That session is unavailable. Showing Main.";
}
initialize();
