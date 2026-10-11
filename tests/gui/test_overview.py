"""The Overview pane: totals, checks, what stands out, file facts and the per-ROC table from the summary dict."""
import io

import pytest

textual = pytest.importorskip("textual")

from rich.console import Console  # noqa: E402
from textual.content import Content  # noqa: E402
from textual.widgets import Static  # noqa: E402

from conftest import pilot_wait  # noqa: E402
from pyevio.gui.app import PyEvioGui  # noqa: E402
from pyevio.gui.overview import OverviewPane, fmt  # noqa: E402


def plain(app, section: str) -> str:
    """The text of one Overview section: markup strings through Content, Rich tables through a recording console."""
    content = app.query_one(f"#{section}", Static).content
    if isinstance(content, str):
        return Content.from_markup(content).plain
    console = Console(record=True, width=200, file=io.StringIO(), force_terminal=False)
    console.print(content)
    return console.export_text()


def check_line(text: str, name: str) -> str:
    lines = [line for line in text.splitlines() if name in line]
    assert lines, f"no line with {name!r} in:\n{text}"
    return lines[0]


async def test_overview_shows_totals_checks_and_rocs_of_the_synthetic_file(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot_wait(pilot, lambda: app.query_one(OverviewPane).summary is not None, what="stats_ready")
        await pilot.pause()

        state = plain(app, "overview-state")
        assert "synthetic.evio" in state and "analysis" in state

        totals = plain(app, "overview-totals")
        assert check_line(totals, "fADC250 hits").endswith(f"{synthetic['expected_hits']:,}")
        assert "3 + 1 trailer" in check_line(totals, "records")
        assert "12 stream frames, 1 other, 0 out of bounds" in check_line(totals, "events")
        assert "Hz" in check_line(totals, "hit rate")
        assert "fADC250" in check_line(totals, "modules") and "DCRB" in check_line(totals, "modules")
        assert check_line(totals, "decode errors").endswith("0")

        checks = plain(app, "overview-checks")
        assert "warn" in check_line(checks, "missing frames")
        assert "info" in check_line(checks, "AIS entries identical")
        assert "ok" in check_line(checks, "trailer")

        standout = plain(app, "overview-standout")
        assert "missing frames" in standout and "duplicate frames" in standout
        assert "AIS entries identical" not in standout  # info checks stay in the checks table, as in the web viewer
        assert "all checks pass" not in standout
        assert "all checks pass" in OverviewPane.standout_markup([{"name": "x", "status": "ok", "value": 0, "detail": ""}])

        facts = plain(app, "overview-file")
        assert "little-endian" in check_line(facts, "byte order")
        assert "3 scanned" in check_line(facts, "records")
        assert "present" in check_line(facts, "trailer")
        assert "every record header read cleanly" in check_line(facts, "record scan")

        rocs = plain(app, "overview-rocs")
        assert "fADC250" in check_line(rocs, " 7 ") and f"{synthetic['expected_hits']:,}" in check_line(rocs, " 7 ")
        assert "DCRB" in check_line(rocs, " 44 ")


async def test_overview_says_analyzing_before_stats_and_no_file_after_closing(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot_wait(pilot, lambda: app.overview.summary is not None, what="stats_ready")
        summary = app.overview.summary

        # The hooks are the contract; drive them directly so the job thread's timing does not matter.
        app.overview.file_closed()
        await pilot.pause()
        assert "No file open" in plain(app, "overview-state") and plain(app, "overview-totals") == ""
        app.overview.file_opened()
        await pilot.pause()
        assert "Analyzing synthetic.evio" in plain(app, "overview-state") and plain(app, "overview-checks") == ""
        app.overview.stats_ready(summary)
        await pilot.pause()
        assert "synthetic.evio" in plain(app, "overview-state") and "fADC250 hits" in plain(app, "overview-totals")


async def test_opening_an_unreadable_file_keeps_the_current_overview(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot_wait(pilot, lambda: app.overview.summary is not None, what="stats_ready")
        app.open_path(synthetic["garbage"])
        await pilot.pause()
        await pilot_wait(pilot, lambda: app.overview.summary is not None, what="stats_ready after the failed open")
        assert session.path == synthetic["path"].resolve()
        assert "synthetic.evio" in plain(app, "overview-state")
        assert f"{synthetic['expected_hits']:,}" in plain(app, "overview-totals")


def test_scan_checks_in_words_and_value_formatting():
    assert OverviewPane.scan_check_words(["truncated_tail", 4096, 1024, 7]) == \
        "truncated tail: the last record declares 4,096 B but 1,024 B are present; 7 complete events kept"
    assert OverviewPane.scan_check_words(["bad_record_header", 0x1000, "bad magic"]) == \
        "bad record header at 0x1000: the scan stopped there (bad magic)"
    assert fmt("n/a") == "n/a" and fmt(None) == "—" and fmt(True) == "yes" and fmt([]) == "none"
    assert fmt(12345) == "12,345" and fmt(2.5) == "2.5" and fmt([1, 3]) == "1, 3"
