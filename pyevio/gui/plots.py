"""Plots tab: hits per frame, rate, occupancy, charge and time in frame, at file, ROC and channel level.

The pane mirrors the browser's Plots tab. A control row picks the level (file,
ROC, channel) and the ROC, port and channel; a 2 × 2 grid of
:class:`HistogramPlot` widgets draws the histograms of that selection:

- file: hits per frame, hit rate over time, occupancy heatmap of ROC × port;
- ROC: occupancy heatmap of port × channel, hit rate, charge spectrum, time in frame;
- channel: hit rate, charge and time in frame of the one channel.

Keys, on the pane (they need the focus inside it; the pane takes it when its tab
is shown): ``f`` / ``r`` / ``c`` switch the level, ``]`` / ``[`` step the ROC,
``}`` / ``{`` the port, ``+`` / ``-`` the channel (rolling over into the next
port, as the browser's Prev / Next do), Tab moves between the plots, ``s`` saves
the focused plot (else the first one) as a PNG in the working directory and ``S``
saves every plot of the level.

Each plot holds only the histogram dict and rebuilds its plotext figure in
``render``, so a resize or the focus border redraws it at the new size; the
dicts are lists of numbers, never a view of the file.
"""
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.widgets import Select, Static
from textual_plotext import PlotextPlot

from pyevio.gui.export import (default_png_name, format_compact, histogram_title, nice_ticks, occupancy_grid, rebin,
                               save_histogram_png, series, tick_labels)
from pyevio.gui.panes import Pane
from pyevio.stream_stats import N_CHANNELS
from pyevio.web import ApiError

LEVEL_FILE, LEVEL_ROC, LEVEL_CHANNEL = "file", "roc", "channel"
LEVELS = (("File", LEVEL_FILE), ("ROC", LEVEL_ROC), ("Channel", LEVEL_CHANNEL))
PLOT_COUNT = 4
MESSAGE_NO_FILE = "Open a file to see its plots."
MESSAGE_ANALYZING = "The analysis is running — the plots appear when it is done."
HINT = "f/r/c level · ]/[ ROC · }/{ port · +/- channel · Tab next plot · s save PNG · S save all"

# plotext colours: one hue for the series, a light-to-dark ramp of the same hue for the heatmap
SERIES_COLOR = (31, 111, 181)
RAMP_LOW = (28, 42, 68)
RAMP_HIGH = (120, 200, 255)
DEAD_CELL = (90, 90, 90)
TEXT_LIGHT, TEXT_DARK = (240, 240, 240), (20, 20, 20)
MIN_PLOT_WIDTH, MIN_PLOT_HEIGHT = 16, 6


def heat_color(value: float, maximum: float) -> tuple:
    """The ramp colour of ``value``; square root, so a few hot channels do not flatten the rest (as the web does)."""
    fraction = (value / maximum) ** 0.5 if maximum > 0 and value > 0 else 0.0
    return tuple(int(round(low + (high - low) * fraction)) for low, high in zip(RAMP_LOW, RAMP_HIGH))


def text_color_on(background: tuple) -> tuple:
    luminance = 0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
    return TEXT_DARK if luminance > 150 else TEXT_LIGHT


class ReleaseFocusOnHide:
    """Give the focus up when hidden, instead of passing it to a sibling.

    Textual's ``blur`` moves the focus to a visible sibling; when the Plots tab is
    switched away the sibling is another widget of this pane, its focus makes
    ``TabbedContent`` activate the Plots tab again, and the tab switch is undone.
    """

    def _on_hide(self, event: events.Hide) -> None:
        if self.has_focus:
            self.screen.set_focus(None)
        super()._on_hide(event)


class PlotsSelect(ReleaseFocusOnHide, Select):
    pass


class HistogramPlot(ReleaseFocusOnHide, PlotextPlot, can_focus=True):
    """One histogram dict drawn with plotext; the figure is rebuilt from the dict at every render."""

    DEFAULT_CSS = """
    HistogramPlot {
        border: round $panel-lighten-2;
        padding: 0;
    }
    HistogramPlot:focus {
        border: round $accent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hist: Optional[dict] = None
        self.title = ""
        self.placeholder = ""

    def show(self, hist: Optional[dict], title: str = "", placeholder: str = "") -> None:
        self.hist = hist
        self.title = title or (histogram_title(hist) if hist else "")
        self.placeholder = placeholder
        self.border_title = self.title
        self.refresh()

    def clear(self, placeholder: str = "") -> None:
        self.show(None, "", placeholder)

    def render(self):
        if self.hist is None:
            return Text(self.placeholder, style="dim")
        width, height = self.size.width, self.size.height
        if width < MIN_PLOT_WIDTH or height < MIN_PLOT_HEIGHT:
            return Text("")
        try:
            self.plt.clear_figure()
            empty = self._build(width, height)
            if empty:
                return Text(empty, style="dim")
            return super().render()
        except Exception as error:  # a plotext failure on odd data must never take the app down
            return Text(f"cannot draw {self.title}: {error}", style="dim")

    # ---------------------------------------------------------------- building the plotext figure
    def _build(self, width: int, height: int) -> Optional[str]:
        """Draw ``self.hist`` into ``self.plt``; returns the reason when there is nothing to draw."""
        hist = self.hist
        if hist["name"] == "occupancy":
            return self._build_heatmap(occupancy_grid(hist), width, height)
        data = series(rebin(hist, max(8, width - 12)))
        if data["empty"]:
            return data["empty"]
        plt = self.plt
        edges, values = data["edges"], data["values"]
        if hist["name"] == "rate":
            xs, ys = [], []
            for i, value in enumerate(values):  # a step line: the bin's value from its left edge to its right one
                xs += [edges[i], edges[i + 1]]
                ys += [value, value]
            plt.plot(xs, ys, marker="braille", color=SERIES_COLOR)
        else:
            centers = [(edges[i] + edges[i + 1]) / 2 for i in range(len(values))]
            plt.bar(centers, values, width=1, color=SERIES_COLOR)
        top = max(values)
        if hist["name"] != "rate" and top <= 4:
            y_ticks = list(range(int(top) + 1))  # counts: whole numbers only
        else:
            y_ticks = nice_ticks(0, top, 4)
            if y_ticks and y_ticks[-1] < top:
                y_ticks.append(round(y_ticks[-1] + (y_ticks[1] - y_ticks[0] if len(y_ticks) > 1 else top), 12))
        x_ticks = [tick for tick in nice_ticks(edges[0], edges[-1], 5) if edges[0] <= tick <= edges[-1]]
        plt.xlim(edges[0], edges[-1])
        plt.ylim(0, y_ticks[-1] if y_ticks else top)
        if x_ticks:
            plt.xticks(x_ticks, tick_labels(x_ticks))
        if y_ticks:
            plt.yticks(y_ticks, tick_labels(y_ticks))
        plt.xlabel(data["x_label"])
        plt.ylabel(data["y_label"])
        return None

    def _build_heatmap(self, grid: dict, width: int, height: int) -> Optional[str]:
        """Each cell as a block of plotext matrix points, so the heatmap fills the widget; ``–`` marks unmapped ports."""
        if grid["empty"] == "no stream frames":
            return grid["empty"]
        plt = self.plt
        rows, cols = len(grid["rows"]), len(grid["cols"])
        maximum = max((value for row, dead_row in zip(grid["values"], grid["dead"])
                       for value, is_dead in zip(row, dead_row) if not is_dead), default=0)
        # The matrix is at least as large as the canvas (one point per cell or more), so plotext leaves no gaps
        # between the blocks; the canvas is the widget minus the tick labels, the frame and the axis titles.
        block_w = max(1, -(-width // cols))
        block_h = max(1, -(-height // rows))
        colors = [[DEAD_CELL if is_dead else heat_color(value, maximum)
                   for value, is_dead in zip(row, dead_row)] for row, dead_row in zip(grid["values"], grid["dead"])]
        matrix = []
        for row in colors:
            line = [color for color in row for _ in range(block_w)]
            matrix += [line] * block_h
        plt.matrix_plot(matrix)
        total_rows = rows * block_h
        x_centers = [col_i * block_w + (block_w - 1) / 2 for col_i in range(cols)]
        y_centers = [(total_rows - 1) - row_i * block_h - (block_h - 1) / 2 for row_i in range(rows)]
        chars_per_cell = max(1, (width - 10) // cols)  # about one block's width in terminal columns
        x_step = max(1, -(-4 // chars_per_cell))  # at least four columns per tick label
        plt.xticks([x for i, x in enumerate(x_centers) if i % x_step == 0],
                   [label for i, label in enumerate(grid["cols"]) if i % x_step == 0])
        plt.yticks(y_centers, grid["rows"])
        plt.xlabel(grid["col_title"])
        plt.ylabel(grid["row_title"])
        # The counts are printed in the cells when every one of them fits with a margin; the dash of an unmapped
        # port always fits.
        longest = max((len(format_compact(value)) for row, dead_row in zip(grid["values"], grid["dead"])
                       for value, is_dead in zip(row, dead_row) if not is_dead), default=0)
        with_values = longest + 1 < chars_per_cell
        for row_i, (row, dead_row) in enumerate(zip(grid["values"], grid["dead"])):
            for col_i, (value, is_dead) in enumerate(zip(row, dead_row)):
                if not is_dead and not with_values:
                    continue
                label = "–" if is_dead else format_compact(value)
                background = colors[row_i][col_i]
                plt.text(label, x_centers[col_i], y_centers[row_i], color=text_color_on(background), background=background,
                         alignment="center")
        return None


class PlotsPane(Pane):
    """The Plots tab; see the module docstring for the keys and the levels."""

    BINDINGS = [
        Binding("f", "level('file')", "File"),
        Binding("r", "level('roc')", "ROC"),
        Binding("c", "level('channel')", "Channel"),
        # The step keys stay out of the footer (the hint line in the pane lists them) so the footer keeps the level and save keys.
        Binding("right_square_bracket", "step('roc', 1)", "Next ROC", key_display="]", show=False),
        Binding("left_square_bracket", "step('roc', -1)", "Prev ROC", key_display="[", show=False),
        Binding("right_curly_bracket", "step('port', 1)", "Next port", key_display="}", show=False),
        Binding("left_curly_bracket", "step('port', -1)", "Prev port", key_display="{", show=False),
        Binding("plus", "step('channel', 1)", "Next channel", key_display="+", show=False),
        Binding("equals_sign", "step('channel', 1)", "Next channel", show=False),
        Binding("minus", "step('channel', -1)", "Prev channel", key_display="-", show=False),
        Binding("s", "save", "Save PNG"),
        Binding("S", "save_all", "Save all", key_display="S"),
    ]
    DEFAULT_CSS = """
    PlotsPane #plots-message {
        padding: 1 0;
    }
    PlotsPane #plots-controls {
        height: 3;
    }
    PlotsPane #plots-controls Select {
        width: 16;
    }
    PlotsPane #plots-controls #plots-roc {
        width: 24;
    }
    PlotsPane #plots-hint {
        width: 1fr;
        color: $text-muted;
        padding: 1 1 0 1;
    }
    PlotsPane #plots-grid {
        height: 1fr;
        grid-size: 2 2;
        grid-gutter: 0 1;
    }
    """

    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.level = LEVEL_FILE
        self.roc: Optional[int] = None
        self.port: Optional[int] = None
        self.channel: int = 0
        self._updating = False  # setting a Select programmatically posts Changed; ignored while this is set

    def compose(self):
        yield Static(MESSAGE_NO_FILE, id="plots-message")
        with Horizontal(id="plots-controls"):
            yield PlotsSelect(LEVELS, value=LEVEL_FILE, allow_blank=False, id="plots-level")
            yield PlotsSelect([], prompt="ROC", id="plots-roc")
            yield PlotsSelect([], prompt="port", id="plots-port")
            yield PlotsSelect([(f"ch {channel}", channel) for channel in range(N_CHANNELS)], prompt="channel", id="plots-channel")
            yield Static(HINT, id="plots-hint")
        with Grid(id="plots-grid"):
            for index in range(PLOT_COUNT):
                yield HistogramPlot(id=f"plot-{index}")

    # ---------------------------------------------------------------- pane hooks
    def file_closed(self) -> None:
        super().file_closed()
        self.roc = self.port = None
        self.channel = 0
        for plot in self.plots:
            plot.clear()
        self._show_message(MESSAGE_NO_FILE)

    def file_opened(self) -> None:
        self._show_message(MESSAGE_ANALYZING)

    def stats_ready(self, summary: dict) -> None:
        super().stats_ready(summary)
        rocs = summary["rocs"]
        self.roc = rocs[0]["roc"] if rocs else None
        self.port = rocs[0]["ports"][0] if rocs and rocs[0]["ports"] else None
        self.channel = 0
        self.level = LEVEL_FILE
        self._sync_controls()
        self._show_message(None)
        self.replot()

    def _show_message(self, message: Optional[str]) -> None:
        self.query_one("#plots-message", Static).update(message or "")
        self.query_one("#plots-message", Static).display = message is not None
        self.query_one("#plots-controls", Horizontal).display = message is None
        self.query_one("#plots-grid", Grid).display = message is None

    def on_show(self, event: events.Show) -> None:
        """Take the focus when the tab is shown, so the pane's keys work at once."""
        focused = self.app.focused
        if focused is None or not any(node is self for node in focused.ancestors_with_self):
            self.plots[0].focus()

    # ---------------------------------------------------------------- selection
    @property
    def plots(self) -> list:
        return [self.query_one(f"#plot-{index}", HistogramPlot) for index in range(PLOT_COUNT)]

    @property
    def visible_plots(self) -> list:
        return [plot for plot in self.plots if plot.display and plot.hist is not None]

    def titles(self) -> list:
        """The titles of the plots on show, for the tests and the status."""
        return [plot.title for plot in self.plots if plot.display]

    def roc_row(self, roc: Optional[int]) -> Optional[dict]:
        if self.summary is None or roc is None:
            return None
        return next((row for row in self.summary["rocs"] if row["roc"] == roc), None)

    def _sync_controls(self) -> None:
        """Set the Select widgets to the pane's state without replotting."""
        self._updating = True
        try:
            rocs = self.summary["rocs"] if self.summary else []
            roc_select = self.query_one("#plots-roc", Select)
            roc_select.set_options([(f"ROC {row['roc']} ({', '.join(row['modules']) or 'no modules'})", row["roc"]) for row in rocs])
            if self.roc is not None:
                roc_select.value = self.roc
            row = self.roc_row(self.roc)
            ports = row["ports"] if row else []
            port_select = self.query_one("#plots-port", Select)
            port_select.set_options([(f"port {port}", port) for port in ports])
            if self.port in ports:
                port_select.value = self.port
            self.query_one("#plots-channel", Select).value = self.channel
            self.query_one("#plots-level", Select).value = self.level
        finally:
            self._updating = False

    @on(Select.Changed)
    def _select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if self._updating or event.value is Select.NULL or event.value != event.select.value:
            return  # programmatic, blank, or stale: the Select already shows another value (fast key repeats)
        select_id = event.select.id
        if select_id == "plots-level" and event.value != self.level:
            self.level = event.value
        elif select_id == "plots-roc" and event.value != self.roc:
            self._set_roc(event.value)
            self._sync_controls()
        elif select_id == "plots-port" and event.value != self.port:
            self.port = event.value
        elif select_id == "plots-channel" and event.value != self.channel:
            self.channel = event.value
        else:
            return
        self.replot()

    def _set_roc(self, roc: int) -> None:
        self.roc = roc
        row = self.roc_row(roc)
        self.port = row["ports"][0] if row and row["ports"] else None
        self.channel = 0

    def action_level(self, level: str) -> None:
        if level == self.level or self.summary is None:
            return
        self.level = level
        self._sync_controls()
        self.replot()

    def action_step(self, what: str, direction: int) -> None:
        if self.summary is None:
            return
        if what == "roc":
            rocs = [row["roc"] for row in self.summary["rocs"]]
            if not rocs:
                return
            index = (rocs.index(self.roc) if self.roc in rocs else 0) + direction
            if not 0 <= index < len(rocs):
                return
            self._set_roc(rocs[index])
        else:
            row = self.roc_row(self.roc)
            ports = row["ports"] if row else []
            if not ports:
                return
            port_i = ports.index(self.port) if self.port in ports else 0
            if what == "port":
                port_i += direction
                if not 0 <= port_i < len(ports):
                    return
                self.port = ports[port_i]
            else:  # the channel, rolling over into the next or previous port
                flat = port_i * N_CHANNELS + self.channel + direction
                if not 0 <= flat < len(ports) * N_CHANNELS:
                    return
                self.port, self.channel = ports[flat // N_CHANNELS], flat % N_CHANNELS
        self._sync_controls()
        self.replot()

    # ---------------------------------------------------------------- plotting
    def _histogram(self, name: str, **selection) -> Optional[dict]:
        try:
            return self.session.histogram(name, **selection)
        except ApiError as error:
            self.app.show_error(error.message)
            return None

    def replot(self) -> None:
        """Fetch the histograms of the current level and selection and hand them to the plots."""
        if self.summary is None:
            return
        plots = self.plots
        requests, placeholder = [], ""
        if self.level == LEVEL_FILE:
            requests = [("hits_per_frame", {}), ("rate", {}), ("occupancy", {})]
        elif self.roc is None:
            placeholder = "no ROCs in the file"
        elif self.level == LEVEL_ROC:
            selection = {"roc": self.roc}
            requests = [("occupancy", selection), ("rate", selection), ("charge", selection), ("time_in_frame", selection)]
        elif self.port is None:
            placeholder = f"ROC {self.roc} has no mapped ports"
        else:
            selection = {"roc": self.roc, "port": self.port, "channel": self.channel}
            requests = [("rate", selection), ("charge", selection), ("time_in_frame", selection)]
        for index, plot in enumerate(plots):
            if index < len(requests):
                name, selection = requests[index]
                hist = self._histogram(name, **selection)
                plot.show(hist, placeholder=f"{name}: not available" if hist is None else "")
                plot.display = True
            else:
                plot.clear(placeholder)
                plot.display = bool(placeholder) and index == 0

    # ---------------------------------------------------------------- PNG export
    def current_plot(self) -> Optional[HistogramPlot]:
        """The focused plot, else the first plot on show."""
        focused = self.app.focused
        if isinstance(focused, HistogramPlot) and focused.hist is not None and focused.display:
            return focused
        visible = self.visible_plots
        return visible[0] if visible else None

    def _save(self, plot: HistogramPlot) -> Optional[Path]:
        path = Path.cwd() / default_png_name(self.session.path, plot.hist["name"], plot.hist.get("selection"))
        try:
            return save_histogram_png(plot.hist, path, plot.title)
        except Exception as error:  # matplotlib missing, an unwritable folder: tell, do not crash
            self.app.show_error(f"cannot save {path.name}: {error}")
            return None

    def action_save(self) -> None:
        plot = self.current_plot()
        if plot is None:
            self.app.notify("nothing to save", severity="warning")
            return
        path = self._save(plot)
        if path is not None:
            self.app.notify(f"saved {path}")

    def action_save_all(self) -> None:
        paths = [path for plot in self.visible_plots for path in [self._save(plot)] if path is not None]
        if paths:
            self.app.notify(f"saved {len(paths)} PNG{'s' if len(paths) != 1 else ''} in {paths[0].parent}")
        else:
            self.app.notify("nothing to save", severity="warning")
