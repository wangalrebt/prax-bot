"""Create sessions and release the event log if workspace or model startup fails."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from prax_bot.extensions.plugins import ExtraTool
from prax_bot.extensions.skills import Skill
from prax_bot.runtime.runs import Run, new_run, reopen_workspace
from prax_bot.runtime.session import Session
from prax_bot.settings import Settings
from prax_bot.workspace.types import EventFn, Workspace, noop

if TYPE_CHECKING:
    from prax_bot.core.llm import LLM


def open_workspace(settings: Settings, root: Path, run_id: str, on_event: EventFn) -> Workspace:
    if settings.backend == "local":
        from prax_bot.workspace.local import create_local_workspace

        return create_local_workspace(settings, root, on_event, run_id)
    from prax_bot.workspace.sandbox import create_workspace, make_client

    return create_workspace(make_client(), settings, root, on_event)


def create_session(
    settings: Settings,
    root: Path,
    *,
    on_event: EventFn = noop,
    protect: Sequence[str] = (),
    skills: Sequence[Skill] = (),
    plugins: Sequence[ExtraTool] = (),
    kind: str = "session",
    resume: tuple[Run, int] | None = None,
    llm: LLM | None = None,
) -> Session:
    if resume is not None:
        past, _ = resume
        if not past.info.get("repo") or Path(past.info["repo"]).resolve() != root.resolve():
            raise ValueError("the run belongs to a different repository")
        if past.info.get("backend", "contree") != settings.backend:
            raise ValueError("the run uses a different backend")
    run_id, directory, events_file, emit = new_run(settings, on_event)
    try:
        workspace = (
            reopen_workspace(*resume, settings, emit, run_id)
            if resume is not None
            else open_workspace(settings, root, run_id, emit)
        )
        session = Session(
            settings,
            workspace,
            root,
            backend=settings.backend,
            run_id=run_id,
            run_dir=directory,
            events_file=events_file,
            on_event=emit,
            protect=protect,
            skills=skills,
            plugins=plugins,
            llm=llm,
            kind=kind,
        )
        if resume is not None:
            past, step = resume
            session.first_task = str(past.info.get("task") or "") or None
            session.instructions = list(past.info.get("followups") or [])
            session.last_summary = str(past.info.get("summary") or "")
            session.resumed_from = {"run_id": past.run_id, "step": step, "image": past.checkpoints[step]["image"]}
            session.record()
        return session
    except BaseException:
        events_file.close()
        raise
