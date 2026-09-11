"""The full-screen interface, driven headlessly: an instruction runs a round, the diff pane shows it, quitting asks about pending changes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from rich.console import Console

from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.ui.common import create_session
from prax_bot.ui.tui import ConfirmScreen, PraxApp
from tests.fakes import FakeLLM, call, reply
from tests.helpers import make_run, make_snapshot, settings


def test_interface_runs_an_instruction_and_shows_the_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import prax_bot.core.llm

    s = dataclasses.replace(settings(tmp_path), backend="local")
    make_run(tmp_path, "20260901T000000Z-aaa")
    make_snapshot(tmp_path, "20260901T000000Z-aaa", 1, changed=False)
    llm = FakeLLM(
        [reply(None, call("write_file", path="b.py", content="1\n")), reply(None, call("finish", summary="wrote b"))]
    )
    monkeypatch.setattr(prax_bot.core.llm, "LLM", lambda settings: llm)
    holder: dict = {}

    ws = reopen_workspace(load_run("20260901T000000Z-aaa", s), 1, s, None, "prepared-ui")
    monkeypatch.setattr("prax_bot.runtime.factory.open_workspace", lambda *args: ws)

    def make_session(out: Console, hook):
        session = create_session(s, tmp_path, out=out, hook=hook)
        holder["run_dir"] = session.run_dir
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


def test_startup_failure_still_allows_quitting(tmp_path: Path) -> None:
    def fail(out, hook):
        raise RuntimeError("workspace unavailable")

    app = PraxApp(settings(tmp_path), tmp_path, "local", fail)

    async def drive() -> None:
        async with app.run_test() as pilot:
            for _ in range(100):
                await pilot.pause(0.01)
                if not app.busy:
                    break
            assert app.session is None and not app.busy
            assert app.query_one("#input").disabled
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app._exit, "a failed startup must not wait for work that will never finish"

    asyncio.run(drive())
