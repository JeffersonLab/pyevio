"""Structure tab: the lazy tree file → records → events → banks → hit words, with header fields and raw words.

The tree on the left is a Textual ``Tree`` whose nodes carry one dict each: the
node dict the session returned (``pyevio.web.structure.children_of``) plus the
paging state of its children and, for events and banks, the index and
compression of the record they live in, so their words are read from that
record's buffer (``words_at``). Children load one page of 500 when a node is
expanded; a page that does not reach the end ends with a "… N more" child whose
selection loads the next page in place.

The panel on the right shows the highlighted node: its fields, its errors, the
decoded payload-port map when it has one, and a window of its raw words (``n`` /
``p`` move the window). Nothing here holds a ``Record``, ``Bank`` or a view of the
file; everything runs on the main thread.
"""
from typing import List, Optional

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode

from pyevio.decoders import fadc250_stream as fs
from pyevio.gui.panes import Pane
from pyevio.web import ApiError

PAGE_SIZE = 500
HEX_WINDOW = 64
# What a session call can raise: ApiError from the model, ValueError/OSError from core on malformed data,
# BufferError when the file is closing. Each is reported in the status strip; none takes the app down.
PANE_ERRORS = (ApiError, ValueError, OSError, BufferError)
HEX_PER_LINE = 8
KIND_MORE = "more"  # the "… N more" child; its data["id"] is the parent's id

# The same role names as the browser's tree.js.
ROLE_LABELS = {
    "stream_info": "stream info", "roc_bank": "ROC bank", "roc_stream_info": "ROC stream info", "payload_hits": "payload",
    "payload_empty": "payload (empty)", "payload_words": "payload", "payload_unmapped": "payload (unmapped port)",
    "time_slice": "time slice", "aggregation_info": "aggregation info (AIS)", "payload_map": "payload-port map",
}
# Fields shown elsewhere in the panel (path line, bit-info rows, errors, entries table) or of no interest.
HIDDEN_FIELDS = {"id", "has_children", "entries", "bit_info", "errors"}
HEX_FIELDS = {"tag", "offset", "declared_end"}


def hex_value(value: int, digits: int = 4) -> str:
    return f"0x{value:0{digits}X}"


# ---------------------------------------------------------------- labels
def node_label(node: dict) -> str:
    """One tree line for a node dict, labeled by kind and role as the browser does."""
    kind = node.get("kind")
    if kind == "file":
        trailer = " + trailer" if node.get("has_trailer") else ""
        return f"file — {node.get('header_layout')}, {node.get('record_count')} records{trailer}"
    if kind == "trailer":
        return f"trailer — {node.get('size', 0):,} B at {hex_value(node.get('offset', 0), 8)}"
    if kind == "record":
        return f"record {node['index']} — {record_meta(node)}"
    if kind == "event":
        tag = node.get("tag", 0)
        name = "stream frame" if tag == fs.STREAM_EVENT_TAG else f"type {hex_value(node.get('data_type', 0), 2)} num {node.get('num')}"
        return f"event {node['index']} — tag {hex_value(tag)} {name}, {node.get('length', 0) + 1} words"
    if kind == "hit":
        flag = "  FLAG" if node.get("flag") else ""
        return f"hit {node['index']} — ch {node['channel']}  t {node['t_ns']} ns  q {node['charge']}  {node['word']}{flag}"
    if kind == KIND_MORE:
        return f"… {node.get('remaining', 0):,} more (press Enter to load)"
    return f"{bank_name(node)} — {bank_meta(node)}"


def record_meta(node: dict) -> str:
    parts = [f"{node.get('event_count', 0)} events", f"{node.get('size', 0):,} B"]
    if node.get("compression") not in (None, "none"):
        parts.append(node["compression"])
    if node.get("is_truncated"):
        parts.append("TRUNCATED")
    if node.get("errors"):
        parts.append(f"{len(node['errors'])} errors")
    return ", ".join(parts)


def bank_name(node: dict) -> str:
    role = ROLE_LABELS.get(node.get("role"))
    if node.get("role") == "roc_bank":
        return f"{role} {node.get('roc')} stream {node.get('stream')}"
    if "port" in node:
        return f"{role} port {node['port']}"
    return role or f"{node.get('structure', 'bank')} {hex_value(node.get('tag', 0))}"


def bank_meta(node: dict) -> str:
    parts = [f"tag {hex_value(node.get('tag', 0))}", f"type {hex_value(node.get('data_type', 0), 2)}"]
    if node.get("num") is not None:
        parts.append(f"num {node['num']}")
    parts.append(f"{node.get('length', 0)} words")
    if node.get("module"):
        parts.append(node["module"])
    if node.get("role") == "payload_hits" and "n_words" in node:
        parts.append(f"{node['n_words']} hits")
    if node.get("entries"):
        parts.append(f"{len(node['entries'])} entries")
    return "  ".join(parts)


def field_text(name: str, value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, int):
        if name in HEX_FIELDS:
            return f"{hex_value(value)} ({value:,})"
        if name == "data_type":
            return hex_value(value, 2)
        return f"{value:,}"
    return str(value)


def fields_lines(node: dict) -> List[str]:
    """``name  value`` rows for the fields table; bit-info fields are prefixed ``bit_info.``."""
    rows = [(name, field_text(name, value)) for name, value in node.items() if name not in HIDDEN_FIELDS and not name.startswith("_")]
    for name, value in (node.get("bit_info") or {}).items():
        rows.append((f"bit_info.{name}", field_text(name, value)))
    width = max((len(name) for name, _ in rows), default=0)
    return [f"{name:<{width}}  {value}" for name, value in rows]


def entries_lines(entries: List[dict]) -> List[str]:
    lines = ["payload-port map", f"{'entry':>5}  {'port':>4}  {'module id':>9}  module"]
    lines += [f"{i:>5}  {entry['port']:>4}  {entry['module_id']:>9}  {entry['module']}" for i, entry in enumerate(entries)]
    return lines


def hex_lines(words: List[str], base_offset: int) -> List[str]:
    """``HEX_PER_LINE`` words per line, the line's byte offset first."""
    lines = []
    for start in range(0, len(words), HEX_PER_LINE):
        lines.append(f"{hex_value(base_offset + start * 4, 8)}  " + " ".join(words[start:start + HEX_PER_LINE]))
    return lines


def words_request(data: dict) -> Optional[dict]:
    """Which words a node has: record headers at file offsets, events and banks inside their record's buffer; hits and the file none."""
    kind = data.get("kind")
    if kind in ("file", "hit", KIND_MORE) or data.get("offset") is None:
        return None
    total_words = int(data.get("size", 0)) // 4
    if kind in ("record", "trailer"):
        return {"offset": data["offset"], "record": None, "total_words": total_words, "compressed": False}
    return {"offset": data["offset"], "record": data.get("_record"), "total_words": total_words,
            "compressed": data.get("_compression") not in (None, "none")}


# ---------------------------------------------------------------- the pane
class DetailPanel(VerticalScroll, can_focus=False):
    """The scrolling detail column. Not focusable on purpose: when the tab switches away while the Tree has the
    focus, Textual blurs the hidden Tree onto a focusable sibling, and a programmatic focus inside a TabPane
    re-activates that tab (``TabbedContent._on_tab_pane_focused``), so the number keys could never leave the
    Structure tab. With no focusable sibling the focus is simply dropped. The mouse wheel still scrolls it."""


class StructurePane(Pane):
    """The structure tree and the detail panel; see the module docstring."""

    BINDINGS = [
        Binding("n", "next_words", "Next words"),
        Binding("p", "previous_words", "Previous words"),
    ]
    DEFAULT_CSS = """
    StructurePane Horizontal {
        height: 1fr;
    }
    StructurePane #structure-tree {
        width: 45%;
        height: 1fr;
        border-right: solid $panel;
    }
    StructurePane #structure-detail {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    StructurePane #structure-placeholder {
        text-style: bold;
    }
    StructurePane #structure-errors {
        color: $error;
    }
    StructurePane #structure-hex-title {
        margin-top: 1;
        text-style: bold;
    }
    StructurePane #structure-hex-note {
        color: $text-muted;
    }
    """

    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self._tree: Tree = Tree("no file open", data={"id": "", "kind": "file", "has_children": False, "_loaded": 0, "_total": 0}, id="structure-tree")
        self._tree.root.allow_expand = False
        self._generation = 0  # bumped on every rebuild; TreeNode ids restart from 0 after Tree.clear()
        self._shown: Optional[tuple] = None  # (generation, TreeNode.id) of the node the panel shows
        self.detail: Optional[dict] = None  # the node dict the panel shows (tests read it)
        self.words_page: Optional[dict] = None  # the last words_at dict (tests read it)
        self._hex: Optional[dict] = None  # the words request of the shown node plus "start", the window's first word

    def compose(self):
        with Horizontal():
            yield self._tree
            with DetailPanel(id="structure-detail"):
                yield Static("Open a file to browse its structure.", id="structure-placeholder")
                yield Static("", id="structure-fields", markup=False)
                yield Static("", id="structure-errors", markup=False)
                yield Static("", id="structure-entries", markup=False)
                yield Static("", id="structure-hex-title", markup=False)
                yield Static("", id="structure-hex", markup=False)
                yield Static("", id="structure-hex-note", markup=False)

    # ---------------------------------------------------------------- pane hooks
    def file_closed(self) -> None:
        super().file_closed()
        self._generation += 1
        self._shown = None
        self.detail = self.words_page = self._hex = None
        self._tree.reset("no file open", {"id": "", "kind": "file", "has_children": False, "_loaded": 0, "_total": 0})
        self._tree.root.allow_expand = False
        self._clear_panel("Open a file to browse its structure.")

    def file_opened(self) -> None:
        self._generation += 1
        self._shown = None
        self._tree.reset("file", {"id": "", "kind": "file", "has_children": True, "_loaded": 0, "_total": None})
        self._tree.root.allow_expand = True
        try:
            self._load_page(self._tree.root)
        except PANE_ERRORS as error:
            self._report(error)
            return
        self._tree.root.expand()
        self._build_lines()
        self._tree.move_cursor(self._tree.root)
        self._show_detail(self._tree.root)

    # ---------------------------------------------------------------- public
    def show_node(self, node_id: str) -> None:
        """Expand the path to the node (``""`` the file, ``r3`` a record, ``r3/e1`` an event, …), select it and show it."""
        try:
            target = self._walk_to(node_id)
        except PANE_ERRORS as error:
            self._report(error)
            return
        if target is None:
            return
        if target.allow_expand and not target.is_expanded:
            target.expand()
        self._build_lines()
        self._tree.move_cursor(target)
        self._tree.scroll_to_node(target, animate=False)
        self._show_detail(target)
        self._tree.focus()

    def _build_lines(self) -> None:
        """Lay the tree lines out now: a node added or expanded this turn has no line (``_line == -1``) until the
        tree rebuilds, and ``move_cursor``/``scroll_to_node`` read it directly. Textual 8.2 rebuilds lazily on idle
        through this private property; nothing public triggers it synchronously."""
        self._tree._tree_lines  # noqa: B018

    # ---------------------------------------------------------------- tree events
    def on_show(self) -> None:
        self._tree.focus()  # the tab became active: arrows, Enter, n and p go to the tree without a Tab first

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        event.stop()
        data = event.node.data
        if data is None or data.get("_total") is not None or not self.session.is_open:
            return
        try:
            self._load_page(event.node)
        except PANE_ERRORS as error:
            self._report(error)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is None or event.node.data.get("kind") != KIND_MORE or event.node.parent is None:
            return
        event.stop()
        try:
            self._load_page(event.node.parent)
        except PANE_ERRORS as error:
            self._report(error)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        event.stop()
        try:
            self._show_detail(event.node)
        except PANE_ERRORS as error:
            self._report(error)

    # ---------------------------------------------------------------- words window
    def action_next_words(self) -> None:
        self._move_window(HEX_WINDOW)

    def action_previous_words(self) -> None:
        self._move_window(-HEX_WINDOW)

    def _move_window(self, step: int) -> None:
        if self._hex is None:
            return
        start = max(0, min(self._hex["start"] + step, max(0, self._hex["total_words"] - 1)))
        if start == self._hex["start"]:
            return
        self._hex["start"] = start
        try:
            self._show_words()
        except PANE_ERRORS as error:
            self._report(error)

    # ---------------------------------------------------------------- loading
    def _load_page(self, tree_node: TreeNode) -> None:
        """Append the next page of children to ``tree_node``, replacing its "more" child; raises ApiError."""
        data = tree_node.data
        start = data.get("_loaded", 0)
        page = self.session.children(data["id"], start, PAGE_SIZE)
        more = tree_node.children[-1] if tree_node.children and (tree_node.children[-1].data or {}).get("kind") == KIND_MORE else None
        if more is not None:
            more.remove()
        if start == 0:
            if data["kind"] == "file":
                data.update(page["node"])
                tree_node.set_label(node_label(data))
            if page.get("errors") is not None and data["kind"] != "file":
                data["errors"] = list(page["errors"])  # the node's own walk errors (bank.errors, record.errors)
                if data["kind"] == "record":
                    tree_node.set_label(node_label(data))
        record = data.get("_record")
        compression = data.get("_compression")
        for child in page["children"]:
            child_data = dict(child)
            if child_data["kind"] == "record":
                child_data["_record"], child_data["_compression"] = child_data["index"], child_data.get("compression")
            elif child_data["kind"] != "trailer":
                child_data["_record"], child_data["_compression"] = record, compression
            child_data["_loaded"], child_data["_total"] = 0, None if child_data.get("has_children") else 0
            tree_node.add(node_label(child_data), data=child_data, allow_expand=bool(child_data.get("has_children")))
        data["_loaded"] = start + len(page["children"])
        data["_total"] = page["total"]
        if data["_total"] > data["_loaded"]:
            remaining = data["_total"] - data["_loaded"]
            tree_node.add(node_label({"kind": KIND_MORE, "remaining": remaining}),
                          data={"id": data["id"], "kind": KIND_MORE, "has_children": False, "remaining": remaining}, allow_expand=False)
        if self._shown == (self._generation, tree_node.id):
            self._show_detail(tree_node, force=True)  # the errors arrived with the page

    def _walk_to(self, node_id: str) -> Optional[TreeNode]:
        """The tree node for ``node_id``, loading pages and expanding ancestors on the way; None (reported) when absent."""
        if not self.session.is_open:
            self._report("no file is open")
            return None
        root = self._tree.root
        if root.data.get("_total") is None:
            self._load_page(root)
        parts = [part for part in node_id.strip("/").split("/") if part]
        node = root
        for depth, _ in enumerate(parts):
            target_id = "/".join(parts[:depth + 1])
            if node.data.get("_total") is None:
                self._load_page(node)
            child = self._find_child(node, target_id)
            while child is None and node.data["_total"] > node.data["_loaded"]:
                self._load_page(node)
                child = self._find_child(node, target_id)
            if child is None:
                self._report(f"no node {target_id!r} in the tree")
                return None
            if not node.is_expanded:
                node.expand()
            node = child
        return node

    @staticmethod
    def _find_child(node: TreeNode, child_id: str) -> Optional[TreeNode]:
        for child in node.children:
            if child.data is not None and child.data.get("kind") != KIND_MORE and child.data.get("id") == child_id:
                return child
        return None

    # ---------------------------------------------------------------- detail panel
    def _show_detail(self, tree_node: TreeNode, force: bool = False) -> None:
        data = tree_node.data
        if data is None or (not force and self._shown == (self._generation, tree_node.id)):
            return
        self._shown = (self._generation, tree_node.id)
        if data.get("kind") == KIND_MORE:
            self.detail = None
            self._hex = None
            self._clear_panel(f"{data['remaining']:,} more children of {data['id'] or 'file'!r} — press Enter to load them")
            return
        self.detail = data
        self.query_one("#structure-placeholder", Static).update(f"node {data.get('id', '')!r}" if data.get("id") else "node '' (file)")
        self.query_one("#structure-fields", Static).update("\n".join(fields_lines(data)))
        errors = data.get("errors") or []
        self.query_one("#structure-errors", Static).update(Text("\n".join(errors), style="bold red") if errors else "")
        self.query_one("#structure-entries", Static).update("\n".join(entries_lines(data["entries"])) if data.get("entries") else "")
        request = words_request(data)
        self._hex = dict(request, start=0) if request and request["total_words"] > 0 else None
        if self._hex is None:
            self.words_page = None
            self.query_one("#structure-hex-title", Static).update("")
            self.query_one("#structure-hex", Static).update("")
            self.query_one("#structure-hex-note", Static).update("")
            return
        try:
            self._show_words()
        except PANE_ERRORS as error:
            self.query_one("#structure-hex", Static).update("")
            self.query_one("#structure-hex-note", Static).update(self._error_text(error))
            self._report(error)

    def _show_words(self) -> None:
        """Fetch and show the current window of the shown node's words; raises ApiError."""
        request = self._hex
        start = request["start"]
        count = min(HEX_WINDOW, request["total_words"] - start)
        page = self.session.words(request["offset"] + start * 4, count, request["record"])
        self.words_page = page
        self.query_one("#structure-hex-title", Static).update("raw words")
        self.query_one("#structure-hex", Static).update("\n".join(hex_lines(page["words"], page["offset"])))
        endian = "little" if page["endian"] == "<" else "big"
        where = "in the file" if request["record"] is None else f"in the buffer of record {request['record']}"
        note = f"words {start:,}–{start + page['count'] - 1:,} of {request['total_words']:,}, {endian}-endian, offsets {where}"
        if request["compressed"]:
            note += "; offsets index the decompressed record buffer"
        note += "  (n: next 64 words, p: previous)"
        self.query_one("#structure-hex-note", Static).update(note)

    def _clear_panel(self, heading: str) -> None:
        self.query_one("#structure-placeholder", Static).update(heading)
        for widget_id in ("fields", "errors", "entries", "hex-title", "hex", "hex-note"):
            self.query_one(f"#structure-{widget_id}", Static).update("")

    @staticmethod
    def _error_text(error) -> str:
        if isinstance(error, ApiError):
            return error.message
        if isinstance(error, str):
            return error
        return f"{type(error).__name__}: {error}"

    def _report(self, error) -> None:
        """Show an error (an exception or a message) in the app's status strip; never raise."""
        message = self._error_text(error)
        show_error = getattr(self.app, "show_error", None)
        if show_error is not None:
            show_error(message)
        else:
            self.notify(message, severity="error")
