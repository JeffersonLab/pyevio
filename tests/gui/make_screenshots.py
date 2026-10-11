"""Regenerate the console viewer screenshots of docs/console_gui.md as SVG files.

Not a test (pytest ignores the name). Run from the repository root:

    uv run python tests/gui/make_screenshots.py [FILE] [--out docs/images] [--size 140x45] [--plots-size 260x50]

FILE defaults to the SRO corpus file; when it is unreadable the synthetic
stream file of the tests is written to a temporary folder and used instead.
The two Plots shots are taken at ``--plots-size``: the occupancy heatmap prints
its counts only when every count fits in its cell, which with 16 cells across
and counts in the millions takes a terminal of about 260 columns.

The analysis runs before the app starts, through the session alone, and its
result goes to the default cache (``~/.cache/pyevio/stats``), so the app loads
it at once and a second run or ``pyevio web`` reuses it. (Under Textual the
worker pool of the analysis does not start; see "Known problems" in
docs/console_gui.md.)

The app is driven headless through Textual's pilot, as the tests do, and each
screen is exported with ``App.export_screenshot(simplify=True)``: the SVG
holds the terminal cells as text, so it can be grepped for what it shows.
"""
import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

from textual.widgets import Tree

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pyevio.gui.app import OpenScreen, PyEvioGui  # noqa: E402
from pyevio.gui.session import Session  # noqa: E402

CORPUS_FILE = Path("/data/evio-lib/corpus/sro_000791.evio.00000.v6")
DEFAULT_OUT = TESTS_DIR.parent / "docs" / "images"
ANALYSIS_TIMEOUT_S = 300
PREFIX = "console_gui_"


def ensure_cached(path: Path) -> None:
    """Run the analysis outside Textual (where its worker pool starts) so the app finds it in the cache."""
    session = Session()
    session.open(path)
    try:
        deadline = time.monotonic() + ANALYSIS_TIMEOUT_S
        while session.state not in ("done", "error", "cancelled"):
            if time.monotonic() > deadline:
                raise TimeoutError(f"the analysis of {path} did not finish in {ANALYSIS_TIMEOUT_S} s")
            progress = session.progress()
            print(f"\ranalyzing {progress['records_done']:,} of {progress['records_total']:,} records", end="", flush=True)
            time.sleep(0.5)
        progress = session.progress()
        if progress["state"] != "done":
            raise RuntimeError(f"the analysis of {path} ended in state {progress['state']}: {progress['error']}")
        source = "from cache" if progress["cached"] else "done in {:.0f} s".format(progress["elapsed_s"])
        print(f"\ranalysis {source}")
    finally:
        try:
            session.close()
        except BufferError:
            pass


async def wait_for(pilot, predicate, timeout_s, what):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen in {timeout_s} s")
        await pilot.pause(0.1)


async def settle(pilot, n=3):
    for _ in range(n):
        await pilot.pause(0.1)


def save(app, out_dir: Path, name: str) -> Path:
    path = out_dir / f"{PREFIX}{name}.svg"
    path.write_text(app.export_screenshot(simplify=True), encoding="utf-8")
    print(f"{path}  {path.stat().st_size / 1024:.0f} KB")
    return path


def first_child(node, **match):
    """The first child whose data has every ``match`` item."""
    for child in node.children:
        data = child.data or {}
        if all(data.get(key) == value for key, value in match.items()):
            return child
    return None


async def open_app(path: Path, size: tuple):
    session = Session()  # the default cache dir, shared with pyevio web
    app = PyEvioGui(session, start_path=path)
    pilot_cm = app.run_test(size=size)
    pilot = await pilot_cm.__aenter__()
    await wait_for(pilot, lambda: session.state == "done", 60, "the cached analysis")
    await wait_for(pilot, lambda: app.overview.summary is not None, 30, "stats_ready")
    await settle(pilot)
    return app, pilot, pilot_cm, session


async def close_app(pilot_cm, session) -> None:
    await pilot_cm.__aexit__(None, None, None)
    try:
        session.close()
    except BufferError:
        pass


async def shoot_main(path: Path, out_dir: Path, size: tuple) -> None:
    """Overview, Records, Structure and the open screen at ``size``."""
    app, pilot, pilot_cm, session = await open_app(path, size)
    try:
        save(app, out_dir, "overview")

        await pilot.press("3")
        await settle(pilot)
        save(app, out_dir, "records")

        # Structure: the first stream frame of record 0 expanded, its first ROC bank and ROC stream info expanded,
        # the cursor on the payload-port map so the detail panel shows the fields, the decoded map and the raw words.
        await pilot.press("4")
        await settle(pilot)
        tree = app.query_one("#structure-tree", Tree)
        app.structure.show_node("r0/e0")
        await wait_for(pilot, lambda: tree.cursor_node.data.get("_total") is not None, 30, "the frame's children")
        frame = tree.cursor_node
        roc_bank = first_child(frame, role="roc_bank")
        if roc_bank is not None:
            app.structure.show_node(roc_bank.data["id"])
            await wait_for(pilot, lambda: roc_bank.data.get("_total") is not None, 30, "the ROC bank's children")
            info = first_child(roc_bank, role="roc_stream_info")
            if info is not None:
                app.structure.show_node(info.data["id"])
                await wait_for(pilot, lambda: info.data.get("_total") is not None, 30, "the ROC stream info's children")
                payload_map = first_child(info, role="payload_map")
                if payload_map is not None:
                    app.structure.show_node(payload_map.data["id"])
        await settle(pilot)
        save(app, out_dir, "structure")

        # The open screen, rooted at the file's folder.
        await pilot.press("o")
        await wait_for(pilot, lambda: isinstance(app.screen, OpenScreen), 10, "the open screen")
        await settle(pilot)
        save(app, out_dir, "open")
        await pilot.press("escape")
        await settle(pilot)
    finally:
        await close_app(pilot_cm, session)


async def shoot_plots(path: Path, out_dir: Path, size: tuple) -> None:
    """The Plots tab at file level and at ROC level, at ``size``."""
    app, pilot, pilot_cm, session = await open_app(path, size)
    try:
        await pilot.press("2")
        await settle(pilot)
        save(app, out_dir, "plots_file")
        await pilot.press("r")
        await settle(pilot)
        save(app, out_dir, "plots_roc")
    finally:
        await close_app(pilot_cm, session)


def parse_size(text: str) -> tuple:
    columns, rows = (int(part) for part in text.lower().split("x"))
    return columns, rows


def run(path: Path, out_dir: Path, size: tuple, plots_size: tuple) -> None:
    print(f"file: {path}")
    ensure_cached(path)
    asyncio.run(shoot_main(path, out_dir, size))
    asyncio.run(shoot_plots(path, out_dir, plots_size))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", default=str(CORPUS_FILE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--size", default="140x45", help="terminal columns x rows (default 140x45)")
    parser.add_argument("--plots-size", default="260x50", help="terminal size of the two Plots shots (default 260x50)")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    size, plots_size = parse_size(args.size), parse_size(args.plots_size)

    path = Path(args.file)
    if path.is_file():
        run(path, out_dir, size, plots_size)
        return
    print(f"{path} is not readable; using the synthetic stream file of the tests", file=sys.stderr)
    from test_stream_stats import write_synthetic_file

    with tempfile.TemporaryDirectory() as folder:
        synthetic, _, _ = write_synthetic_file(Path(folder) / "synthetic.evio")
        run(Path(synthetic), out_dir, size, plots_size)


if __name__ == "__main__":
    main()
