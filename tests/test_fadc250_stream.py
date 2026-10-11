"""Tests for pyevio.decoders.fadc250_stream.

Synthetic frames are built word by word by the helpers below, in both byte
orders. The real-data test runs only with PYEVIO_CORPUS=1 and checks the
decoder against an independent struct-based walk of record 0 of the SRO
corpus file.
"""
import os
import struct

import numpy as np
import pytest

from pyevio.decoders import fadc250_stream as fs

CORPUS_FILE = "/data/evio-lib/corpus/sro_000791.evio.00000.v6"


# ---------------------------------------------------------------- frame builders
def bank(tag, data_type, num, payload):
    """EVIO bank: length word (words after it), header word, payload words."""
    return [len(payload) + 1, (tag << 16) | (data_type << 8) | num] + list(payload)


def segment(tag, data_type, payload):
    """EVIO segment: one header word tag<<24 | type<<16 | length, then payload words."""
    return [(tag << 24) | (data_type << 16) | len(payload)] + list(payload)


def hit_word(channel, time_raw, charge, flag=False):
    word = (time_raw << fs.HIT_TIME_SHIFT) | (channel << fs.HIT_CHANNEL_SHIFT) | charge
    return word | (1 << fs.HIT_FLAG_BIT) if flag else word


def stream_info_bank(frame_number, timestamp, ports):
    """ROC-level 0xFF30 bank: TSS 0x31 and a payload-port map 0x41 (one uint16 entry per port)."""
    packed_ports = [ports[i] | (ports[i + 1] << 16 if i + 1 < len(ports) else 0) for i in range(0, len(ports), 2)]
    return bank(fs.ROC_STREAM_INFO_TAG, 0x20, 0x21,
                segment(fs.ROC_TSS_SEGMENT_TAG, 1, [frame_number, timestamp & 0xFFFFFFFF, timestamp >> 32])
                + segment(fs.ROC_PAYLOAD_PORT_SEGMENT_TAG, 5, packed_ports))


def roc_bank(roc, stream_mask, frame_number, timestamp, payload_banks, with_stream_info=True):
    """ROC bank with num = stream status (2 streams, the given mask) and payload banks ``[(port, [hit words])]``."""
    children = stream_info_bank(frame_number, timestamp, [port for port, _ in payload_banks]) if with_stream_info else []
    for port, hits in payload_banks:
        children += bank(port, 0x00, 0 if hits else 1, hits)  # an empty payload bank has num 1
    return bank(roc, 0x10, (2 << 4) | stream_mask, children)


def stream_frame(frame_number, timestamp, roc_banks):
    sib = bank(fs.SIB_TAG, 0x20, 0,
               segment(fs.SIB_TSS_SEGMENT_TAG, 1, [frame_number, timestamp & 0xFFFFFFFF, timestamp >> 32])
               + segment(fs.SIB_AIS_SEGMENT_TAG, 1, [0x00070021] * 24))
    return bank(fs.STREAM_EVENT_TAG, 0x10, 0, sib + [w for rb in roc_banks for w in rb])


def to_words(words, endian):
    """Pack to bytes in the given byte order and read back the way EvioFile callers do."""
    raw = struct.pack(f"{endian}{len(words)}I", *words)
    return np.frombuffer(raw, dtype=np.dtype(endian + "u4"))


FRAME_NUMBER = 1234
TIMESTAMP = (5 << 32) | 0x8000_0000
HITS_A = [hit_word(0, 10, 100), hit_word(3, 20, 200), hit_word(15, 16383, 8191)]
HITS_B = [hit_word(7, 4000, 1)]
HITS_C = [hit_word(1, 1, 1), hit_word(2, 2, 2)]


def one_crate_frame():
    """One crate (ROC 7) as two streams; stream 1 has an empty port 5, stream 2 ends with an empty port 16."""
    return stream_frame(FRAME_NUMBER, TIMESTAMP, [
        roc_bank(7, 1, FRAME_NUMBER, TIMESTAMP, [(1, HITS_A), (3, HITS_B), (5, [])]),
        roc_bank(7, 2, FRAME_NUMBER, TIMESTAMP, [(2, HITS_C), (16, [])]),
    ])


# ---------------------------------------------------------------- synthetic frames
@pytest.mark.parametrize("endian", ["<", ">"])
def test_decode_frame_columns(endian):
    frame = fs.decode_frame(to_words(one_crate_frame(), endian))

    assert frame.is_stream_frame
    assert frame.frame_number == FRAME_NUMBER
    assert frame.timestamp == TIMESTAMP
    assert frame.errors == 0 and frame.error_messages == []
    assert frame.n_hits == 6
    assert frame.n_roc_banks == 2
    assert frame.n_payload_banks == 5
    assert frame.n_empty_payload_banks == 2
    assert frame.n_payload_words == 6
    assert frame.n_flag_words == 0

    assert frame.roc.tolist() == [7] * 6
    assert frame.stream.tolist() == [1, 1, 1, 1, 2, 2]
    assert frame.port.tolist() == [1, 1, 1, 3, 2, 2]
    assert frame.channel.tolist() == [0, 3, 15, 7, 1, 2]
    assert frame.time_raw.tolist() == [10, 20, 16383, 4000, 1, 2]
    assert frame.t_ns.tolist() == [40, 80, 65532, 16000, 4, 8]
    assert frame.charge.tolist() == [100, 200, 8191, 1, 1, 2]
    assert frame.t_ns.dtype == np.uint16 and frame.time_raw.dtype == np.uint16

    rocs = frame.roc_banks
    assert rocs["roc"].tolist() == [7, 7]
    assert rocs["stream"].tolist() == [1, 2]
    assert rocs["stream_status"].tolist() == [0x21, 0x22]
    assert rocs["n_payload_banks"].tolist() == [3, 2]
    assert rocs["n_empty_payload_banks"].tolist() == [1, 1]
    assert rocs["n_hits"].tolist() == [4, 2]
    assert rocs["first_hit_index"].tolist() == [0, 4]
    assert fs.empty_banks_per_port(frame.empty_banks) == {(7, 1, 5): 1, (7, 2, 16): 1}


def test_non_stream_event_is_not_an_error():
    frame = fs.decode_frame(to_words(bank(0xFF50, 0x10, 0, bank(1, 1, 0, [1, 2, 3])), "<"))
    assert not frame.is_stream_frame
    assert frame.n_hits == 0 and frame.errors == 0


def test_last_roc_bank_without_children_i17():
    """I17: the Parquet example crashed on a frame whose last ROC bank has no children (length 1)."""
    words = stream_frame(FRAME_NUMBER, TIMESTAMP, [
        roc_bank(7, 1, FRAME_NUMBER, TIMESTAMP, [(1, HITS_A)]),
        bank(7, 0x10, 0x22, []),
    ])
    frame = fs.decode_frame(to_words(words, "<"))
    assert frame.errors == 0
    assert frame.n_hits == 3
    assert frame.roc_banks["n_payload_banks"].tolist() == [1, 0]
    assert frame.roc_banks["n_hits"].tolist() == [3, 0]


def test_last_roc_bank_with_only_an_empty_payload_bank():
    words = stream_frame(FRAME_NUMBER, TIMESTAMP, [
        roc_bank(7, 1, FRAME_NUMBER, TIMESTAMP, [(1, HITS_A)]),
        roc_bank(7, 2, FRAME_NUMBER, TIMESTAMP, [(2, [])]),
    ])
    frame = fs.decode_frame(to_words(words, "<"))
    assert frame.errors == 0
    assert frame.n_hits == 3 and frame.n_empty_payload_banks == 1
    assert frame.roc_banks["n_payload_banks"].tolist() == [1, 1]


def test_roc_bank_without_stream_info_bank():
    words = stream_frame(FRAME_NUMBER, TIMESTAMP, [roc_bank(9, 2, FRAME_NUMBER, TIMESTAMP, [(4, HITS_C)], with_stream_info=False)])
    frame = fs.decode_frame(to_words(words, "<"))
    assert frame.errors == 0 and frame.n_hits == 2 and frame.port.tolist() == [4, 4]


# ---------------------------------------------------------------- bounds
def test_payload_bank_length_past_event_end_stops_walk():
    words = one_crate_frame()
    # Corrupt the length word of the last payload bank (port 16, empty): make it run past the event.
    last_payload_i = len(words) - 2
    assert words[last_payload_i] == 1 and (words[last_payload_i + 1] >> 16) == 16
    words[last_payload_i] = 1000

    frame = fs.decode_frame(to_words(words, "<"))
    assert frame.errors == 1
    assert "payload bank length 1000" in frame.error_messages[0]
    assert frame.n_hits == 6  # hits before the bad bank are kept
    assert frame.roc_banks["n_payload_banks"].tolist() == [3, 1]


def test_roc_bank_length_past_event_end_stops_walk():
    words = one_crate_frame()
    sib_end = 2 + words[2] + 1
    words[sib_end] = 100_000  # first ROC bank claims more words than the event holds

    frame = fs.decode_frame(to_words(words, "<"))
    assert frame.errors == 1 and frame.n_hits == 0 and frame.n_roc_banks == 0
    assert "ROC bank length 100000" in frame.error_messages[0]


def test_event_length_past_given_words_is_an_error():
    words = one_crate_frame()
    truncated = words[:-4]  # the event length word now exceeds the array
    frame = fs.decode_frame(to_words(truncated, "<"))
    assert frame.errors >= 1
    assert "runs past" in frame.error_messages[0]
    assert frame.is_stream_frame and frame.frame_number == FRAME_NUMBER


def test_tiny_arrays_never_raise():
    for words in ([], [0], [5, fs.STREAM_EVENT_TAG << 16], [1, fs.STREAM_EVENT_TAG << 16]):
        frame = fs.decode_frame(np.array(words, dtype=np.uint32))
        assert frame.n_hits == 0


# ---------------------------------------------------------------- hit word layout and validation
def test_hit_word_layout_round_trip():
    words = np.array([hit_word(5, 100, 1024)], dtype=np.uint32)
    channel, time_raw, charge = fs.unpack_hit_words(words)
    assert (channel[0], time_raw[0], charge[0]) == (5, 100, 1024)
    assert words[0] == (5 << 13) | (1024 & 0x1FFF) | ((400 // 4) << 17)  # layout as tests/test_bank_scanning.py encodes it
    assert fs.HIT_TIME_TICK_NS == 4


def test_validate_hit_words_rejects_flag_bit():
    good = np.array([hit_word(1, 16383, 1)], dtype=np.uint32)
    fs.validate_hit_words(good)
    bad = np.array([hit_word(1, 1, 1, flag=True), hit_word(2, 2, 2)], dtype=np.uint32)
    with pytest.raises(ValueError, match="1 hit words"):
        fs.validate_hit_words(bad)
    assert fs.count_flag_words(bad) == 1


def test_stream_hits_validate_reports_flag_words_and_errors():
    words = stream_frame(FRAME_NUMBER, TIMESTAMP, [roc_bank(7, 1, FRAME_NUMBER, TIMESTAMP, [(1, [hit_word(1, 1, 1, flag=True)])])])
    hits = fs.decode_frames([to_words(words, "<")])
    assert hits.n_flag_words == 1
    with pytest.raises(ValueError, match="bit 31"):
        hits.validate()


# ---------------------------------------------------------------- batch form
def test_decode_frames_concatenates_and_indexes():
    frame_a = to_words(one_crate_frame(), "<")
    frame_b = to_words(stream_frame(FRAME_NUMBER + 4, TIMESTAMP + 262144, [roc_bank(44, 1, 0, 0, [(3, HITS_B)])]), "<")
    other = to_words(bank(0xFF50, 0x10, 0, []), "<")

    hits = fs.decode_frames([frame_a, other, frame_b], first_event_index=10)
    hits.validate()
    assert hits.n_events == 3 and hits.n_stream_frames == 2 and hits.n_other_events == 1
    assert hits.n_hits == 7
    assert hits.frames["frame"].tolist() == [FRAME_NUMBER, FRAME_NUMBER + 4]
    assert hits.frames["event_index"].tolist() == [10, 12]
    assert hits.frames["n_hits"].tolist() == [6, 1]
    assert hits.frames["first_hit_index"].tolist() == [0, 6]
    assert hits.frames["n_empty_payload_banks"].tolist() == [2, 0]
    assert hits.frame.tolist() == [FRAME_NUMBER] * 6 + [FRAME_NUMBER + 4]
    assert hits.event_index.tolist() == [10] * 6 + [12]
    assert hits.roc.tolist() == [7] * 6 + [44]
    assert hits.roc_banks["first_hit_index"].tolist() == [0, 4, 6]

    joined = fs.concatenate_stream_hits([hits, fs.decode_frames([frame_b], first_event_index=20)])
    assert joined.n_hits == 8
    assert joined.frames["first_hit_index"].tolist() == [0, 6, 7]
    assert joined.roc_banks["first_hit_index"].tolist() == [0, 4, 6, 7]
    assert joined.event_index.tolist()[-1] == 20
    joined.validate()


def test_decode_frames_empty_input():
    hits = fs.decode_frames([])
    assert hits.n_hits == 0 and len(hits.frames) == 0 and len(hits.roc) == 0
    hits.validate()


# ---------------------------------------------------------------- real data
def independent_payload_word_sums(mm, offset, length, endian):
    """Plain struct walk of one stream frame: ``{roc id: sum of (payload bank length - 1)}``."""
    def word(i):
        return struct.unpack_from(endian + "I", mm, offset + 4 * i)[0]

    n = length // 4
    assert word(0) + 1 == n and (word(1) >> 16) == 0xFF60
    sums = {}
    roc_i = 2 + word(2) + 1
    while roc_i < n:
        roc_end = roc_i + word(roc_i) + 1
        roc = word(roc_i + 1) >> 16
        child_i = roc_i + 2
        if (word(child_i + 1) >> 16) == 0xFF30:
            child_i += word(child_i) + 1
        while child_i < roc_end:
            sums[roc] = sums.get(roc, 0) + word(child_i) - 1
            child_i += word(child_i) + 1
        roc_i = roc_end
    return sums


@pytest.mark.skipif(os.environ.get("PYEVIO_CORPUS") != "1", reason="set PYEVIO_CORPUS=1 to run on the SRO corpus")
def test_corpus_record0_hits_match_payload_word_sums():
    from pyevio import EvioFile

    with EvioFile(CORPUS_FILE) as evio_file:
        record = evio_file.get_record(0)  # never the last record: it is a trailer with an index
        n_events = record.header.event_count
        assert n_events == 11
        offsets = record.get_event_offsets()[:n_events]
        endian = evio_file.header.endian

        expected = {}
        for offset, length in offsets:
            for roc, words in independent_payload_word_sums(evio_file.mm, offset, length, endian).items():
                expected[roc] = expected.get(roc, 0) + words

        hits = fs.decode_frames(fs.event_words(evio_file.mm, offset, length, endian) for offset, length in offsets)
        hits.validate()

    assert hits.n_stream_frames == 11 and hits.errors == 0
    assert hits.n_hits == sum(expected.values()) == 499_660
    rocs, counts = np.unique(hits.roc, return_counts=True)
    assert dict(zip(rocs.tolist(), counts.tolist())) == expected
    assert set(np.unique(hits.stream).tolist()) == {1, 2}
    assert len(hits.roc_banks) == 24 * 11
