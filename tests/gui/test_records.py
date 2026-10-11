"""The Records pane: one page of record rows, the trailer row, Enter to the Structure tab, paging."""
import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Static, TabbedContent  # noqa: E402

from conftest import pilot_wait  # noqa: E402
from pyevio.gui.app import PyEvioGui  # noqa: E402
from pyevio.gui.records import PAGE_SIZE, RecordsTable  # noqa: E402
from pyevio.gui.session import Session  # noqa: E402
from test_stream_stats import write_synthetic_file  # noqa: E402


def status(app) -> str:
    return str(app.query_one("#records-placeholder", Static).render())


def cell_texts(table: RecordsTable, row_key: str) -> list:
    return [str(cell) for cell in table.get_row(row_key)]


async def focus_table(app, pilot) -> RecordsTable:
    await pilot.press("3")
    await pilot.pause()
    table = app.query_one("#records-table", RecordsTable)
    app.set_focus(table)
    await pilot.pause()
    return table


async def test_records_table_lists_the_records_and_the_trailer(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(160, 50)) as pilot:
        table = app.query_one("#records-table", RecordsTable)
        await pilot_wait(pilot, lambda: table.row_count == 4, what="first page")
        assert table.cursor_type == "row"
        assert "rows 0—3 of 4 records" in status(app)
        assert [column.label.plain for column in table.columns.values()][:5] == ["#", "offset", "size (B)", "events decl / found", "type"]

        row0 = cell_texts(table, "r0")
        assert row0[0] == "0" and row0[1] == "0x00000038" and row0[3] == "4 / 4" and row0[4] == "0" and row0[5] == "none"  # header type 0 in the synthetic records
        assert row0[6] == "—" and row0[8] == "—" and row0[9] == "—" and row0[10] == "—"
        assert cell_texts(table, "r1")[3] == "5 / 5"  # record 1 has the extra non-stream event
        trailer = cell_texts(table, "t")
        assert trailer[0] == "3" and trailer[4] == "trailer" and trailer[6] == "set" and trailer[3] == "0 / 0"

        # The whole page is in the table before the analysis is necessarily done; the keys are node ids.
        assert [key.value for key in table.rows] == ["r0", "r1", "r2", "t"]

        app.records.file_closed()
        await pilot.pause()
        assert table.row_count == 0 and "No file open" in status(app)


async def test_enter_opens_the_record_in_the_structure_tab(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(160, 50)) as pilot:
        table = app.query_one("#records-table", RecordsTable)
        await pilot_wait(pilot, lambda: table.row_count == 4, what="first page")
        table = await focus_table(app, pilot)
        assert app.focused is table
        table.move_cursor(row=2)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == "tab-structure"
        assert "'r2'" in str(app.query_one("#structure-placeholder", Static).render())


async def test_paging_through_a_file_of_more_than_one_page(tmp_path):
    path, _, _ = write_synthetic_file(tmp_path / "long.evio", n_records=600, frames_per_record=1)
    session = Session(cache_dir=tmp_path / "cache", workers=1)
    try:
        app = PyEvioGui(session, start_path=path)
        async with app.run_test(size=(160, 50)) as pilot:
            table = app.query_one("#records-table", RecordsTable)
            await pilot_wait(pilot, lambda: table.row_count == PAGE_SIZE, what="first page")
            assert app.records.total == 601 and app.records.start == 0
            assert "rows 0—499 of 601 records" in status(app) and "] next page" in status(app)
            assert "trailer" not in status(app)  # the trailer is on the second page

            table = await focus_table(app, pilot)
            # Down at the last row of the page loads the next page with the cursor on its first row.
            table.move_cursor(row=PAGE_SIZE - 1)
            await pilot.pause()
            await pilot.press("down")
            await pilot_wait(pilot, lambda: app.records.start == PAGE_SIZE, what="next page by cursor")
            assert table.row_count == 101 and table.cursor_row == 0
            assert "rows 500—600 of 601 records (the trailer is the last row)" in status(app)
            assert cell_texts(table, "r500")[0] == "500" and cell_texts(table, "t")[4] == "trailer"
            assert [key.value for key in table.rows][:2] == ["r500", "r501"]

            # Down at the very last row stays put; up at the first row goes back with the cursor on the last row.
            table.move_cursor(row=100)
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert app.records.start == PAGE_SIZE and table.cursor_row == 100
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("up")
            await pilot_wait(pilot, lambda: app.records.start == 0, what="previous page by cursor")
            assert table.row_count == PAGE_SIZE and table.cursor_row == PAGE_SIZE - 1

            # The bracket keys page explicitly.
            await pilot.press("right_square_bracket")
            await pilot_wait(pilot, lambda: app.records.start == PAGE_SIZE, what="next page by key")
            await pilot.press("right_square_bracket")  # no page after the last one
            await pilot.pause()
            assert app.records.start == PAGE_SIZE
            await pilot.press("left_square_bracket")
            await pilot_wait(pilot, lambda: app.records.start == 0, what="previous page by key")
            assert table.cursor_row == 0 and "rows 0—499 of 601 records" in status(app)
            await pilot.press("left_square_bracket")  # no page before the first one
            await pilot.pause()
            assert app.records.start == 0
    finally:
        try:
            session.close()
        except BufferError:
            pass
