"""Summary and histogram JSON from a :class:`StreamStats` and the file's record headers.

Ratios whose denominator is zero are the string ``"n/a"``: a file with frames
but no hits has no hit rate, and the viewer prints the string as given instead
of ``NaN``.
"""
from dataclasses import asdict
from typing import Optional

import numpy as np

from pyevio.decoders import fadc250_stream as fs
from pyevio.evio_file import EvioFile
from pyevio.stream_stats import N_CHANNELS, StreamStats
from pyevio.web import ApiError

HISTOGRAM_NAMES = ("hits_per_frame", "rate", "occupancy", "charge", "time_in_frame")
NOT_AVAILABLE = "n/a"


def ratio(numerator: float, denominator: float):
    return numerator / denominator if denominator else NOT_AVAILABLE


# ---------------------------------------------------------------- summary
def record_table_summary(evio_file: EvioFile) -> dict:
    """One pass over the record headers: counts the viewer's record table shows at the top."""
    sizes, events, events_found, with_errors, last_bit, compressed, without_index, truncated = [], 0, 0, 0, 0, 0, 0, 0
    for record_i in range(evio_file.record_count):
        record = evio_file.get_record(record_i)
        found = record.scan_events()  # decompresses a compressed record; released below
        sizes.append(record.size)
        events += record.header.event_count
        events_found += len(found)
        with_errors += bool(record.errors)
        last_bit += record.header.is_last_record
        compressed += record.compression_type != record.COMPRESSION_NONE
        without_index += record.header.index_array_length == 0
        truncated += record.is_truncated
        record.release()
    return {"count": evio_file.record_count, "has_trailer": evio_file.trailer is not None, "events": events,
            "events_found": events_found, "with_errors": with_errors, "last_record_bit": last_bit, "compressed": compressed,
            "without_index": without_index, "truncated": truncated, "min_size": min(sizes) if sizes else 0,
            "max_size": max(sizes) if sizes else 0, "mean_size": ratio(sum(sizes), len(sizes))}


def file_scan_checks(evio_file: EvioFile) -> list:
    """``EvioFile.checks`` tuples as check rows: a truncated tail and record headers the scan could not read."""
    rows = []
    for check in evio_file.checks:
        if check[0] == "truncated_tail":
            _, declared, present, salvaged = check
            rows.append({"name": "truncated tail", "status": "error", "value": declared - present,
                         "detail": f"last record declares {declared} bytes, {present} present; {salvaged} complete events kept"})
        elif check[0] == "bad_record_header":
            _, offset, message = check
            rows.append({"name": "bad record header", "status": "error", "value": offset, "detail": f"scan stopped at 0x{offset:X}: {message}"})
        else:
            rows.append({"name": check[0], "status": "warn", "value": None, "detail": repr(check[1:])})
    return rows


def file_checks(evio_file: EvioFile, records: dict, stats: StreamStats) -> list:
    """Checks as ``{name, status, value, detail}`` rows; status is ok, info, warn or error."""
    checks = stats.checks

    def row(name, status, value, detail=""):
        return {"name": name, "status": status, "value": value, "detail": detail}

    rows = [
        row("header layout", "info", evio_file.header_layout, f"{evio_file.header.file_type}, version {evio_file.header.version}"),
        row("trailer", "ok" if records["has_trailer"] else "warn", records["has_trailer"], "" if records["has_trailer"] else "file ends without a trailer record"),
        *file_scan_checks(evio_file),
        row("record errors", "error" if records["with_errors"] else "ok", records["with_errors"], "records whose event walk stopped early"),
        row("events found", "ok" if records["events_found"] == records["events"] else "error", records["events_found"],
            f"headers declare {records['events']}"),
        row("last-record bit", "info", records["last_record_bit"], "records with the last-record bit set"),
        row("compressed records", "info", records["compressed"], ""),
        row("records without index", "info", records["without_index"], ""),
        row("out-of-bounds events", "error" if stats.n_out_of_bounds_events else "ok", stats.n_out_of_bounds_events, "events whose range runs past the file"),
        row("stream frames", "info", stats.n_frames, f"of {stats.n_events} events; {stats.n_other_events} other"),
        row("frame stride", "info", checks.stride, "most common frame-number step (4 = one stripe of a 4-way run)"),
        row("missing frames", "warn" if checks.missing_frames else "ok", checks.missing_frames, "counted in steps of the stride"),
        row("duplicate frames", "warn" if checks.duplicate_frames else "ok", checks.duplicate_frames, ""),
        row("out-of-order frames", "warn" if checks.out_of_order else "ok", checks.out_of_order, ""),
        row("timestamp-step outliers", "warn" if checks.dts_outliers else "ok", checks.dts_outliers,
            f"median step {checks.frame_period_ticks:.0f} ticks"),
        row("decode errors", "error" if stats.errors else "ok", stats.errors, stats.error_messages[0] if stats.error_messages else ""),
        row("flag words", "warn" if stats.n_flag_words else "ok", stats.n_flag_words, "hit words with bit 31 set"),
        row("unmapped payload banks", "warn" if stats.n_unmapped_payload_banks else "ok", stats.n_unmapped_payload_banks,
            "ports absent from the 0x41 payload map, decoded as fADC250"),
        # F15: the SRO hardware fills every AIS slot with one stream's entry. The decoder never uses the AIS
        # to find ROC banks, so the repetition costs nothing; the row makes it visible.
        row("AIS entries identical", "info" if stats.frames_with_degenerate_ais else "ok", stats.frames_with_degenerate_ais,
            f"of {stats.frames_with_ais} frames with an aggregation info segment: the segment repeats one stream's entry "
            "in every slot; it does not list the streams" if stats.frames_with_degenerate_ais else ""),
    ]
    return rows


def per_roc_rows(stats: StreamStats) -> list:
    hits = stats.hits_per_roc()
    rows = []
    for roc_i, roc in enumerate(stats.rocs.tolist()):
        modules = sorted({int(m) for m in stats.module_id[roc_i] if m != fs.MODULE_UNKNOWN})
        ports = [int(p) for p, m in zip(stats.ports, stats.module_id[roc_i]) if m != fs.MODULE_UNKNOWN]
        rows.append({"roc": roc, "modules": [fs.MODULE_NAMES.get(m, str(m)) for m in modules], "ports": ports,
                     "hits": hits[roc], "non_fadc_words": int(stats.non_fadc_words_per_port[roc_i].sum()),
                     "empty_banks": int(stats.empty_banks[roc_i].sum())})
    return rows


def build_summary(evio_file: EvioFile, stats: StreamStats, relative_path: str, from_cache: bool) -> dict:
    header = evio_file.header
    records = record_table_summary(evio_file)
    totals = {name: getattr(stats, name) for name in StreamStats.SCALAR_FIELDS if name not in ("version", "path", "tick_ns", "workers", "wall_seconds")}
    totals.update({
        "duration_s": stats.duration_s, "live_time_s": stats.live_time_s, "frame_period_ns": stats.frame_period_ns,
        "hit_rate_hz": ratio(stats.n_hits, stats.live_time_s), "hits_per_frame": ratio(stats.n_hits, stats.n_frames),
        "stream_fraction": ratio(stats.n_frames, stats.n_events),
        "modules": [{"id": int(m), "name": fs.MODULE_NAMES.get(int(m), str(m)), "words": int(w)}
                    for m, w in zip(stats.module_ids, stats.module_words)],
    })
    return {
        "file": {"path": relative_path, "size": evio_file.file_size, "header_layout": evio_file.header_layout,
                 "endian": header.endian, "file_type": header.file_type, "version": header.version,
                 "record_count": evio_file.record_count, "has_trailer": evio_file.trailer is not None,
                 "header_record_count": header.record_count, "trailer_position": header.trailer_position,
                 "first_record_offset": evio_file.first_record_offset, "index_array_length": header.index_array_length,
                 "user_header_length": header.user_header_length},
        "records": records,
        "scan_checks": [list(check) for check in evio_file.checks],
        "totals": totals,
        "frames": {**asdict(stats.checks), "frame_period_ns": stats.frame_period_ns},
        "rocs": per_roc_rows(stats),
        "checks": file_checks(evio_file, records, stats),
        "stats": {"version": stats.version, "workers": stats.workers, "wall_seconds": stats.wall_seconds, "tick_ns": stats.tick_ns,
                  "bytes_processed": stats.bytes_processed, "cached": from_cache},
    }


# ---------------------------------------------------------------- histograms
def _axis_index(values: np.ndarray, value: int, what: str) -> int:
    position = int(np.searchsorted(values, value))
    if position >= len(values) or int(values[position]) != value:
        raise ApiError(404, f"{what} {value} not in the file (have {values.tolist()})")
    return position


def _select(stats: StreamStats, hist: np.ndarray, roc: Optional[int], port: Optional[int], channel: Optional[int]) -> np.ndarray:
    """Reduce a (roc, port, channel, …) array to the selection, summing the axes left open."""
    if roc is None and (port is not None or channel is not None):
        raise ApiError(400, "port and ch need a roc")
    if port is None and channel is not None:
        raise ApiError(400, "ch needs a port")
    if channel is not None and not 0 <= channel < N_CHANNELS:
        raise ApiError(404, f"channel {channel} outside 0—{N_CHANNELS - 1}")
    if roc is not None:
        hist = hist[_axis_index(stats.rocs, roc, "ROC")]
        if port is not None:
            hist = hist[_axis_index(stats.ports, port, "port")]
            if channel is not None:
                hist = hist[channel]
                return hist
            return hist.sum(axis=0)
        return hist.sum(axis=(0, 1))
    return hist.sum(axis=(0, 1, 2))


def histogram(stats: StreamStats, name: str, roc: Optional[int] = None, port: Optional[int] = None,
              channel: Optional[int] = None) -> dict:
    """One histogram as JSON lists; counts sum to the file totals (hits, or frames for ``hits_per_frame``)."""
    selection = {"roc": roc, "port": port, "ch": channel}
    if name == "hits_per_frame":
        return {"name": name, "counts": stats.hits_per_frame_hist.tolist(), "edges": stats.hits_per_frame_edges.tolist(),
                "x_unit": "hits per frame", "y_unit": "frames", "selection": selection}
    if name == "rate":
        counts = _select(stats, stats.rate_counts, roc, port, channel)
        seconds = stats.frames_per_rate_bin * stats.frame_period_ns * 1e-9
        rate_hz = np.divide(counts, seconds, out=np.zeros(len(counts)), where=seconds > 0)
        return {"name": name, "counts": counts.tolist(), "rate_hz": rate_hz.tolist(), "edges_ns": stats.rate_edges_ns.tolist(),
                "frames_per_bin": stats.frames_per_rate_bin.tolist(), "x_unit": "ns from the first frame", "y_unit": "Hz",
                "selection": selection}
    if name == "occupancy":
        counts = stats.occupancy
        if roc is not None:
            counts = counts[_axis_index(stats.rocs, roc, "ROC")]
            if port is not None:
                counts = counts[_axis_index(stats.ports, port, "port")]
                if channel is not None:
                    if not 0 <= channel < N_CHANNELS:
                        raise ApiError(404, f"channel {channel} outside 0—{N_CHANNELS - 1}")
                    counts = counts[channel:channel + 1]
        elif port is not None or channel is not None:
            raise ApiError(400, "port and ch need a roc")
        return {"name": name, "rocs": stats.rocs.tolist(), "ports": stats.ports.tolist(), "channels": N_CHANNELS,
                "module_id": stats.module_id.tolist(), "counts": counts.tolist(), "y_unit": "hits", "selection": selection}
    if name == "charge":
        return {"name": name, "counts": _select(stats, stats.charge_hist, roc, port, channel).tolist(),
                "edges": stats.charge_edges.tolist(), "x_unit": "charge (13-bit ADC units)", "y_unit": "hits", "selection": selection}
    if name == "time_in_frame":
        return {"name": name, "counts": _select(stats, stats.time_hist, roc, port, channel).tolist(),
                "edges_ns": stats.time_edges_ns.tolist(), "x_unit": "ns from the frame start", "y_unit": "hits", "selection": selection}
    raise ApiError(404, f"unknown histogram {name!r}; names: {', '.join(HISTOGRAM_NAMES)}")
