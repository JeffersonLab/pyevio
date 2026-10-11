"""Records tab: a paged table of the record headers; Enter opens the record in the Structure tab."""
from textual.widgets import Static

from pyevio.gui.panes import Pane


class RecordsPane(Pane):
    def compose(self):
        yield Static("Open a file to list its records.", id="records-placeholder")

    def file_opened(self) -> None:
        page = self.session.records(0, 1)
        self.query_one("#records-placeholder", Static).update(f"{page['total']:,} records")
