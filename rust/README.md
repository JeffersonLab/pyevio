# evio-stream: a streaming EVIO v6 reader in Rust

`rust/` is a cargo workspace of two crates: `evio-stream`, a read-only,
streaming reader for the files pyevio reads, written against the same
format references (`docs/evio6_file_structure.md`,
`docs/stream_decoding.md`) and checked against the same rulers, a library
with two command-line tools; and `evio-tui`, a minimal terminal viewer on
top of it (see [evio-tui](#evio-tui-a-minimal-terminal-viewer) below).

What it reads:

- EVIO v6 files with a file header (`v6`), bare v6 record streams without one
  (`v6-records`, the evio writer buffer dumps), and the JLab SRO hardware
  stream of 8-word block headers with no file header (`sro-8word`).
- Plain, LZ4, LZ4_BEST and gzip records; both byte orders.
- A final record cut by the end of the file: its complete events are kept and
  reported as a `truncated_tail` check, as pyevio does. Garbage after the last
  record ends the scan with a `bad_record_header` check.
- The bank / segment / tagsegment tree of any event, every length checked
  against its container; a bad length is an error value, never a panic.
- CODA streaming fADC250 frames (tag 0xFF60): the frame walk of
  `pyevio.decoders.fadc250_stream`, with the module per port from the 0x41
  payload map, hits for fADC250 ports, word counts for other modules (DCRB),
  empty payload banks, and the AIS counted but never used to locate ROC banks.

What it refuses: evio-library v4 files and v1—3 files, with an error naming
the version (`EVIO version 4 file is not supported ... evio2xml, eviocopy`),
as pyevio does.

The reader works over any `std::io::Read` (file, stdin, pipe), never seeks,
and holds one record in memory at a time. No `unsafe` (`#![forbid(unsafe_code)]`).

## Library

```rust
use evio_stream::{Bank, Checks, FrameDecoder, RecordReader};

let mut reader = RecordReader::open("run.evio")?;      // or RecordReader::new(any_read)
println!("{}", reader.layout());                       // v6 | v6-records | sro-8word
let mut decoder = FrameDecoder::new();
while let Some(record) = reader.next_record()? {       // one record in memory
    for event in record.iter_events() {                // Words<'_> over the record buffer
        let bank = Bank::event(event)?;                // tag, data_type, num, length, children()
        if let Some(frame) = decoder.decode(event) {   // None for a non-0xFF60 event
            for hit in frame.hits() { /* frame, timestamp, roc, stream, port, channel, time_raw, charge */ }
        }
    }
    reader.recycle(record);                            // reuse the buffer
}
reader.truncated_tail();                               // Option<TruncatedTail>
reader.bad_record_header();                            // Option<BadRecordHeader>
// Or in one call: Checks::scan(reader)? -> records, events, trailer, checks, errors, bytes
```

Types: `RecordReader<R>` (iterator of `Result<Record, Error>`), `Record`
(header, buffer, `events()`, `event(i)`, `errors`, `is_truncated`),
`RecordHeader` / `FileHeader` / `FileLayout` / `Compression`, `Words`
(bounds-checked word view in file byte order), `Bank` / `Children` /
`WalkError`, `FrameDecoder` / `Frame` / `Hit` / `RocBank` / `PayloadBank`,
`Checks`, `Error`.

Layout decisions:

- Words stay in file byte order; `Words::get` decodes each word with the
  file's `ByteOrder` on read. A little-endian file on a little-endian host
  pays nothing, and the SRO walk reads only headers, so swapping a whole
  15 GB record stream in place would touch every byte twice for nothing.
- A `Record` owns one `Vec<u8>`: the bytes after the header as read from the
  file for a plain record, the decompressed bytes for a compressed one
  (sized as index array + padded user header + uncompressed length). Event
  spans and bank views are slices into it; nothing is copied per event. The
  reader reads the body into reserved capacity without zeroing it, and
  `recycle` hands the allocation back for the next record.
- `FrameDecoder` keeps its scratch vectors across frames; a decoded `Frame`
  borrows the event and the decoder, and `hits()` decodes the words lazily.

## Tools

```
cargo build --release
target/release/evio-count FILE|- [--fadc250]
target/release/evio-dump  FILE|- [--record N] [--event M] [--words K]
```

`evio-count` prints layout, byte order, records, events, trailer, the
`truncated_tail` and `bad_record_header` checks, record errors and the
throughput. With `--fadc250` it walks every stream frame and decodes every
hit word: frames, ROC banks, payload banks (empty, unmapped), fADC250 hits
(complete records and the salvaged tail separately), other-module words, flag
words, AIS counts, decode errors, a per-ROC table.

`evio-dump` prints the file header and one line per record header; with
`--record N` the full header and event table of record N; with `--event M`
the bank tree of event M of record N (tag, type, num, length) with the first
K words of each leaf in hex.

## evio-tui: a minimal terminal viewer

```
cd rust
cargo build --release
target/release/evio-tui FILE
```

`rust/evio-tui` is a fast way to look inside a file from a terminal,
nothing more; the rich viewers are the Python ones (`pyevio gui`,
`pyevio web`). It is built on `ratatui` and `crossterm` (the only
dependencies beyond `evio-stream`). Three screens, one level deeper each:

- **Records.** A background thread streams the file from the start with
  `RecordReader` and sends one small summary per record over a channel; the
  list fills while the scan runs and the interface never waits on the file.
  One row per record: index, offset, bytes, record or trailer, events
  declared and found, compression, truncated, error count (rows with errors
  or a cut tail in red). The side panel (terminals 100 columns and wider)
  shows every header field of the selected record and the file line
  (layout, byte order, file type, declared record count, dictionary, first
  event, trailer index). The status line shows records, events, bytes,
  seconds and GB/s while the scan runs, then the checks: trailer,
  `truncated_tail (declared, present, salvaged)`, `bad_record_header`.
- **Events of a record.** Enter opens the selected record: index, length in
  words, tag, data type, num, and "stream frame" for tag 0xFF60. One record
  is in memory at a time.
- **Bank tree and words of an event.** Enter opens the selected event: one
  row per bank, segment or tagsegment at every level (kind, tag, type, num,
  length, total words), a `WalkError` in red where a length was bad. The
  panel below shows the selected structure's first 64 words in hex, 8 per
  row with the word offset at the left; `n`/`p` move the window. For a
  stream frame `f` shows the `FrameDecoder` summary: frame number,
  timestamp, ROC and payload bank counts, hits, other-module words, AIS, a
  per-ROC table (module, payload banks, empty, hits, other words), and the
  decoder's errors (red) and warnings (yellow).

Keys: arrows, PgUp/PgDn, Home/End, `g`/`G` move; Enter (or Right) opens;
Esc (or Left, Backspace) goes back one level (it closes the frame panel
first); `n`/`p` next/previous 64 words; `f` frame summary; `?` one-screen
help; `q` or Ctrl-C quits from anywhere.
Standard input is not supported: the viewer opens records by seeking to
their offset, so `evio-tui -` prints a message and exits.

Random access: the library reader is forward-only, but every layout starts
each record with a record header, so opening a record seeks a fresh file
handle to the offset noted during the scan and starts a new
`RecordReader::new` there: a 14-word v6 header is taken for the
`v6-records` layout, an 8-word SRO block header for `sro-8word`, and the
first record read is the one wanted. No layout hint and no library change
were needed; `evio-tui/tests/corpus_access.rs` checks it on the converted
corpus file and on the raw SRO stream (record 7000 of each, the trailer and
the cut tail), and `tests/render.rs` draws the screens into ratatui's
`TestBackend` for the golden files.

Measured on this host, one thread, by the harness in
`tests/corpus_access.rs` (`--test-threads=1`; the logged run is
`/data/pyevio/logs/phase6_rust_tui_tests.log`):

- `sro_000791.evio.00000.v6` (15.5 GB, `v6`): scan 1.66 s, 9.35 GB/s with
  the file in the page cache, 7891 records + trailer (the list has one row
  per record, the trailer included, and the status line names the trailer
  separately so the count matches `evio-count`), 86,794 events; the first
  record message arrives 1 ms after the thread starts; record 7000 (offset
  0x3364DC560, 1.9 MB, 11 stream frames) opens in 0.3 ms.
- Raw `sro_000791.evio.00000` (15.5 GB, `sro-8word`): 7893 records (the cut
  tail included), 86,794 events, `truncated_tail (1967400, 531760, 2)`;
  record 7000 (offset 0x33610BA10) opens in 0.4 ms; the cut tail opens with
  its 2 salvaged events. The scan took 1.59 s (9.80 GB/s) with the file in
  the page cache and 4.97 s (3.13 GB/s) in the logged run, where part of it
  had been evicted by the scans before it.

Opening a record is a seek plus one record read, so it costs the same at
record 0 and at record 7000; a 64 MB gzip record would take as long as its
decompression.

Code layout: `scan` (the thread and its messages), `access` (seek and read
one record), `state` (what is shown and selected, the keys), `format` (the
text of every cell and panel), `tree` (the flattened bank tree), `ui` (the
only module that draws). Everything but `ui` runs without a terminal, and
the state machine is what the tests drive.

## Rulers and tests

```
cd rust
cargo test --release --workspace        # both crates: unit, golden, render, corpus harness
cargo fmt --all --check && cargo clippy --all-targets -- -D warnings
cd evio-stream
EVIO_CORPUS=1 cargo test --release --test corpus -- --nocapture   # 140 GB, minutes
```

- Golden set (`tests/golden.rs`, `tests/data/golden_manifest.tsv` generated
  from the evio-optim `manifest.yaml`): all 46 rows pass. 39 readable rows
  (C++ and Java writers: plain LE/BE, LZ4, LZ4_BEST, gzip, dictionary, first
  event, odd user header, trailer with and without index, append, split,
  many records, 12-deep nesting, one 40 MB event, zero events, HIPO file
  type, buffer dumps) give the manifest event count with the expected layout
  and no check; every event's bank tree walks without error. The 7 reject
  rows (v4 LE/BE, v4 with dictionary, v1—3) end with the version error.
  The test skips when `/data/evio-lib/golden` (or `EVIO_GOLDEN_ROOT`) is absent.
- Corpus (`tests/corpus.rs`, `tests/data/corpus.tsv` from `corpus.yaml`): the
  8 converted v6 files (plain and LZ4) give their record and event counts.
- Raw SRO files of run 791: file 00000 gives 7893 records (the cut tail
  included), 86,794 events, `truncated_tail (1967400, 531760, 2)`,
  763,992,122 fADC250 hits in the complete blocks + 17,652 in the two
  salvaged frames = 764,009,774, 3,065,574,754 DCRB words, 357,743 empty
  payload banks, 0 decode errors; file 00001 gives 7893 records, 86,805
  events, `(1960136, 1939708, 10)`, 764,052,526 hits in the complete blocks
  + 87,735 in the ten salvaged frames = 764,140,261, and 3,065,895,268 DCRB
  words. These match pyevio and the independent decoders
  (`docs/stream_decoding.md`, the per-ROC `+salvage` columns of the SRO
  reference tables). On both files `evio-count` also reports one record
  error: the cut event of the tail ("event 2 ... declares 176292 bytes,
  record data ends at ..."), the same message pyevio keeps in
  `Record.errors`; it is the expected trace of the truncation, not a defect.

Logs of the last run: `/data/pyevio/logs/phase6_rust_tests.log` and
`/data/pyevio/logs/phase6_rust_corpus.log`; the workspace run with
evio-tui: `/data/pyevio/logs/phase6_rust_tui_tests.log`.

## Speed (one thread)

Measured on this host (24 cores, 62 GB) with the raw file
`sro_000791.evio.00000` (15.5 GB) in the page cache, one thread:

- `evio-count --fadc250`: 3.1 s, 5.0 GB/s, every one of the 764 million hit
  words decoded (channel, time, charge, flag) and the 3.8 billion payload
  words walked. pyevio decodes hits at about 1 GB/s on one core, and
  `pyevio stats` takes 26 s on the same file with 20 workers.
- `evio-count` alone (record scan, event walk by length words): 1.6 s,
  9.5 GB/s.
- Converted v6 files from disk, record scan only: about 3.1 GB/s plain
  (1.3 GB/s on a cold first read); LZ4 records 1.3 GB/s of compressed input,
  bounded by the safe-Rust LZ4 decoder (`lz4_flex` with `safe-decode`),
  which also bounds `--fadc250` on an LZ4 file (11.2 s, 1.07 GB/s of input,
  about 1.4 GB/s of decompressed output).

## Not included

Writing, random access by record index in the library (the reader is
forward-only; evio-tui seeks to offsets it noted in its own scan),
dictionaries and composite data decoding (the bytes are exposed, not
parsed), and the statistics layer of `pyevio.stream_stats`.

## Streaming differences from pyevio

pyevio walks the whole block chain at open to tell an evio-library v4 file
from an SRO stream. A forward-only reader cannot look ahead, so a v4 file
whose first block has no bit-info bits yields its first blocks as `sro-8word`
records and returns the version error on the block that sets the last-block
or dictionary bit. The golden test drains the iterator and checks the
terminal error. Likewise, after a record with the last-record bit, bytes that
are no record header end the scan quietly (pyevio records no check there
either), and a record cut by the end of the file after such a record is
reported as a cut tail rather than ignored.
