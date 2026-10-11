"""Records tab: a paged table of the record headers; Enter opens the record in the Structure tab.

The table holds one page of ``PAGE_SIZE`` rows of ``session.records(start, count)``
at a time, so a file of a million records costs one page of strings. The row
key is the record's node id (``r12``, ``t`` for the trailer), which is all the
Structure tab needs. Paging: ``]`` and ``[`` replace the page; moving the cursor
down past the last row (down, PageDown) loads the next page with the cursor on
its first row, and up past the first row loads the previous page with the cursor
on its last row.
"""
from typing import Optional

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable, Static

from pyevio.gui.panes import Pane

PAGE_SIZE = 500
COLUMNS = ("#", "offset", "size (B)", "events decl / found", "type", "compression", "last-record bit", "index",
           "truncated", "header quirk", "errors")
NO_FILE_TEXT = "No file open."


def row_cells(row: dict) -> list:
    """The cells of one ``describe_record`` dict; the problem cells are coloured. Every cell is a Text, never markup."""
    bad, warn = "red", "yellow"
    events = Text(f"{row['event_count']} / {row['events_found']}", style=bad if row["events_found"] != row["event_count"] else "")
    kind = "trailer" if row["is_trailer"] else str(row["header_type"])
    errors = Text("—")
    if row["errors"]:
        errors = Text(f"{len(row['errors'])}: {row['errors'][0]}", style=bad)
    return [
        Text(str(row["index"]), justify="right"),
        Text(f"0x{row['offset']:08X}"),
        Text(f"{row['size']:,}", justify="right"),
        events,
        Text(kind),
        Text(row["compression"]),
        Text("set" if row["bit_info"]["is_last_record"] else "—"),
        Text(f"{row['index_array_length']} B" if row["index_array_length"] else "none"),
        Text("yes", style=bad) if row["is_truncated"] else Text("—"),
        Text(str(row["header_quirk"]), style=warn) if row["header_quirk"] else Text("—"),
        errors,
    ]


class RecordsTable(DataTable):
    """A row-cursor table that asks its pane for the next or previous page at the edges."""

    DEFAULT_CSS = """
    RecordsTable {
        height: 1fr;
    }
    """

    class PageRequested(Message):
        """The cursor tried to leave the page: ``step`` is +1 (next page) or -1 (previous page)."""

        def __init__(self, step: int) -> None:
            super().__init__()
            self.step = step

    def action_cursor_down(self) -> None:
        if self.row_count and self.cursor_row >= self.row_count - 1:
            self.post_message(self.PageRequested(+1))
        else:
            super().action_cursor_down()

    def action_page_down(self) -> None:
        if self.row_count and self.cursor_row >= self.row_count - 1:
            self.post_message(self.PageRequested(+1))
        else:
            super().action_page_down()

    def action_cursor_up(self) -> None:
        if self.row_count and self.cursor_row <= 0:
            self.post_message(self.PageRequested(-1))
        else:
            super().action_cursor_up()

    def action_page_up(self) -> None:
        if self.row_count and self.cursor_row <= 0:
            self.post_message(self.PageRequested(-1))
        else:
            super().action_page_up()


class RecordsPane(Pane):
    """See the module docstring; the hooks are those of :class:`~pyevio.gui.panes.Pane`."""

    BINDINGS = [
        Binding("]", "next_page", "Next page"),
        Binding("[", "previous_page", "Previous page"),
    ]
    DEFAULT_CSS = """
    RecordsPane #records-placeholder {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.start = 0  # index of the first row in the table
        self.loaded = 0  # rows in the table
        self.total = 0  # rows in the file (records + trailer)
        self.ends_with_trailer = False  # the last row of the page is the trailer

    def compose(self):
        # The status line keeps the id of the earlier placeholder so the app tests keep reading it.
        yield Static(NO_FILE_TEXT, id="records-placeholder", markup=False)
        yield RecordsTable(cursor_type="row", zebra_stripes=True, id="records-table")

    def on_mount(self) -> None:
        self._ensure_columns()

    def on_show(self) -> None:
        if self.session.is_open:
            self.table.focus()

    @property
    def table(self) -> RecordsTable:
        return self.query_one("#records-table", RecordsTable)

    def _ensure_columns(self) -> None:
        table = self.table
        if not table.columns:
            for column in COLUMNS:
                table.add_column(column, key=column)

    # ---------------------------------------------------------------- hooks
    def file_closed(self) -> None:
        super().file_closed()
        self.start = self.loaded = self.total = 0
        self.ends_with_trailer = False
        self.table.clear()
        self._set_status(NO_FILE_TEXT)

    def file_opened(self) -> None:
        self.load_page(0)

    # ---------------------------------------------------------------- paging
    def load_page(self, start: int, cursor_row: Optional[int] = 0) -> None:
        """Replace the table with the page at ``start``; ``cursor_row`` is where the cursor lands (None: the last row)."""
        self._ensure_columns()
        page = self.session.records(max(0, start), PAGE_SIZE)
        table = self.table
        table.clear()
        for row in page["rows"]:
            table.add_row(*row_cells(row), key=row["id"])
        self.start, self.loaded, self.total = page["start"], len(page["rows"]), page["total"]
        self.ends_with_trailer = bool(page["rows"]) and page["rows"][-1]["is_trailer"]
        if self.loaded:
            table.move_cursor(row=self.loaded - 1 if cursor_row is None else min(cursor_row, self.loaded - 1), animate=False)
        self._set_status(self.status_line())

    def status_line(self) -> str:
        if not self.total:
            return "0 records in this file"
        first, last = self.start, self.start + self.loaded - 1
        trailer = " (the trailer is the last row)" if self.ends_with_trailer else ""
        hint = "  ·  ] next page  [ previous page" if self.total > PAGE_SIZE else ""
        return f"rows {first}—{last} of {self.total:,} records{trailer}  ·  Enter opens the record{hint}"

    def _set_status(self, text: str) -> None:
        self.query_one("#records-placeholder", Static).update(text)

    @property
    def has_next_page(self) -> bool:
        return self.start + self.loaded < self.total

    @property
    def has_previous_page(self) -> bool:
        return self.start > 0

    def action_next_page(self) -> None:
        if self.has_next_page:
            self.load_page(self.start + self.loaded, cursor_row=0)

    def action_previous_page(self) -> None:
        if self.has_previous_page:
            self.load_page(self.start - PAGE_SIZE, cursor_row=0)

    def on_records_table_page_requested(self, event: RecordsTable.PageRequested) -> None:
        event.stop()
        if event.step > 0 and self.has_next_page:
            self.load_page(self.start + self.loaded, cursor_row=0)
        elif event.step < 0 and self.has_previous_page:
            self.load_page(self.start - PAGE_SIZE, cursor_row=None)

    # ---------------------------------------------------------------- selection
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        node_id = event.row_key.value
        if node_id:
            self.app.show_structure_node(node_id)
