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


@pytest.mark.parametrize("last", [False, True])
def test_records_without_index_array(tmp_path, last):
    records = [make_record(1, [make_event(2), make_event(0), make_event(5)], last=last, with_index=False),
               make_record(2, [make_event(1)], last=True, with_index=False)]
    with EvioFile(write_file(tmp_path / "no_index.evio", records)) as f:
        assert event_lengths(f) == [[16, 8, 28], [12]]
