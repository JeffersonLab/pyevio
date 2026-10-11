"""Histogram dicts as plot series, and one histogram as a PNG through matplotlib.

Everything here is pure: the input is a dict of :func:`pyevio.web.report.histogram`
(lists of numbers), the output a dict, a string or a file. No Textual, so the
plots pane and the tests share one reading of the five histogram shapes:

- ``hits_per_frame``, ``charge``: ``counts`` over ``edges``;
- ``time_in_frame``: ``counts`` over ``edges_ns``;
- ``rate``: ``rate_hz`` over ``edges_ns``, with ``counts`` and ``frames_per_bin`` per bin;
- ``occupancy``: ``counts`` at three depths by selection — (roc, port, channel) for the
  file, (port, channel) for a ROC, one channel for a channel — plus ``module_id``
  (roc, port), 255 where the ROC's map does not list the port.

matplotlib is imported inside :func:`save_histogram_png` only, with the ``Agg``
backend: a farm node has no display, and the viewer must start without matplotlib.
"""
import math
from pathlib import Path
from typing import Optional

from pyevio.decoders.fadc250_stream import MODULE_UNKNOWN

NS_PER_S = 1e-9
PLOT_COLOR = "#1f6fb5"  # one hue for every single-series plot
DEAD_COLOR = "#8a8a8a"  # a port the ROC's map does not list


# ---------------------------------------------------------------- ticks and labels
def nice_ticks(low: float, high: float, count: int) -> list:
    """Round tick positions covering ``[low, high]``: 1, 2 or 5 times a power of ten (as the web charts do)."""
    span = (high - low) or 1.0
    rough = span / max(count, 1)
    power = 10 ** math.floor(math.log10(rough))
    step = next(candidate * power for candidate in (1, 2, 5, 10) if candidate * power >= rough)
    first = math.ceil(low / step) * step
    ticks = []
    tick = first
    while tick <= high + step * 1e-9:
        ticks.append(round(tick, 12))
        tick += step
    return ticks


def format_compact(value: float) -> str:
    """``1.2k``, ``3.4M``; integers below 1000 as they are."""
    if value == 0:
        return "0"
    magnitude = abs(value)
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if magnitude >= limit:
            return f"{value / limit:.3g}{suffix}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3g}"


def format_tick(value: float, step: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1e4:
        return format_compact(value)
    decimals = 0 if step >= 1 else min(9, math.ceil(-math.log10(step)))
    return f"{value:.{decimals}f}"


def tick_labels(ticks: list) -> list:
    step = ticks[1] - ticks[0] if len(ticks) > 1 else (abs(ticks[0]) if ticks and ticks[0] else 1.0)
    return [format_tick(tick, step) for tick in ticks]


# ---------------------------------------------------------------- the dicts as series
def selection_label(selection: Optional[dict]) -> str:
    """``file``, ``ROC 7``, ``ROC 7 port 1 ch 3``."""
    selection = selection or {}
    if selection.get("roc") is None:
        return "file"
    parts = [f"ROC {selection['roc']}"]
    if selection.get("port") is not None:
        parts.append(f"port {selection['port']}")
        if selection.get("ch") is not None:
            parts.append(f"ch {selection['ch']}")
    return " ".join(parts)


HISTOGRAM_TITLES = {"hits_per_frame": "Hits per frame", "rate": "Hit rate", "occupancy": "Occupancy",
                    "charge": "Charge", "time_in_frame": "Time in frame"}


def histogram_title(hist: dict) -> str:
    """``Charge — ROC 7 port 1 ch 3``; the occupancy names its axes."""
    name = HISTOGRAM_TITLES.get(hist["name"], hist["name"])
    if hist["name"] == "occupancy":
        depth = occupancy_depth(hist)
        name += " (ROC × port)" if depth == 3 else " (port × channel)" if depth == 2 else ""
    return f"{name} — {selection_label(hist.get('selection'))}"


def series(hist: dict) -> dict:
    """A one-dimensional histogram as ``{edges, values, x_label, y_label, log_y, empty}`` for drawing.

    ``values[i]`` belongs to ``[edges[i], edges[i + 1])``. The rate is drawn in seconds
    from the first frame (the dict has nanoseconds); ``empty`` names why there is nothing
    to draw, or is ``None``.
    """
    name = hist["name"]
    if name == "rate":
        values = list(hist["rate_hz"])
        edges = [edge * NS_PER_S for edge in hist["edges_ns"]]
        x_label, y_label, log_y = "time since the first frame (s)", "hit rate (Hz)", False
        counts = hist["counts"]
    elif name in ("hits_per_frame", "charge", "time_in_frame"):
        values = list(hist["counts"])
        edges = list(hist["edges"] if name != "time_in_frame" else hist["edges_ns"])
        x_label, y_label, log_y = hist["x_unit"], hist["y_unit"], name == "charge"
        counts = values
    else:
        raise ValueError(f"{name!r} is not a one-dimensional histogram")
    if not values or len(edges) != len(values) + 1:
        empty = "no stream frames"
    elif not any(count > 0 for count in counts):
        empty = "no stream frames" if name == "hits_per_frame" else "no hits"
    else:
        empty = None
    return {"edges": edges, "values": values, "x_label": x_label, "y_label": y_label, "log_y": log_y, "empty": empty}


def rebin(hist: dict, max_bins: int) -> dict:
    """The same histogram with adjacent bins merged so that at most ``max_bins`` remain.

    Counts add up. The rate of a merged bin is the frames-per-bin weighted mean of the
    rates, which equals the merged counts over the merged live time, so no frame period
    is needed. Returns ``hist`` itself when it already fits.
    """
    name = hist["name"]
    key_values = "rate_hz" if name == "rate" else "counts"
    key_edges = "edges_ns" if name in ("rate", "time_in_frame") else "edges"
    n_bins = len(hist[key_values])
    if max_bins < 1 or n_bins <= max_bins:
        return hist
    factor = math.ceil(n_bins / max_bins)
    edges = hist[key_edges]
    starts = list(range(0, n_bins, factor))
    new_edges = [edges[start] for start in starts] + [edges[n_bins]]
    merged = dict(hist)
    merged[key_edges] = new_edges
    merged["counts"] = [sum(hist["counts"][start:start + factor]) for start in starts]
    if name == "rate":
        frames = [sum(hist["frames_per_bin"][start:start + factor]) for start in starts]
        weighted = [sum(rate * weight for rate, weight in zip(hist["rate_hz"][start:start + factor],
                                                              hist["frames_per_bin"][start:start + factor]))
                    for start in starts]
        merged["frames_per_bin"] = frames
        merged["rate_hz"] = [total / count if count else 0.0 for total, count in zip(weighted, frames)]
    return merged


def occupancy_depth(hist: dict) -> int:
    """3 for the file (roc, port, channel), 2 for a ROC (port, channel), 1 for one channel.

    Read from the selection, not from the shape of ``counts``: a file without stream
    frames has no ROCs, and its empty file-level list looks like any other depth.
    """
    selection = hist.get("selection") or {}
    if selection.get("roc") is None:
        return 3
    if selection.get("port") is None:
        return 2
    return 1 if selection.get("ch") is not None else 2


def occupancy_grid(hist: dict) -> dict:
    """The occupancy as a grid: ``rows``/``cols`` labels, ``values[row][col]``, ``dead[row][col]``, titles, ``empty``.

    File level: ROC × port, the channels summed. ROC level: port × channel. Channel
    level: one cell. ``dead`` marks ports the ROC's map does not list (module 255).
    """
    selection = hist.get("selection") or {}
    rocs, ports, module_id, channels = hist["rocs"], hist["ports"], hist["module_id"], hist["channels"]
    roc, port, channel = selection.get("roc"), selection.get("port"), selection.get("ch")
    if roc is None:
        values = [[sum(per_channel) for per_channel in per_port] for per_port in hist["counts"]]
        dead = [[module == MODULE_UNKNOWN for module in row] for row in module_id]
        rows, cols = [f"ROC {roc}" for roc in rocs], [str(port) for port in ports]
        row_title, col_title = "ROC", "payload port"
    else:
        roc_i = rocs.index(roc) if roc in rocs else 0
        modules = module_id[roc_i] if roc_i < len(module_id) else [MODULE_UNKNOWN] * len(ports)
        row_title, col_title = "payload port", "channel"
        if port is None:  # port × channel
            values = [list(per_channel) for per_channel in hist["counts"]]
            dead = [[module == MODULE_UNKNOWN] * channels for module in modules]
            rows, cols = [f"port {port}" for port in ports], [str(channel) for channel in range(channels)]
        else:  # one port: its channels, or the one channel
            port_i = ports.index(port) if port in ports else 0
            is_dead = port_i < len(modules) and modules[port_i] == MODULE_UNKNOWN
            values = [list(hist["counts"])]
            dead = [[is_dead] * len(values[0])]
            rows = [f"port {port}"]
            cols = [str(channel)] if channel is not None else [str(index) for index in range(len(values[0]))]
    empty = None
    if not rows or not cols:
        empty = "no stream frames"
    elif not any(value > 0 for row, dead_row in zip(values, dead) for value, is_dead in zip(row, dead_row) if not is_dead):
        empty = "no hits"
    return {"rows": rows, "cols": cols, "values": values, "dead": dead, "row_title": row_title, "col_title": col_title,
            "value_label": hist.get("y_unit", "hits"), "empty": empty}


# ---------------------------------------------------------------- PNG
def default_png_name(file_path, hist_name: str, selection: Optional[dict] = None) -> str:
    """``synthetic_charge_roc7_port1_ch3.png``: the file's stem, the histogram, the selection that is set."""
    stem = Path(str(file_path)).stem if file_path else "histogram"
    parts = [stem, hist_name]
    selection = selection or {}
    for key, label in (("roc", "roc"), ("port", "port"), ("ch", "ch")):
        if selection.get(key) is not None:
            parts.append(f"{label}{selection[key]}")
    return "_".join(parts) + ".png"


def save_histogram_png(hist: dict, path, title: str = "") -> Path:
    """Draw one histogram dict (any of the five names) with matplotlib and write ``path`` as a PNG."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm

    path = Path(path)
    figure, axes = plt.subplots(figsize=(8, 4.5), dpi=120)
    try:
        if hist["name"] == "occupancy":
            _draw_occupancy_png(axes, figure, occupancy_grid(hist), PowerNorm)
        else:
            _draw_series_png(axes, series(hist))
        axes.set_title(title or histogram_title(hist), loc="left", fontsize=11)
        figure.tight_layout()
        figure.savefig(path, format="png")
    finally:
        plt.close(figure)
    return path


def _draw_series_png(axes, data: dict) -> None:
    if data["empty"]:
        axes.text(0.5, 0.5, data["empty"], ha="center", va="center", transform=axes.transAxes, color=DEAD_COLOR)
        axes.set_xticks([])
        axes.set_yticks([])
        return
    axes.stairs(data["values"], data["edges"], fill=True, color=PLOT_COLOR, alpha=0.85, linewidth=0)
    axes.set_xlim(data["edges"][0], data["edges"][-1])
    if data["log_y"]:
        axes.set_yscale("log")
    else:
        axes.set_ylim(bottom=0)
    axes.set_xlabel(data["x_label"])
    axes.set_ylabel(data["y_label"])
    axes.grid(True, axis="y", color="#dddddd", linewidth=0.6)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)


def _draw_occupancy_png(axes, figure, grid: dict, PowerNorm) -> None:
    import numpy as np

    if grid["empty"] == "no stream frames":
        axes.text(0.5, 0.5, grid["empty"], ha="center", va="center", transform=axes.transAxes, color=DEAD_COLOR)
        axes.set_xticks([])
        axes.set_yticks([])
        return
    values = np.array(grid["values"], dtype=float)
    dead = np.array(grid["dead"], dtype=bool)
    masked = np.ma.masked_where(dead, values)
    maximum = float(masked.max()) if masked.count() else 0.0
    colormap = plt_colormap("Blues").with_extremes(bad=DEAD_COLOR)
    image = axes.imshow(masked, cmap=colormap, norm=PowerNorm(0.5, vmin=0, vmax=maximum or 1), aspect="auto",
                        interpolation="nearest")
    axes.set_xticks(range(len(grid["cols"])), grid["cols"])
    axes.set_yticks(range(len(grid["rows"])), grid["rows"])
    axes.set_xlabel(grid["col_title"])
    axes.set_ylabel(grid["row_title"])
    for row_i, (row, dead_row) in enumerate(zip(grid["values"], grid["dead"])):
        for col_i, (value, is_dead) in enumerate(zip(row, dead_row)):
            label = "–" if is_dead else format_compact(value)
            dark = (not is_dead) and maximum > 0 and math.sqrt(value / maximum) > 0.55
            axes.text(col_i, row_i, label, ha="center", va="center", fontsize=7, color="white" if dark else "black")
    bar = figure.colorbar(image, ax=axes)
    bar.set_label(f"{grid['value_label']} (square-root scale); grey: port not in the ROC's map")


def plt_colormap(name: str):
    import matplotlib

    return matplotlib.colormaps[name]
