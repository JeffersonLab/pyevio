"""The Parquet example and `pyevio debug` on a synthetic streaming file.

Both callers use pyevio.decoders.fadc250_stream; these tests pin that the
example survives the F17 frame shape (a last ROC bank without children),
sorts hits by (frame, event, t_ns) so duplicate frames keep valid hit
ranges (F20), and that the debug command decodes a stream frame.
"""
import importlib.util
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from test_fadc250_stream import HITS_A, HITS_B, bank, hit_word, roc_bank, stream_frame

MAGIC = 0xC0DA0100
EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "examples" / "fadc250_stream_to_parquet.py"


def load_example():
    spec = importlib.util.spec_from_file_location("fadc250_stream_to_parquet", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pack_words(words):
    return struct.pack("<%dI" % len(words), *words)


def make_record(number, events):
    """EVIO v6 record with an event index, little-endian."""
    index = struct.pack("<%dI" % len(events), *(len(e) for e in events))
    data = b"".join(events)
    length_words = 14 + (len(index) + len(data)) // 4
    header = struct.pack("<14I", length_words, number, 14, len(events), len(index), 6, 0, MAGIC, len(data), 0, 0, 0, 0, 0)
    return header + index + data


def write_stream_file(path):
    """Two records: frame 100 (two hits, F17 shape), a non-stream event, then frame 100 again and frame 104."""
    frame_100_a = stream_frame(100, 1000, [
        roc_bank(7, 1, 100, 1000, [(1, [hit_word(2, 50, 10), hit_word(2, 30, 20)])]),
        bank(7, 0x10, 0x22, []),  # F17: last ROC bank has no children
    ])
    other = bank(0xFF50, 0x10, 0, bank(1, 1, 0, [1, 2]))
    frame_100_b = stream_frame(100, 1000, [roc_bank(9, 2, 100, 1000, [(2, [hit_word(4, 40, 30)]), (4, [])])])
    frame_104 = stream_frame(104, 1000 + 262144, [roc_bank(7, 1, 104, 1000 + 262144, [(3, HITS_A + HITS_B)])])
    records = [make_record(1, [pack_words(frame_100_a), pack_words(other)]),
               make_record(2, [pack_words(frame_100_b), pack_words(frame_104)])]
    file_header = struct.pack("<14I", 0x4556494F, 1, 14, len(records), 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path.write_bytes(file_header + b"".join(records))
    return str(path)


def test_example_decode_handles_i17_and_duplicate_frames(tmp_path):
    example = load_example()
    hits, frames, stats, stream_hits = example.decode(write_stream_file(tmp_path / "stream.evio"))
    checks = example.validate(stream_hits, frames)

    assert stats["hits"] == 7 and stats["frames"] == 3 and stats["skipped_events"] == 1
    assert stats["payload_words"] == 7 and stats["empty_payload_banks"] == 1
    assert stats["decode_errors"] == 0 and stats["decode_warnings"] == 0
    assert stats["out_of_order"] == 0
    assert checks == {"missing_frames": 3, "duplicate_frames": 1}

    # frames sorted by (frame, event index); hit ranges follow the same order
    assert frames["frame"].tolist() == [100, 100, 104]
    assert frames["n_hits"].tolist() == [2, 1, 4]
    assert frames["hit_offset"].tolist() == [0, 2, 3]
    assert hits["frame"].tolist() == [100, 100, 100, 104, 104, 104, 104]
    assert hits["roc"].tolist() == [7, 7, 9, 7, 7, 7, 7]
    assert hits["t_ns"].tolist()[:3] == [120, 200, 160]  # within an event, sorted by time
    assert hits["stream"].tolist() == [1, 1, 2, 1, 1, 1, 1]
    assert hits["port"].dtype == np.uint16


def test_example_convert_writes_parquet(tmp_path):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    example = load_example()
    path = write_stream_file(tmp_path / "stream.evio")
    example.convert(path, tmp_path / "out")
    hits = pq.read_table(tmp_path / "out" / "stream.hits.parquet")
    assert hits.num_rows == 7
    assert hits.schema.metadata[b"decoder_version"] == example.DECODER_VERSION.encode()


def test_debug_command_decodes_stream_frame(tmp_path):
    from pyevio.cli import cli

    path = write_stream_file(tmp_path / "stream.evio")
    result = CliRunner().invoke(cli, ["debug", path, "--record", "0", "--event", "0", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "Streaming time frame 100" in result.output
    assert "2 hits in 2 ROC banks" in result.output
    assert "Data Bank (Data Words: 2)" in result.output  # both hit words, including the last one of the bank
    assert "Channel: 2, Charge: 10, Time: 200" in result.output
    assert "Channel: 2, Charge: 20, Time: 120" in result.output
