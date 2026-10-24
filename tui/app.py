"""
Textual TUI for the VEX AIM Benchmark Harness.

Layout:
  ┌─ Header ──────────────────────────────────────────────────────────────┐
  │ [Left panel: session / robot / timer / camera] │ [Right: activity log] │
  ├──────────────────────────────────────────────────────────────────────┤
  │ [Action bar: Generate | Transfer | START RUN | Stop Run]             │
  └─ Footer ──────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ProgressBar,
    RichLog,
    Static,
)

if TYPE_CHECKING:
    from harness import Harness


# ---------------------------------------------------------------------------
# Sub-widgets
# ---------------------------------------------------------------------------

class InfoRow(Static):
    """A key/value row in the info panel."""
    def __init__(self, label: str, value: str = "—", **kwargs):
        super().__init__(**kwargs)
        self._label = label
        self._value = value

    def compose(self) -> ComposeResult:
        yield Label(f"{self._label}:", classes="info-label")
        yield Label(self._value, id=f"val-{self._label.lower().replace(' ', '-')}", classes="info-value")

    def set_value(self, v: str) -> None:
        try:
            self.query_one(f"#val-{self._label.lower().replace(' ', '-')}", Label).update(v)
        except Exception:
            pass


class SessionPanel(Static):
    """Left panel: session details, robot status, timer, camera status."""

    def compose(self) -> ComposeResult:
        yield Label("SESSION", classes="panel-title")
        yield InfoRow("Game", id="row-game")
        yield InfoRow("Model", id="row-model")
        yield InfoRow("Attempt", id="row-attempt")
        yield InfoRow("Trial", id="row-trial")
        yield InfoRow("Status", id="row-status")
        yield Static("", classes="divider")

        yield Label("ROBOT", classes="panel-title")
        yield InfoRow("Connection", id="row-connection")
        yield InfoRow("Port", id="row-port")
        yield Static("", classes="divider")

        yield Label("TIMER", classes="panel-title")
        yield Label("0 / 60 s", id="timer-label", classes="timer-text")
        yield ProgressBar(total=60, show_eta=False, id="run-timer")
        yield Static("", classes="divider")

        yield Label("CAMERA", classes="panel-title")
        yield InfoRow("Frames", id="row-frames")
        yield InfoRow("Last frame", id="row-last-frame")

    def update_info(self, **kwargs: str) -> None:
        """Update named info rows. kwarg names match row IDs minus 'row-'."""
        mapping = {
            "game": "row-game",
            "model": "row-model",
            "attempt": "row-attempt",
            "trial": "row-trial",
            "status": "row-status",
            "connection": "row-connection",
            "port": "row-port",
            "frames": "row-frames",
            "last_frame": "row-last-frame",
        }
        for key, val in kwargs.items():
            row_id = mapping.get(key)
            if row_id:
                try:
                    self.query_one(f"#{row_id}", InfoRow).set_value(val)
                except Exception:
                    pass

    def set_timer(self, elapsed: int, total: int = 60) -> None:
        try:
            self.query_one("#timer-label", Label).update(f"{elapsed} / {total} s")
            self.query_one("#run-timer", ProgressBar).progress = elapsed
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

CSS = """
Screen {
    background: $surface;
}

Header {
    background: $primary;
}

#main-layout {
    height: 1fr;
}

#left-panel {
    width: 30;
    min-width: 26;
    border-right: solid $primary;
    padding: 0 1;
}

#activity-log {
    width: 1fr;
    border: none;
}

.panel-title {
    color: $accent;
    text-style: bold;
    margin-top: 1;
}

.info-label {
    color: $text-muted;
    width: 12;
}

.info-value {
    color: $text;
}

InfoRow {
    layout: horizontal;
    height: 1;
}

.divider {
    height: 1;
}

.timer-text {
    text-align: center;
    color: $accent;
    text-style: bold;
    height: 1;
}

#action-bar {
    height: 3;
    padding: 0 1;
    align: center middle;
    background: $surface-darken-1;
    border-top: solid $primary;
}

#btn-generate {
    background: $primary;
    margin: 0 1;
}
#btn-transfer {
    background: $warning;
    margin: 0 1;
}
#btn-start {
    background: $success;
    margin: 0 1;
}
#btn-stop {
    background: $error;
    margin: 0 1;
}

ProgressBar {
    width: 100%;
}
"""


class BenchmarkApp(App):
    """VEX AIM Benchmark Harness TUI."""

    CSS = CSS
    TITLE = "VEX AIM Benchmark Harness"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("g", "generate", "Generate Code"),
        Binding("t", "transfer", "Transfer"),
        Binding("r", "start_run", "Start Run"),
        Binding("s", "stop_run", "Stop Run"),
    ]

    def __init__(self, harness: "Harness"):
        super().__init__()
        self.harness = harness
        self._run_event: asyncio.Event = asyncio.Event()
        self._timer_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    #  Compose                                                             #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="left-panel"):
                yield SessionPanel(id="session-panel")
            yield RichLog(id="activity-log", highlight=True, markup=True,
                          wrap=True, auto_scroll=True)
        with Horizontal(id="action-bar"):
            yield Button("Generate Code (g)", id="btn-generate", variant="primary")
            yield Button("Transfer to Robot (t)", id="btn-transfer", variant="warning")
            yield Button("▶  START RUN  (r)", id="btn-start", variant="success")
            yield Button("Stop Run (s)", id="btn-stop", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.harness.set_tui(self)
        self.log_message("[bold green]Harness ready.[/]")
        self.log_message(
            f"Game [cyan]{self.harness.game_number}[/] — "
            f"Models: {', '.join(self.harness.model_order)}"
        )
        self.refresh_session_panel()

    # ------------------------------------------------------------------ #
    #  Public helpers (called by Harness)                                  #
    # ------------------------------------------------------------------ #

    def log_message(self, text: str) -> None:
        """Append a line to the activity log."""
        try:
            self.query_one("#activity-log", RichLog).write(text)
        except Exception:
            pass

    def refresh_session_panel(self) -> None:
        h = self.harness
        panel = self.query_one("#session-panel", SessionPanel)
        panel.update_info(
            game=str(h.game_number),
            model=h.current_model or "—",
            attempt=f"{h.current_attempt} / {h.cfg['benchmark']['code_attempts']}",
            trial=f"{h.current_trial} / {h.cfg['benchmark']['trials_per_code']}",
            status=h.status,
            connection="OK" if h.robot_connected else "NOT FOUND",
            port=h.robot_port or "—",
            frames=str(h.frame_count),
            last_frame=h.last_frame_name or "—",
        )

    def start_timer_display(self, total: int = 60) -> None:
        """Animate the run timer in the panel."""
        if self._timer_task:
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._run_timer(total))

    def stop_timer_display(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    async def _run_timer(self, total: int) -> None:
        panel = self.query_one("#session-panel", SessionPanel)
        for elapsed in range(total + 1):
            panel.set_timer(elapsed, total)
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ #
    #  Button handlers                                                     #
    # ------------------------------------------------------------------ #

    @on(Button.Pressed, "#btn-generate")
    async def _on_generate(self) -> None:
        await self.action_generate()

    @on(Button.Pressed, "#btn-transfer")
    async def _on_transfer(self) -> None:
        await self.action_transfer()

    @on(Button.Pressed, "#btn-start")
    async def _on_start(self) -> None:
        await self.action_start_run()

    @on(Button.Pressed, "#btn-stop")
    async def _on_stop(self) -> None:
        await self.action_stop_run()

    # ------------------------------------------------------------------ #
    #  Actions                                                             #
    # ------------------------------------------------------------------ #

    async def action_generate(self) -> None:
        if self.harness.status not in ("ready", "awaiting_generate"):
            self.log_message("[yellow]Cannot generate now — check status.[/]")
            return
        asyncio.create_task(self.harness.generate_current_code())

    async def action_transfer(self) -> None:
        if self.harness.status != "awaiting_transfer":
            self.log_message("[yellow]No code ready to transfer.[/]")
            return
        asyncio.create_task(self.harness.transfer_current_code())

    async def action_start_run(self) -> None:
        if self.harness.status != "awaiting_run":
            self.log_message("[yellow]Robot not ready to run. Transfer code first.[/]")
            return
        self._run_event.set()

    async def action_stop_run(self) -> None:
        await self.harness.emergency_stop()

    async def action_quit(self) -> None:
        await self.harness.emergency_stop()
        self.exit()

    # ------------------------------------------------------------------ #
    #  Wait-for-trigger (called from Harness to block until user presses r)
    # ------------------------------------------------------------------ #

    async def wait_for_run_trigger(self) -> None:
        """Block until the user presses START RUN."""
        self._run_event.clear()
        self.log_message(
            "[bold cyan]>>> Press  r  or click [START RUN] to begin the run <<<[/]"
        )
        self.harness.status = "awaiting_run"
        self.refresh_session_panel()
        await self._run_event.wait()
