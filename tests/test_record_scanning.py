import struct
import zlib

import lz4.block
import numpy as np
import pytest

from pyevio.evio_file import EvioFile

MAGIC = 0xc0da0100
LAST_RECORD_BIT = 1 << 9


def make_event(n_payload_words, tag=0xFF60):
    """A bank: length word, tag/type/num word, then payload words."""
    words = [n_payload_words + 1, (tag << 16) | (0x10 << 8)] + list(range(n_payload_words))
    return struct.pack("<%dI" % len(words), *words)


def padded(data):
    """Pad bytes to whole words, as writers store user headers."""
    return data + bytes(-len(data) % 4)


def make_record(number, events, last, with_index, user_header=b"", compression=0):
    """A 14-word-header record; ``user_header`` may have an odd length (its padding is implicit).

    ``compression`` 1 (LZ4), 2 (LZ4_BEST) or 3 (gzip) compresses index + padded
    user header + events the way the evio writers do: the compressed length in
    word 10 counts whole words and pad3 (bits 24—25 of word 5) counts the
    filler bytes in the last word.
    """
    index = struct.pack("<%dI" % len(events), *(len(e) for e in events)) if with_index else b""
    data = b"".join(events)
    bit_info = LAST_RECORD_BIT if last else 0
    body = index + padded(user_header) + data
    compression_word = 0
    if compression in (1, 2):
        body = lz4.block.compress(body, store_size=False)
    elif compression == 3:
        body = zlib.compress(body, wbits=31)
    if compression:
        pad3 = -len(body) % 4
        body += bytes(pad3)
        bit_info |= pad3 << 24
        compression_word = (compression << 28) | (len(body) // 4)
    length_words = 14 + len(body) // 4
    header = struct.pack("<14I", length_words, number, 14, len(events), len(index),
                         bit_info | 6, len(user_header), MAGIC, len(data), compression_word, 0, 0, 0, 0)
    return header + body


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


def write_file(path, records, trailer=b"", user_header=b""):
    """A v6 file: 14-word header, optional (odd-length) user header, records, trailer bytes."""
    header = struct.pack("<14I", 0x4556494F, 1, 14, len(records), 0, 6, len(user_header), MAGIC, 0, 0, 0, 0, 0, 0)
    path.write_bytes(header + padded(user_header) + b"".join(records) + trailer)
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
    """The trailer is exposed as `trailer`; its index never yields events."""
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
    """A bare v4 block chain is refused with the version in the message.

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
    """HIPO writers store the type id bytes "HIPO" in order whatever the byte order."""
    records = [make_record(1, [make_event(1)], last=True, with_index=True)]
    header = b"HIPO" + struct.pack("<13I", 1, 14, 1, 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path = tmp_path / "hipo.evio"
    path.write_bytes(header + b"".join(records))
    with EvioFile(str(path)) as f:
        assert f.header.file_type == "HIPO"
        assert f.header.endian == "<"
        assert event_lengths(f) == [[12]]


def test_user_header_lengths_are_padded_to_words(tmp_path):
    """Odd user header lengths at file level (7) and record level (5) shift nothing."""
    records = [make_record(1, [make_event(2), make_event(3)], last=True, with_index=True, user_header=b"abcde")]
    with EvioFile(write_file(tmp_path / "padded.evio", records, user_header=b"1234567"), ) as f:
        assert f.first_record_offset == 56 + 8
        record = f.get_record(0)
        assert record.header.user_header_length == 5
        assert record.data_start == record.index_array_end + 8
        assert [e.length for e in record.get_events()] == [16, 20]
        assert [e.get_bank().length for e in record.get_events()] == [3, 4]
        assert record.errors == []


@pytest.mark.parametrize("compression", [1, 2, 3], ids=["lz4", "lz4best", "gzip"])
def test_compressed_record_events_are_read_from_the_decompressed_buffer(tmp_path, compression):
    """Events of an LZ4, LZ4_BEST or gzip record come from Record.buffer; offsets are into it."""
    events = [make_event(2), make_event(3), make_event(1)]
    records = [make_record(1, events, last=False, with_index=True, user_header=b"xyz", compression=compression),
               make_record(2, [make_event(4)], last=True, with_index=True)]
    with EvioFile(write_file(tmp_path / "compressed.evio", records)) as f:
        compressed = f.get_record(0)
        assert compressed.compression_type == compression
        assert compressed.buffer is not f.mm
        assert len(compressed.buffer) == 12 + 4 + sum(len(e) for e in events)
        assert compressed.scan_events() == [(16, 16), (32, 20), (52, 12)]
        assert [e.get_bank().get_data() for e in compressed.get_events()] == [
            struct.pack("<II", 0, 1), struct.pack("<III", 0, 1, 2), struct.pack("<I", 0)]
        assert compressed.errors == []
        assert compressed.events_to_numpy(signature=0xFF60).tolist() == [
            3, 0xFF601000, 0, 1, 4, 0xFF601000, 0, 1, 2, 2, 0xFF601000, 0]

        plain = f.get_record(1)
        assert plain.buffer is f.mm
        assert event_lengths(f)[1] == [24]

        compressed.release()
        assert compressed.scan_events() == [(16, 16), (32, 20), (52, 12)]


def test_truncated_final_record_keeps_complete_events(tmp_path):
    """A final record cut by EOF keeps its complete events and becomes a check."""
    events = [make_event(2), make_event(3), make_event(5)]
    whole = make_record(2, events, last=False, with_index=False)
    cut = whole[:len(whole) - 12]  # the last event loses its tail
    records = [make_record(1, [make_event(1)], last=False, with_index=True)]
    path = tmp_path / "truncated.evio"
    write_file(path, records)
    with open(path, "ab") as f:
        f.write(cut)
    with EvioFile(str(path)) as f:
        assert f.record_count == 2
        tail = f.get_record(1)
        assert tail.is_truncated
        assert [e.length for e in tail.get_events()] == [16, 20]
        assert len(tail.errors) == 1 and "declares 28 bytes" in tail.errors[0]
        assert f.checks == [("truncated_tail", len(whole), len(cut), 2)]
        assert f.get_total_event_count() == 3  # 1 + the 2 salvaged, although the tail header declares 3
        assert f.get_record_and_event(2)[1].length == 20
        with pytest.raises(IndexError):
            f.get_record_and_event(3)


def test_record_with_zero_header_length_ends_the_scan(tmp_path):
    """A header declaring 0 words cannot be stepped over; the scan stops with a check."""
    stuck = struct.pack("<14I", 0, 2, 0, 0, 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    records = [make_record(1, [make_event(1)], last=False, with_index=True)]
    with EvioFile(write_file(tmp_path / "stuck.evio", records, trailer=stuck)) as f:
        assert f.record_count == 1
        assert f.checks[0][:2] == ("bad_record_header", 56 + len(records[0]))


def test_garbage_after_a_record_ends_the_scan(tmp_path):
    """Bytes that are no record header stop the record scan with a check."""
    records = [make_record(1, [make_event(1)], last=False, with_index=True)]
    with EvioFile(write_file(tmp_path / "garbage.evio", records, trailer=b"\xff" * 40)) as f:
        assert f.record_count == 1
        assert len(f.checks) == 1
        assert f.checks[0][:2] == ("bad_record_header", 56 + len(records[0]))


def test_root_bank_never_reads_past_its_event(tmp_path):
    """An event whose bank length word overruns the event is clamped and reported."""
    event = bytearray(make_event(2))
    struct.pack_into("<I", event, 0, 1000)
    records = [make_record(1, [bytes(event), make_event(1)], last=True, with_index=True)]
    with EvioFile(write_file(tmp_path / "overrun.evio", records)) as f:
        first, second = f.get_record(0).get_events()
        bank = first.get_bank()
        assert bank.length == 1000
        assert bank.end_offset == first.end_offset
        assert bank.data_length == 8
        assert bank.get_data() == struct.pack("<II", 0, 1)
        assert bank.get_children() == []  # a bank of banks whose payload words are no bank header
        assert len(bank.errors) == 2 and "declares 1000 words" in bank.errors[0]
        assert second.get_bank().errors == []


def test_close_releases_mmap_and_file(tmp_path):
    """After the with block both the mmap and the file are closed."""
    records = [make_record(1, [make_event(1)], last=True, with_index=True)]
    with EvioFile(write_file(tmp_path / "close.evio", records)) as f:
        f.get_record(0).get_events()
    assert f.mm.closed
    assert f.file.closed
    f.close()  # a second close is harmless


def test_close_with_live_view_raises_and_still_closes_the_file(tmp_path):
    """A numpy view of the mmap makes __exit__ raise BufferError naming the cause; the file closes anyway."""
    records = [make_record(1, [make_event(1)], last=True, with_index=True)]
    with pytest.raises(BufferError, match="numpy array or memoryview"):
        with EvioFile(write_file(tmp_path / "view.evio", records)) as f:
            view = np.frombuffer(f.mm, dtype=np.uint8, count=8)
    assert f.file.closed
    assert not f.mm.closed
    del view
    f.close()
    assert f.mm.closed


@pytest.mark.parametrize("last", [False, True])
def test_records_without_index_array(tmp_path, last):
    records = [make_record(1, [make_event(2), make_event(0), make_event(5)], last=last, with_index=False),
               make_record(2, [make_event(1)], last=True, with_index=False)]
    with EvioFile(write_file(tmp_path / "no_index.evio", records)) as f:
        assert event_lengths(f) == [[16, 8, 28], [12]]
