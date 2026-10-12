# GUI viewer

`pyevio gui` opens a page for browsing an EVIO v6 file: file checks, channel occupancy, rate, charge and timing plots, and a clickable structure tree from the file header down to single fADC hit words. Decoding runs in the browser, and the file is never uploaded.

Plots are written for CODA streaming fADC250 files (0xFF60 built time frames); this is the only EVIO flavor tested so far. File-based (triggered) EVIO uses a different decoding method that hasn't been validated here and may hit hiccups on large or unexpected layouts — pass `--mode=file` to skip the streaming-specific decode and show the structure tree only.

## Start it

```bash
pyevio gui                       # file browser starts in the current folder
pyevio gui /path/to/runs         # or in another folder
pyevio gui --port 9000 --no-browser
pyevio gui /path/to/runs --mode=file   # file-based EVIO: structure tree only (experimental)
```

Pick a file with **Browse folders**, **Open .evio file**, or by dragging it onto the page. A 93 MB file loads in under a second. The whole file is held in browser memory, so multi-GB files may not fit.

The page also works without the server: open `pyevio/gui/index.html` directly. Only the folder browser needs `pyevio gui`.

**Files on a remote machine** (e.g. a farm node): run `pyevio gui /path/to/runs --no-browser` there, connect with `ssh -L 8765:localhost:8765 user@host`, and open http://localhost:8765 locally. The server listens on localhost only.

## What it shows

### Overview

Totals, file checks (last-record bits, missing event index, compressed records, frame gaps, out-of-order frames) and notable channels.

![Overview tab](images/web_viewer_overview.png)

### Plots

Three levels, from the whole file down to one channel.

**File:** hits per frame, and total hit rate over time. Gaps show missing frames.

![File-level plots](images/web_viewer_plots_file.png)

**Per ROC:** one block per crate with its board × channel occupancy map, hit rate over time, charge spectrum and hit time within the frame. All maps share one color scale, and charts of the same kind share one y-axis, so crates can be compared directly.

![Per-ROC plots](images/web_viewer_plots_roc.png)

**Per channel:** pick ROC → port → channel, step with Prev / Next, or click a cell in a ROC map.

![Per-channel plots](images/web_viewer_plots_channel.png)

### Structure

A collapsible tree, 100 records per page, with jump-to-record. Each node shows its decoded fields and raw words.

![Structure tab](images/web_viewer_structure.png)

## Data format assumed

```
Record (header 14 w) → Event 0xFF60 (built time frame, 65,536 ns)
  ├─ 0xFF31 stream info: segment 0x32 = frame, timestamp; segment 0x42 = ROC list
  └─ ROC bank (tag = ROC id)
       ├─ 0xFF30 ROC stream info: segment 0x31 = frame, timestamp; segment 0x41 = payload ports
       └─ payload bank (tag = port): length 1 → empty; length 1+N → N hit words
hit word: t = bits[30:17] × 4 ns, ch = bits[16:13], q = bits[12:0]
```

The hit-word layout was inferred from the data and checked for consistency, not taken from a spec. The Overview tab warns if a file breaks those checks. Compressed (LZ4/gzip) records are skipped in the browser and reported.

## Convert to Parquet

[examples/fadc250_stream_to_parquet.py](../examples/fadc250_stream_to_parquet.py) decodes the same hits with pyevio and numpy into two Parquet tables, for analysis outside the browser (requires `pip install pyarrow`):

```bash
python examples/fadc250_stream_to_parquet.py run.evio   # → out/run.hits.parquet, out/run.frames.parquet
```

Hits (`frame, roc, port, ch, t_ns, q`) are sorted by `(frame, t_ns)`. Rows `hit_offset … hit_offset + n_hits` of the hits table belong to that frame.
