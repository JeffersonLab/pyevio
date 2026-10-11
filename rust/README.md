# evio-stream: a streaming EVIO v6 reader in Rust

`rust/evio-stream` is a read-only, streaming reader for the files pyevio
reads, written against the same format references
(`docs/evio6_file_structure.md`, `docs/stream_decoding.md`) and checked
against the same rulers. It is a library with two command-line tools.

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

## Rulers and tests

```
cd rust/evio-stream
cargo test --release                    # unit tests + the golden manifest
EVIO_CORPUS=1 cargo test --release --test corpus -- --nocapture   # 140 GB, minutes
cargo fmt --check && cargo clippy --all-targets -- -D warnings
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
`/data/pyevio/logs/phase6_rust_corpus.log`.

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

Writing, random access by record index (the reader is forward-only),
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
