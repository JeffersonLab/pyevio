"""Structure tab: the lazy tree file → records → events → banks → hit words, with header fields and raw words."""
from textual.widgets import Static

from pyevio.gui.panes import Pane


class StructurePane(Pane):
    def compose(self):
        yield Static("Open a file to browse its structure.", id="structure-placeholder")

    def file_opened(self) -> None:
        self.show_node("")

    def show_node(self, node_id: str) -> None:
        """Select the node (``""`` the file, ``r3`` a record, ``r3/e1`` an event, …) and show its fields and words."""
        node = self.session.children(node_id, 0, 1)
        self.query_one("#structure-placeholder", Static).update(f"node {node_id!r}: {node['total']:,} children")
