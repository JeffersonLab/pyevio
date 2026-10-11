# Changelog

All notable changes to pyevio are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Finding numbers (F1—F68) refer to the project's defect ledger.

## [Unreleased]

### Added

- `rust/`: a cargo workspace beside the Python package. `evio-stream` is a streaming, read-only EVIO v6 reader library over any `std::io::Read` (v6 files, bare record streams and the SRO 8-word block stream, both byte orders, LZ4/LZ4_BEST/gzip records, checked bank walk, fADC250 frame walk) with the `evio-count` and `evio-dump` tools; it reproduces the golden manifest, the corpus and the SRO reference numbers and decodes every hit of a 15.5 GB raw file in about 3 s on one thread. `evio-tui` is a minimal terminal viewer on top of it (records, events, bank tree and words, frame summary). See `rust/README.md`. Not part of the Python wheel.
- `stream_stats.start_worker_services()`: starts the multiprocessing forkserver and resource tracker early, so a program that captures stderr (Textual replaces it with an object whose `fileno()` is -1) can still run the parallel analysis; `compute_stream_stats` calls it too (F63).
- File check "AIS entries identical": the decoder counts the aggregation info segment entries and distinct values per frame; `stream_stats` and the viewer Overview report frames whose AIS repeats one stream's entry in every slot (F15).
- Decode warnings: a malformed SIB segment after the time slice segment (an AIS whose length runs past the SIB end, or with no entries) touches no hit, so the decoder counts it in `warnings` / `warning_messages` (per frame and in total) instead of `errors`; `StreamHits.validate()` passes, `stream_stats` carries the totals, the viewer shows a "decode warnings" check, `pyevio debug` prints the messages and the Parquet example prints them to stderr (F58, F59). Stats version 3: caches written by versions 1 and 2 are recomputed.
- `pyevio stats` prints the decode warnings and the AIS check (frames with an aggregation info segment, frames whose segment repeats one entry), the same checks the viewer shows (F61).
- Reading of JLab SRO hardware streams: files with no file header and 8-word block headers whose version word is 4 with no bit-info bits open natively as `EvioFile.header_layout == "sro-8word"`. The whole header chain is checked at open; an evio-library v4 file (a header with the last-record or dictionary bit, or a 14-word header) raises the version error instead. `FileHeader.layout`, `FileHeader.container_version`, `FileHeader.for_sro_stream()`, `RecordHeader.is_sro_block_word()`.
- Reading of bare EVIO v6 record streams (an evio writer buffer dump, 14-word record headers with no file header) as `EvioFile.header_layout == "v6-records"`; `EvioFile.record_header_problem(offset)` names what is wrong with a record header; `pyevio info` prints the layout and the file checks.
- Compressed records: LZ4, LZ4_BEST and gzip records are decompressed on first access. `Record.buffer` holds the index array, user header and events (the file mmap for an uncompressed record, the decompressed bytes otherwise); `Record.release()` drops the buffer; `Record.compression_type`.
- Salvage of a truncated final record: a record whose declared length runs past the end of the file stays the last record, its complete events are read, and `EvioFile.checks` reports `("truncated_tail", declared_bytes, present_bytes, events_salvaged)`. `Record.is_truncated`, `Record.declared_end`, `EvioFile.record_event_count(record)`.
- File checks and walk errors: `EvioFile.checks` (`truncated_tail`, `bad_record_header`), `Record.errors` and `Bank.errors` record why a scan or a child walk stopped, with offsets.
- `EvioFile.close()`; `EvioFile.trailer` and `Record.is_trailer`; `RecordHeader.header_type`, `RecordHeader.is_trailer`, `RecordHeader.pad1`/`pad2`/`pad3`; `FileHeader.file_type`.
- `Segment` and `TagSegment` (one-word headers) as children of banks of segments and tagsegments; `Bank.child_class()`; `Bank(..., limit=)` bounds a bank to its parent, event or buffer.
- `pyevio.decoders.fadc250_stream`: the one copy of the streaming fADC250 frame walk and hit-word layout, as pure functions over `uint32` event words (`decode_frames`, `decode_frame`, `event_words`, `unpack_hit_words`, `validate_hit_words`, `concatenate_stream_hits`). Returns hit columns (`frame`, `event_index`, `roc`, `stream`, `port`, `channel`, `time_raw`, `t_ns`, `charge`) and per-frame, per-ROC-bank, per-empty-bank and per-non-fADC-bank tables. The module behind each port comes from the 0x41 payload map: fADC250 ports are decoded, other modules (DCRB) are counted as words.
- `pyevio.stream_stats`: one parallel pass over a streaming file to rate, charge, time-in-frame, occupancy and hits-per-frame histograms, empty banks and other-module words per port, and frame checks (stride, missing and duplicate frames, out-of-order frames, timestamp-step outliers). `StreamStats` round-trips through `.npz` with a version field; the timestamp tick length is the knob `tick_ns`.
- `pyevio stats FILE [--out FILE.npz] [--workers N] [--rate-bins N] [--tick-ns X] [--records N]`.
- `pyevio debug` prints a stream-frame summary for 0xFF60 events and the module, hits and other-module words per ROC bank.
- `pyevio web`: a browser viewer for CODA streaming fADC250 files (`docs/web_viewer.md`), and `examples/fadc250_stream_to_parquet.py`, which writes the hits of a file to Parquet.
- `pyevio gui [FILE | FOLDER]`: a console viewer (Textual, optional extra `pyevio[gui]`) with the browser viewer's four tabs in the terminal — file checks and totals, plotext plots at file, ROC and channel level, the paged record table and the structure tree down to hit words with a detail panel of fields and raw words — plus a file-open screen; it reads through the same `pyevio.web` model functions as `pyevio web` and shares its statistics cache (`--cache-dir`, `--workers`), and `s` / `S` save the plots as PNG through matplotlib (`docs/console_gui.md`).
- Golden-set harness `tests/golden/test_golden.py` (one test per row of the evio golden manifest, path in `GOLDEN_MANIFEST`) and the SRO corpus tests `tests/golden/test_corpus.py` (`PYEVIO_CORPUS=1`).
- Documentation: `docs/stream_decoding.md` (frame layout, hit word, time units, striping, `stream_stats` outputs, file quirks); `docs/evio6_file_structure.md` gains an SRO hardware layout section.

### Changed

- `EvioFile.record_count`, `get_record()`, `get_records()` and `iter_records()` cover data records only. The trailer record is `EvioFile.trailer` (or `None`) and yields no events. `stream_stats.n_records` counts data records, `n_trailer_records` the trailer.
- `Bank.size`, `Bank.data_length` and `Bank.end_offset` count the length word plus the words that follow it (see F38 under Fixed); `Bank.to_numpy()` returns one more element per leaf bank than before and uses the file byte order.
- Event offsets of a compressed record (`Record.get_event_offsets()`, `Record.scan_events()`) index `Record.buffer`. Code that read event bytes from `evio_file.mm` must read from `record.buffer`.
- `EvioFile` caches one `Record` (the last accessed) instead of every record.
- `EvioFile.__exit__` propagates the `BufferError` that `close()` raises while a numpy view of the mmap is alive; `__del__` stays silent.
- The error for files pyevio does not read names the detected EVIO version and points at the evio tools (`evio2xml`, `eviocopy`). HIPO files (file type id `HIPO`) open as v6 files.
- `examples/fadc250_stream_to_parquet.py`: a `stream` column, `port` widened to `uint16`, hits sorted by (frame, event index, t_ns) and frames by (frame, event index), `DECODER_VERSION` "2"; event bytes come from `record.buffer` and no `Event` objects are kept.
- `RecordHeader.parse()` and `BufferReader` raise without printing hex dumps first.
- Packaging: pytest, pyyaml and pyarrow sit in the default `dev` dependency group, so `uv run pytest` works without extras; setuptools package discovery is restricted to `pyevio*`.
- `pyevio web` cache key: the file path, size and modification time without the stats version. An entry written by another version is recomputed and overwritten at the same name instead of staying behind as an orphan (F60).
- `textual` leaves the core dependencies: it is installed by the `pyevio[gui]` extra (with `textual-plotext`) only, so the library installs with click, lz4, rich, numpy and matplotlib.

### Removed

- `docs/evio_roc_time_slice_bank.md` and `docs/evio_roc_tsb.png`: the bank layout they described does not match SRO data (F41). `docs/specs.md` moves to `docs/history/specs.md` as the pre-fix goal statement.
- The `pyevio ui` command and the `pyevio/ui/` package (a hard-coded sample tree); `pyevio gui` replaces them.

### Fixed

- F38 — leaf banks lost their last word: `Bank` counted the length word as part of the declared length, so every `to_numpy()` and child walk stopped one word short.
- F3 — trailer records produced phantom events past the end of the file: the trailer's record index was read as event lengths.
- F4 — LZ4, LZ4_BEST and gzip records were read as uncompressed bytes and produced garbage events.
- F5 — an event or bank length that ran past its record or the file was trusted; corrupt data raised deep inside numpy or struct calls. Every length is checked against its container and an overrun ends the walk with a recorded error.
- F6 — a truncated final record, garbage after the last record, or a second file header (two files concatenated) was accepted as a record. Record headers are validated before use and a problem ends the scan with a `bad_record_header` check.
- F7 — `with EvioFile(...)` leaked the mmap and the file descriptor silently while a numpy view was alive.
- F8 — a dictionary or first event of odd length shifted every event: the user header length is stored padded to whole words.
- F9 — v4 and v1—3 files failed with a generic "Unsupported EVIO version" error.
- F39 — HIPO files (file type id `HIPO`) were rejected.
- F43 — `EvioFile.get_event()` raised on every call (a 2-tuple unpacked into four names); `pyevio event` by global index failed with it.
- F45 — children of a bank of segments or tagsegments were read as two-word banks: the first segment word of a streaming frame's stream info bank was taken for a length, and the walk stopped with an entry in `Bank.errors` and no children.
- F49 — `pyevio event N --record R` looked the event up by global index instead of its index within the record.
- F2 — SRO hardware files (8-word block headers, version word 4, no file header) could not be opened.
- F10 — two bank-scanning tests failed on `main` because their fixtures declared lengths that disagreed with their byte sizes; corrected to EVIO length semantics.
- F37 — `uv run` and `pip install` could not build the package: setuptools auto-discovery picked up a second top-level directory as a package.
- F40 — `pyevio debug` printed "Error accessing event" for every event (it called a method `Record` does not have) and skipped every payload bank with two or more hits.
- F44 — `pyevio debug` displayed bank extents one word short.
- F16 — the hit-word layout and the frame walk existed in five copies (viewer, Parquet example, `pyevio debug`, two analysis examples) that had drifted apart; the Parquet example and `pyevio debug` call the shared decoder. The viewer's copy and `examples/analyze_evio_data.py` / `examples/analyze_roc_data2.py` remain.
- F17 — the Parquet example crashed (`IndexError`) when the last ROC bank of a frame had no children or ended in an empty payload bank.
- F19 — the example's `validate()` checked a `uint16` column against 65,536, which could never fail, with `assert`, which `python -O` removes; the raw 14-bit time field is checked and `ValueError` raised.
- F20 — duplicate frame numbers interleaved the hits of both copies in the Parquet output, so the per-frame hit ranges pointed at the wrong rows.
- F42 — every crate was decoded as fADC250, so DCRB channel masks became bogus hits; the module type comes from the payload map.
- F13 (decoder and statistics side) — fixed caps of 8 ROCs and 64 boards dropped crates silently; each crate sends two ROC banks per frame, so 12 crates make 24. The decoder and `stream_stats` have no caps: histogram axes grow with the data, both streams of a crate merge by ROC id, and the stream identity (ROC bank num mask) is kept per hit. The viewer's own caps remain.
- F14 — empty payload banks (length 1, num 1) and payload data type 0x00 were treated as anomalies.
- F11 — a file of a 4-way striped run reported 3 of every 4 frames missing, and time was computed from frame numbers; the stride is read from the data and time from timestamps.
- F12 — the frame period and the timestamp tick were guessed from `timestamp / frame` of the first frame; the period is the median timestamp step per frame and the tick length is a knob.
- F28 (binning) — rate histograms binned from frame 0 to the last frame, so a later stripe squeezed into the right edge and partial last bins read low; `stream_stats` bins from the first to the last timestamp and normalizes by the frames each bin covers.
- F48 — the Parquet example, `stream_stats` and `pyevio debug` read event bytes from the file mmap, which is wrong for compressed records, and the example kept mmap views past the `with` block.
- F47 — every command printed a traceback for a file pyevio cannot read (an EVIO v4 file, a file without the magic number); the message goes to stderr with exit code 1.
- F50 — `pyevio event` and `pyevio debug` printed the offsets of a compressed record as if they were file offsets; a note names them as offsets in the decompressed record buffer, and `pyevio event --hexdump` reads the event from that buffer.
- F51 — `pyevio --version` printed 0.1.0; it prints `pyevio.__version__`.
- F52 — the `pyevio FILE` shortcut for `pyevio info FILE` never ran (click resolves the command name first); the dead branch is removed and `pyevio` with no arguments prints the help.
- F53 — `pyevio event N` by global index printed `Record #None`; it prints the record the event lives in.
- F54 — `pyevio dump --record R` with R out of range printed the message and exited 0; it is a parameter error (exit code 2). The `--hexdump` title of `pyevio event` lost a stray `)`.
- Files that set the last-record bit on every record showed 1 record and 0 events; the scan continues while another record header follows, and records without an event index are walked by the length word of each event bank. (Fixed before the defect ledger existed; no finding number.)
