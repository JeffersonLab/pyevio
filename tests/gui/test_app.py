"""The app skeleton: open on start, progress to done, tabs, the open screen, errors."""
import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input, Static, TabbedContent  # noqa: E402

from conftest import pilot_wait  # noqa: E402
from pyevio.gui.app import OpenScreen, PyEvioGui, StatusStrip  # noqa: E402
from pyevio.gui.overview import OverviewPane  # noqa: E402
from pyevio.gui.records import RecordsPane  # noqa: E402
from pyevio.gui.structure import StructurePane  # noqa: E402


def status_text(app):
    return str(app.query_one("#status-text", Static).render())


def error_text(app):
    return str(app.query_one("#status-error", Static).render())


async def test_start_with_a_file_runs_the_analysis_and_fills_the_panes(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot_wait(pilot, lambda: app.query_one(OverviewPane).summary is not None, what="stats_ready")
        assert "analysis done" in status_text(app) and "3 records" in status_text(app)
        assert app.query_one(OverviewPane).summary["totals"]["n_hits"] == synthetic["expected_hits"]
        assert "4 records" in str(app.query_one("#records-placeholder", Static).render())
        assert error_text(app) == ""
        assert app.sub_title == str(synthetic["path"].resolve())


async def test_number_keys_switch_tabs(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot_wait(pilot, lambda: session.state == "done", what="analysis")
        tabs = app.query_one("#tabs", TabbedContent)
        for key, tab in (("3", "tab-records"), ("4", "tab-structure"), ("2", "tab-plots"), ("1", "tab-overview")):
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == tab
        app.show_structure_node("r1")
        await pilot.pause()
        assert tabs.active == "tab-structure" and "'r1'" in str(app.query_one("#structure-placeholder", Static).render())


async def test_open_screen_opens_a_typed_path_and_an_unreadable_file_keeps_the_current_one(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot_wait(pilot, lambda: session.state == "done", what="analysis")
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, OpenScreen)
        field = app.screen.query_one("#open-path", Input)
        field.value = str(synthetic["garbage"])
        await pilot.press("enter")
        await pilot_wait(pilot, lambda: not isinstance(app.screen, OpenScreen), what="open screen to close")
        assert "garbage.evio" in error_text(app)
        assert session.path == synthetic["path"].resolve() and session.state == "done"
        assert app.query_one(RecordsPane) is not None and "4 records" in str(app.query_one("#records-placeholder", Static).render())

        # Escape cancels without touching the session.
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, OpenScreen) and session.path == synthetic["path"].resolve()


async def test_start_without_a_file_shows_the_open_screen(session, synthetic):
    app = PyEvioGui(session, start_dir=synthetic["folder"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OpenScreen)
        assert "No file open" in status_text(app)
        app.screen.query_one("#open-path", Input).value = str(synthetic["path"])
        await pilot.press("enter")
        await pilot_wait(pilot, lambda: app.query_one(StructurePane).query_one("#structure-placeholder", Static).render() is not None
                         and session.is_open, what="file to open")
        await pilot_wait(pilot, lambda: app.query_one(OverviewPane).summary is not None, what="stats_ready")
        assert "analysis done" in status_text(app)
