# pyevio

A Python library and command-line tool for reading and inspecting EVIO (Event Input/Output) files from Jefferson Lab.

## Overview

`pyevio` provides a Python API and a command-line interface for:

- Reading EVIO v6 files, record by record and event by event, through a memory map
- Reading the raw streaming files the JLab SRO hardware writes (8-word block headers, no file header)
- Decompressing LZ4, LZ4_BEST and gzip records on access
- Walking the bank tree of an event (banks, segments, tagsegments) with every length checked against its container
- Converting bank data to NumPy arrays
- Decoding CODA streaming fADC250 time frames into hit arrays and computing histograms and file checks over a whole file in one parallel pass
- Browsing a file — checks, plots, record table, structure tree — in a browser (`pyevio web`, [docs/web_viewer.md](docs/web_viewer.md)) or in the terminal over SSH (`pyevio gui`, [docs/console_gui.md](docs/console_gui.md)); the two share one statistics cache

## What pyevio reads

| file | how it is read |
|---|---|
| EVIO v6 file (file type id `EVIO` or `HIPO` in word 0, 14-word headers) | native; dictionary and first event in the user header are skipped with their padding |
| Records compressed with LZ4, LZ4_BEST or gzip | decompressed on first access into a per-record buffer |
| A trailer record with or without an index | exposed as `EvioFile.trailer`; it yields no events |
| JLab SRO hardware stream: no file header, 8-word block headers with version word 4 and no bit-info bits | native, as `header_layout == "sro-8word"`; a final block cut by the end of the file keeps its complete events and is reported in `checks` |
| EVIO v4 or v1—3 file written by the evio library | `ValueError` naming the version and pointing at the evio tools (`evio2xml`, `eviocopy`) |
| EVIO v6 record stream without a file header (an evio writer buffer dump) | native, as `header_layout == "v6-records"`; records start at offset 0 and `EvioFile.header` is a stand-in |

The SRO detection walks the whole chain of 8-word headers at open: a chain in which some header carries a bit-info bit (last-record, dictionary) is an evio-library v4 file and raises the version error. The format details are in [docs/evio6_file_structure.md](docs/evio6_file_structure.md) (container format) and [docs/stream_decoding.md](docs/stream_decoding.md) (streaming fADC250 payload).

## Installation

```bash
pip install pyevio
```

The browser viewer installs as an extra: `pip install 'pyevio[web]'` (see [docs/web_viewer.md](docs/web_viewer.md)); the console viewer as `pip install 'pyevio[gui]'` (see [docs/console_gui.md](docs/console_gui.md)).

Requirements: Python 3.9 or later. Dependencies installed with the package: click, lz4, rich, numpy, matplotlib.

## Python API

Objects nest the way the file does: `EvioFile` → `Record` → `Event` → `Bank`, `Segment`, `TagSegment`.

```python
from pyevio import EvioFile

with EvioFile("run.evio") as evio_file:
    print(evio_file.header_layout)            # "v6" or "sro-8word"
    print(evio_file.record_count)             # data records; the trailer is evio_file.trailer
    print(evio_file.get_total_event_count())
    print(evio_file.checks)                   # [] for a clean file

    record = evio_file.get_record(0)
    for event in record.get_events():
        root = event.get_bank()               # the event's top-level bank
        for child in root.get_children():     # Bank, Segment or TagSegment, typed by the parent
            print(hex(child.tag), child.data_type, child.length)
        data = root.to_numpy()                # None for a container bank
```

### Records and events

- `record_count`, `get_record(i)`, `iter_records()` cover the data records. The trailer record (header type 3) is `evio_file.trailer`, or `None` when the file ends in a plain record. `Record.is_trailer` tells the two apart.
- `Record.get_events()` builds `Event` objects; `Record.get_event_offsets()` returns `(offset, length)` pairs without building objects, which is the way to walk a large file.
- For an uncompressed record, offsets are file offsets and `Record.buffer` is the file mmap. For an LZ4, LZ4_BEST or gzip record, `Record.buffer` is the decompressed bytes and every offset indexes that buffer. Read event bytes through `record.buffer`; `Record.release()` drops the decompressed buffer and the Event cache.
- `EvioFile` caches one `Record` (the last accessed), so a compressed file never sits in memory whole.
- A final record whose declared length runs past the end of the file stays the last record: `Record.is_truncated` is true, `record.header.event_count` is the header's claim and `len(record.get_event_offsets())` is the number of complete events present. `record_count` counts that record.

### Checks and errors

- `EvioFile.checks` lists what the record scan found wrong with the file: `("truncated_tail", declared_bytes, present_bytes, events_salvaged)` and `("bad_record_header", offset, message)` where the scan stopped on bytes that are no record header (garbage, a byte-swapped header, a second file header of two concatenated files).
- `Record.errors` and `Bank.errors` list why an event or child walk stopped early: a length that runs past its container, a decompression failure. A bank whose declared length passes its parent is clamped to the parent; no read runs past a container because a length word said so.

### Closing

`close()` or the end of the `with` block releases the mmap and the file. A numpy array or memoryview that still refers to the mmap makes `close()` raise `BufferError` after the file is closed; drop the views (`del`) and call `close()` again. Such views come from `np.frombuffer` on the mmap, for example `fadc250_stream.event_words()` on an uncompressed record; `Bank.to_numpy()` and `Bank.get_data()` return copies.

### Bank lengths

A length word counts the words that follow it: a structure with length N occupies N + 1 words. `Bank.size`, `data_length` and `end_offset` follow that rule, and `Bank.get_children()` types the children by the parent's data type (banks of banks hold `Bank`, banks of segments hold `Segment`, banks of tagsegments hold `TagSegment`).

## Streaming fADC250 decoder and statistics

`pyevio.decoders.fadc250_stream` holds the one copy of the frame walk and the hit-word layout. It works on `uint32` event words and returns numpy columns (`frame`, `roc`, `stream`, `port`, `channel`, `time_raw`, `t_ns`, `charge`) plus per-frame, per-ROC-bank and per-empty-bank tables. Only ports the payload map names as fADC250 are decoded as hits; other modules (DCRB) are counted as words.

```python
from pyevio import EvioFile
from pyevio.decoders import fadc250_stream as fs

with EvioFile("sro_000791.evio.00000") as evio_file:
    record = evio_file.get_record(0)
    buffer = record.buffer
    events = [fs.event_words(buffer, offset, length, evio_file.header.endian)
              for offset, length in record.get_event_offsets()]
    hits = fs.decode_frames(events)
    print(hits.n_stream_frames, hits.n_hits, hits.frames["frame"][:4])
    del events  # mmap views; close() refuses while they live
```

`pyevio.stream_stats` runs the decoder over a whole file in parallel (one worker per record range) and returns `StreamStats`: rate, charge and time-in-frame histograms per (ROC, port, channel), occupancy, hits per frame, empty banks per port, and frame checks (stride, missing and duplicate frames, out-of-order frames, timestamp-step outliers). The result round-trips through `.npz`.

```python
from pyevio.stream_stats import compute_stream_stats, StreamStats

stats = compute_stream_stats("sro_000791.evio.00000", workers=8)
print(stats.n_frames, stats.n_hits, stats.checks.stride, stats.checks.missing_frames)
print(stats.rocs, stats.ports)                 # axes of every histogram
rate = stats.rate_hz(roc=7)                    # Hz per time bin, summed over ports and channels
charge = stats.per_roc(stats.charge_hist)      # (roc, 128) counts; edges in stats.charge_edges
stats.save("run791.npz")
stats = StreamStats.load("run791.npz")         # rejects a file written by another stats version
```

The frame layout, the hit word, the time units and the checks are described in [docs/stream_decoding.md](docs/stream_decoding.md). `examples/fadc250_stream_to_parquet.py` writes the hits of a file to Parquet with the same decoder.

## Command-line usage

`pyevio --help` lists the commands; `pyevio COMMAND --help` lists the options of one command.

### File information

```bash
pyevio info run.evio          # header fields and a record table
pyevio info run.evio --full   # every record row
pyevio info run.evio --hexdump
```

### Records and events

```bash
pyevio record run.evio 5                  # record 5: header, events, tag statistics
pyevio record run.evio 5 --analyze        # event size distribution
pyevio event run.evio 25                  # event 25 by global index
pyevio event run.evio 2 --record 3        # event 2 of record 3
pyevio event run.evio 25 --hexdump
```

### Event word dumps

```bash
pyevio dump run.evio                      # words of event 2 (the default start) as hex
pyevio dump run.evio 100 --start 200      # 100 events from global index 200
pyevio dump run.evio 100 --record 5       # 100 events of record 5
pyevio dump run.evio --as dec --only 0,1,2
```

### Streaming statistics

```bash
pyevio stats run.evio                     # totals, frame checks, per-ROC table
pyevio stats run.evio --out run.npz       # save the histograms
pyevio stats run.evio --workers 8 --records 200 --tick-ns 1 --rate-bins 256
```

### Debugging a streaming record

```bash
pyevio debug run.evio --record 1                 # frame summary per event
pyevio debug run.evio --record 1 --event 0       # one event, every ROC bank
pyevio debug run.evio --record 1 --payload 0 --hexdump
```

### Raw words

```bash
pyevio hex run.evio 256 --bytes            # 30 words from byte offset 256 (decimal offsets only)
pyevio hex run.evio 200 --size 50          # 50 words from word 200
pyevio hex run.evio 256 --bytes --endian '<'   # the default is big-endian
```

### Other commands

```bash
pyevio ana run.evio --per-record           # event tag statistics over the file
pyevio gui run.evio                        # console viewer (Textual), see docs/console_gui.md
pyevio gui /path/to/runs                   # the same, picking a file from a folder
pyevio web /path/to/runs                   # browser viewer, see docs/web_viewer.md
```

## Development

```bash
git clone https://github.com/JeffersonLab/pyevio
cd pyevio
uv sync             # installs the package and the dev group (pytest, pyyaml, pyarrow)
uv run pytest -q
```

Two test sets need data outside the repository and skip when it is absent:

- The golden-set harness (`tests/golden/test_golden.py`) reads the evio golden manifest from the path in `GOLDEN_MANIFEST` and the files from the `golden_root` the manifest names. One test runs per manifest row: event counts, no event past the end of the file, and the version error for v4 and v1—3 files.
- The SRO corpus tests (`tests/golden/test_corpus.py`) scan 15 GB files and run only with `PYEVIO_CORPUS=1`:

  ```bash
  PYEVIO_CORPUS=1 uv run pytest tests/golden/test_corpus.py -v
  ```

## License

[License information]

## Contributors

[Contributor information]
