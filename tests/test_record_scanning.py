import struct

import pytest

from pyevio.evio_file import EvioFile

MAGIC = 0xc0da0100
LAST_RECORD_BIT = 1 << 9


def make_event(n_payload_words, tag=0xFF60):
    """A bank: length word, tag/type/num word, then payload words."""
    words = [n_payload_words + 1, (tag << 16) | (0x10 << 8)] + list(range(n_payload_words))
    return struct.pack("<%dI" % len(words), *words)


def make_record(number, events, last, with_index):
    index = struct.pack("<%dI" % len(events), *(len(e) for e in events)) if with_index else b""
    data = b"".join(events)
    bit_info = LAST_RECORD_BIT if last else 0
    length_words = 14 + (len(index) + len(data)) // 4
    header = struct.pack("<14I", length_words, number, 14, len(events), len(index),
                         bit_info | 6, 0, MAGIC, len(data), 0, 0, 0, 0, 0)
    return header + index + data


def make_trailer(number, records, with_index):
    """A trailer record (header species 3): its index lists (length, event count) per record."""
    index = b""
    if with_index:
        for record in records:
            length_words, _, _, events = struct.unpack("<4I", record[:16])
            index += struct.pack("<II", length_words * 4, events)
    header = struct.pack("<14I", 14 + len(index) // 4, number, 14, 0, len(index),
                         (3 << 28) | LAST_RECORD_BIT | 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    return header + index


def write_file(path, records, trailer=b""):
    header = struct.pack("<14I", 0x4556494F, 1, 14, len(records), 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path.write_bytes(header + b"".join(records) + trailer)
    return str(path)


def event_lengths(evio_file):
    return [[e.length for e in evio_file.get_record(i).get_events()]
            for i in range(evio_file.record_count)]


def test_standard_file(tmp_path):
    records = [make_record(1, [make_event(2), make_event(3)], last=False, with_index=True),
               make_record(2, [make_event(1)], last=True, with_index=True)]
    with EvioFile(write_file(tmp_path / "std.evio", records)) as f:
        assert event_lengths(f) == [[16, 20], [12]]


def test_last_record_bit_on_every_record(tmp_path):
    """CODA streaming files set the last-record bit on every record."""
    records = [make_record(i, [make_event(i)], last=True, with_index=True) for i in range(1, 4)]
    with EvioFile(write_file(tmp_path / "every_last.evio", records)) as f:
        assert f.record_count == 3


def test_stops_at_last_record_before_non_record_data(tmp_path):
    records = [make_record(1, [make_event(1)], last=True, with_index=True)]
    with EvioFile(write_file(tmp_path / "trailer.evio", records, trailer=b"\0" * 64)) as f:
        assert f.record_count == 1


@pytest.mark.parametrize("with_index", [False, True])
def test_trailer_is_not_a_data_record(tmp_path, with_index):
    """The trailer is exposed as `trailer`; its index never yields events (F3)."""
    records = [make_record(1, [make_event(2), make_event(3)], last=False, with_index=True),
               make_record(2, [make_event(1)], last=False, with_index=True)]
    trailer = make_trailer(3, records, with_index)
    with EvioFile(write_file(tmp_path / "trailer.evio", records, trailer)) as f:
        assert f.record_count == 2
        assert f.get_total_event_count() == 3
        assert event_lengths(f) == [[16, 20], [12]]
        assert f.trailer is not None
        assert f.trailer.is_trailer
        assert f.trailer.offset == 56 + sum(len(r) for r in records)
        assert f.trailer.header.index_array_length == (16 if with_index else 0)
        assert f.trailer.scan_events() == []
        assert f.trailer.get_events() == []


def test_file_with_only_a_trailer(tmp_path):
    trailer = make_trailer(1, [], with_index=False)
    with EvioFile(write_file(tmp_path / "empty.evio", [], trailer)) as f:
        assert f.record_count == 0
        assert f.get_total_event_count() == 0
        assert f.trailer is not None and f.trailer.event_count == 0


def test_event_count_bounded_by_index_and_data_end(tmp_path):
    """Header event count and index size disagree: the smaller wins; a length past the record stops the walk."""
    events = [make_event(2), make_event(3)]
    index = struct.pack("<3I", 16, 20, 400)  # three entries, the last past the record
    data = b"".join(events)
    header = struct.pack("<14I", 14 + (len(index) + len(data)) // 4, 1, 14, 5, len(index),
                         LAST_RECORD_BIT | 6, 0, MAGIC, len(data), 0, 0, 0, 0, 0)
    with EvioFile(write_file(tmp_path / "bounds.evio", [header + index + data])) as f:
        record = f.get_record(0)
        assert record.scan_events() == [(56 + 56 + 12, 16), (56 + 56 + 12 + 16, 20)]
        assert len(record.errors) == 1 and "declares 400 bytes" in record.errors[0]


def test_file_without_file_header_names_its_version(tmp_path):
    """A bare v4 block chain is refused with the version in the message (F9).

    Word 5 carries the v4 last-block bit (0x204, as the evio writers set it);
    an all-zero bit-info would be the SRO hardware shape, which pyevio reads.
    """
    block = struct.pack("<8I", 8, 1, 8, 0, 0, 0x204, 0, MAGIC)
    path = tmp_path / "v4.evio"
    path.write_bytes(block)
    with pytest.raises(ValueError, match=r"version 4") as raised:
        EvioFile(str(path))
    assert "evio" in str(raised.value).lower()


def test_hipo_file_type_id_in_ascii_byte_order(tmp_path):
    """HIPO writers store the type id bytes "HIPO" in order whatever the byte order (F39)."""
    records = [make_record(1, [make_event(1)], last=True, with_index=True)]
    header = b"HIPO" + struct.pack("<13I", 1, 14, 1, 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path = tmp_path / "hipo.evio"
    path.write_bytes(header + b"".join(records))
    with EvioFile(str(path)) as f:
        assert f.header.file_type == "HIPO"
        assert f.header.endian == "<"
        assert event_lengths(f) == [[12]]


@pytest.mark.parametrize("last", [False, True])
def test_records_without_index_array(tmp_path, last):
    records = [make_record(1, [make_event(2), make_event(0), make_event(5)], last=last, with_index=False),
               make_record(2, [make_event(1)], last=True, with_index=False)]
    with EvioFile(write_file(tmp_path / "no_index.evio", records)) as f:
        assert event_lengths(f) == [[16, 8, 28], [12]]
