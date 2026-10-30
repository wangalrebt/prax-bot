from __future__ import annotations

import io
import json
import shlex
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from rich.console import Console

from prax_bot.ui.session import comparison_arguments, handle_line, show_status
from prax_bot.ui.web import PAGE, WebSession, make_handler


def comparison() -> dict:
    return {
        "id": "comparison-1",
        "status": "done",
        "selected": None,
        "stale": False,
        "candidates": [
            {"label": "A", "run_id": "run-a", "status": "done", "verification": "passed", "summary": "first fix"},
            {"label": "B", "run_id": "run-b", "status": "done", "verification": "failed", "summary": "second fix"},
        ],
    }


def test_shared_commands_preserve_approaches_and_require_an_explicit_choice() -> None:
    calls: list[tuple] = []

    def compare(objective, **approaches):
        calls.append(("compare", objective, approaches))
        return comparison()

    session = SimpleNamespace(
        compare=compare,
        choose_candidate=lambda label, **kwargs: calls.append(("choose", label, kwargs)),
    )
    output = io.StringIO()
    out = Console(file=output, width=160)
    objective = '--a is literal here; preserve "quotes"\nand /quit as objective text'
    approach_a, approach_b = "one's minimal fix", 'use "a second" approach'
    line = shlex.join(["/compare", "--a", approach_a, "--b", approach_b, "--", objective])
    assert handle_line(session, line, out)  # type: ignore[arg-type]
    assert calls == [("compare", objective, {"approach_a": approach_a, "approach_b": approach_b})]
    assert "run-a" in output.getvalue() and "run-b" in output.getvalue()
    assert "checks passed" in output.getvalue() and "checks failed" in output.getvalue()
    assert "/choose A" in output.getvalue() and "/apply" in output.getvalue()

    assert handle_line(session, "/choose B --comparison comparison-1", out)  # type: ignore[arg-type]
    assert calls[-1] == ("choose", "B", {"comparison_id": "comparison-1"})
    assert handle_line(session, "/choose A", out)  # type: ignore[arg-type]
    assert calls[-1] == ("choose", "A", {"comparison_id": None})


@pytest.mark.parametrize("argument", ["", "--a", "--b", "--a x", "--unknown objective"])
def test_invalid_comparison_arguments_are_rejected(argument: str) -> None:
    with pytest.raises(ValueError):
        comparison_arguments(argument)


@pytest.mark.parametrize("argument", ["", "C", "A B", "A --comparison", "B --comparison ''"])
def test_invalid_choices_do_not_reach_the_session(argument: str) -> None:
    calls: list = []
    session = SimpleNamespace(choose_candidate=lambda *args, **kwargs: calls.append((args, kwargs)))
    output = io.StringIO()
    assert handle_line(session, "/choose " + argument, Console(file=output))  # type: ignore[arg-type]
    assert calls == [] and "usage: /choose" in output.getvalue()


def test_status_includes_candidate_runs_results_and_selection() -> None:
    state = {
        "model": "test-model",
        "effort": "low",
        "rounds": 0,
        "steps": 0,
        "tokens": 0,
        "context": 0,
        "context_budget": 1000,
        "backend": "local",
        "checkpoints": 1,
        "cost": 0,
        "verification": "not run",
        "files": [],
        "log": "run.json",
        "comparison": comparison(),
    }
    state["comparison"]["selected"] = "A"
    output = io.StringIO()
    session = SimpleNamespace(diff=lambda: "", state=lambda: state)
    show_status(session, Console(file=output, width=160))  # type: ignore[arg-type]
    text = output.getvalue()
    assert "comparison-1" in text and "run-a" in text and "run-b" in text
    assert "first fix" in text and "second fix" in text and "candidate A is selected" in text


@pytest.fixture
def http_queue():
    submitted: list[str] = []
    web = SimpleNamespace(submit=submitted.append)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(web, "tok", PAGE))  # type: ignore[arg-type]
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path: str, body: object, *, token: str = "tok") -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{base}{path}?token={token}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        yield submitted, post
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_comparison_posts_queue_safely_quoted_shared_commands(http_queue) -> None:
    submitted, post = http_queue
    objective = '--a literal; "quoted"\n/quit'
    approach_a, approach_b = "one's idea", "$(literal text)"
    assert post("/compare", {"objective": objective, "approach_a": approach_a, "approach_b": approach_b}) == (
        200,
        {"ok": True},
    )
    assert shlex.split(submitted[-1]) == ["/compare", "--a", approach_a, "--b", approach_b, "--", objective]
    assert comparison_arguments(submitted[-1].partition(" ")[2]) == (objective, approach_a, approach_b)
    assert post("/compare", {"objective": "fix it"}) == (200, {"ok": True})
    assert comparison_arguments(submitted[-1].partition(" ")[2]) == ("fix it", "", "")

    comparison_id = "comparison-1 'literal'\n/quit"
    assert post("/choose", {"candidate": "B", "comparison_id": comparison_id}) == (200, {"ok": True})
    assert shlex.split(submitted[-1]) == ["/choose", "B", "--comparison", comparison_id]


def test_invalid_comparison_posts_do_not_queue_work(http_queue) -> None:
    submitted, post = http_queue
    for body in (
        {},
        {"objective": " "},
        {"objective": 4},
        {"objective": "fix", "approach_a": None},
        {"objective": "fix", "approach_b": []},
    ):
        assert post("/compare", body)[0] == 400
    for body in (
        {},
        {"candidate": "C", "comparison_id": "id"},
        {"candidate": ["A"], "comparison_id": "id"},
        {"candidate": "A"},
        {"candidate": "A", "comparison_id": " "},
        {"candidate": "B", "comparison_id": 3},
    ):
        assert post("/choose", body)[0] == 400
    assert post("/compare", {"objective": "fix"}, token="wrong")[0] == 403
    assert post("/choose", {"candidate": "A", "comparison_id": "id"}, token="wrong")[0] == 403
    assert submitted == []


def test_comparison_events_refresh_state_without_overwriting_the_parent_step() -> None:
    events: list[dict] = []
    web = WebSession.__new__(WebSession)
    web.emit = events.append
    web.state_event = lambda: {"kind": "state", "comparison": comparison()}
    web.current_step = "parent step"
    for kind in ("comparison.start", "comparison.updated", "comparison.done", "comparison.selected"):
        web.hook(kind, comparison_id="comparison-1")
        assert events[-2]["kind"] == kind and events[-1]["kind"] == "state"
    web.hook(
        "comparison.event", comparison_id="comparison-1", candidate="A", event_kind="tool.call", event={"name": "run"}
    )
    assert events[-2]["candidate"] == "A" and events[-1]["kind"] == "state"
    assert web.current_step == "parent step"
    count = len(events)
    web.hook(
        "comparison.event", comparison_id="comparison-1", candidate="B", event_kind="checkpoint", event={"step": 1}
    )
    assert len(events) == count + 1 and events[-1]["candidate"] == "B"
