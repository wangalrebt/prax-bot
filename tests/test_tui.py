"""The full-screen interface, driven headlessly: an instruction runs a round, the diff pane shows it, quitting asks about pending changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from rich.console import Console

from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.runtime.session import Session, new_run
from prax_bot.ui.tui import ConfirmScreen, PraxApp
from tests.test_agent import FakeLLM, call, reply
from tests.test_resume import make_snapshot
from tests.test_runs import make_run, settings


def test_interface_runs_an_instruction_and_shows_the_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm

    s = settings(tmp_path)
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    llm = FakeLLM(
        [reply(None, call("write_file", path="b.py", content="1\n")), reply(None, call("finish", summary="wrote b"))]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)
    holder: dict = {}

    def make_session(out: Console, hook) -> Session:
        ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, hook, "20260902T000000Z-ui")
        run_id, run_dir, events_file, on_event = new_run(s, verbose=False, out=out)
        holder["run_dir"] = run_dir
        session = Session(
            s,
            ws,
            tmp_path,
            backend="local",
            run_id=run_id,
            run_dir=run_dir,
            events_file=events_file,
            on_event=lambda kind, **p: (on_event(kind, **p), hook(kind, **p)),
            protect=[],
            read_line=lambda: None,
            out=out,
        )
        return session

    app = PraxApp(s, tmp_path, "local", make_session)

    async def drive() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(200):  # the session opens in a worker
                await pilot.pause(0.05)
                if app.session is not None and not app.busy:
                    break
            assert app.session is not None and not app.busy
            await pilot.click("#input")
            await pilot.press(*"add b", "enter")
            for _ in range(400):
                await pilot.pause(0.05)
                if not app.busy and app.session.rounds == 1:
                    break
            assert app.session.rounds == 1 and app.rounds == [("add b", 2)]
            log_text = "\n".join(str(line) for line in app.query_one("#log").lines)
            assert "write_file" in log_text and "wrote b" in log_text
            assert "b.py" in str(app.query_one("#changes").render())
            assert "1 files pending" in str(app.query_one("#status").render())
            await pilot.press("f2")
            await pilot.pause(0.1)
            assert app.query_one("#diff").has_class("visible")
            await pilot.press("ctrl+q")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmScreen), "pending changes are asked about before leaving"
            await pilot.click("#no")
            await pilot.pause(0.2)

    asyncio.run(drive())
    info = json.loads((holder["run_dir"] / "run.json").read_text())
    assert info["task"] == "add b" and info["session"]["rounds"] == 1 and info["applied"] is False
