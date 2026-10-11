"""SRO hardware stream layout: 8-word block headers, no file header, truncated tail (F2, F6, D4).

The JLab SRO writers produce EVIO v6 streaming content in EVIO4-style block
containers: block header ``{length, block number, 8, event count, 0, 4, 0,
0xc0da0100}`` with the version word exactly 4, no index array, no last-block
bit, and (on three of four files of run 791) a final block cut by the end of
the file. ``write_sro_file`` builds such files; the last tests read the real
file when it is present.
"""

import os
import struct
import time

import pytest

from pyevio.evio_file import EvioFile

MAGIC = 0xc0da0100
RAW_SRO_DIR = "/data/sro_boyarinov_data_2026"


def bank(tag, data_type, num, payload_words):
    """An EVIO bank as words: length (words after it), header word, payload."""
    return [len(payload_words) + 1, (tag << 16) | (data_type << 8) | num] + list(payload_words)


def segment(tag, data_type, payload_words):
    return [(tag << 24) | (data_type << 16) | len(payload_words)] + list(payload_words)


def stream_frame(frame_number, hits_per_port):
    """One 0xFF60 frame: SIB (TSS + AIS segments) and one ROC bank with payload banks."""
    sib = bank(0xFF31, 0x20, 0, segment(0x32, 1, [frame_number, frame_number * 65536, 0]) + segment(0x42, 1, [0x00070021] * 4))
    info = bank(0xFF30, 0x20, 0x21, segment(0x31, 1, [frame_number, frame_number * 65536, 0]) + segment(0x41, 5, [1 | (3 << 16)]))
    payloads = []
    for port, n_hits in hits_per_port:
        payloads += bank(port, 0x00, 0 if n_hits else 1, [0x1000 + i for i in range(n_hits)])
    roc = bank(7, 0x10, 0x21, info + payloads)
    return bank(0xFF60, 0x10, 0, sib + roc)


def sro_block(number, events, version_word=4):
    body = [w for event in events for w in event]
    header = [8 + len(body), number, 8, len(events), 0, version_word, 0, MAGIC]
    return struct.pack("<%dI" % (8 + len(body)), *(header + body))


def write_sro_file(path, blocks, drop_tail_bytes=0):
    """Blocks back to back, no file header; ``drop_tail_bytes`` cuts the last block short."""
    data = b"".join(blocks)
    if drop_tail_bytes:
        data = data[:len(data) - drop_tail_bytes]
    path.write_bytes(data)
    return str(path)


EVENTS_A = [stream_frame(0, [(1, 3), (3, 0)]), stream_frame(4, [(1, 1), (3, 2)])]
EVENTS_B = [stream_frame(8, [(1, 0), (3, 0)]), stream_frame(12, [(1, 5), (3, 1)]), stream_frame(16, [(1, 2), (3, 2)])]
EVENTS_C = [stream_frame(20, [(1, 4), (3, 4)]), stream_frame(24, [(1, 1), (3, 1)]), stream_frame(28, [(1, 2), (3, 0)])]


def test_sro_file_reads_as_8word_records(tmp_path):
    blocks = [sro_block(1, EVENTS_A), sro_block(2, EVENTS_B)]
    with EvioFile(write_sro_file(tmp_path / "sro.evio", blocks)) as f:
        assert f.header_layout == "sro-8word"
        assert f.header.layout == "sro-8word"
        assert f.header.endian == "<"
        assert (f.header.version, f.header.container_version, f.header.file_type) == (6, 4, "SRO")
        assert f.header.record_count == 2
        assert f.first_record_offset == 0
        assert f.trailer is None
        assert f.checks == []
        assert f.record_count == 2
        assert [f.get_record(i).header.header_length for i in range(2)] == [8, 8]
        assert [f.get_record(i).header.record_number for i in range(2)] == [1, 2]
        assert [f.get_record(i).event_count for i in range(2)] == [2, 3]
        assert f.get_total_event_count() == 5

        record = f.get_record(1)
        assert record.buffer is f.mm
        assert record.data_start == len(blocks[0]) + 32
        assert record.get_event_offsets() == [(record.data_start + sum(len(e) * 4 for e in EVENTS_B[:i]), len(EVENTS_B[i]) * 4)
                                              for i in range(3)]
        assert record.errors == []
        for event, words in zip(record.get_events(), EVENTS_B):
            root = event.get_bank()
            assert (root.tag, root.length) == (0xFF60, words[0])
            sib, roc = root.get_children()
            assert [s.tag for s in sib.get_children()] == [0x32, 0x42]
            assert [c.tag for c in roc.get_children()] == [0xFF30, 1, 3]
            assert root.errors == [] and sib.errors == [] and roc.errors == []


def test_sro_truncated_tail_keeps_complete_events(tmp_path):
    """A final block cut mid-event keeps its complete events; checks reports the bytes (D4)."""
    blocks = [sro_block(1, EVENTS_A), sro_block(2, EVENTS_B), sro_block(3, EVENTS_C)]
    cut = len(EVENTS_C[2]) * 4 - 8  # the third event loses all but its first two words
    with EvioFile(write_sro_file(tmp_path / "sro_tail.evio", blocks, drop_tail_bytes=cut)) as f:
        assert f.record_count == 3
        tail = f.get_record(2)
        assert tail.is_truncated
        assert tail.event_count == 3  # the header's claim
        assert len(tail.get_event_offsets()) == 2
        assert [e.get_bank().tag for e in tail.get_events()] == [0xFF60, 0xFF60]
        assert len(tail.errors) == 1 and "record data ends" in tail.errors[0]
        assert f.checks == [("truncated_tail", len(blocks[2]), len(blocks[2]) - cut, 2)]
        assert f.get_total_event_count() == 2 + 3 + 2
        assert f.get_record_and_event(6)[1].get_bank().get_children()[0].get_children()[0].to_numpy()[0] == 24
        with pytest.raises(IndexError):
            f.get_record_and_event(7)


def test_sro_tail_cut_inside_the_header(tmp_path):
    """Fewer than 8 words left after the last complete block: no record, one check."""
    blocks = [sro_block(1, EVENTS_A), sro_block(2, EVENTS_B)]
    data = b"".join(blocks) + struct.pack("<3I", 500, 3, 8)
    path = tmp_path / "sro_short.evio"
    path.write_bytes(data)
    with EvioFile(str(path)) as f:
        assert f.record_count == 2
        assert f.checks == [("bad_record_header", len(data) - 12, "12 bytes left, a record header needs 32")]


def test_byte_swapped_block_is_named_in_the_check(tmp_path):
    """A block in the other byte order is no record for this file; the check says so."""
    swapped = sro_block(2, EVENTS_B)
    swapped = b"".join(swapped[i:i + 4][::-1] for i in range(0, len(swapped), 4))
    with EvioFile(write_sro_file(tmp_path / "sro_swapped.evio", [sro_block(1, EVENTS_A), swapped])) as f:
        assert f.record_count == 1
        assert "byte-swapped record header" in f.checks[0][2]


def test_v6_record_stream_without_file_header(tmp_path):
    """A buffer dump (14-word v6 record headers, no file header) reads as layout "v6-records"."""
    event = struct.pack("<4I", 3, 0xFF601000, 1, 2)
    record = struct.pack("<14I", 14 + 1 + 4, 1, 14, 1, 4, 6, 0, MAGIC, 16, 0, 0, 0, 0, 0) + struct.pack("<I", 16) + event
    path = tmp_path / "buffer.dump"
    path.write_bytes(record + record)
    with EvioFile(str(path)) as f:
        assert f.header_layout == "v6-records"
        assert (f.header.layout, f.header.version, f.header.container_version, f.header.file_type) == (
            "v6-records", 6, 6, "EVIO records")
        assert f.first_record_offset == 0
        assert f.record_count == 2 and f.header.record_count == 2
        assert f.trailer is None and f.checks == []
        assert [[e.length for e in f.get_record(i).get_events()] for i in range(2)] == [[16], [16]]


def test_info_command_prints_layout_checks_and_counts(tmp_path):
    """`pyevio info` on an SRO file shows the layout, the truncated-tail check and the record count."""
    from click.testing import CliRunner
    from pyevio.cli.info import info_command

    blocks = [sro_block(1, EVENTS_A), sro_block(2, EVENTS_B), sro_block(3, EVENTS_C)]
    cut = len(EVENTS_C[2]) * 4 - 8
    path = write_sro_file(tmp_path / "sro_tail.evio", blocks, drop_tail_bytes=cut)
    result = CliRunner().invoke(info_command, [path], obj={})
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # rich wraps long lines at the terminal width
    assert "sro-8word" in out
    assert "Total Records: 3" in out
    assert "Total Events: 7" in out
    assert "truncated_tail" in out
    assert f"declared {len(blocks[2])} bytes, {len(blocks[2]) - cut} present, 2 complete events kept" in out

    with EvioFile(path) as f:
        assert f.record_header_problem(0) is None
        assert f.record_header_problem(4) is not None


def test_garbage_between_blocks_ends_the_scan(tmp_path):
    """Bytes without the magic word end the scan with a check (F6)."""
    blocks = [sro_block(1, EVENTS_A), b"\xee" * 64, sro_block(2, EVENTS_B)]
    with EvioFile(write_sro_file(tmp_path / "sro_garbage.evio", blocks)) as f:
        assert f.record_count == 1
        assert f.checks[0][:2] == ("bad_record_header", len(blocks[0]))
        assert "no magic number" in f.checks[0][2]


def test_second_file_header_ends_the_scan(tmp_path):
    """Two v6 files concatenated: the second file header is no record (F6)."""
    event = struct.pack("<4I", 3, 0xFF601000, 1, 2)
    record = struct.pack("<14I", 14 + 1 + 4, 1, 14, 1, 4, 6, 0, MAGIC, 16, 0, 0, 0, 0, 0) + struct.pack("<I", 16) + event
    file_header = struct.pack("<14I", 0x4556494F, 1, 14, 1, 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path = tmp_path / "twice.evio"
    path.write_bytes(file_header + record + file_header + record)
    with EvioFile(str(path)) as f:
        assert f.record_count == 1
        assert f.checks == [("bad_record_header", 56 + len(record), "file header (EVIO) where a record header should start")]


@pytest.mark.parametrize("blocks, version", [
    ([sro_block(1, EVENTS_A), sro_block(2, [], version_word=0x204)], 4),   # evio-library v4: last-block bit on the final block
    ([sro_block(1, EVENTS_A, version_word=0x104), sro_block(2, EVENTS_B)], 4),  # dictionary bit on the first block
    ([sro_block(1, EVENTS_A, version_word=3)], 3),  # v1-3 block
    ([sro_block(1, EVENTS_A), sro_block(2, EVENTS_B, version_word=2)], 2),
], ids=["v4-last-block-bit", "v4-dictionary-bit", "v3", "v2-later-block"])
def test_other_bare_block_chains_are_refused(tmp_path, blocks, version):
    """Only the SRO shape is read; any bit-info bit or other version anywhere in the chain is the F9 error (D17)."""
    with pytest.raises(ValueError, match=rf"version {version}") as raised:
        EvioFile(write_sro_file(tmp_path / "other.evio", blocks))
    assert "evio" in str(raised.value).lower()


# ------------------------------------------------------------ the real files
RAW_REFERENCE = {
    # file: (complete blocks, events in complete blocks, tail declared bytes, tail present bytes, salvaged events)
    "sro_000791.evio.00000": (7892, 86792, 1967400, 531760, 2),
    "sro_000791.evio.00001": (7892, 86795, 1960136, 1939708, 10),
}


@pytest.mark.parametrize("name", list(RAW_REFERENCE))
def test_raw_sro_file_counts(name):
    """Record and event counts of the raw hardware file against the independent reference."""
    path = os.path.join(RAW_SRO_DIR, name)
    if not os.path.isfile(path):
        pytest.skip(f"raw SRO file absent: {path}")
    complete_blocks, complete_events, tail_declared, tail_present, salvaged = RAW_REFERENCE[name]

    started = time.perf_counter()
    with EvioFile(path) as f:
        opened = time.perf_counter() - started
        assert f.header_layout == "sro-8word"
        assert f.trailer is None
        assert f.record_count == complete_blocks + 1
        assert f.checks == [("truncated_tail", tail_declared, tail_present, salvaged)]
        tail = f.get_record(f.record_count - 1)
        assert tail.is_truncated and len(tail.get_event_offsets()) == salvaged
        total = f.get_total_event_count()
        elapsed = time.perf_counter() - started
    print(f"{name}: open {opened:.2f} s, open + count {elapsed:.2f} s, records {f.record_count}, events {total}")
    assert total == complete_events + salvaged
    assert elapsed < 60, "the scan reads headers only and must finish in seconds"


def test_stream_stats_runs_on_the_raw_sro_file():
    """stream_stats reads the sro-8word layout through the public names (header.endian, record_count, buffer, trailer)."""
    path = os.path.join(RAW_SRO_DIR, "sro_000791.evio.00000")
    if not os.path.isfile(path):
        pytest.skip(f"raw SRO file absent: {path}")
    from pyevio.stream_stats import compute_stream_stats

    stats = compute_stream_stats(path, workers=2, max_records=20)
    assert stats.checks.n_frames == 20 * 11
    assert stats.checks.stride == 4
    assert stats.checks.first_frame == 0 and stats.checks.last_frame == 4 * (20 * 11 - 1)
    assert stats.checks.missing_frames == 0 and stats.checks.duplicate_frames == 0
