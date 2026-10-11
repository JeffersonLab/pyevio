"""Tests for pyevio.stream_stats: synthetic frames through the stats path, and a corpus smoke test."""
import os

import numpy as np
import pytest
from click.testing import CliRunner

from pyevio.decoders import fadc250_stream as fs
from pyevio import stream_stats
from test_fadc250_stream import DCRB_WORDS, DEGENERATE_AIS, DISTINCT_AIS, bank, hit_word, roc_bank, stream_frame
from test_fadc250_stream_example import make_record, pack_words

CORPUS_FILE = "/data/evio-lib/corpus/sro_000791.evio.00000.v6"
MAGIC = 0xC0DA0100
PERIOD = 65536  # timestamp ticks per frame
STRIDE = 4


def make_trailer(number):
    """A trailer record (header type 3, last-record bit) with an index that would look like events."""
    import struct
    index = struct.pack("<4I", 100, 200, 300, 400)
    header = struct.pack("<14I", 14 + 4, number, 14, 0, len(index), (3 << 28) | (1 << 9) | 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    return header + index


def write_synthetic_file(path, n_records=3, frames_per_record=4, with_trailer=True, distinct_ais=False):
    """Striped frames (stride 4) with one duplicate and one gap; fADC250 crate 7 and DCRB crate 44.

    The AIS of every frame repeats one entry as the SRO hardware writes it; ``distinct_ais``
    lists 24 distinct entries instead.
    """
    import struct
    ais_entries = DISTINCT_AIS if distinct_ais else DEGENERATE_AIS
    records = []
    frame = 1000
    expected_hits = 0
    frame_numbers = []
    for record_i in range(n_records):
        events = []
        for event_i in range(frames_per_record):
            n = record_i * frames_per_record + event_i
            if n == 5:
                frame_here = frame - STRIDE  # duplicate of the previous frame
            else:
                frame_here = frame
                frame += STRIDE * (2 if n == 7 else 1)  # leave one frame missing after event 7
            frame_numbers.append(frame_here)
            hits_port_1 = [hit_word(ch, 100 * ch + n, 10 + n) for ch in range(16)]
            hits_port_3 = [hit_word(3, 16000, 8191)] * (n + 1)
            expected_hits += len(hits_port_1) + len(hits_port_3)
            events.append(pack_words(stream_frame(frame_here, frame_here * PERIOD, [
                roc_bank(7, 1, frame_here, frame_here * PERIOD, [(1, hits_port_1), (3, hits_port_3), (5, [])]),
                roc_bank(44, 2, frame_here, frame_here * PERIOD, [(4, DCRB_WORDS), (6, [])], module_id=fs.MODULE_DCRB),
            ], ais_entries=ais_entries)))
        if record_i == 1:
            events.append(pack_words(bank(0xFF50, 0x10, 0, bank(1, 1, 0, [1, 2]))))  # one non-stream event
        records.append(make_record(record_i + 1, events))
    if with_trailer:
        records.append(make_trailer(n_records + 1))
    file_header = struct.pack("<14I", 0x4556494F, 1, 14, len(records), 0, 6, 0, MAGIC, 0, 0, 0, 0, 0, 0)
    path.write_bytes(file_header + b"".join(records))
    return str(path), expected_hits, frame_numbers


@pytest.mark.parametrize("workers", [1, 2])
def test_synthetic_file_totals_and_checks(tmp_path, workers):
    path, expected_hits, frame_numbers = write_synthetic_file(tmp_path / "synthetic.evio")
    stats = stream_stats.compute_stream_stats(path, workers=workers, rate_bins=8, chunk_records=1)

    # n_records counts data records (EvioFile.record_count, D12); the trailer is reported apart.
    assert stats.n_records == 3 and stats.n_trailer_records == 1
    assert stats.n_events == 13 and stats.n_frames == 12 and stats.n_other_events == 1
    assert stats.n_hits == expected_hits
    assert stats.non_fadc_words == 12 * len(DCRB_WORDS)
    assert stats.n_empty_payload_banks == 24 and stats.errors == stats.warnings == 0 and stats.n_flag_words == 0
    assert stats.frames["errors"].tolist() == stats.frames["warnings"].tolist() == [0] * 12
    assert stats.rocs.tolist() == [7, 44] and stats.ports.tolist() == [1, 3, 4, 5, 6]
    assert dict(zip(stats.module_ids.tolist(), stats.module_words.tolist())) == {fs.MODULE_FADC250: expected_hits,
                                                                                 fs.MODULE_DCRB: 12 * len(DCRB_WORDS)}
    # module per (roc, port): 7 → fADC250 on ports 1, 3, 5; 44 → DCRB on ports 4, 6; unknown elsewhere
    module_7 = stats.module_id[0].tolist()
    assert module_7 == [fs.MODULE_FADC250, fs.MODULE_FADC250, fs.MODULE_UNKNOWN, fs.MODULE_FADC250, fs.MODULE_UNKNOWN]
    assert stats.module_id[1].tolist() == [fs.MODULE_UNKNOWN, fs.MODULE_UNKNOWN, fs.MODULE_DCRB, fs.MODULE_UNKNOWN, fs.MODULE_DCRB]

    # occupancy: port 1 has one hit per channel per frame; port 3 all on channel 3
    assert stats.occupancy[0, 0].tolist() == [12] * 16
    assert int(stats.occupancy[0, 1, 3]) == sum(range(1, 13)) and int(stats.occupancy[0, 1].sum()) == sum(range(1, 13))
    assert stats.hits_per_roc() == {7: expected_hits, 44: 0}
    assert int(stats.empty_banks[0, 3]) == 12 and int(stats.empty_banks[1, 4]) == 12
    assert int(stats.non_fadc_words_per_port[1, 2]) == 12 * len(DCRB_WORDS)
    # charge 8191 lands in the last charge bin; time 16000 ticks in bin 62
    assert int(stats.charge_hist[0, 1, 3, -1]) == sum(range(1, 13))
    assert int(stats.time_hist[0, 1, 3, 16000 >> stream_stats.TIME_BIN_SHIFT]) == sum(range(1, 13))
    assert int(stats.rate_counts.sum()) == expected_hits and int(stats.frames_per_rate_bin.sum()) == 12
    assert int(stats.hits_per_frame_hist.sum()) == 12

    checks = stats.checks
    assert checks.n_frames == 12 and checks.stride == STRIDE
    assert checks.duplicate_frames == 1 and checks.missing_frames == 1 and checks.out_of_order == 0
    assert checks.frame_period_ticks == PERIOD and checks.dts_outliers == 0
    assert checks.first_frame == 1000 and checks.last_frame == max(frame_numbers)
    assert stats.frames["frame"].tolist() == frame_numbers
    assert stats.frames["n_hits"].tolist()[:2] == [17, 18]
    # F15: every frame's AIS repeats one entry in its 24 slots
    assert stats.frames_with_ais == stats.frames_with_degenerate_ais == 12
    assert stats.frames["n_ais_entries"].tolist() == [24] * 12 and stats.frames["n_ais_distinct"].tolist() == [1] * 12

    # rate: every bin with frames reports hits / (frames × period); the sum over bins recovers the hits
    rate = stats.rate_hz()
    covered = stats.frames_per_rate_bin > 0
    assert np.allclose(rate[covered] * stats.frames_per_rate_bin[covered] * stats.frame_period_ns * 1e-9, stats.rate_counts.sum(axis=(0, 1, 2))[covered])
    assert rate[~covered].tolist() == [0.0] * int((~covered).sum())
    channel_rate = stats.rate_hz(roc=7, port=1, channel=0)
    assert int(stats.rate_counts[0, 0, 0].sum()) == 12 and len(channel_rate) == 8 and channel_rate.sum() > 0


@pytest.mark.parametrize("workers", [1, 2])
def test_distinct_ais_entries_are_not_degenerate(tmp_path, workers):
    path, _, _ = write_synthetic_file(tmp_path / "distinct_ais.evio", distinct_ais=True)
    stats = stream_stats.compute_stream_stats(path, workers=workers, rate_bins=8, chunk_records=1)
    assert stats.n_frames == stats.frames_with_ais == 12 and stats.frames_with_degenerate_ais == 0
    assert stats.frames["n_ais_entries"].tolist() == [24] * 12 and stats.frames["n_ais_distinct"].tolist() == [24] * 12


def test_npz_round_trip_and_version(tmp_path):
    path, expected_hits, _ = write_synthetic_file(tmp_path / "synthetic.evio", n_records=1)
    stats = stream_stats.compute_stream_stats(path, workers=1, rate_bins=4)
    out = tmp_path / "stats.npz"
    stats.save(str(out))
    loaded = stream_stats.StreamStats.load(str(out))
    assert loaded.n_hits == expected_hits == stats.n_hits
    assert loaded.checks == stats.checks
    assert (loaded.frames_with_ais, loaded.frames_with_degenerate_ais) == (4, 4)
    assert loaded.rocs.tolist() == stats.rocs.tolist()
    assert np.array_equal(loaded.rate_counts, stats.rate_counts) and np.array_equal(loaded.frames, stats.frames)
    assert loaded.version == stream_stats.STATS_VERSION

    stats.version = "0"
    stats.save(str(out))
    with pytest.raises(ValueError, match="stats version"):
        stream_stats.StreamStats.load(str(out))


def test_stats_command_prints_summary(tmp_path):
    from pyevio.cli import cli

    path, expected_hits, _ = write_synthetic_file(tmp_path / "synthetic.evio")
    out = tmp_path / "stats.npz"
    result = CliRunner().invoke(cli, ["stats", path, "--workers", "1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert f"{expected_hits:,}" in result.output and "stride 4" in result.output and "1 missing" in result.output
    assert "DCRB" in result.output and out.exists()


@pytest.mark.skipif(os.environ.get("PYEVIO_CORPUS") != "1", reason="set PYEVIO_CORPUS=1 to run on the SRO corpus")
def test_corpus_first_200_records_match_decoder():
    from pyevio import EvioFile

    stats = stream_stats.compute_stream_stats(CORPUS_FILE, workers=4, max_records=200)

    with EvioFile(CORPUS_FILE) as evio_file:
        endian = evio_file.header.endian
        chunks = []
        for record_i in range(200):
            record = evio_file.get_record(record_i)
            events = [fs.event_words(record.buffer, o, l, endian) for o, l in record.get_event_offsets()[:record.header.event_count]]
            chunks.append(fs.decode_frames(events))
        decoded = fs.concatenate_stream_hits(chunks)
        del events, chunks  # views of the mmap must be gone before EvioFile.close()

    assert stats.n_frames == decoded.n_stream_frames == 2200
    assert stats.n_hits == decoded.n_hits
    assert stats.non_fadc_words == decoded.non_fadc_words
    assert stats.n_empty_payload_banks == decoded.n_empty_payload_banks
    rocs, counts = np.unique(decoded.roc, return_counts=True)
    assert stats.hits_per_roc() == {**{int(r): 0 for r in stats.rocs}, **dict(zip(rocs.tolist(), counts.tolist()))}
    assert stats.checks.stride == 4 and stats.checks.missing_frames == 0 and stats.checks.duplicate_frames == 0
    assert stats.checks.frame_period_ticks == 65536
    assert int(stats.rate_counts.sum()) == decoded.n_hits and int(stats.occupancy.sum()) == decoded.n_hits
    assert stats.errors == stats.warnings == 0 and stats.n_trailer_records == 0
    # F15: every frame of the run carries a 24-slot AIS holding one value
    assert stats.frames_with_ais == stats.frames_with_degenerate_ais == 2200
    assert int(fs.ais_degenerate_mask(decoded.frames).sum()) == 2200
    assert np.unique(decoded.frames["n_ais_entries"]).tolist() == [24] and np.unique(decoded.frames["n_ais_distinct"]).tolist() == [1]
    # rate_hz is hits per second of live time: its frame-weighted mean equals hits / (frames × period)
    rate = stats.rate_hz()
    weighted_mean = (rate * stats.frames_per_rate_bin).sum() / stats.frames_per_rate_bin.sum()
    assert np.isclose(weighted_mean, stats.n_hits / stats.live_time_s, rtol=1e-9)
