"""The Structure tab: the lazy tree down to hit words, the detail panel with fields and raw words, paging and navigation."""
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Static, TabbedContent, Tree  # noqa: E402

from conftest import pilot_wait  # noqa: E402
from pyevio.decoders import fadc250_stream as fs  # noqa: E402
from pyevio.gui.app import PyEvioGui  # noqa: E402
from pyevio.gui.structure import StructurePane  # noqa: E402
from test_stream_stats import write_synthetic_file  # noqa: E402


def labels(node):
    return [child.label.plain for child in node.children]


def ids(node):
    return [child.data["id"] for child in node.children]


def child_by_id(node, child_id):
    return next(child for child in node.children if child.data.get("id") == child_id)


def panel_text(app, widget_id):
    content = app.query_one(f"#structure-{widget_id}", Static).content
    return content if isinstance(content, str) else content.plain


async def open_app(session, path):
    app = PyEvioGui(session, start_path=path)
    pilot_cm = app.run_test(size=(120, 40))
    pilot = await pilot_cm.__aenter__()
    pane = app.query_one(StructurePane)
    tree = app.query_one("#structure-tree", Tree)
    await pilot_wait(pilot, lambda: session.is_open and tree.root.data.get("_total") is not None, what="root page")
    return app, pilot, pilot_cm, pane, tree


async def expand_and_wait(pilot, node):
    """Expand a node and wait for its first page."""
    node.expand()
    await pilot_wait(pilot, lambda: node.data.get("_total") is not None, what=f"children of {node.data['id']!r}")
    await pilot.pause()


async def test_root_shows_the_records_and_the_trailer(session, synthetic):
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["path"])
    try:
        tabs = app.query_one("#tabs", TabbedContent)
        await pilot.press("4")
        await pilot.pause()
        assert app.focused is tree  # the tab activation focuses the tree: arrows, Enter, n and p work at once
        await pilot.press("2")  # and the number keys still leave the tab while the tree has the focus
        await pilot.pause()
        assert tabs.active == "tab-plots" and app.focused is not tree
        await pilot.press("4")
        await pilot.pause()
        assert tabs.active == "tab-structure" and app.focused is tree
        assert tree.root.label.plain.startswith("file — ") and "3 records + trailer" in tree.root.label.plain
        assert tree.root.is_expanded and ids(tree.root) == ["r0", "r1", "r2", "t"]
        assert labels(tree.root)[0].startswith("record 0 — 4 events, ") and labels(tree.root)[3].startswith("trailer — ")
        assert not tree.root.children[3].allow_expand and tree.root.children[0].allow_expand
        # The root is shown in the panel: its fields and no words (the file node has none).
        assert pane.detail["kind"] == "file" and "record_count   3\n" in panel_text(app, "fields") and pane.words_page is None
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_expanding_walks_record_frame_roc_payload_and_hits(session, synthetic):
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["path"])
    try:
        record = child_by_id(tree.root, "r1")
        await expand_and_wait(pilot, record)
        assert ids(record) == ["r1/e0", "r1/e1", "r1/e2", "r1/e3", "r1/e4"]
        event_labels = labels(record)
        assert event_labels[0].startswith("event 0 — tag 0xFF60 stream frame, ") and event_labels[0].endswith(" words")
        assert event_labels[4].startswith("event 4 — tag 0xFF50 type 0x10 num 0, ")
        assert all(child.data["_record"] == 1 and child.data["_compression"] == "none" for child in record.children)

        frame = record.children[0]
        await expand_and_wait(pilot, frame)
        frame_labels = labels(frame)
        assert frame_labels[0].startswith("stream info — tag 0xFF31")
        assert frame_labels[1].startswith("ROC bank 7 stream 1 — tag 0x0007")
        assert frame_labels[2].startswith("ROC bank 44 stream 2 — tag 0x002C")
        assert [child.data["role"] for child in frame.children] == ["stream_info", "roc_bank", "roc_bank"]

        roc_7 = frame.children[1]
        await expand_and_wait(pilot, roc_7)
        roc_labels = labels(roc_7)
        assert roc_labels[0].startswith("ROC stream info — tag 0xFF30")
        assert roc_labels[1].startswith("payload port 1 — ") and "fADC250" in roc_labels[1] and "16 hits" in roc_labels[1]
        assert roc_labels[3].startswith("payload (empty) port 5 — ")
        by_port = {child.data.get("port"): child for child in roc_7.children[1:]}
        assert by_port[1].allow_expand and not by_port[5].allow_expand

        payload = by_port[1]
        await expand_and_wait(pilot, payload)
        assert len(payload.children) == 16 and all(not child.allow_expand for child in payload.children)
        n = 1 * 4  # record 1, event 0 is frame index 4 of the synthetic file
        assert labels(payload)[3] == f"hit 3 — ch 3  t {(300 + n) * fs.HIT_TIME_TICK_NS} ns  q {10 + n}  {payload.children[3].data['word']}"
        assert [child.data["channel"] for child in payload.children] == list(range(16))

        # The ROC stream info carries the decoded payload-port map; the DCRB payload has words but no hit children.
        info = roc_7.children[0]
        await expand_and_wait(pilot, info)
        assert [child.data["role"] for child in info.children] == ["time_slice", "payload_map"]
        assert "3 entries" in labels(info)[1]
        dcrb = frame.children[2]
        await expand_and_wait(pilot, dcrb)
        dcrb_labels = labels(dcrb)
        assert any(label.startswith("payload port 4 — ") and "DCRB" in label for label in dcrb_labels)
        assert not next(child for child in dcrb.children if child.data.get("port") == 4).allow_expand
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_detail_panel_shows_fields_errors_and_words(session, synthetic):
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["path"])
    try:
        record = child_by_id(tree.root, "r1")
        await expand_and_wait(pilot, record)
        event = record.children[0]
        tree.move_cursor(event)
        await pilot.pause()
        assert pane.detail["id"] == "r1/e0"
        fields = panel_text(app, "fields")
        assert "tag           0xFF60 (65,376)" in fields and "kind          event" in fields and "structure     Bank" in fields
        assert panel_text(app, "placeholder") == "node 'r1/e0'"
        assert panel_text(app, "errors") == ""
        words = pane.words_page
        assert words["record"] == 1 and int(words["words"][0], 16) == event.data["length"] and words["count"] == 64
        hex_text = panel_text(app, "hex")
        assert hex_text.splitlines()[0].startswith(f"0x{event.data['offset']:08X}  {words['words'][0]} {words['words'][1]}")
        assert len(hex_text.splitlines()[0].split()) == 1 + 8
        note = panel_text(app, "hex-note")
        assert "words 0–63 of" in note and "record 1" in note and "decompressed" not in note

        # n / p move the window of 64 words while the tree has the focus.
        tree.focus()
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pane.words_page["offset"] == event.data["offset"] + 64 * 4 and "words 64–" in panel_text(app, "hex-note")
        await pilot.press("p")
        await pilot.pause()
        assert pane.words_page["offset"] == event.data["offset"]
        await pilot.press("p")
        await pilot.pause()
        assert pane.words_page["offset"] == event.data["offset"]  # clipped at the first word

        # A record node reads its header words at its file offset.
        tree.move_cursor(record)
        await pilot.pause()
        assert pane.detail["kind"] == "record" and pane.words_page["record"] is None and pane.words_page["offset"] == record.data["offset"]
        assert "bit_info.is_last_record" in panel_text(app, "fields") and "offsets in the file" in panel_text(app, "hex-note")

        # Hits have fields but no words.
        frame = record.children[0]
        await expand_and_wait(pilot, frame)
        roc_7 = frame.children[1]
        await expand_and_wait(pilot, roc_7)
        payload = roc_7.children[1]
        await expand_and_wait(pilot, payload)
        tree.move_cursor(payload.children[0])
        await pilot.pause()
        assert pane.detail["kind"] == "hit" and pane.words_page is None and "channel" in panel_text(app, "fields") and panel_text(app, "hex") == ""
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_show_node_expands_the_path_and_selects_the_node(session, synthetic):
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["path"])
    try:
        app.show_structure_node("r1/e2")
        await pilot.pause()
        record = child_by_id(tree.root, "r1")
        assert record.is_expanded and tree.cursor_node is child_by_id(record, "r1/e2")
        assert pane.detail["id"] == "r1/e2" and pane.detail["index"] == 2
        assert panel_text(app, "placeholder") == "node 'r1/e2'"
        await pilot_wait(pilot, lambda: child_by_id(record, "r1/e2").data.get("_total") is not None, what="event children")
        assert ids(child_by_id(record, "r1/e2"))[0] == "r1/e2/b" + format(child_by_id(record, "r1/e2").children[0].data["offset"], "x")

        pane.show_node("r1/e2/" + ids(child_by_id(record, "r1/e2"))[1].split("/")[-1])
        await pilot.pause()
        assert pane.detail["role"] == "roc_bank" and pane.detail["roc"] == 7 and tree.cursor_node.data["id"] == pane.detail["id"]

        pane.show_node("t")
        await pilot.pause()
        assert pane.detail["kind"] == "trailer" and tree.cursor_node is tree.root.children[3]

        pane.show_node("")
        await pilot.pause()
        assert pane.detail["kind"] == "file" and tree.cursor_node is tree.root

        # An unknown node reports an error and keeps the selection.
        pane.show_node("r7")
        await pilot.pause()
        assert "r7" in str(app.query_one("#status-error", Static).render()) and tree.cursor_node is tree.root
    finally:
        await pilot_cm.__aexit__(None, None, None)


@pytest.fixture
def many_records(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    path, _, _ = write_synthetic_file(folder / "many.evio", n_records=600, frames_per_record=1)
    return Path(path)


async def test_more_child_pages_the_root_and_show_node_pages_until_present(session, many_records):
    app, pilot, pilot_cm, pane, tree = await open_app(session, many_records)
    try:
        assert len(tree.root.children) == 501 and tree.root.data["_total"] == 601
        more = tree.root.children[-1]
        assert more.label.plain == "… 101 more (press Enter to load)" and more.data["kind"] == "more" and not more.allow_expand
        tree.focus()
        await pilot.pause()
        tree.move_cursor(more)
        await pilot.pause()
        assert "101 more" in panel_text(app, "placeholder")
        await pilot.press("enter")
        await pilot.pause()
        assert len(tree.root.children) == 601 and ids(tree.root)[-1] == "t" and tree.root.children[-1].data["kind"] == "trailer"
        assert tree.cursor_node.data["id"] == "r500"  # the cursor lands on the first row of the new page
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_show_node_loads_root_pages_until_the_record_is_present(session, many_records):
    app, pilot, pilot_cm, pane, tree = await open_app(session, many_records)
    try:
        assert len(tree.root.children) == 501
        app.show_structure_node("r550/e0")
        await pilot.pause()
        assert len(tree.root.children) == 601
        record = child_by_id(tree.root, "r550")
        assert record.is_expanded and tree.cursor_node.data["id"] == "r550/e0" and pane.detail["id"] == "r550/e0"
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_lz4_record_words_come_from_the_decompressed_buffer(session, synthetic):
    if synthetic["golden_lz4"] is None:
        pytest.skip("golden set absent")
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["golden_lz4"])
    try:
        record = next(child for child in tree.root.children if child.data["kind"] == "record" and child.data["compression_type"] != 0)
        assert record.data["compression"] in record.label.plain
        pane.show_node(f"{record.data['id']}/e0")
        await pilot.pause()
        event = pane.detail
        assert event["kind"] == "event" and event["compressed"] and pane.words_page["record"] == record.data["index"]
        assert int(pane.words_page["words"][0], 16) == event["length"]
        assert "offsets index the decompressed record buffer" in panel_text(app, "hex-note")
        await pilot_wait(pilot, lambda: tree.cursor_node.data.get("_total") is not None, what="event children")
        assert all(child.data["structure"] in ("Bank", "Segment", "TagSegment") for child in tree.cursor_node.children)
    finally:
        await pilot_cm.__aexit__(None, None, None)


async def test_opening_another_file_rebuilds_the_tree(session, synthetic, many_records):
    app, pilot, pilot_cm, pane, tree = await open_app(session, synthetic["path"])
    try:
        record = child_by_id(tree.root, "r1")
        await expand_and_wait(pilot, record)
        tree.move_cursor(record.children[0])
        await pilot.pause()
        assert pane.detail["id"] == "r1/e0"
        app.open_path(many_records)
        await pilot_wait(pilot, lambda: session.path == many_records.resolve() and tree.root.data.get("record_count") == 600, what="new file")
        await pilot.pause()
        assert "600 records" in tree.root.label.plain and len(tree.root.children) == 501
        assert pane.detail["kind"] == "file" and pane.detail["record_count"] == 600
        assert labels(tree.root)[0].startswith("record 0 — 1 events, ")
        # Garbage keeps the current file and its tree.
        app.open_path(synthetic["garbage"])
        await pilot.pause()
        assert "garbage.evio" in str(app.query_one("#status-error", Static).render())
        assert "600 records" in tree.root.label.plain and len(tree.root.children) == 501
    finally:
        await pilot_cm.__aexit__(None, None, None)
