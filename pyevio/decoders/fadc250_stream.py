"""Decode CODA streaming fADC250 time frames into numpy hit arrays.

A streaming time frame is one EVIO event. The decoder takes the words of
one event (or of many events) as a numpy ``uint32`` array and returns hit
columns plus per-frame, per-ROC-bank and per-empty-bank tables. File
access stays in pyevio core: :func:`event_words` slices one event out of a
memory map, and the CLI, the examples and the web server call this module.

Frame layout, measured on /data/evio-lib/corpus/sro_000791.evio.00000.v6
(record 0) and described in the evio-optim SRO notes::

    event bank          tag 0xFF60, type 0x10 (bank of banks), num 0
      SIB               tag 0xFF31, type 0x20 (bank of segments)
        TSS segment     tag 0x32, len 3: {frame number, timestamp lo, timestamp hi}
        AIS segment     tag 0x42, len 24: aggregated-stream entries (ignored here)
      ROC bank          tag = ROC id, type 0x10, num = stream status byte
        stream-info     tag 0xFF30, type 0x20: TSS 0x31 + payload-port map 0x41
        payload bank    tag = payload port, type 0x00, one word per hit
        payload bank    ...
      ROC bank          ...

Each crate sends two streams, so the same ROC id appears as two ROC banks
per frame. The banks differ in the num byte: bits 6—4 hold the stream
count (2) and bits 3—0 the stream mask (1 or 2). The decoder keeps that
mask as the ``stream`` column. The port split between the two streams is
mostly odd/even but not always (ROC 9 stream 2 carries ports 1, 2, 3, 4,
6, 8), so the num byte is the only reliable stream identity.

An empty payload bank has length 1 and num 1; a data bank has num 0. Both
are valid. The walk trusts no length word: every bank end is checked
against its container, a bad length stops the walk and lands in
``errors`` and ``error_messages`` instead of an exception.

Hit word layout (one ``uint32`` per hit), the single copy for pyevio.
Evidence: every earlier decoder in this repository and the viewer agree
on the bit positions and on the 4 ns tick, the fADC250 sample period
(250 MSPS). 2^14 ticks × 4 ns = 65,536 ns equals the 65,536-tick timestamp
step per frame in the SRO notes only if the timestamp tick is 1 ns, which
is open (question Q2)::

    bit  31      flag, never set in the SRO data (Jon Zarling's decoder skips such words)
    bits 30—17   time in 4 ns ticks from the frame start (14 bits, 0—16383)
    bits 16—13   fADC250 channel (4 bits, 0—15)
    bits 12—0    charge (13 bits, 8191 = overflow)
"""
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

import numpy as np

# ---------------------------------------------------------------- hit word layout
HIT_FLAG_BIT = 31
HIT_TIME_SHIFT = 17
HIT_TIME_MASK = 0x3FFF
HIT_TIME_BITS = 14
HIT_CHANNEL_SHIFT = 13
HIT_CHANNEL_MASK = 0xF
HIT_CHARGE_MASK = 0x1FFF
HIT_CHARGE_OVERFLOW = 0x1FFF
HIT_TIME_TICK_NS = 4
HIT_FORMAT = "fadc250-stream-v1: t=bits[30:17]*4ns ch=bits[16:13] q=bits[12:0]"

# ---------------------------------------------------------------- bank tags
STREAM_EVENT_TAG = 0xFF60
SIB_TAG = 0xFF31
SIB_TSS_SEGMENT_TAG = 0x32
SIB_AIS_SEGMENT_TAG = 0x42
ROC_STREAM_INFO_TAG = 0xFF30
ROC_TSS_SEGMENT_TAG = 0x31
ROC_PAYLOAD_PORT_SEGMENT_TAG = 0x41
BANK_OF_BANKS_TYPES = (0x10, 0x0E)

# Only the first error messages of a batch are kept; the count is always exact.
MAX_ERROR_MESSAGES = 50

FRAME_DTYPE = np.dtype([
    ("frame", np.uint32), ("timestamp", np.uint64), ("n_hits", np.uint32), ("first_hit_index", np.uint64),
    ("event_index", np.uint32), ("n_roc_banks", np.uint16), ("n_payload_banks", np.uint16),
    ("n_empty_payload_banks", np.uint16), ("errors", np.uint16),
])
ROC_BANK_DTYPE = np.dtype([
    ("event_index", np.uint32), ("frame", np.uint32), ("roc", np.uint16), ("stream", np.uint8),
    ("stream_status", np.uint8), ("n_payload_banks", np.uint16), ("n_empty_payload_banks", np.uint16),
    ("n_hits", np.uint32), ("first_hit_index", np.uint64),
])
EMPTY_BANK_DTYPE = np.dtype([("event_index", np.uint32), ("roc", np.uint16), ("stream", np.uint8), ("port", np.uint16)])


def event_words(mm, offset: int, length: int, endian: str) -> np.ndarray:
    """Return the words of one event as a ``uint32`` view of ``mm`` in the file's byte order.

    ``offset`` and ``length`` are in bytes, as ``Record.get_event_offsets`` returns them.
    Raises ``ValueError`` when the range runs past the buffer, so a caller never reads
    past the file because an index word said so.
    """
    if offset < 0 or length < 0 or offset + length > len(mm):
        raise ValueError(f"event at byte {offset} with length {length} runs past the buffer end ({len(mm)} bytes)")
    return np.frombuffer(mm, dtype=np.dtype(endian + "u4"), count=length // 4, offset=offset)


def unpack_hit_words(hit_words: np.ndarray):
    """Split hit words into ``(channel, time_raw, charge)`` columns.

    ``time_raw`` is the 14-bit tick field; multiply by ``HIT_TIME_TICK_NS`` for nanoseconds.
    The flag bit is masked away; count it with :func:`count_flag_words` or reject it
    with :func:`validate_hit_words`.
    """
    hit_words = np.asarray(hit_words, dtype=np.uint32)
    channel = ((hit_words >> HIT_CHANNEL_SHIFT) & HIT_CHANNEL_MASK).astype(np.uint8)
    time_raw = ((hit_words >> HIT_TIME_SHIFT) & HIT_TIME_MASK).astype(np.uint16)
    charge = (hit_words & HIT_CHARGE_MASK).astype(np.uint16)
    return channel, time_raw, charge


def count_flag_words(hit_words: np.ndarray) -> int:
    """Count hit words with bit 31 set."""
    return int(np.count_nonzero(np.asarray(hit_words, dtype=np.uint32) >> HIT_FLAG_BIT))


def validate_hit_words(hit_words: np.ndarray) -> None:
    """Raise ``ValueError`` when the raw time field does not fit its 14 bits.

    The check reads the field without the mask: ``word >> 17`` exceeds ``2**14 - 1``
    exactly when the flag bit is set. A masked field can never fail, which is why the
    check runs on the raw words and before any unit conversion.
    """
    raw_time_field = np.asarray(hit_words, dtype=np.uint32) >> HIT_TIME_SHIFT
    n_bad = int(np.count_nonzero(raw_time_field > HIT_TIME_MASK))
    if n_bad:
        raise ValueError(f"{n_bad} hit words have a time field outside {HIT_TIME_BITS} bits (bit 31 set)")


@dataclass
class StreamHits:
    """Hits of many decoded events, concatenated in event order.

    Hit columns share one index. ``frames`` has one row per stream frame
    (``first_hit_index`` + ``n_hits`` gives the hit range); ``roc_banks`` one row per
    ROC bank with its hit range and stream identity; ``empty_banks`` one row per
    empty payload bank. Events that are not 0xFF60 stream frames count in
    ``n_other_events`` and get no row.
    """
    frame: np.ndarray
    event_index: np.ndarray
    roc: np.ndarray
    stream: np.ndarray
    port: np.ndarray
    channel: np.ndarray
    time_raw: np.ndarray
    t_ns: np.ndarray
    charge: np.ndarray
    frames: np.ndarray
    roc_banks: np.ndarray
    empty_banks: np.ndarray
    n_events: int = 0
    n_stream_frames: int = 0
    n_other_events: int = 0
    n_hits: int = 0
    n_payload_banks: int = 0
    n_empty_payload_banks: int = 0
    n_payload_words: int = 0
    n_flag_words: int = 0
    errors: int = 0
    error_messages: List[str] = field(default_factory=list)

    HIT_COLUMNS = ("frame", "event_index", "roc", "stream", "port", "channel", "time_raw", "t_ns", "charge")

    def hit_columns(self) -> dict:
        """Return the hit columns as ``{name: array}``."""
        return {name: getattr(self, name) for name in self.HIT_COLUMNS}

    def validate(self) -> None:
        """Raise ``ValueError`` on any inconsistency the decode found.

        Detects: walk errors (bad lengths), hit words with bit 31 set, and a hit
        count that differs from the payload word count or the per-frame sums.
        """
        if self.errors:
            first = "; ".join(self.error_messages[:3])
            raise ValueError(f"{self.errors} decode errors, first: {first}")
        if self.n_flag_words:
            raise ValueError(f"{self.n_flag_words} hit words have a time field outside {HIT_TIME_BITS} bits (bit 31 set)")
        if self.n_hits != self.n_payload_words:
            raise ValueError(f"hit count {self.n_hits} != sum of payload bank words {self.n_payload_words}")
        if int(self.frames["n_hits"].sum()) != self.n_hits:
            raise ValueError("per-frame hit counts do not add up to the hit count")


@dataclass
class FrameHits:
    """Hits of one decoded event, with the frame metadata as scalars."""
    frame_number: int
    timestamp: int
    is_stream_frame: bool
    roc: np.ndarray
    stream: np.ndarray
    port: np.ndarray
    channel: np.ndarray
    time_raw: np.ndarray
    t_ns: np.ndarray
    charge: np.ndarray
    roc_banks: np.ndarray
    empty_banks: np.ndarray
    n_hits: int
    n_roc_banks: int
    n_payload_banks: int
    n_empty_payload_banks: int
    n_payload_words: int
    n_flag_words: int
    errors: int
    error_messages: List[str]


def concatenate_stream_hits(chunks: Iterable[StreamHits]) -> StreamHits:
    """Join the results of several :func:`decode_frames` calls into one, keeping event order.

    Hit indices in ``frames`` and ``roc_banks`` are shifted so they index the joined columns;
    ``event_index`` values are kept as the chunks produced them.
    """
    chunks = list(chunks)
    if not chunks:
        return decode_frames([])
    columns = {name: np.concatenate([getattr(c, name) for c in chunks]) for name in StreamHits.HIT_COLUMNS}
    frames = np.concatenate([c.frames for c in chunks])
    roc_banks = np.concatenate([c.roc_banks for c in chunks])
    hit_offsets = np.cumsum([0] + [c.n_hits for c in chunks[:-1]], dtype=np.uint64)
    frames["first_hit_index"] += np.repeat(hit_offsets, [len(c.frames) for c in chunks])
    roc_banks["first_hit_index"] += np.repeat(hit_offsets, [len(c.roc_banks) for c in chunks])
    messages = [m for c in chunks for m in c.error_messages][:MAX_ERROR_MESSAGES]
    return StreamHits(
        **columns, frames=frames, roc_banks=roc_banks,
        empty_banks=np.concatenate([c.empty_banks for c in chunks]),
        n_events=sum(c.n_events for c in chunks), n_stream_frames=sum(c.n_stream_frames for c in chunks),
        n_other_events=sum(c.n_other_events for c in chunks), n_hits=sum(c.n_hits for c in chunks),
        n_payload_banks=sum(c.n_payload_banks for c in chunks),
        n_empty_payload_banks=sum(c.n_empty_payload_banks for c in chunks),
        n_payload_words=sum(c.n_payload_words for c in chunks), n_flag_words=sum(c.n_flag_words for c in chunks),
        errors=sum(c.errors for c in chunks), error_messages=messages,
    )


class _Walk:
    """Accumulates what the bank walk finds over one or more events, before any numpy work.

    Hit words are collected as slices of the event arrays and concatenated once at the end,
    which keeps the per-event cost at a few hundred Python-level header reads.
    """

    def __init__(self):
        self.hit_slices = []
        # One entry per payload bank with data: its event, frame, roc, stream, port and word count.
        self.bank_event, self.bank_frame, self.bank_roc, self.bank_stream, self.bank_port, self.bank_n = [], [], [], [], [], []
        self.frame_rows, self.roc_bank_rows, self.empty_rows = [], [], []
        self.n_events = self.n_stream_frames = self.n_other_events = 0
        self.n_hits = self.n_payload_banks = self.n_empty_payload_banks = self.n_payload_words = 0
        self.errors = 0
        self.error_messages = []

    def error(self, event_index: int, word_i: int, reason: str) -> None:
        self.errors += 1
        if len(self.error_messages) < MAX_ERROR_MESSAGES:
            self.error_messages.append(f"event {event_index} word {word_i}: {reason}")

    def walk_event(self, words: np.ndarray, event_index: int) -> None:
        """Walk one event; a bad length stops the walk and is recorded, never raised."""
        self.n_events += 1
        w = np.ascontiguousarray(words, dtype=np.uint32)  # a no-op view when the dtype already matches
        n = len(w)
        read = w.item  # returns Python ints; faster than indexing a numpy scalar
        errors_before = self.errors
        if n < 2:
            self.n_other_events += 1
            self.error(event_index, 0, f"event has {n} words, a bank header needs 2")
            return
        if (read(1) >> 16) != STREAM_EVENT_TAG:
            self.n_other_events += 1
            return
        self.n_stream_frames += 1
        event_end = read(0) + 1
        if event_end > n:
            self.error(event_index, 0, f"event length {event_end} words runs past the {n} words given")
            event_end = n

        frame_number, timestamp, sib_end = self._read_sib(read, event_end, event_index)
        hits_before, payload_before, empty_before, roc_banks_before = self.n_hits, self.n_payload_banks, self.n_empty_payload_banks, len(self.roc_bank_rows)

        roc_bank_i = sib_end
        while roc_bank_i < event_end:
            if roc_bank_i + 1 >= event_end:
                self.error(event_index, roc_bank_i, "ROC bank header runs past the event end")
                break
            roc_end = roc_bank_i + read(roc_bank_i) + 1
            if roc_end > event_end:
                self.error(event_index, roc_bank_i, f"ROC bank length {read(roc_bank_i)} runs past the event end {event_end}")
                break
            roc_header = read(roc_bank_i + 1)
            if ((roc_header >> 8) & 0x3F) not in BANK_OF_BANKS_TYPES:
                self.error(event_index, roc_bank_i, f"ROC bank 0x{roc_header:08X} is not a bank of banks")
                roc_bank_i = roc_end
                continue
            self._walk_roc_bank(w, read, roc_bank_i + 2, roc_end, roc_header, event_index, frame_number)
            roc_bank_i = roc_end

        self.frame_rows.append((
            frame_number, timestamp, self.n_hits - hits_before, hits_before, event_index,
            len(self.roc_bank_rows) - roc_banks_before, self.n_payload_banks - payload_before,
            self.n_empty_payload_banks - empty_before, self.errors - errors_before,
        ))

    def _read_sib(self, read, event_end: int, event_index: int):
        """Return ``(frame_number, timestamp, sib_end)`` from the SIB at word 2; zeros on a bad SIB."""
        if 3 >= event_end:
            self.error(event_index, 2, "no room for the stream info bank")
            return 0, 0, event_end
        sib_end = 2 + read(2) + 1
        if sib_end > event_end:
            self.error(event_index, 2, f"SIB length {read(2)} runs past the event end {event_end}")
            return 0, 0, event_end
        if (read(3) >> 16) != SIB_TAG:
            self.error(event_index, 3, f"expected SIB tag 0x{SIB_TAG:04X}, found 0x{read(3) >> 16:04X}")
            return 0, 0, sib_end
        # The TSS is the first segment of the SIB: header word tag<<24 | type<<16 | length.
        if 7 >= sib_end or (read(4) >> 24) != SIB_TSS_SEGMENT_TAG or (read(4) & 0xFFFF) < 3:
            self.error(event_index, 4, "SIB does not start with a time slice segment (tag 0x32, length 3)")
            return 0, 0, sib_end
        return read(5), read(6) | (read(7) << 32), sib_end

    def _walk_roc_bank(self, w, read, child_i: int, roc_end: int, roc_header: int, event_index: int, frame_number: int) -> None:
        roc = roc_header >> 16
        stream_status = roc_header & 0xFF
        stream = stream_status & 0xF
        hits_before, payload_before, empty_before = self.n_hits, self.n_payload_banks, self.n_empty_payload_banks
        # Skip the stream-info bank; the SRO data always places it first.
        if child_i + 1 < roc_end and (read(child_i + 1) >> 16) == ROC_STREAM_INFO_TAG:
            child_i += read(child_i) + 1
        while child_i < roc_end:
            if child_i + 1 >= roc_end:
                self.error(event_index, child_i, "payload bank header runs past the ROC bank end")
                break
            payload_len = read(child_i)
            if payload_len == 0 or child_i + payload_len + 1 > roc_end:
                self.error(event_index, child_i, f"payload bank length {payload_len} runs past the ROC bank end {roc_end}")
                break
            port = read(child_i + 1) >> 16
            n_words = payload_len - 1
            self.n_payload_banks += 1
            self.n_payload_words += n_words
            if n_words == 0:
                self.n_empty_payload_banks += 1
                self.empty_rows.append((event_index, roc, stream, port))
            else:
                self.hit_slices.append(w[child_i + 2:child_i + 2 + n_words])
                self.bank_event.append(event_index)
                self.bank_frame.append(frame_number)
                self.bank_roc.append(roc)
                self.bank_stream.append(stream)
                self.bank_port.append(port)
                self.bank_n.append(n_words)
                self.n_hits += n_words
            child_i += payload_len + 1
        self.roc_bank_rows.append((
            event_index, frame_number, roc, stream, stream_status, self.n_payload_banks - payload_before,
            self.n_empty_payload_banks - empty_before, self.n_hits - hits_before, hits_before,
        ))

    def to_stream_hits(self) -> StreamHits:
        """Build the numpy columns: one concatenation of hit words, then vectorized unpacking."""
        if self.hit_slices:
            hit_words = np.concatenate(self.hit_slices)
        else:
            hit_words = np.empty(0, dtype=np.uint32)
        bank_n = np.asarray(self.bank_n, dtype=np.int64)
        channel, time_raw, charge = unpack_hit_words(hit_words)
        return StreamHits(
            frame=np.repeat(np.asarray(self.bank_frame, dtype=np.uint32), bank_n),
            event_index=np.repeat(np.asarray(self.bank_event, dtype=np.uint32), bank_n),
            roc=np.repeat(np.asarray(self.bank_roc, dtype=np.uint16), bank_n),
            stream=np.repeat(np.asarray(self.bank_stream, dtype=np.uint8), bank_n),
            port=np.repeat(np.asarray(self.bank_port, dtype=np.uint16), bank_n),
            channel=channel, time_raw=time_raw,
            t_ns=(time_raw * np.uint16(HIT_TIME_TICK_NS)).astype(np.uint16),  # max 16383 × 4 = 65,532 fits
            charge=charge,
            frames=np.array(self.frame_rows, dtype=FRAME_DTYPE),
            roc_banks=np.array(self.roc_bank_rows, dtype=ROC_BANK_DTYPE),
            empty_banks=np.array(self.empty_rows, dtype=EMPTY_BANK_DTYPE),
            n_events=self.n_events, n_stream_frames=self.n_stream_frames, n_other_events=self.n_other_events,
            n_hits=self.n_hits, n_payload_banks=self.n_payload_banks,
            n_empty_payload_banks=self.n_empty_payload_banks, n_payload_words=self.n_payload_words,
            n_flag_words=count_flag_words(hit_words), errors=self.errors, error_messages=self.error_messages,
        )


def decode_frames(events: Iterable[np.ndarray], first_event_index: int = 0) -> StreamHits:
    """Decode many events (each a ``uint32`` word array) into one :class:`StreamHits`.

    Events are numbered from ``first_event_index`` in iteration order; the number is the
    ``event_index`` of their hits and table rows. The result holds every hit of the batch
    in memory, so a caller streams a large file in chunks (for example one record at a time)
    and joins or reduces the chunks itself.
    """
    walk = _Walk()
    for event_i, words in enumerate(events, start=first_event_index):
        walk.walk_event(words, event_i)
    return walk.to_stream_hits()


def decode_frame(words: np.ndarray, event_index: int = 0) -> FrameHits:
    """Decode one event into a :class:`FrameHits`.

    A non-stream event (tag other than 0xFF60) returns ``is_stream_frame=False`` with empty
    columns and no error; malformed lengths return the hits found before the bad length
    and describe it in ``error_messages``.
    """
    stream_hits = decode_frames([words], first_event_index=event_index)
    is_stream_frame = stream_hits.n_stream_frames == 1
    frame_row = stream_hits.frames[0] if is_stream_frame else None
    return FrameHits(
        frame_number=int(frame_row["frame"]) if is_stream_frame else 0,
        timestamp=int(frame_row["timestamp"]) if is_stream_frame else 0,
        is_stream_frame=is_stream_frame,
        roc=stream_hits.roc, stream=stream_hits.stream, port=stream_hits.port, channel=stream_hits.channel,
        time_raw=stream_hits.time_raw, t_ns=stream_hits.t_ns, charge=stream_hits.charge,
        roc_banks=stream_hits.roc_banks, empty_banks=stream_hits.empty_banks,
        n_hits=stream_hits.n_hits, n_roc_banks=len(stream_hits.roc_banks),
        n_payload_banks=stream_hits.n_payload_banks, n_empty_payload_banks=stream_hits.n_empty_payload_banks,
        n_payload_words=stream_hits.n_payload_words, n_flag_words=stream_hits.n_flag_words,
        errors=stream_hits.errors, error_messages=stream_hits.error_messages,
    )


def empty_banks_per_port(empty_banks: np.ndarray) -> dict:
    """Count empty payload banks per ``(roc, stream, port)`` from an ``empty_banks`` table."""
    if len(empty_banks) == 0:
        return {}
    keys, counts = np.unique(empty_banks[["roc", "stream", "port"]], return_counts=True)
    return {(int(k["roc"]), int(k["stream"]), int(k["port"])): int(c) for k, c in zip(keys, counts)}
