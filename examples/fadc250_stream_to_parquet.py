"""Decode CODA streaming fADC250 hits from an EVIO v6 file into Parquet.

Container access goes through pyevio; the frame walk and the hit word
layout live in pyevio.decoders.fadc250_stream. Requires pyarrow
(pip install pyarrow). Browse the same files with `pyevio web`.

Usage:
  python fadc250_stream_to_parquet.py run.evio            # writes out/run.hits.parquet + out/run.frames.parquet
  python fadc250_stream_to_parquet.py runs/*.evio --out out --force

Read the output:
  hits   = pq.read_table("out/run.hits.parquet")    # frame, roc, stream, port, ch, t_ns, q (sorted by frame, event, t_ns)
  frames = pq.read_table("out/run.frames.parquet")  # frame, timestamp_ns, n_hits, hit_offset (sorted by frame, event)

Every hit of a file stays in memory until it is written, so a 15 GB SRO file
needs more RAM than most hosts have; this example is for files up to a few GB.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from pyevio import EvioFile
from pyevio.decoders import fadc250_stream as fs

DECODER_VERSION = "2"
HIT_FORMAT = fs.HIT_FORMAT


# ---------------------------------------------------------------- decoding
def decode(path):
    """Return (hits dict of numpy arrays, frames dict, stats dict, StreamHits).

    Hits are sorted by (frame, event index, t_ns) and frames by (frame, event index),
    so the hit ranges in the frames table stay right when a frame number repeats.
    """
    with EvioFile(str(path)) as evio_file:
        endian = evio_file.header.endian
        chunks = []
        out_of_bounds = 0
        event_index = 0
        for record_i in range(evio_file.record_count):
            record = evio_file.get_record(record_i)
            events = []
            # A trailer record declares 0 events; its index holds record positions, not event lengths.
            for offset, length in record.get_event_offsets()[:record.header.event_count]:
                if offset + length > evio_file.file_size:
                    out_of_bounds += 1
                    continue
                events.append(fs.event_words(evio_file.mm, offset, length, endian))
            chunks.append(fs.decode_frames(events, first_event_index=event_index))
            event_index += len(events)
        stream_hits = fs.concatenate_stream_hits(chunks)
        del chunks
        record_count = evio_file.record_count

    order = np.lexsort((stream_hits.t_ns, stream_hits.event_index, stream_hits.frame))
    hits = {
        "frame": stream_hits.frame[order],
        "roc": stream_hits.roc[order],
        "stream": stream_hits.stream[order],
        "port": stream_hits.port[order],
        "ch": stream_hits.channel[order],
        "t_ns": stream_hits.t_ns[order],
        "q": stream_hits.charge[order],
    }
    frames = stream_hits.frames
    out_of_order = int((np.diff(frames["frame"].astype(np.int64)) < 0).sum())
    frame_order = np.lexsort((frames["event_index"], frames["frame"]))
    frames = frames[frame_order]
    n_hits = frames["n_hits"].astype(np.uint64)
    offsets = np.concatenate([[0], np.cumsum(n_hits)[:-1]]) if len(n_hits) else np.empty(0)
    frame_tab = {"frame": frames["frame"].astype(np.uint32), "timestamp_ns": frames["timestamp"].astype(np.uint64),
                 "n_hits": frames["n_hits"].astype(np.uint32), "hit_offset": offsets.astype(np.uint64)}
    stats = {"records": record_count, "frames": len(frames), "hits": stream_hits.n_hits,
             "skipped_events": stream_hits.n_other_events + out_of_bounds, "out_of_order": out_of_order,
             "payload_words": stream_hits.n_payload_words, "non_fadc_words": stream_hits.non_fadc_words,
             "empty_payload_banks": stream_hits.n_empty_payload_banks, "decode_errors": stream_hits.errors}
    return hits, frame_tab, stats, stream_hits


def validate(stream_hits, frames):
    """Self-consistency checks; raises ValueError on failure and returns the frame checks."""
    stream_hits.validate()  # decode errors, hit words with bit 31 set, hits + non-fADC words vs payload words
    if int(frames["n_hits"].sum()) != stream_hits.n_hits:
        raise ValueError("per-frame hit counts do not add up")
    frame_numbers = frames["frame"]
    if len(frame_numbers):
        n_unique = len(np.unique(frame_numbers))
        return {"missing_frames": int(frame_numbers[-1] - frame_numbers[0] + 1 - n_unique),
                "duplicate_frames": int(len(frame_numbers) - n_unique)}
    return {}


def convert(path, out_dir, force=False):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path, out_dir = Path(path), Path(out_dir)
    hits_p = out_dir / f"{path.stem}.hits.parquet"
    frames_p = out_dir / f"{path.stem}.frames.parquet"
    if not force and hits_p.exists() and hits_p.stat().st_mtime > path.stat().st_mtime:
        print(f"skip  {path.name} (already converted)")
        return
    t0 = time.time()
    hits, frames, stats, stream_hits = decode(path)
    checks = validate(stream_hits, frames)
    meta = {"source_file": path.name, "source_bytes": str(path.stat().st_size), "decoder_version": DECODER_VERSION,
            "hit_format": HIT_FORMAT, **{k: str(v) for k, v in {**stats, **checks}.items()}}
    out_dir.mkdir(parents=True, exist_ok=True)
    for table, p in ((hits, hits_p), (frames, frames_p)):
        t = pa.table(table)
        t = t.replace_schema_metadata({k.encode(): v.encode() for k, v in meta.items()})
        pq.write_table(t, p, compression="zstd")
    print(f"done  {path.name}: {stats['frames']:,} frames, {stats['hits']:,} hits, "
          f"{stats['out_of_order']:,} out-of-order, {checks} -> {hits_p.name} "
          f"({hits_p.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.1f} s")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".evio files to convert")
    ap.add_argument("--out", default="out", help="output folder (default: ./out)")
    ap.add_argument("--force", action="store_true", help="re-convert even if the Parquet is up to date")
    args = ap.parse_args(argv)
    failed = 0
    for p in args.files:
        try:
            convert(p, args.out, args.force)
        except Exception as e:  # keep going with the other files
            failed += 1
            print(f"FAIL  {p}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
