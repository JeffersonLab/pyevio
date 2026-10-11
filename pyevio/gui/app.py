"""The Textual app: status strip, four tabs, the file-open screen and the progress poll.

Threading: the analysis runs in the session's job thread and writes only its own
fields; this app reads them on a timer (``_poll``) from the main thread and
drives the panes. No widget is ever touched from the job thread.
"""
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Footer, Header, Input, Label, ProgressBar, Static, TabbedContent, TabPane

from pyevio.gui.overview import OverviewPane
from pyevio.gui.plots import PlotsPane
from pyevio.gui.records import RecordsPane
from pyevio.gui.session import STATE_EMPTY, Session
from pyevio.gui.structure import StructurePane
from pyevio.web import ApiError, is_evio_name
from pyevio.web.jobs import STATE_DONE, STATE_ERROR, STATE_CANCELLED

POLL_INTERVAL_S = 0.2
TAB_IDS = ("overview", "plots", "records", "structure")


class EvioDirectoryTree(DirectoryTree):
    """Folders and EVIO-named files; ``show_all`` lists every file."""

    def __init__(self, path, show_all: bool = False, **kwargs):
        super().__init__(path, **kwargs)
        self.show_all = show_all

    def filter_paths(self, paths):
        for path in paths:
            if path.name.startswith("."):
                continue
            if path.is_dir() or self.show_all or is_evio_name(path.name):
                yield path


class OpenScreen(ModalScreen[Optional[Path]]):
    """Pick a file: type a path (a folder re-roots the tree), or select one in the tree. Escape cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace", "go_up", "Parent folder"),
        Binding("ctrl+a", "toggle_all", "All files"),
    ]
    DEFAULT_CSS = """
    OpenScreen {
        align: center middle;
    }
    OpenScreen > Vertical {
        width: 90%;
        height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    OpenScreen Input {
        margin-bottom: 1;
    }
    OpenScreen EvioDirectoryTree {
        height: 1fr;
    }
    OpenScreen #open-hint {
        color: $text-muted;
    }
    """

    def __init__(self, root: Path):
        super().__init__()
        self.root = Path(root).expanduser().resolve()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Open an EVIO file — type a path and press Enter, or pick one below")
            yield Input(value=str(self.root), placeholder="path to a file or folder", id="open-path")
            yield EvioDirectoryTree(self.root, id="open-tree")
            yield Label("Enter opens · Backspace goes to the parent folder · Ctrl+A shows every file · Escape cancels", id="open-hint")

    def reroot(self, folder: Path) -> None:
        self.root = folder
        self.query_one("#open-path", Input).value = str(folder)
        self.query_one("#open-tree", EvioDirectoryTree).path = folder

    def on_input_submitted(self, event: Input.Submitted) -> None:
        path = Path(event.value).expanduser()
        if path.is_file():
            self.dismiss(path.resolve())
        elif path.is_dir():
            self.reroot(path.resolve())
        else:
            self.notify(f"no such file or folder: {event.value}", severity="error")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.dismiss(Path(event.path).resolve())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_go_up(self) -> None:
        if self.root.parent != self.root:
            self.reroot(self.root.parent)

    def action_toggle_all(self) -> None:
        tree = self.query_one("#open-tree", EvioDirectoryTree)
        tree.show_all = not tree.show_all
        tree.reload()


class StatusStrip(Horizontal):
    """One line under the header: the file, the analysis state, progress and the last error."""

    DEFAULT_CSS = """
    StatusStrip {
        height: 1;
        padding: 0 1;
    }
    StatusStrip #status-text {
        width: 1fr;
    }
    StatusStrip ProgressBar {
        width: 30;
    }
    StatusStrip #status-error {
        color: $error;
        width: auto;
        margin-left: 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("No file open — press o", id="status-text")
        yield ProgressBar(total=100, show_eta=False, id="status-progress")
        yield Static("", id="status-error")

    def show(self, text: str, fraction: Optional[float] = None) -> None:
        self.query_one("#status-text", Static).update(text)
        bar = self.query_one("#status-progress", ProgressBar)
        bar.display = fraction is not None
        if fraction is not None:
            bar.update(progress=fraction * 100)

    def show_error(self, text: str) -> None:
        self.query_one("#status-error", Static).update(text)


class PyEvioGui(App):
    """``pyevio gui``: the console viewer."""

    TITLE = "pyevio"
    CSS = """
    TabbedContent {
        height: 1fr;
    }
    """
    BINDINGS = [
        Binding("o", "open_file", "Open"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('plots')", "Plots"),
        Binding("3", "tab('records')", "Records"),
        Binding("4", "tab('structure')", "Structure"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, session: Session, start_path: Optional[Path] = None, start_dir: Optional[Path] = None):
        super().__init__()
        self.session = session
        self.start_path = Path(start_path) if start_path else None
        self.start_dir = Path(start_dir) if start_dir else (self.start_path.parent if self.start_path else Path.cwd())
        self._poll_timer = None
        # Direct references: TabbedContent mounts its panes after the app's own mount, so a query can come too early.
        self.overview = OverviewPane(session, id="overview")
        self.plots = PlotsPane(session, id="plots")
        self.records = RecordsPane(session, id="records")
        self.structure = StructurePane(session, id="structure")
        self.panes = [self.overview, self.plots, self.records, self.structure]

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusStrip(id="status")
        with TabbedContent(id="tabs"):
            with TabPane("Overview", id="tab-overview"):
                yield self.overview
            with TabPane("Plots", id="tab-plots"):
                yield self.plots
            with TabPane("Records", id="tab-records"):
                yield self.records
            with TabPane("Structure", id="tab-structure"):
                yield self.structure
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#status", StatusStrip).show("No file open — press o", None)
        self.call_after_refresh(self._start)  # the panes are mounted by then

    def _start(self) -> None:
        if self.start_path is not None:
            self.open_path(self.start_path)
        else:
            self.action_open_file()

    def on_unmount(self) -> None:
        # The panes' children are gone by now; only the session needs closing (the job thread is cancelled).
        try:
            self.session.close()
        except BufferError:
            pass  # a pane kept a view; the process ends anyway

    # ---------------------------------------------------------------- opening files
    def action_open_file(self) -> None:
        self.push_screen(OpenScreen(self.session.path.parent if self.session.path else self.start_dir), callback=self.open_path)

    def open_path(self, path: Optional[Path]) -> None:
        """Open ``path`` in the session and reset every pane; a failure keeps the current file and shows the reason."""
        if path is None:
            return
        status = self.query_one("#status", StatusStrip)
        status.show_error("")
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        for pane in self.panes:
            pane.file_closed()
        try:
            self.session.open(path)
        except (ValueError, OSError, BufferError) as error:
            self.show_error(f"{path}: {error}")
            if self.session.is_open:
                for pane in self.panes:
                    pane.file_opened()
            self._poll()
            return
        self.sub_title = str(path)
        self.start_dir = Path(path).parent
        for pane in self.panes:
            pane.file_opened()
        self._poll()
        if self.session.state != STATE_DONE:
            self._poll_timer = self.set_interval(POLL_INTERVAL_S, self._poll)

    def show_error(self, message: str) -> None:
        self.query_one("#status", StatusStrip).show_error(message)
        self.notify(message, severity="error", timeout=8)

    # ---------------------------------------------------------------- analysis progress
    def _poll(self) -> None:
        status = self.query_one("#status", StatusStrip)
        progress = self.session.progress()
        state = progress["state"]
        name = self.session.path.name if self.session.path else ""
        if state == STATE_EMPTY:
            status.show("No file open — press o", None)
            return
        if state == STATE_DONE:
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None
            source = "cached" if progress["cached"] else f"{progress['elapsed_s']:.1f} s"
            status.show(f"{name} — analysis done ({progress['records_total']:,} records, {source})", None)
            try:
                summary = self.session.summary()
            except ApiError as error:
                self.show_error(error.message)
                return
            for pane in self.panes:
                pane.stats_ready(summary)
            return
        if state in (STATE_ERROR, STATE_CANCELLED):
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None
            status.show(f"{name} — analysis {state}", None)
            if progress["error"]:
                self.show_error(progress["error"])
            return
        eta = f", {progress['eta_s']:.0f} s left" if progress["eta_s"] else ""
        status.show(f"{name} — analyzing {progress['records_done']:,} of {progress['records_total']:,} records{eta}",
                    progress["fraction"])

    # ---------------------------------------------------------------- navigation
    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = f"tab-{name}"

    def show_structure_node(self, node_id: str) -> None:
        """Switch to the Structure tab and select the node (``r3`` a record, ``r3/e1`` an event, …)."""
        self.action_tab("structure")
        self.structure.show_node(node_id)
