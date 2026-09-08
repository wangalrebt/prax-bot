"""The full-screen terminal interface: a conversation pane, a sidebar, a diff pane, and one input line.

Everything the session prints goes through a Rich console whose file object forwards each line to the
conversation pane, so the plain session and this interface share the same round logic.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from .settings import Settings

FILE_RE = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


class PaneFile:
    """A file-like object for a Rich console: lines are forwarded to a RichLog from any thread."""

    def __init__(self, app: PraxApp) -> None:
        self.app = app
        self.buffer = ""
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            self.buffer += text
            lines = self.buffer.split("\n")
            self.buffer = lines.pop()
            complete = lines
        for line in complete:
            self.app.post_line(line)
        return len(text)

    def flush(self) -> None:
        with self.lock:
            pending, self.buffer = self.buffer, ""
        if pending:
            self.app.post_line(pending)

    def isatty(self) -> bool:
        return True


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 60; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    Button { margin: 0 1; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.question)
            with Horizontal(id="buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class PraxApp(App[None]):
    TITLE = "Prax Bot"
    CSS = """
    #body { height: 1fr; }
    #sidebar { width: 34; border-right: solid $accent 30%; padding: 0 1; }
    #sidebar Static { margin-bottom: 1; }
    #main { width: 1fr; }
    #log { height: 1fr; padding: 0 1; }
    #diff { height: 1fr; padding: 0 1; border-top: solid $accent 30%; display: none; }
    #diff.visible { display: block; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    Input { border: tall $accent 50%; }
    """
    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "toggle_diff", "Diff"),
        Binding("f3", "apply", "Apply"),
        Binding("f4", "undo", "Undo"),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
    ]

    def __init__(
        self, settings: Settings, root: Path, backend: str, make_session: Callable[[Console, Any], Any]
    ) -> None:
        super().__init__()
        self.settings, self.root, self.backend = settings, root, backend
        self.make_session = make_session
        self.session: Any = None
        self.out = Console(file=PaneFile(self), force_terminal=True, color_system="truecolor", width=100)
        self.busy = False
        self.rounds: list[tuple[str, int]] = []
        self.pending_lines: list[str] = []
        self.current_step = ""

    # ---- composition ----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static(id="info")
                yield Static(id="changes")
                yield Static(id="rounds")
            with Vertical(id="main"):
                yield RichLog(id="log", wrap=False, min_width=20, highlight=False, markup=False)
                yield RichLog(id="diff", wrap=False, highlight=False, markup=False)
        yield Static("starting…", id="status")
        yield Input(placeholder="an instruction, or /help", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.root)
        self.query_one("#input", Input).disabled = True
        self.fit_console()
        self.render_sidebar()
        for line in self.pending_lines:
            self.query_one("#log", RichLog).write(Text.from_ansi(line))
        self.pending_lines.clear()
        self.open_session()

    def on_resize(self) -> None:
        self.fit_console()

    def fit_console(self) -> None:
        """Render into exactly the conversation pane's width so panels and tables line up."""
        try:
            log = self.query_one("#log", RichLog)
            width = log.scrollable_content_region.width or log.content_size.width
        except Exception:
            return
        if width > 20:
            self.out.width = width - 1

    # ---- output from any thread ----
    def post_line(self, line: str) -> None:
        if not self.is_running or not self._log_ready:
            self.pending_lines.append(line)
            return
        try:
            self.call_from_thread(self._write_line, line)
        except RuntimeError:  # already on the app thread
            self._write_line(line)

    @property
    def _log_ready(self) -> bool:
        try:
            self.query_one("#log", RichLog)
            return True
        except Exception:
            return False

    def _write_line(self, line: str) -> None:
        self.query_one("#log", RichLog).write(Text.from_ansi(line))

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def session_event(self, kind: str, **payload: Any) -> None:
        """Hook wrapped around the session's event callback to keep the status line live."""
        if kind == "tool.call":
            self.current_step = f"step {payload.get('step')} · {payload.get('name')}"
            self.call_from_thread(self.set_status, f"working · {self.current_step}")
        elif kind in ("sandbox.pack", "sandbox.base", "sandbox.ready", "sandbox.setup"):
            self.call_from_thread(self.set_status, f"sandbox · {kind.split('.')[1]}")

    # ---- session lifecycle ----
    @work(thread=True, exclusive=True, group="round")
    def open_session(self) -> None:
        try:
            self.session = self.make_session(self.out, self.session_event)
        except Exception as e:
            self.out.print(f"[red]✗ could not start: {type(e).__name__}: {escape(str(e)[:300])}[/]")
            self.call_from_thread(self.set_status, "failed to start · ctrl+q to quit")
            return
        self.out.print("[dim]type an instruction, or F1 for help[/]")
        self.call_from_thread(self.finish_work, "idle")

    def finish_work(self, status: str | None = None) -> None:
        self.fit_console()
        self.busy = False
        inp = self.query_one("#input", Input)
        inp.disabled = False
        inp.focus()
        self.render_sidebar()
        self.render_diff()
        self.set_status(status or self.idle_status())

    def idle_status(self) -> str:
        if self.session is None:
            return "idle"
        s = self.session
        pending = FILE_RE.findall(s.last_diff or "")
        return f"idle · {s.rounds} rounds · {s.steps} steps · {s.llm.usage.total_tokens:,} tokens · {len(pending)} files pending · effort {s.settings.effort}"

    def render_sidebar(self) -> None:
        s = self.settings
        info = Text.assemble(
            ("session\n", "bold"),
            f"{self.root.name}\n",
            (f"{s.model}\n", "dim"),
            f"{self.backend} · effort {self.session.settings.effort if self.session else s.effort}",
        )
        self.query_one("#info", Static).update(info)
        files = FILE_RE.findall(self.session.last_diff) if self.session else []
        changes = Text.assemble(("changes\n", "bold"), "\n".join(files) if files else ("none pending", "dim"))
        self.query_one("#changes", Static).update(changes)
        rounds = Text("rounds\n", style="bold")
        for text, steps in self.rounds[-12:]:
            rounds.append(f"{steps:>3}  {text[:26]}\n")
        if not self.rounds:
            rounds.append("none yet", style="dim")
        self.query_one("#rounds", Static).update(rounds)

    def render_diff(self) -> None:
        pane = self.query_one("#diff", RichLog)
        pane.clear()
        diff = self.session.last_diff if self.session else ""
        if diff.strip():
            pane.write(Syntax(diff, "diff", theme="ansi_dark", word_wrap=False))
        else:
            pane.write(Text("no changes pending", style="dim"))

    # ---- input ----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line or self.busy or self.session is None:
            return
        if line.lower() in ("/quit", "/exit", "/q"):
            self.action_quit_app()
            return
        self.start_work(line)

    def start_work(self, line: str) -> None:
        self.fit_console()
        self.busy = True
        self.query_one("#input", Input).disabled = True
        self.set_status("working…")
        label = line if not line.startswith("/") else line.split(" ", 1)[0]
        self.out.print(f"\n[bold cyan]›[/] {escape(line)}")
        self.run_line(line, label)

    @work(thread=True, exclusive=True, group="round")
    def run_line(self, line: str, label: str) -> None:
        before = self.session.rounds
        steps_before = self.session.steps
        keep_going = True
        try:
            keep_going = self.session.handle_line(line)
        except Exception as e:
            self.out.print(f"[red]✗ {type(e).__name__}: {escape(str(e)[:300])}[/]")
        if self.session.rounds > before:
            self.rounds.append((label, self.session.steps - steps_before))
        self.call_from_thread(self.finish_work)
        if not keep_going:
            self.call_from_thread(self.action_quit_app)

    # ---- actions ----
    def action_toggle_diff(self) -> None:
        pane = self.query_one("#diff", RichLog)
        pane.toggle_class("visible")
        if pane.has_class("visible"):
            self.render_diff()

    def action_apply(self) -> None:
        if self.session and not self.busy:
            self.start_work("/apply")

    def action_undo(self) -> None:
        if self.session and not self.busy:
            self.start_work("/undo")

    def action_help(self) -> None:
        from .cli import SESSION_HELP

        self.out.print(SESSION_HELP.replace("[", "\\["))
        self.out.print("keys: F2 diff pane · F3 apply · F4 undo · ctrl+q quit")

    def action_quit_app(self) -> None:
        if self.session is None or self.busy:
            self.exit()
            return
        pending = (self.session.last_diff or "").strip()
        if not pending:
            self.session.close()
            self.exit()
            return

        def done(apply: bool | None) -> None:
            if apply:
                self.session.apply()
            self.session.close()
            self.exit()

        self.push_screen(ConfirmScreen("Apply the pending changes to the working tree before leaving?"), done)


def run_ui(
    settings: Settings, root: Path, backend: str, protect: list[str], skills: Any, plugins: Any, verbose: bool
) -> None:
    """Open the interface; the sandbox is created after the screen is up so its progress is visible."""
    from .cli import Session, _new_run, _open_workspace

    def make_session(out: Console, hook: Callable[..., None]) -> Any:
        run_id, run_dir, events_file, on_event = _new_run(settings, verbose, out=out)

        def on_event_with_hook(kind: str, **payload: Any) -> None:
            on_event(kind, **payload)
            hook(kind, **payload)

        out.print(f"[bold]sandbox[/] {settings.base_image}" if backend == "contree" else "[bold]local workspace[/]")
        ws = _open_workspace(settings, backend, root, run_id, on_event_with_hook, out=out)
        return Session(
            settings,
            ws,
            root,
            backend=backend,
            run_id=run_id,
            run_dir=run_dir,
            events_file=events_file,
            on_event=on_event_with_hook,
            protect=protect,
            skills=skills,
            plugins=plugins,
            read_line=lambda: None,
            out=out,
        )

    PraxApp(settings, root, backend, make_session).run()
