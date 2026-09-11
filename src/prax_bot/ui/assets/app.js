const TOKEN = window.PRAX_TOKEN;
const $ = (s) => document.querySelector(s);
const COMMANDS = [
  ["/apply", "apply all pending files, or /apply PATH ..."],
  ["/undo", "undo the last instruction"],
  ["/diff", "show the pending changes"],
  ["/goal", "work in rounds: /goal [--check CMD] [--rounds N] OBJECTIVE"],
  ["/resume", "swap in a past run's checkpoint: /resume RUN-ID [--from N]"],
  ["/effort", "off, low, high or xhigh for the next instructions"],
  ["/verify", "run the configured test and lint commands"],
  ["/stop", "stop after the current call"],
  ["/status", "steps, tokens, checkpoints, pending changes"],
  ["/help", "list the commands"],
  ["/quit", "end the session"],
];
let state = {},
  turn = null,
  group = null,
  callRows = {},
  roundStepsStart = 0,
  lastSeq = 0;
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
    const o = JSON.parse(a || "{}");
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
function newTurn(text) {
  $("#empty").style.display = "none";
  turn = el("div", "turn");
  turn.dataset.label = text.slice(0, 34);
  turn.appendChild(el("div", "user", text));
  group = null;
  chat.appendChild(turn);
  turn.scrollIntoView({ block: "end", behavior: "smooth" });
}
function ensureGroup() {
  if (!turn) newTurn("(continued)");
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
  const g = ensureGroup();
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
  if (!turn) newTurn("(continued)");
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
function addDiff(diff) {
  if (!turn || !diff) return;
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
  turn.appendChild(d);
}
function renderState(s) {
  state = s;
  $("#hrepo").textContent = s.name;
  $("#hbackend").textContent = s.backend;
  $("#hmodel").textContent = s.model;
  document.querySelectorAll("#effort button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === s.effort);
    b.disabled = s.closed || s.status === "working";
  });
  $("#sinfo").textContent = s.repo;
  const files = s.files || [],
    sf = $("#sfiles");
  files.forEach((f) => {
    if (!knownFiles.has(f)) selectedFiles.add(f);
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
    checkbox.disabled = s.closed || s.status === "working";
    checkbox.onchange = () => {
      if (checkbox.checked) selectedFiles.add(f);
      else selectedFiles.delete(f);
      updateApplyButton();
    };
    row.append(checkbox, document.createTextNode(f));
    sf.appendChild(row);
  });
  if (!files.length)
    sf.appendChild(
      el("div", "dim", s.diff_error ? "diff unavailable" : "none pending"),
    );
  if (s.diff_error) sf.appendChild(el("div", "log bad", s.diff_error));
  $("#sexcluded").textContent = (s.excluded || []).join("\n") || "none";
  const checks = $("#schecks");
  checks.replaceChildren(el("div", "path", s.verification || "not run"));
  (s.checks || []).forEach((c) => {
    const details = el("details"),
      passed = c.exit_code === 0 && !c.timed_out && !c.error;
    details.append(
      el("summary", "path", c.name + ": " + (passed ? "passed" : "failed")),
    );
    details.append(
      el(
        "pre",
        "check-output",
        c.command +
          "\nexit " +
          (c.exit_code ?? "unavailable") +
          " · " +
          (c.elapsed_s || 0).toFixed(2) +
          "s" +
          "\ntested image: " +
          c.image +
          (c.timed_out ? "\ntimed out" : "") +
          "\n" +
          (c.error || c.output || ""),
      ),
    );
    checks.appendChild(details);
  });
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
  $("#sround").textContent = s.rounds + " rounds · " + s.steps + " steps";
  $("#stoks").textContent =
    s.tokens.toLocaleString() +
    " tokens · " +
    s.calls +
    " calls · context " +
    (s.context || 0).toLocaleString();
  $("#spend").textContent = files.length
    ? files.length + " files pending"
    : "nothing pending";
  $("#squeue").textContent = s.queued ? s.queued + " queued" : "";
  $("#send").disabled =
    s.closed || s.status === "failed" || s.status === "stopping";
  $("#undo").disabled = s.closed || s.status !== "idle" || !s.can_undo;
  $("#verify").disabled =
    s.closed || s.status !== "idle" || s.verification === "not configured";
  $("#stop").disabled = s.closed || s.status !== "working" || s.stopping;
  updateApplyButton();
  if (s.closed) {
    st.textContent = "session ended";
    input.disabled = true;
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
  switch (e.kind) {
    case "instruction.start":
      newTurn(e.text);
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
        ensureGroup();
        turn.appendChild(el("div", "think", e.text.trim().slice(0, 600)));
      }
      break;
    case "finish":
      addSummary(e, true);
      break;
    case "session.round":
      if (!e.result.finished) addSummary(e.result, false);
      break;
    case "verification.result":
      addLog(
        e.name + ": " + (e.passed ? "passed" : "failed") + " · " + e.command,
      );
      break;
    case "todo":
      renderTodos(e.todos);
      break;
    case "instruction.done":
      if (group) group.classList.remove("open");
      break;
    case "state": {
      const pr = state.rounds || 0;
      renderState(e);
      if (e.rounds > pr && turn && !turn.querySelector(".stats")) {
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
function connect() {
  const es = new EventSource(
    "/events?token=" + encodeURIComponent(TOKEN) + "&after=" + lastSeq,
  );
  es.onmessage = (m) => {
    try {
      const event = JSON.parse(m.data);
      if (event.seq && event.seq <= lastSeq) return;
      if (event.seq) lastSeq = event.seq;
      handle(event);
    } catch (err) {
      console.error(err);
    }
  };
  es.onerror = () => {
    es.close();
    if (!state.closed) setTimeout(connect, 1500);
  };
}
async function request(path, body = {}) {
  const response = await fetch(path + "?token=" + encodeURIComponent(TOKEN), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function send(text) {
  if (!text.trim()) return;
  try {
    await request("/send", { text });
    if (input.value === text) input.value = "";
    autosize();
    menu.classList.remove("on");
  } catch (error) {
    addLog("✗ " + error.message);
  }
}
$("#send").onclick = () => send(input.value);
$("#apply").onclick = async () => {
  try {
    if (state.pending_apply) await request("/send", { text: "/apply" });
    else await request("/apply", { files: [...selectedFiles] });
  } catch (error) {
    addLog("✗ " + error.message);
  }
};
$("#undo").onclick = () => send("/undo");
$("#verify").onclick = () => send("/verify");
$("#stop").onclick = () =>
  request("/stop").catch((error) => addLog("✗ " + error.message));
document
  .querySelectorAll("#effort button")
  .forEach((b) => (b.onclick = () => send("/effort " + b.dataset.v)));
$("#tabchat").onclick = () => {
  chat.classList.add("on");
  traj.classList.remove("on");
  $("#tabchat").classList.add("on");
  $("#tabtraj").classList.remove("on");
};
$("#tabtraj").onclick = () => {
  traj.classList.add("on");
  chat.classList.remove("on");
  $("#tabtraj").classList.add("on");
  $("#tabchat").classList.remove("on");
};
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
input.focus();
connect();
