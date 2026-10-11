"""The Plots tab: the three levels, the keys, a ROC without hits, the PNG save; the heatmap and the log charge axis."""
import re

import pytest

textual = pytest.importorskip("textual")
pytest.importorskip("textual_plotext")

from conftest import pilot_wait  # noqa: E402
from pyevio.gui.app import PyEvioGui  # noqa: E402
from pyevio.gui.export import format_compact, occupancy_grid  # noqa: E402
from pyevio.gui.plots import HistogramPlot, PlotsPane, render_heatmap  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SCREENSHOT_PLOT = (127, 19)  # the size of one plot in the 2 × 2 grid of the 260 × 50 screenshots


async def open_plots(app, pilot):
    """Wait for the analysis, switch to the Plots tab (the pane takes the focus) and return the pane."""
    await pilot_wait(pilot, lambda: app.plots.summary is not None, what="stats_ready")
    await pilot.press("2")
    await pilot.pause()
    return app.query_one(PlotsPane)


def rendered(plot: HistogramPlot) -> str:
    return plot.render().plain


async def test_file_level_shows_hits_per_frame_rate_and_occupancy(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        titles = pane.titles()
        assert len(titles) == 3
        assert "Hits per frame" in titles[0] and "Hit rate" in titles[1] and "Occupancy" in titles[2]
        assert all("file" in title for title in titles)
        hits, rate, occupancy = pane.plots[:3]
        assert rate.hist["name"] == "rate" and rate.hist["selection"] == {"roc": None, "port": None, "ch": None}
        assert "hits per frame" in rendered(hits) and "frames" in rendered(hits)
        assert "hit rate (Hz)" in rendered(rate) and "(s)" in rendered(rate)
        text = rendered(occupancy)
        assert "ROC 7" in text and "ROC 44" in text and "payload port" in text
        assert "–" in text  # ROC 44 has ports its map does not list
        assert not pane.plots[3].display
        assert isinstance(app.focused, HistogramPlot)


async def test_roc_level_has_four_plots_named_after_the_roc(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("r")
        await pilot.pause()
        titles = pane.titles()
        assert len(titles) == 4 and pane.level == "roc" and pane.roc == 7
        assert "Occupancy" in titles[0] and "Hit rate" in titles[1] and "Charge" in titles[2] and "Time in frame" in titles[3]
        assert all("ROC 7" in title for title in titles)
        charge = pane.plots[2]
        assert charge.hist["selection"]["roc"] == 7 and sum(charge.hist["counts"]) > 0
        assert "charge (13-bit ADC units)" in rendered(charge) and "hits (log)" in rendered(charge)
        assert "channel" in rendered(pane.plots[0]) and "port 1" in rendered(pane.plots[0])
        assert str(app.query_one("#status-error").render()) == ""


async def test_channel_level_steps_to_port_1_channel_3(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("plus")
            await pilot.pause()
        assert pane.level == "channel" and (pane.roc, pane.port, pane.channel) == (7, 1, 3)
        titles = pane.titles()
        assert len(titles) == 3 and all("ROC 7 port 1 ch 3" in title for title in titles)
        assert pane.plots[1].hist["selection"] == {"roc": 7, "port": 1, "ch": 3}
        # Stepping past the last channel of port 1 rolls over into port 3; the port keys move one port.
        pane.channel = 15
        await pilot.press("plus")
        await pilot.pause()
        assert (pane.port, pane.channel) == (3, 0)
        await pilot.press("minus")
        await pilot.pause()
        assert (pane.port, pane.channel) == (1, 15)
        await pilot.press("right_curly_bracket")
        await pilot.pause()
        assert pane.port == 3 and "port 3 ch 15" in pane.titles()[0]
        # The Select widgets follow the keys.
        from textual.widgets import Select

        assert app.query_one("#plots-port", Select).value == 3
        assert app.query_one("#plots-channel", Select).value == 15
        assert app.query_one("#plots-level", Select).value == "channel"
        # And the pane follows the Select widgets (the mouse user's path).
        app.query_one("#plots-channel", Select).value = 9
        await pilot.pause()
        assert pane.channel == 9 and "port 3 ch 9" in pane.titles()[0]
        app.query_one("#plots-roc", Select).value = 44
        await pilot.pause()
        assert (pane.roc, pane.port, pane.channel) == (44, 4, 0) and "ROC 44 port 4 ch 0" in pane.titles()[0]


async def test_keys_do_not_crash_on_a_roc_without_hits(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("r")
        await pilot.press("right_square_bracket")  # ROC 44, the DCRB
        await pilot.pause()
        assert pane.roc == 44 and all("ROC 44" in title for title in pane.titles())
        assert "no hits" in rendered(pane.plots[2])
        assert "–" in rendered(pane.plots[0])  # ports the DCRB's map does not list
        for key in ("c", "plus", "plus", "minus", "right_curly_bracket", "left_curly_bracket", "right_square_bracket",
                    "left_square_bracket", "f", "r", "c"):
            await pilot.press(key)
            await pilot.pause()
            for plot in pane.plots:
                if plot.display:
                    rendered(plot)
        assert pane.roc == 7 and pane.level == "channel"
        assert str(app.query_one("#status-error").render()) == ""


async def test_s_saves_a_png_and_notifies(session, synthetic, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = PyEvioGui(session, start_path=synthetic["path"])
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        path = tmp_path / "synthetic_hits_per_frame.png"
        assert path.is_file() and path.read_bytes()[:8] == PNG_SIGNATURE
        assert notices and str(path) in notices[-1]
        # The focused plot is the one saved: Tab moves to the rate plot.
        await pilot.press("tab")
        await pilot.press("s")
        await pilot.pause()
        assert (tmp_path / "synthetic_rate.png").is_file()
        # S saves every plot of the level; at channel level the names carry the selection.
        await pilot.press("c")
        await pilot.press("S")
        await pilot.pause()
        for name in ("rate", "charge", "time_in_frame"):
            assert (tmp_path / f"synthetic_{name}_roc7_port1_ch0.png").read_bytes()[:8] == PNG_SIGNATURE
        assert "3 PNGs" in notices[-1]
        assert pane.level == "channel"


# ---------------------------------------------------------------- the heatmap and the log charge axis, without an app
def occupancy_dict(counts: list, ports: list, dead_ports: tuple = (), roc: int = 7) -> dict:
    """A ROC-level occupancy dict (as ``pyevio.web.report.histogram`` returns it) of ``counts[port][channel]``."""
    module_id = [[255 if port in dead_ports else 2 for port in ports]]
    return {"name": "occupancy", "rocs": [roc], "ports": ports, "channels": len(counts[0]), "module_id": module_id,
            "counts": counts, "y_unit": "hits", "selection": {"roc": roc, "port": None, "ch": None}}


def row_lines(text: str, labels: list) -> dict:
    """The line index of every row label, as ``{label: line}``; a label must be on exactly one line."""
    lines = text.split("\n")
    found = {}
    for label in labels:
        hits = [index for index, line in enumerate(lines) if re.match(r" *" + re.escape(label) + "┤", line)]
        assert len(hits) == 1, f"{label!r} on lines {hits}"
        found[label] = hits[0]
    return found


def test_heatmap_prints_every_count_once_and_intact():
    """F64: 16 ports in 16 lines, counts whose compact forms overstruck each other under plotext (93.6k under 55k read 955kk)."""
    ports = list(range(1, 17))
    counts = [[(port * 16 + channel) * 613 + (0 if (port + channel) % 7 else -(port * 16 + channel) * 613)
               for channel in range(16)] for port in ports]
    counts[7][0], counts[8][0], counts[7][1], counts[8][1] = 93600, 55000, 144000, 39000
    hist = occupancy_dict(counts, ports)
    text = render_heatmap(occupancy_grid(hist), *SCREENSHOT_PLOT).plain
    lines = text.split("\n")
    assert len(lines) == SCREENSHOT_PLOT[1] and max(len(line) for line in lines) <= SCREENSHOT_PLOT[0]
    rows = row_lines(text, [f"port {port}" for port in ports])
    for port, row in zip(ports, counts):
        cells = lines[rows[f"port {port}"]].split("┤", 1)[1].rstrip("│").split()
        assert cells == [format_compact(value) for value in row], f"port {port}"
    assert "955kk" not in text and "kk" not in text
    assert lines[-1].startswith("payload port") and "channel" in lines[-1]
    for channel in range(16):
        assert f" {channel} " in f" {lines[-2]} "


def test_heatmap_rows_and_columns_are_all_the_same_size():
    """F65: 13 rows at the screenshot size and at a taller one; the dash of an unmapped port is always printed."""
    ports = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    counts = [[(port * 16 + channel) * 1000 for channel in range(16)] for port in ports]
    hist = occupancy_dict(counts, ports, dead_ports=(4, 11))
    labels = [f"port {port}" for port in ports]
    for width, height in (SCREENSHOT_PLOT, (126, 19), (126, 45), (80, 30)):
        text = render_heatmap(occupancy_grid(hist), width, height).plain
        lines = text.split("\n")
        assert len(lines) == height and max(len(line) for line in lines) <= width, (width, height)
        rows = row_lines(text, labels)
        steps = {rows[labels[i + 1]] - rows[labels[i]] for i in range(len(labels) - 1)}
        assert len(steps) == 1, (width, height, steps)
        axis = next(line for line in lines if "└" in line)
        ticks = [index for index, char in enumerate(axis) if char == "┬"]
        assert len({ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)}) == 1, (width, height, ticks)
        dashes = [line for line in lines if line.startswith(" port 4┤") or line.startswith("port 11┤")]
        assert len(dashes) == 2 and all(line.count("–") == 16 for line in dashes), (width, height)
    tall = render_heatmap(occupancy_grid(hist), 126, 45).plain
    assert row_lines(tall, labels)[labels[1]] - row_lines(tall, labels)[labels[0]] == 3  # 42 lines // 13 rows


def test_heatmap_too_low_for_its_rows_shares_lines_or_says_so():
    ports = list(range(1, 17))
    hist = occupancy_dict([[port * 10 + channel for channel in range(16)] for port in ports], ports)
    text = render_heatmap(occupancy_grid(hist), 56, 14).plain  # one plot of the 120 × 40 test terminal: 11 lines for 16 rows
    lines = text.split("\n")
    assert len(lines) == 14 and "▀" in text and " port 1┤" in text and "port 15┤" in text and "port 16" not in text
    assert all("▀" * 2 in line for line in lines if "┤" in line)
    small = render_heatmap(occupancy_grid(hist), 40, 8).plain
    assert "need a plot of" in small and "▀" not in small
    # Narrow cells: the tick labels that fit are whole channel numbers (never "12" cut to "1"), evenly thinned.
    for width in (36, 44, 50, 70):
        lines = render_heatmap(occupancy_grid(hist), width, 14).plain.split("\n")
        labels = lines[-2].split()
        assert len(labels) >= 4 and all(label in [str(channel) for channel in range(16)] for label in labels), (width, labels)
        steps = {int(labels[i + 1]) - int(labels[i]) for i in range(len(labels) - 1)}
        assert len(steps) == 1, (width, labels)


def charge_render(hist: dict, width: int, height: int) -> str:
    """The charge plot as plotext draws it, the way ``HistogramPlot.render`` does but without an app."""
    plot = HistogramPlot()
    plot.hist = hist
    plot.plt.clear_figure()
    empty = plot._build(width, height)
    assert empty is None
    plot.plt.plotsize(width, height)
    plot.plt._set_size(width, height)
    return ANSI.sub("", plot.plt.build())


def test_charge_spectrum_is_logarithmic_with_power_of_ten_ticks():
    """F68: the terminal charge plot reads like the browser's: log y, ticks labelled with the real counts."""
    edges = list(range(0, 8193, 64))
    counts = [0] * 128
    counts[0], counts[1], counts[2], counts[10], counts[20], counts[40] = 20_000_000, 2_000_000, 50_000, 300, 7, 1
    hist = {"name": "charge", "counts": counts, "edges": edges, "x_unit": "charge (13-bit ADC units)", "y_unit": "hits",
            "selection": {"roc": 7, "port": None, "ch": None}}
    text = charge_render(hist, *SCREENSHOT_PLOT)
    tick_labels = [line.split("┤")[0].strip() for line in text.split("\n") if "┤" in line]
    powers = {format_compact(10 ** power) for power in range(12)}
    assert len(tick_labels) >= 4 and all(label in powers for label in tick_labels), tick_labels
    assert "1" in tick_labels and "10M" in tick_labels and "hits (log)" in text
    # The bars: the full-scale first bin fills the canvas (column 0), the empty upper half of the spectrum draws nothing.
    lines = [line for line in text.split("\n") if "│" in line or "┤" in line]
    column = lambda line, x: line.split("┤" if "┤" in line else "│", 1)[1][x]  # noqa: E731
    heights = {x: sum(column(line, x) != " " for line in lines) for x in (0, 100)}
    assert heights[0] >= 12 and heights[100] == 0, heights
    # Zero bins (plotext's own log scale raises on them) and a one-count spectrum draw without an error.
    for sparse in ([0, 5, 0, 0, 1000, 0], [0, 0, 1, 0]):
        hist = dict(hist, counts=sparse, edges=list(range(len(sparse) + 1)))
        assert "cannot draw" not in charge_render(hist, 60, 12)
        assert "hits (log)" in charge_render(hist, 60, 12)


async def test_file_closed_clears_the_plots(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        pane.file_closed()
        await pilot.pause()
        assert pane.summary is None and all(plot.hist is None for plot in pane.plots)
        assert not pane.query_one("#plots-grid").display
        assert "Open a file" in str(pane.query_one("#plots-message").render())
