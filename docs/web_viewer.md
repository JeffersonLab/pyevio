# Web viewer

`pyevio web` opens a page for browsing an EVIO v6 file: file checks, channel occupancy, rate, charge and timing plots, a record table and a structure tree from the file header down to single fADC hit words. pyevio reads and decodes the file on the server; the browser receives JSON (histograms, record headers, tree nodes, requested words) and renders it. The file never leaves the machine it is on, and a 15 GB file produces the same few kilobytes per response as a 90 MB one.

Plots are written for CODA streaming fADC250 files (0xFF60 built time frames). Any other EVIO v6 file shows the record table and the structure tree.

## Install and start

The server needs FastAPI and uvicorn, installed as the optional extra:

```bash
pip install 'pyevio[web]'
```

```bash
pyevio web                       # serve the current folder
pyevio web /path/to/runs         # serve another folder
pyevio web /path/to/runs --port 9000 --no-browser --workers 8 --cache-dir /scratch/pyevio-cache
```

The command prints a URL of the form `http://127.0.0.1:8765/?token=…` and opens it in a browser (unless `--no-browser`). The token is random per start and required on every API request: another user on the same machine cannot read your files by guessing the port. Open the printed URL, including the token; a page without it shows nothing.

Only files under ROOT (the folder given on the command line, default the current folder) can be listed and opened. Paths are resolved with symlinks before the check, so a link that points outside ROOT is refused.

### Analysis and cache

Opening a file starts one analysis job on the server (`pyevio stats` on all cores minus two, or `--workers N`); the page shows its progress and the estimated time to completion. Opening another file cancels the running job. Results are cached as `.npz` under `~/.cache/pyevio/stats` (or `--cache-dir DIR`), keyed by the file path, size, modification time and the statistics version, so a second open of an unchanged file is immediate. The cache folder can be deleted at any time.

### Files on a remote machine

On a farm node, run the server there and forward the port over SSH:

```bash
# on the node
pyevio web /path/to/runs --no-browser
# on your machine
ssh -L 8765:localhost:8765 user@node
```

Then open the URL the server printed on the node, token included, in a local browser. The server listens on 127.0.0.1 only and rejects requests whose `Host` header is not `localhost` or `127.0.0.1`, which also blocks web pages that reach the port through DNS rebinding.

`--verbose` logs every request; by default only errors are logged.

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
