"""Decode CODA streaming fADC250 hits from an EVIO v6 file into Parquet.

Container access goes through pyevio. The hit words are decoded with numpy.
Requires pyarrow (pip install pyarrow). Browse the same files with `pyevio gui`.

Hit word layout (big-endian u32):
  bits 30-17 time in 4 ns ticks from frame start
  bits 16-13 channel
  bits 12-0  charge q

Usage:
  python fadc250_stream_to_parquet.py run.evio            # writes out/run.hits.parquet + out/run.frames.parquet
  python fadc250_stream_to_parquet.py runs/*.evio --out out --force

Read the output:
  hits   = pq.read_table("out/run.hits.parquet")    # frame, roc, port, ch, t_ns, q (sorted by frame, t_ns)
  frames = pq.read_table("out/run.frames.parquet")  # frame, timestamp_ns, n_hits, hit_offset
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyevio import EvioFile

DECODER_VERSION = "1"
HIT_FORMAT = "fadc250-stream-v1: t=bits[30:17]*4ns ch=bits[16:13] q=bits[12:0]"


# ---------------------------------------------------------------- decoding
def decode(path):
    """Return (hits dict of numpy arrays, frames dict, stats dict)."""
    f = EvioFile(str(path))
    mm = f.mm
    dt = np.dtype(f.header.endian + "u4")
    words, b_frame, b_roc, b_port, b_n = [], [], [], [], []     # raw hit words + one entry per non-empty payload bank
    frames, ts_list, nhits, payload_words = [], [], [], 0
    skipped = 0
    for ri in range(f.record_count):
        for e in f.get_record(ri).get_events():
            w = np.frombuffer(mm, dtype=dt, count=e.length // 4, offset=e.offset)
            if (w[1] >> 16) != 0xFF60 or (w[3] >> 16) != 0xFF31:
                skipped += 1
                continue
            frame = int(w[5])
            ts = int(w[6]) | (int(w[7]) << 32)
            i = 2 + int(w[2]) + 1                      # skip stream-info bank
            nh = 0
            while i < len(w):
                end = i + int(w[i]) + 1
                roc = int(w[i + 1] >> 16)
                j = i + 2
                if (w[j + 1] >> 16) == 0xFF30:         # skip ROC stream-info bank
                    j += int(w[j]) + 1
                while j < end:
                    plen = int(w[j])
                    payload_words += plen - 1
                    if plen > 1:
                        words.append(w[j + 2:j + plen + 1])
                        b_frame.append(frame); b_roc.append(roc); b_port.append(int(w[j + 1] >> 16)); b_n.append(plen - 1)
                        nh += plen - 1
                    j += plen + 1
                i = end
            frames.append(frame)
            ts_list.append(ts)
            nhits.append(nh)
    h = np.concatenate(words).astype(np.uint32) if words else np.empty(0, np.uint32)
    del words
    b_n = np.asarray(b_n, dtype=np.int64)
    hits = {
        "frame": np.repeat(np.asarray(b_frame, np.uint32), b_n),
        "roc": np.repeat(np.asarray(b_roc, np.uint16), b_n),
        "port": np.repeat(np.asarray(b_port, np.uint8), b_n),
        "ch": ((h >> 13) & 0xF).astype(np.uint8),
        "t_ns": (((h >> 17) & 0x3FFF) * 4).astype(np.uint16),
        "q": (h & 0x1FFF).astype(np.uint16),
    }
    # sort by frame, then time in frame (the event builder delivers some frames out of order)
    order = np.lexsort((hits["t_ns"], hits["frame"]))
    hits = {k: v[order] for k, v in hits.items()}
    frames, ts_list, nhits = map(np.asarray, (frames, ts_list, nhits))
    out_of_order = int((np.diff(frames) < 0).sum())
    order = np.argsort(frames, kind="stable")
    frames, ts_list, nhits = frames[order], ts_list[order], nhits[order]
    offsets = np.concatenate([[0], np.cumsum(nhits)[:-1]]) if len(nhits) else np.empty(0)
    frame_tab = {"frame": frames.astype(np.uint32), "timestamp_ns": ts_list.astype(np.uint64),
                 "n_hits": nhits.astype(np.uint32), "hit_offset": offsets.astype(np.uint64)}
    stats = {"records": f.record_count, "frames": len(frames), "hits": len(h), "skipped_events": skipped,
             "out_of_order": out_of_order, "payload_words": payload_words}
    return hits, frame_tab, stats


def validate(hits, frames, stats):
    """Cheap self-consistency checks; raises AssertionError on failure."""
    assert stats["hits"] == stats["payload_words"], "hit count != sum of (payload bank length - 1)"
    assert int(frames["n_hits"].sum()) == stats["hits"], "per-frame hit counts do not add up"
    if stats["hits"]:
        assert hits["t_ns"].max() < 65536, "hit time outside the 65.5 us frame"
    f = frames["frame"]
    if len(f):
        return {"missing_frames": int(f[-1] - f[0] + 1 - len(np.unique(f))), "duplicate_frames": int(len(f) - len(np.unique(f)))}
    return {}


def convert(path, out_dir, force=False):
    path, out_dir = Path(path), Path(out_dir)
    hits_p = out_dir / f"{path.stem}.hits.parquet"
    frames_p = out_dir / f"{path.stem}.frames.parquet"
    if not force and hits_p.exists() and hits_p.stat().st_mtime > path.stat().st_mtime:
        print(f"skip  {path.name} (already converted)")
        return
    t0 = time.time()
    hits, frames, stats = decode(path)
    checks = validate(hits, frames, stats)
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
