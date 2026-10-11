"""One pass over the streaming frames of an EVIO file: histograms and file checks.

Built on :mod:`pyevio.decoders.fadc250_stream`. ``compute_stream_stats`` opens
the file, scans every frame header once (frame number, timestamp) for the
frame checks and the time axis, then decodes all records in parallel by
record ranges, each worker opening the file itself and returning numpy
partials that add. The result, :class:`StreamStats`, is small (counts in
fixed bins, sized by the ROC and port sets found in the data) and round-trips
through ``.npz`` with a version field.

Bins:

- ``rate``: hits per time bin from the first to the last frame timestamp,
  per (ROC, port, channel). ``rate_hz()`` normalizes by the frames each bin
  covers, so a partial bin or a missing stripe reads right. Timestamps are
  in ticks; ``tick_ns`` converts them (knob, default 1 ns).
- ``charge``: 13-bit charge in 128 bins of 64; ``time_in_frame``: 14-bit
  tick field in 64 bins of 256 ticks (1,024 ns); both per (ROC, port, channel),
  with per-ROC views summed on demand.
- ``hits_per_frame``: histogram of hits per frame in 64 linear bins.

Trailer records (header type 3) are skipped; events whose range runs past
the file are counted, never read.
"""
import json
import multiprocessing
import os
import struct
import time
from dataclasses import dataclass, field, asdict, fields
from typing import List, Optional

import numpy as np

from pyevio.evio_file import EvioFile
from pyevio.decoders import fadc250_stream as fs

STATS_VERSION = "1"
N_CHANNELS = 16
CHARGE_BIN_SHIFT = 6  # 13-bit charge >> 6 → 128 bins of 64
N_CHARGE_BINS = (fs.HIT_CHARGE_MASK + 1) >> CHARGE_BIN_SHIFT
TIME_BIN_SHIFT = 8  # 14-bit tick field >> 8 → 64 bins of 256 ticks
N_TIME_BINS = (fs.HIT_TIME_MASK + 1) >> TIME_BIN_SHIFT
N_HITS_PER_FRAME_BINS = 64
DEFAULT_RATE_BINS = 256
DEFAULT_TICK_NS = 1.0
DEFAULT_CHUNK_RECORDS = 8
RECORD_TYPE_TRAILER = 3
# A frame whose timestamp step per frame deviates by more than this fraction from the median is an outlier.
DTS_OUTLIER_TOLERANCE = 0.01
MAX_ERROR_MESSAGES = 50

FRAME_SCAN_DTYPE = np.dtype([("record", np.uint32), ("event", np.uint32), ("frame", np.uint32), ("timestamp", np.uint64)])
FRAME_STATS_DTYPE = np.dtype([
    ("event_index", np.uint32), ("frame", np.uint32), ("timestamp", np.uint64), ("n_hits", np.uint32),
    ("non_fadc_words", np.uint32), ("errors", np.uint16),
])


def record_type(record) -> int:
    """Record header type: bits 28—31 of the bit-info/version word (3 = trailer)."""
    return (record.header.bit_info >> 20) & 0xF


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


# ---------------------------------------------------------------- frame scan (serial, headers only)
@dataclass
class FrameScan:
    """Frame numbers and timestamps of every stream frame, in file order, plus record bookkeeping."""
    frames: np.ndarray  # FRAME_SCAN_DTYPE
    n_records: int
    n_trailer_records: int
    n_events: int
    n_other_events: int
    n_out_of_bounds_events: int
    events_before_record: np.ndarray  # per record: global index of its first event (trailers hold the running count)


def scan_frames(evio_file: EvioFile, max_records: Optional[int] = None) -> FrameScan:
    """Read frame number and timestamp from the SIB of every event without decoding hits."""
    endian = evio_file.header.endian
    mm = evio_file.mm
    rows = []
    n_records = n_trailers = n_events = n_other = n_out_of_bounds = 0
    events_before = []
    record_limit = evio_file.record_count if max_records is None else min(max_records, evio_file.record_count)
    for record_i in range(record_limit):
        record = evio_file.get_record(record_i)
        events_before.append(n_events)
        n_records += 1
        if record_type(record) == RECORD_TYPE_TRAILER:
            n_trailers += 1
            continue
        for event_i, (offset, length) in enumerate(record.get_event_offsets()[:record.header.event_count]):
            if offset + length > evio_file.file_size:
                n_out_of_bounds += 1
                continue
            n_events += 1
            if length < 32:
                n_other += 1
                continue
            words = struct.unpack_from(endian + "8I", mm, offset)
            if (words[1] >> 16) != fs.STREAM_EVENT_TAG or (words[3] >> 16) != fs.SIB_TAG or (words[4] >> 24) != fs.SIB_TSS_SEGMENT_TAG:
                n_other += 1
                continue
            rows.append((record_i, event_i, words[5], words[6] | (words[7] << 32)))
    return FrameScan(np.array(rows, dtype=FRAME_SCAN_DTYPE), n_records, n_trailers, n_events, n_other, n_out_of_bounds,
                     np.asarray(events_before, dtype=np.int64))


@dataclass
class FrameChecks:
    """What the frame sequence says about the file: stride, gaps, duplicates and timestamp steps."""
    n_frames: int = 0
    first_frame: int = 0
    last_frame: int = 0
    first_timestamp: int = 0
    last_timestamp: int = 0
    stride: int = 0  # most common positive frame delta between consecutive frames (4 for a 4-way striped run)
    frame_period_ticks: float = 0.0  # median timestamp step per frame number
    missing_frames: int = 0  # frames absent between first and last, counted in steps of the stride
    duplicate_frames: int = 0
    out_of_order: int = 0  # consecutive frames whose number decreases
    dts_outliers: int = 0  # consecutive frames whose timestamp step per frame deviates from the median


def frame_checks(frames: np.ndarray) -> FrameChecks:
    """Compute :class:`FrameChecks` from a frame scan table in file order."""
    checks = FrameChecks(n_frames=len(frames))
    if len(frames) == 0:
        return checks
    frame = frames["frame"].astype(np.int64)
    timestamp = frames["timestamp"].astype(np.int64)
    unique_frames = np.unique(frame)
    checks.first_frame, checks.last_frame = int(unique_frames[0]), int(unique_frames[-1])
    checks.first_timestamp, checks.last_timestamp = int(timestamp.min()), int(timestamp.max())
    checks.duplicate_frames = int(len(frame) - len(unique_frames))
    d_frame = np.diff(frame)
    d_timestamp = np.diff(timestamp)
    checks.out_of_order = int(np.count_nonzero(d_frame < 0))
    forward = d_frame > 0
    if forward.any():
        deltas, counts = np.unique(d_frame[forward], return_counts=True)
        checks.stride = int(deltas[np.argmax(counts)])
        step_per_frame = d_timestamp[forward] / d_frame[forward]
        checks.frame_period_ticks = float(np.median(step_per_frame))
        if checks.frame_period_ticks > 0:
            deviation = np.abs(step_per_frame - checks.frame_period_ticks) / checks.frame_period_ticks
            checks.dts_outliers = int(np.count_nonzero(deviation > DTS_OUTLIER_TOLERANCE))
        checks.missing_frames = int((checks.last_frame - checks.first_frame) // checks.stride + 1 - len(unique_frames))
    return checks


# ---------------------------------------------------------------- accumulation
class Accumulator:
    """Histogram partial over some events; axes (ROC ids, ports) grow with the data and partials add."""

    ARRAYS_3D = ("rate", "charge", "time")  # (roc, port, channel, bins)
    ARRAYS_2D = ("empty_banks", "non_fadc_words_per_port")  # (roc, port)

    def __init__(self, t_first: int, t_last: int, n_rate_bins: int):
        self.t_first, self.t_last, self.n_rate_bins = int(t_first), int(t_last), int(n_rate_bins)
        self.rocs = np.empty(0, dtype=np.uint16)
        self.ports = np.empty(0, dtype=np.uint16)
        self.rate = np.zeros((0, 0, N_CHANNELS, n_rate_bins), dtype=np.int64)
        self.charge = np.zeros((0, 0, N_CHANNELS, N_CHARGE_BINS), dtype=np.int64)
        self.time = np.zeros((0, 0, N_CHANNELS, N_TIME_BINS), dtype=np.int64)
        self.empty_banks = np.zeros((0, 0), dtype=np.int64)
        self.non_fadc_words_per_port = np.zeros((0, 0), dtype=np.int64)
        self.module_id = np.full((0, 0), fs.MODULE_UNKNOWN, dtype=np.uint8)
        self.words_per_module = {}  # module id → payload words (fADC250 hits count as words)
        self.frame_rows = []  # FRAME_STATS_DTYPE arrays, concatenated at the end
        self.totals = {name: 0 for name in ("n_events", "n_frames", "n_other_events", "n_hits", "n_payload_banks",
                                            "n_empty_payload_banks", "n_unmapped_payload_banks", "n_payload_words",
                                            "non_fadc_words", "n_flag_words", "errors")}
        self.error_messages: List[str] = []

    def _grow(self, rocs: np.ndarray, ports: np.ndarray) -> None:
        """Extend the axes to the union with ``rocs``/``ports``, re-indexing every array."""
        new_rocs = np.union1d(self.rocs, rocs).astype(np.uint16)
        new_ports = np.union1d(self.ports, ports).astype(np.uint16)
        if len(new_rocs) == len(self.rocs) and len(new_ports) == len(self.ports):
            return
        roc_pos = np.searchsorted(new_rocs, self.rocs)
        port_pos = np.searchsorted(new_ports, self.ports)
        for name in self.ARRAYS_3D + self.ARRAYS_2D + ("module_id",):
            old = getattr(self, name)
            fill = fs.MODULE_UNKNOWN if name == "module_id" else 0
            new = np.full((len(new_rocs), len(new_ports)) + old.shape[2:], fill, dtype=old.dtype)
            new[np.ix_(roc_pos, port_pos)] = old
            setattr(self, name, new)
        self.rocs, self.ports = new_rocs, new_ports

    def _time_bin(self, timestamps: np.ndarray) -> np.ndarray:
        span = max(self.t_last - self.t_first, 1)
        bins = (timestamps.astype(np.int64) - self.t_first) * self.n_rate_bins // span
        return np.clip(bins, 0, self.n_rate_bins - 1)

    def absorb(self, hits: fs.StreamHits) -> None:
        """Add the hits and tables of one decoded batch."""
        seen_rocs = np.unique(np.concatenate([hits.roc_banks["roc"], hits.empty_banks["roc"], hits.non_fadc_banks["roc"]]))
        seen_ports = np.unique(np.concatenate([hits.port, hits.empty_banks["port"], hits.non_fadc_banks["port"]]))
        self._grow(seen_rocs, seen_ports)
        n_ports = len(self.ports)
        n_cells = len(self.rocs) * n_ports * N_CHANNELS

        if hits.n_hits:
            base = int(hits.frames["event_index"].min())
            n_events_span = int(hits.frames["event_index"].max()) - base + 1
            bin_of_event = np.zeros(n_events_span, dtype=np.int64)
            bin_of_event[hits.frames["event_index"].astype(np.int64) - base] = self._time_bin(hits.frames["timestamp"])
            hit_bin = bin_of_event[hits.event_index.astype(np.int64) - base]
            hit_roc_i = np.searchsorted(self.rocs, hits.roc).astype(np.int64)
            hit_port_i = np.searchsorted(self.ports, hits.port).astype(np.int64)
            cell = (hit_roc_i * n_ports + hit_port_i) * N_CHANNELS + hits.channel
            self.rate += np.bincount(cell * self.n_rate_bins + hit_bin, minlength=n_cells * self.n_rate_bins).reshape(self.rate.shape)
            self.charge += np.bincount(cell * N_CHARGE_BINS + (hits.charge >> CHARGE_BIN_SHIFT),
                                       minlength=n_cells * N_CHARGE_BINS).reshape(self.charge.shape)
            self.time += np.bincount(cell * N_TIME_BINS + (hits.time_raw >> TIME_BIN_SHIFT),
                                     minlength=n_cells * N_TIME_BINS).reshape(self.time.shape)
            self.module_id[hit_roc_i, hit_port_i] = fs.MODULE_FADC250

        for table, target, weights in ((hits.empty_banks, "empty_banks", None),
                                       (hits.non_fadc_banks, "non_fadc_words_per_port", "n_words")):
            if len(table) == 0:
                continue
            roc_i = np.searchsorted(self.rocs, table["roc"]).astype(np.int64)
            port_i = np.searchsorted(self.ports, table["port"]).astype(np.int64)
            counts = np.bincount(roc_i * n_ports + port_i, weights=None if weights is None else table[weights].astype(np.float64),
                                 minlength=len(self.rocs) * n_ports)
            setattr(self, target, getattr(self, target) + counts.astype(np.int64).reshape(len(self.rocs), n_ports))
            self.module_id[roc_i, port_i] = table["module_id"]

        self.words_per_module[fs.MODULE_FADC250] = self.words_per_module.get(fs.MODULE_FADC250, 0) + hits.n_hits
        if len(hits.non_fadc_banks):
            modules, inverse = np.unique(hits.non_fadc_banks["module_id"], return_inverse=True)
            sums = np.bincount(inverse, weights=hits.non_fadc_banks["n_words"].astype(np.float64), minlength=len(modules))
            for module, words in zip(modules.tolist(), sums.tolist()):
                self.words_per_module[int(module)] = self.words_per_module.get(int(module), 0) + int(words)

        frame_rows = np.empty(len(hits.frames), dtype=FRAME_STATS_DTYPE)
        for name in FRAME_STATS_DTYPE.names:
            frame_rows[name] = hits.frames[name]
        self.frame_rows.append(frame_rows)
        for name in self.totals:
            if name == "n_frames":
                self.totals[name] += hits.n_stream_frames
            else:
                self.totals[name] += getattr(hits, name)
        self.error_messages = (self.error_messages + hits.error_messages)[:MAX_ERROR_MESSAGES]

    def merge(self, other: "Accumulator") -> None:
        """Add another partial (built with the same time axis) into this one."""
        self._grow(other.rocs, other.ports)
        roc_pos = np.searchsorted(self.rocs, other.rocs)
        port_pos = np.searchsorted(self.ports, other.ports)
        index = np.ix_(roc_pos, port_pos)
        for name in self.ARRAYS_3D + self.ARRAYS_2D:
            getattr(self, name)[index] += getattr(other, name)
        known = other.module_id != fs.MODULE_UNKNOWN
        self.module_id[index] = np.where(known, other.module_id, self.module_id[index])
        for module, words in other.words_per_module.items():
            self.words_per_module[module] = self.words_per_module.get(module, 0) + words
        self.frame_rows.extend(other.frame_rows)
        for name in self.totals:
            self.totals[name] += other.totals[name]
        self.error_messages = (self.error_messages + other.error_messages)[:MAX_ERROR_MESSAGES]


# ---------------------------------------------------------------- workers
_worker_file: Optional[EvioFile] = None


def _init_worker(path: str) -> None:
    global _worker_file
    _worker_file = EvioFile(path)


def _decode_records(evio_file: EvioFile, record_range, events_before_record: np.ndarray, t_first: int, t_last: int,
                    n_rate_bins: int, chunk_records: int) -> Accumulator:
    """Decode a range of records in chunks and accumulate histograms; trailers and out-of-bounds events are skipped."""
    accumulator = Accumulator(t_first, t_last, n_rate_bins)
    endian = evio_file.header.endian
    record_list = list(record_range)
    for chunk_start in range(0, len(record_list), chunk_records):
        chunk = record_list[chunk_start:chunk_start + chunk_records]
        events = []
        first_event_index = int(events_before_record[chunk[0]])
        for record_i in chunk:
            record = evio_file.get_record(record_i)
            if record_type(record) == RECORD_TYPE_TRAILER:
                continue
            for offset, length in record.get_event_offsets()[:record.header.event_count]:
                if offset + length <= evio_file.file_size:
                    events.append(fs.event_words(evio_file.mm, offset, length, endian))
            evio_file._records.pop(record_i, None)  # free the record cache; a worker visits each record once
        accumulator.absorb(fs.decode_frames(events, first_event_index=first_event_index))
    return accumulator


def _process_range(args) -> Accumulator:
    start, end, events_before_record, t_first, t_last, n_rate_bins, chunk_records = args
    return _decode_records(_worker_file, range(start, end), events_before_record, t_first, t_last, n_rate_bins, chunk_records)


# ---------------------------------------------------------------- result
@dataclass
class StreamStats:
    """Histograms and checks of one file. Arrays are indexed by position in ``rocs``/``ports`` and channel 0—15."""
    version: str
    path: str
    file_size: int
    bytes_processed: int  # end of the last record read; equals file_size unless a record limit was given
    tick_ns: float
    workers: int
    wall_seconds: float
    n_records: int
    n_trailer_records: int
    n_events: int
    n_frames: int
    n_other_events: int
    n_out_of_bounds_events: int
    n_hits: int
    n_payload_banks: int
    n_empty_payload_banks: int
    n_unmapped_payload_banks: int
    n_payload_words: int
    non_fadc_words: int
    n_flag_words: int
    errors: int
    error_messages: List[str]
    checks: FrameChecks
    rocs: np.ndarray
    ports: np.ndarray
    module_id: np.ndarray  # (roc, port); MODULE_UNKNOWN where a port never appeared
    module_ids: np.ndarray  # module ids with payload words, aligned with module_words
    module_words: np.ndarray
    frames: np.ndarray  # FRAME_STATS_DTYPE, sorted by event index
    rate_counts: np.ndarray  # (roc, port, channel, rate bin)
    rate_edges_ns: np.ndarray  # rate bin edges in ns from the first timestamp (len rate bins + 1)
    frames_per_rate_bin: np.ndarray
    occupancy: np.ndarray  # (roc, port, channel)
    charge_hist: np.ndarray  # (roc, port, channel, 128)
    charge_edges: np.ndarray
    time_hist: np.ndarray  # (roc, port, channel, 64)
    time_edges_ns: np.ndarray
    hits_per_frame_hist: np.ndarray
    hits_per_frame_edges: np.ndarray
    empty_banks: np.ndarray  # (roc, port)
    non_fadc_words_per_port: np.ndarray  # (roc, port)

    SCALAR_FIELDS = ("version", "path", "file_size", "bytes_processed", "tick_ns", "workers", "wall_seconds", "n_records", "n_trailer_records",
                     "n_events", "n_frames", "n_other_events", "n_out_of_bounds_events", "n_hits", "n_payload_banks",
                     "n_empty_payload_banks", "n_unmapped_payload_banks", "n_payload_words", "non_fadc_words",
                     "n_flag_words", "errors", "error_messages")

    @property
    def frame_period_ns(self) -> float:
        return self.checks.frame_period_ticks * self.tick_ns

    @property
    def duration_s(self) -> float:
        """Time span of the frames: first to last timestamp plus one frame period."""
        return ((self.checks.last_timestamp - self.checks.first_timestamp) * self.tick_ns + self.frame_period_ns) * 1e-9

    @property
    def live_time_s(self) -> float:
        """Time the frames in this file record: frames × frame period.

        A file holding one stripe of a 4-way striped run records a quarter of ``duration_s``;
        rates are hits per second of live time, as ``rate_hz`` computes them.
        """
        return self.n_frames * self.frame_period_ns * 1e-9

    def roc_index(self, roc: int) -> int:
        position = int(np.searchsorted(self.rocs, roc))
        if position >= len(self.rocs) or self.rocs[position] != roc:
            raise KeyError(f"ROC {roc} not in the file")
        return position

    def rate_hz(self, roc: Optional[int] = None, port: Optional[int] = None, channel: Optional[int] = None) -> np.ndarray:
        """Hit rate per time bin in Hz, normalized by the frames each bin covers (0 where a bin has no frame)."""
        counts = self.rate_counts
        if roc is not None:
            counts = counts[self.roc_index(roc)]
            if port is not None:
                counts = counts[int(np.searchsorted(self.ports, port))]
                if channel is not None:
                    counts = counts[channel]
        summed = counts.reshape(-1, self.rate_counts.shape[-1]).sum(axis=0)
        seconds = self.frames_per_rate_bin * self.frame_period_ns * 1e-9
        return np.divide(summed, seconds, out=np.zeros(len(summed)), where=seconds > 0)

    def hits_per_roc(self) -> dict:
        return {int(roc): int(total) for roc, total in zip(self.rocs, self.occupancy.sum(axis=(1, 2)))}

    def per_roc(self, hist: np.ndarray) -> np.ndarray:
        """Sum a (roc, port, channel, bins) histogram over ports and channels → (roc, bins)."""
        return hist.sum(axis=(1, 2))

    def save(self, path: str) -> None:
        meta = {name: getattr(self, name) for name in self.SCALAR_FIELDS}
        meta["checks"] = asdict(self.checks)
        arrays = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in self.SCALAR_FIELDS and f.name != "checks"}
        np.savez_compressed(path, meta=json.dumps(meta), **arrays)

    @classmethod
    def load(cls, path: str) -> "StreamStats":
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            if meta.get("version") != STATS_VERSION:
                raise ValueError(f"{path}: stats version {meta.get('version')!r}, this pyevio writes {STATS_VERSION!r}")
            checks = FrameChecks(**meta.pop("checks"))
            arrays = {name: data[name] for name in data.files if name != "meta"}
        return cls(checks=checks, **meta, **arrays)


def compute_stream_stats(path: str, workers: Optional[int] = None, rate_bins: int = DEFAULT_RATE_BINS,
                         tick_ns: float = DEFAULT_TICK_NS, max_records: Optional[int] = None,
                         chunk_records: int = DEFAULT_CHUNK_RECORDS) -> StreamStats:
    """Scan, decode and histogram a streaming file. ``workers=1`` runs in-process."""
    t_start = time.perf_counter()
    workers = default_workers() if workers is None else max(1, int(workers))
    with EvioFile(path) as evio_file:
        file_size = evio_file.file_size
        scan = scan_frames(evio_file, max_records)
        checks = frame_checks(scan.frames)
        n_records = scan.n_records
        last_record = evio_file.get_record(n_records - 1) if n_records else None
        bytes_processed = last_record.offset + last_record.size if last_record else 0
        if workers == 1:
            partial = _decode_records(evio_file, range(n_records), scan.events_before_record,
                                      checks.first_timestamp, checks.last_timestamp, rate_bins, chunk_records)
    if workers > 1:
        n_tasks = min(n_records, workers * 4) or 1
        bounds = np.linspace(0, n_records, n_tasks + 1, dtype=np.int64)
        tasks = [(int(bounds[i]), int(bounds[i + 1]), scan.events_before_record, checks.first_timestamp,
                  checks.last_timestamp, rate_bins, chunk_records) for i in range(n_tasks) if bounds[i] < bounds[i + 1]]
        partial = Accumulator(checks.first_timestamp, checks.last_timestamp, rate_bins)
        # forkserver: a forked copy of a multi-threaded parent (numpy, pytest) may deadlock; workers re-import pyevio instead.
        context = multiprocessing.get_context("forkserver")
        with context.Pool(workers, initializer=_init_worker, initargs=(path,)) as pool:
            for result in pool.imap_unordered(_process_range, tasks):
                partial.merge(result)

    frames = np.concatenate(partial.frame_rows) if partial.frame_rows else np.empty(0, dtype=FRAME_STATS_DTYPE)
    frames = frames[np.argsort(frames["event_index"], kind="stable")]
    span = max(checks.last_timestamp - checks.first_timestamp, 1)
    rate_edges_ns = np.linspace(0, span, rate_bins + 1) * tick_ns
    frames_per_rate_bin = np.bincount(partial._time_bin(frames["timestamp"]), minlength=rate_bins) if len(frames) else np.zeros(rate_bins, dtype=np.int64)
    max_hits = int(frames["n_hits"].max()) if len(frames) else 0
    hits_per_frame_edges = np.linspace(0, max(max_hits, 1), N_HITS_PER_FRAME_BINS + 1)
    hits_per_frame_hist, _ = np.histogram(frames["n_hits"], bins=hits_per_frame_edges)
    module_ids = np.array(sorted(partial.words_per_module), dtype=np.uint8)
    module_words = np.array([partial.words_per_module[m] for m in module_ids], dtype=np.int64)

    return StreamStats(
        version=STATS_VERSION, path=os.path.abspath(path), file_size=file_size, bytes_processed=bytes_processed,
        tick_ns=float(tick_ns), workers=workers,
        wall_seconds=time.perf_counter() - t_start,
        n_records=n_records, n_trailer_records=scan.n_trailer_records, n_events=scan.n_events,
        n_frames=partial.totals["n_frames"], n_other_events=partial.totals["n_other_events"],
        n_out_of_bounds_events=scan.n_out_of_bounds_events, n_hits=partial.totals["n_hits"],
        n_payload_banks=partial.totals["n_payload_banks"], n_empty_payload_banks=partial.totals["n_empty_payload_banks"],
        n_unmapped_payload_banks=partial.totals["n_unmapped_payload_banks"], n_payload_words=partial.totals["n_payload_words"],
        non_fadc_words=partial.totals["non_fadc_words"], n_flag_words=partial.totals["n_flag_words"],
        errors=partial.totals["errors"], error_messages=partial.error_messages, checks=checks,
        rocs=partial.rocs, ports=partial.ports, module_id=partial.module_id, module_ids=module_ids, module_words=module_words,
        frames=frames, rate_counts=partial.rate, rate_edges_ns=rate_edges_ns, frames_per_rate_bin=frames_per_rate_bin,
        occupancy=partial.charge.sum(axis=3), charge_hist=partial.charge,
        charge_edges=np.arange(N_CHARGE_BINS + 1) << CHARGE_BIN_SHIFT,
        time_hist=partial.time, time_edges_ns=(np.arange(N_TIME_BINS + 1) << TIME_BIN_SHIFT) * fs.HIT_TIME_TICK_NS,
        hits_per_frame_hist=hits_per_frame_hist, hits_per_frame_edges=hits_per_frame_edges,
        empty_banks=partial.empty_banks, non_fadc_words_per_port=partial.non_fadc_words_per_port,
    )
