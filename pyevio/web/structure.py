"""Tree nodes, record pages and raw words, read through pyevio core.

Node ids are strings the viewer passes back unchanged:

- ``""`` — the file; children are the data records ``r0, r1, …`` and the trailer ``t``.
- ``r12`` — record 12; children are its events ``r12/e0, …``.
- ``r12/e3`` — event 3 of record 12, the top-level bank; children are its banks.
- ``r12/e3/b1a0/b1b8`` — a bank by byte offset (hex) below its parent; children are
  its banks, or its hit words when it is an fADC250 payload bank of a stream frame.

Every offset in an id is checked against the children pyevio found for the
parent, so an id cannot make the server read an arbitrary byte range. Children
lists are paged (``start``, ``count``) because a record can hold thousands of
events and a file thousands of records.

Offsets of events and banks index the record's buffer (``Record.buffer``): the
file itself for a plain record, the decompressed bytes for an LZ4 or gzip
record. Record offsets are file offsets. ``words_at`` reads either, by record.

Format knowledge stays in core and the decoder: this module only names what
``Bank`` and ``fadc250_stream`` return.
"""
import re
from typing import List, Optional

import numpy as np

from pyevio.bank import Bank
from pyevio.decoders import fadc250_stream as fs
from pyevio.evio_file import EvioFile
from pyevio.record import Record
from pyevio.web import ApiError

MAX_WORDS = 4096
MAX_HITS = 1024
DEFAULT_CHILDREN = 500
MAX_CHILDREN = 5000
MAX_NODE_DEPTH = 32
NODE_PART = re.compile(r"^(r\d+|e\d+|b[0-9a-f]+|t)$")
COMPRESSION_NAMES = {Record.COMPRESSION_NONE: "none", Record.COMPRESSION_LZ4: "lz4", Record.COMPRESSION_LZ4_BEST: "lz4_best",
                     Record.COMPRESSION_GZIP: "gzip"}


def page(items: list, start: int, count: int) -> dict:
    count = max(0, min(count, MAX_CHILDREN))
    start = max(0, start)
    return {"total": len(items), "start": start, "children": items[start:start + count]}


# ---------------------------------------------------------------- records
def describe_record(record: Record, index: int, evio_file: Optional[EvioFile] = None) -> dict:
    """Record header fields plus the events found and the walk errors; ``index`` is the viewer's row number.

    ``header_quirk`` is what core knows about a malformed header at this offset
    (``EvioFile.record_header_problem``, when the installed core has it), else None.
    """
    header = record.header
    record.scan_events()  # fills record.errors
    header_problem = getattr(evio_file, "record_header_problem", None)
    return {
        "header_quirk": header_problem(record.offset) if header_problem else None,
        "id": "t" if record.is_trailer else f"r{index}", "kind": "trailer" if record.is_trailer else "record",
        "index": index, "offset": record.offset, "size": record.size, "record_number": header.record_number,
        "header_length": header.header_length, "header_type": header.header_type, "is_trailer": record.is_trailer,
        "event_count": header.event_count, "events_found": len(record.scan_events()),
        "index_array_length": header.index_array_length, "user_header_length": header.user_header_length,
        "compression_type": header.compression_type, "compression": COMPRESSION_NAMES.get(header.compression_type, str(header.compression_type)),
        "compressed_length": header.compressed_data_length,
        "uncompressed_length": header.uncompressed_data_length, "version": header.version,
        "is_truncated": record.is_truncated, "declared_end": record.declared_end,
        "bit_info": {"raw": header.bit_info, "has_dictionary": header.has_dictionary, "is_last_record": header.is_last_record,
                     "has_first_event": header.has_first_event, "event_type": header.event_type,
                     "pad": [header.pad1, header.pad2, header.pad3]},
        "errors": list(record.errors), "has_children": header.event_count > 0 and not record.is_trailer,
    }


def record_rows(evio_file: EvioFile, start: int, count: int) -> dict:
    """Rows ``start … start+count`` of the record table; the trailer is the last row."""
    total = evio_file.record_count + (1 if evio_file.trailer is not None else 0)
    start = max(0, start)
    count = max(0, min(count, MAX_CHILDREN))
    rows = []
    for index in range(start, min(start + count, total)):
        if index < evio_file.record_count:
            record = evio_file.get_record(index)
            rows.append(describe_record(record, index, evio_file))
            record.release()  # a compressed record's buffer is not needed once its header row is built
        else:
            rows.append(describe_record(evio_file.trailer, index, evio_file))
    return {"total": total, "start": start, "rows": rows}


# ---------------------------------------------------------------- banks
def describe_bank(bank: Bank, node_id: str, role: Optional[str] = None) -> dict:
    data = {
        "id": node_id, "kind": "bank", "structure": bank.STRUCTURE_NAME, "tag": bank.tag, "data_type": bank.data_type,
        "num": bank.num, "pad": bank.pad, "length": bank.length, "offset": bank.offset, "size": bank.size, "type_name": bank.type_name,
        "is_container": bank.is_container(), "has_children": bank.is_container() and bank.data_length > 0,
    }
    if role:
        data["role"] = role
    return data


# Segments inside the stream-info banks of a frame; the AIS (0x42) is displayed as the file has it, never decoded.
INFO_SEGMENT_ROLES = {fs.SIB_TSS_SEGMENT_TAG: "time_slice", fs.SIB_AIS_SEGMENT_TAG: "aggregation_info",
                      fs.ROC_TSS_SEGMENT_TAG: "time_slice", fs.ROC_PAYLOAD_PORT_SEGMENT_TAG: "payload_map"}


def payload_map_rows(evio_file: EvioFile, segment: Bank) -> List[dict]:
    """The decoded entries of a 0x41 payload-port segment, zero entries included, from the decoder."""
    words = fs.event_words(segment.mm, segment.data_offset, segment.data_length, evio_file.header.endian).tolist()
    return [{"port": port, "module_id": module, "module": fs.MODULE_NAMES.get(module, str(module))}
            for port, module in fs.payload_map_entries(words, segment.pad or 0, evio_file.header.endian == ">")]


def _event_bank(evio_file: EvioFile, record_index: int, event_index: int) -> Bank:
    record = evio_file.get_record(record_index)
    offsets = record.get_event_offsets()
    if event_index >= len(offsets):
        raise ApiError(404, f"record {record_index} has {len(offsets)} events, no event {event_index}")
    offset, length = offsets[event_index]
    return Bank(record.buffer, offset, evio_file.header.endian, limit=offset + length)


def _child_at(parent: Bank, offset: int) -> Bank:
    for child in parent.get_children():
        if child.offset == offset:
            return child
    raise ApiError(404, f"no bank at offset 0x{offset:X} inside the bank at 0x{parent.offset:X}")


def _payload_labels(evio_file: EvioFile, event: Bank) -> dict:
    """``{(roc, stream, port): (role, module id)}`` for the payload banks of one stream frame, from the decoder."""
    words = fs.event_words(event.mm, event.offset, event.size, evio_file.header.endian)
    frame = fs.decode_frame(words)
    del words  # a view of the buffer; dropped before EvioFile.close()
    labels = {}
    for row in frame.empty_banks:
        labels[(int(row["roc"]), int(row["stream"]), int(row["port"]))] = ("payload_empty", int(row["module_id"]))
    for row in frame.non_fadc_banks:
        labels[(int(row["roc"]), int(row["stream"]), int(row["port"]))] = ("payload_words", int(row["module_id"]))
    if frame.n_hits:
        keys = np.unique(np.stack([frame.roc, frame.stream, frame.port], axis=1).astype(np.int64), axis=0)
        for roc, stream, port in keys.tolist():
            labels[(roc, stream, port)] = ("payload_hits", fs.MODULE_FADC250)
    return labels


def _bank_children(evio_file: EvioFile, chain: List[Bank], node_id: str) -> List[dict]:
    """Children of the last bank in ``chain`` with their roles inside a stream frame (if the event is one)."""
    event, bank = chain[0], chain[-1]
    children = []
    in_stream_frame = event.tag == fs.STREAM_EVENT_TAG
    labels = _payload_labels(evio_file, event) if (in_stream_frame and len(chain) == 2 and bank.tag != fs.SIB_TAG) else None
    info_segments = in_stream_frame and len(chain) in (2, 3) and bank.tag in (fs.SIB_TAG, fs.ROC_STREAM_INFO_TAG)
    for child in bank.get_children():
        role = None
        if in_stream_frame and len(chain) == 1:
            role = "stream_info" if child.tag == fs.SIB_TAG else "roc_bank"
        elif labels is not None:
            role = "roc_stream_info" if child.tag == fs.ROC_STREAM_INFO_TAG else "payload"
        elif info_segments:
            role = INFO_SEGMENT_ROLES.get(child.tag)
        described = describe_bank(child, f"{node_id}/b{child.offset:x}", role)
        if role == "payload_map":
            described["entries"] = payload_map_rows(evio_file, child)
        if role == "roc_bank":
            described.update({"roc": child.tag, "stream": (child.num or 0) & 0xF})
        if role == "payload":
            label, module = labels.get((bank.tag, (bank.num or 0) & 0xF, child.tag), ("payload_unmapped", fs.MODULE_UNKNOWN))
            n_words = child.data_length // 4
            described.update({"port": child.tag, "n_words": n_words, "module": fs.MODULE_NAMES.get(module, str(module)),
                              "role": label, "has_children": label == "payload_hits" and n_words > 0})
        children.append(described)
    return children


def _hit_children(evio_file: EvioFile, bank: Bank, node_id: str) -> List[dict]:
    """Decoded hit words of an fADC250 payload bank, capped at MAX_HITS."""
    words = fs.event_words(bank.mm, bank.data_offset, bank.data_length, evio_file.header.endian).tolist()  # a copy, no live view
    channel, time_raw, charge = (column.tolist() for column in fs.unpack_hit_words(np.asarray(words, dtype=np.uint32)))
    return [{"id": f"{node_id}/h{i}", "kind": "hit", "index": i, "word": f"{word:08x}", "channel": channel[i],
             "time_raw": time_raw[i], "t_ns": time_raw[i] * fs.HIT_TIME_TICK_NS, "charge": charge[i],
             "flag": bool(word >> fs.HIT_FLAG_BIT), "has_children": False}
            for i, word in enumerate(words)]


# ---------------------------------------------------------------- nodes
def _parse_node(node: str) -> List[str]:
    parts = [part for part in node.strip("/").split("/") if part] if node else []
    if len(parts) > MAX_NODE_DEPTH or any(not NODE_PART.match(part) for part in parts):
        raise ApiError(400, f"malformed node id {node!r}")
    return parts


def children_of(evio_file: EvioFile, node: str, start: int = 0, count: int = DEFAULT_CHILDREN) -> dict:
    """The node's own description and a page of its children (see the module docstring for the id scheme)."""
    parts = _parse_node(node)
    if not parts:
        header = evio_file.header
        node_info = {"id": "", "kind": "file", "size": evio_file.file_size, "header_layout": evio_file.header_layout,
                     "record_count": evio_file.record_count, "has_trailer": evio_file.trailer is not None,
                     "endian": header.endian, "version": header.version, "file_type": header.file_type}
        rows = record_rows(evio_file, start, count)
        return {"total": rows["total"], "start": rows["start"], "children": rows["rows"], "node": node_info, "errors": []}

    if parts[0] == "t":
        if evio_file.trailer is None or len(parts) > 1:
            raise ApiError(404, "the file has no trailer" if evio_file.trailer is None else "a trailer has no children")
        result = page([], start, count)
        result.update({"node": describe_record(evio_file.trailer, evio_file.record_count, evio_file), "errors": []})
        return result

    record_index = int(parts[0][1:])
    if record_index >= evio_file.record_count:
        raise ApiError(404, f"record {record_index} does not exist ({evio_file.record_count} records)")
    record = evio_file.get_record(record_index)
    if len(parts) == 1:
        events = []
        buffer = record.buffer
        for event_i, (offset, length) in enumerate(record.get_event_offsets()):
            bank = Bank(buffer, offset, evio_file.header.endian, limit=offset + length)
            described = describe_bank(bank, f"r{record_index}/e{event_i}")
            described.update({"kind": "event", "index": event_i, "length_bytes": length, "has_children": bank.is_container(),
                              "compressed": record.compression_type != record.COMPRESSION_NONE})
            events.append(described)
        result = page(events, start, count)
        result.update({"node": describe_record(record, record_index, evio_file), "errors": list(record.errors)})
        return result

    if not parts[1].startswith("e"):
        raise ApiError(400, f"expected an event id after {parts[0]!r}")
    chain = [_event_bank(evio_file, record_index, int(parts[1][1:]))]
    for part in parts[2:]:
        if not part.startswith("b"):
            raise ApiError(400, f"expected a bank id, found {part!r}")
        chain.append(_child_at(chain[-1], int(part[1:], 16)))
    bank = chain[-1]
    node_id = "/".join(parts)
    if bank.is_container():
        children = _bank_children(evio_file, chain, node_id)
    elif len(chain) == 3 and chain[0].tag == fs.STREAM_EVENT_TAG and chain[1].tag != fs.SIB_TAG and bank.data_type == 0:
        labels = _payload_labels(evio_file, chain[0])
        label, _ = labels.get((chain[1].tag, (chain[1].num or 0) & 0xF, bank.tag), ("payload_unmapped", fs.MODULE_UNKNOWN))
        children = _hit_children(evio_file, bank, node_id) if label == "payload_hits" else []
    else:
        children = []
    node_info = describe_bank(bank, node_id)
    if len(chain) == 1:
        node_info.update({"kind": "event", "index": int(parts[1][1:])})
    result = page(children, start, count)
    result.update({"node": node_info, "errors": list(bank.errors)})
    return result


# ---------------------------------------------------------------- raw words
def words_at(evio_file: EvioFile, offset: int, count: int, record_index: Optional[int] = None) -> dict:
    """``count`` 32-bit words from byte ``offset`` as hex strings in the file's byte order, clipped to the buffer.

    Without ``record_index`` the offset is a file offset (record headers, events of
    plain records). With it, the offset indexes that record's buffer, which is the
    only way to see the events of an LZ4 or gzip record; for a plain record both
    views agree.
    """
    if offset < 0 or offset % 4:
        raise ApiError(400, f"offset must be a non-negative multiple of 4, got {offset}")
    if record_index is None:
        buffer, source = evio_file.mm, "file"
    else:
        if record_index >= evio_file.record_count:
            raise ApiError(404, f"record {record_index} does not exist ({evio_file.record_count} records)")
        buffer, source = evio_file.get_record(record_index).buffer, f"record {record_index}"
    if offset >= len(buffer):
        raise ApiError(404, f"offset {offset} is past the end of the {source} buffer ({len(buffer)} bytes)")
    count = max(0, min(count, MAX_WORDS, (len(buffer) - offset) // 4))
    words = np.frombuffer(buffer, dtype=np.dtype(evio_file.header.endian + "u4"), count=count, offset=offset).tolist()
    return {"offset": offset, "count": count, "record": record_index, "endian": evio_file.header.endian,
            "max_count": MAX_WORDS, "words": [f"{word:08x}" for word in words]}
