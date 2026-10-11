# Streaming fADC250 decoding

How pyevio reads CODA streaming fADC250 files: the frame layout, the hit word, the time units, the striping of a run across files, the outputs of `stream_stats` and the quirks of the files the JLab SRO hardware writes. The decoder is `pyevio/decoders/fadc250_stream.py`, the statistics module is `pyevio/stream_stats.py`, and `pyevio stats FILE` runs the latter from the command line.

Every number quoted here was measured on run 791 (`sro_000791.evio.00000` and `.00001`, 15.5 GB each) and on the same data converted to clean EVIO v6, with readers that share no code with pyevio: a standalone walker, the evio C++ library, and the jana4ml4fpga `evio_sro_parser`.

## Frame layout

A streaming time frame is one EVIO event. The event is a bank of banks with tag 0xFF60; its first child is the stream info bank (SIB), the rest are ROC banks, one per stream of each crate.

```
event bank          tag 0xFF60, type 0x10 (bank of banks), num 0
  SIB               tag 0xFF31, type 0x20 (bank of segments)
    TSS segment     tag 0x32, length 3: {frame number, timestamp low, timestamp high}
    AIS segment     tag 0x42, length 24: aggregated-stream entries
  ROC bank          tag = ROC id, type 0x10, num = stream status byte
    stream-info     tag 0xFF30, type 0x20 (bank of segments)
      TSS segment   tag 0x31, length 3: the same frame number and timestamp as the SIB
      payload map   tag 0x41: uint16 entries, one per payload port
    payload bank    tag = payload port, type 0x00, num 0, one word per hit
    payload bank    tag = payload port, length 1, num 1: an empty port
    ...
  ROC bank          ...
```

Facts the decoder relies on, each measured on both raw files with no exception:

- Every event has tag 0xFF60, type 0x10, num 0 (173,587 events).
- The SIB starts with the TSS segment: header word `0x32010003`, then frame number, timestamp low word, timestamp high word. The decoder reads the frame number and the 64-bit timestamp from here; `stream_stats.scan_frames` reads only these words when it scans a file for its time axis.
- Every ROC bank's stream-info TSS (tag 0x31) carries the same frame number and timestamp as the SIB (0 mismatches).
- Each crate sends two streams, so each ROC id appears as two ROC banks per frame. The `num` byte of the ROC bank is the stream status: bits 6—4 hold the stream count (2) and bits 3—0 the stream mask (1 or 2), so the two banks carry num 0x21 (33) and 0x22 (34). The mask is the stream identity. The port split between the two streams is odd/even in 8 of the 12 crates of run 791 and 5, 7, 9, 11, 13, 15 against 1, 2, 3, 4, 6, 8 in ROC ids 9, 27, 29 and 84, so port parity identifies nothing. The decoder keeps the mask as the `stream` column; `stream_stats` merges both streams of a crate by ROC id.
- Run 791 has 12 crates, so every frame holds 24 ROC banks.
- The payload map (segment 0x41) holds uint16 entries, two per word, read in memory order (low half first in a little-endian file); pad value 2 in the segment header means an odd entry count. An entry is `module << 8 | port`: bits 11—8 module id, bit 7 bond, bits 6—5 lane, bits 4—0 port. Module 0 is fADC250, module 1 is DCRB. Every payload bank's tag appears in its stream's map (0 exceptions in 14.2 million payload banks per file).
- Every payload bank declares data type 0x00. A bank with data has num 0; an empty bank has length 1 and num 1. Both are valid. File 00000 holds 357,735 empty banks among 14,233,888 payload banks (2.51%), all on ROC ids 7, 9, 25 and 27; ports 14 and 16 of ROC ids 7 and 25 are mapped and never carry a hit.

The decoder walks this tree by length words and trusts none of them: every bank end is checked against its container, and a bad length stops the walk of that event with a counted error (`errors`, `error_messages`, the first 50 messages kept) instead of an exception. A ROC bank without a stream-info bank, or a port absent from the map, is decoded as fADC250 and counted in `n_unmapped_payload_banks`. The decoder reads the AIS segment only to count its entries and their distinct values (`n_ais_entries`, `n_ais_distinct` per frame): ROC banks come from the walk.

## Hit word

One `uint32` per hit in an fADC250 payload bank:

| bits | field | range |
|---|---|---|
| 31 | flag | never set in the SRO data (0 words in either raw file); `n_flag_words` counts it |
| 30—17 | time from the frame start, in 4 ns ticks | 0—16,383 ticks = 0—65,532 ns |
| 16—13 | fADC250 channel | 0—15 |
| 12—0 | charge | 0—8,191; 8,191 means overflow |

`unpack_hit_words` splits the columns; `t_ns = time_raw × 4`. `validate_hit_words` rejects words whose raw time field exceeds 14 bits, which happens exactly when bit 31 is set.

Evidence for the layout: the jana4ml4fpga `evio_sro_parser`, written independently of pyevio, decodes the same bit positions, and its hit counts per (ROC id, port) agree with pyevio for all 76 instrumented pairs in both raw files (763,992,122 hits in the complete blocks of file 00000, 764,052,526 in file 00001). The port totals are also fixed by the bank structure: hits = Σ(payload length − 1) over fADC250 ports. The per-channel split rests on the bit layout alone, and the DAQ group has not confirmed it.

## Module types

The module id of a port comes from the payload map of its ROC bank. In run 791 the map says fADC250 (module 0) on ROC ids 7, 9, 25, 27, 29 and 84, and DCRB (module 1) on ROC ids 44, 45, 46, 53, 54 and 55. ROC ids 7, 9, 25 and 27 map 16 ports, 29 and 84 map 12 (ports 10, 12, 14 and 16 absent), and the DCRB crates map ports 3—16.

Only fADC250 payload words become hits. Payload words of any other module are counted: per ROC bank in `roc_banks["non_fadc_words"]`, per bank in the `non_fadc_banks` table, and in the total `non_fadc_words`. DCRB payload words are two-word channel masks (the independent parser reads word 0 as a 3-bit channel group and a 29-bit mask, word 1 as a 19-bit mask continuation and an 11-bit time); pyevio decodes nothing from them. `StreamHits.validate()` checks `n_hits + non_fadc_words == n_payload_words` and that the per-frame hit counts add up.

Record 0 of the converted file 00000 decodes to 499,660 payload words in 264 ROC banks and 1,804 payload banks (45 empty).

## Time

Two tick lengths are in play:

- The hit time inside a frame is a 14-bit field in 4 ns ticks, the fADC250 sample period (`HIT_TIME_TICK_NS = 4`, fixed). The largest value is 16,383 × 4 ns = 65,532 ns.
- The frame timestamp (TSS) counts ticks whose length is the knob `tick_ns` of `stream_stats` (`compute_stream_stats(..., tick_ns=1.0)`, `pyevio stats --tick-ns 1`), default 1 ns. The timestamp step per frame number, measured as median = min = max on all four files, is 65,536 ticks, which equals 2^14 hit ticks × 4 ns at a 1 ns timestamp tick. The DAQ group has not confirmed the tick length; a different length goes into the knob.

With the default, one frame lasts 65.536 µs. `stream_stats` derives the frame period as the median timestamp step per frame number (`checks.frame_period_ticks`), so a wrong knob scales every time quantity by one factor:

- `duration_s`: first to last timestamp plus one frame period — the span of the run the file covers.
- `live_time_s`: frames in the file × frame period — the time the file records.
- `rate_hz(...)`: hits per time bin divided by the frames the bin covers × frame period, so a partial bin, a gap and a stripe of a striped run all read right.

On file 00000 the span is 22.752 s and the live time 5.688 s: the file holds one frame in four.

## Striping across files

A run is written round-robin by frame over its files: `<run>.evio.0000N` holds frames N, N + 4, N + 8, … for a four-way split. Measured on stripes 0 and 1 of run 791: first frames 0 and 1, every frame delta 4, 0 missing, 0 duplicate, 0 out of order, 0 timestamp steps other than 65,536 per frame; frame 0 with timestamp 0 is a real frame.

`stream_stats` reads the stride from the data as the most common positive frame delta between consecutive frames (`checks.stride`) and counts missing frames against it: `(last − first) / stride + 1 − distinct frames`, so a stripe of a split run reports 0 missing frames instead of three in four. The other frame checks: `duplicate_frames` (frame numbers seen more than once), `out_of_order` (consecutive frames whose number decreases) and `dts_outliers` (consecutive frames whose timestamp step per frame deviates from the median by more than 1%).

Statistics are per file. For a whole run, the charge, time-in-frame, occupancy and empty-bank histograms of its stripes add, once their ROC and port axes are aligned. Each file's rate axis runs from its own first to its last timestamp, so rate series of different stripes are compared bin by bin only when the files cover the same span.

## `stream_stats` outputs

`compute_stream_stats(path, workers=None, rate_bins=256, tick_ns=1.0, max_records=None, chunk_records=8)` makes two passes: a serial scan of every event's SIB for frame numbers and timestamps, which fixes the time axis and the frame checks, then a parallel decode by record ranges (`workers` processes, default cores minus two; `workers=1` runs in-process). Each worker opens the file itself and returns partial histograms whose axes grow with the ROC and port sets it meets; the partials add.

`StreamStats` fields:

| field | shape | content |
|---|---|---|
| `rocs`, `ports` | (R,), (P,) | sorted axes of every histogram; `roc_index(roc)` maps an id to its row |
| `module_id` | (R, P) | module behind each port, 0xFF where the port never appeared |
| `module_ids`, `module_words` | (M,), (M,) | payload words per module id; fADC250 hits count as words |
| `rate_counts`, `rate_edges_ns`, `frames_per_rate_bin` | (R, P, 16, B), (B + 1,), (B,) | hits per time bin from the first to the last timestamp, B = `rate_bins`; `rate_hz()` normalizes |
| `occupancy` | (R, P, 16) | hits per channel |
| `charge_hist`, `charge_edges` | (R, P, 16, 128), (129,) | 13-bit charge in 128 bins of 64 |
| `time_hist`, `time_edges_ns` | (R, P, 16, 64), (65,) | 14-bit tick field in 64 bins of 256 ticks = 1,024 ns |
| `hits_per_frame_hist`, `hits_per_frame_edges` | (64,), (65,) | hits per frame in 64 linear bins from 0 to the largest count |
| `empty_banks` | (R, P) | empty payload banks per port |
| `non_fadc_words_per_port` | (R, P) | words of other modules per port |
| `frames` | (F,) structured | per frame: event index, frame number, timestamp, hits, other-module words, errors; sorted by event index |
| `checks` | `FrameChecks` | frame count, first and last frame and timestamp, stride, frame period, missing, duplicate, out-of-order frames, timestamp-step outliers |

Scalars: `n_records` (data records), `n_trailer_records`, `n_events`, `n_frames`, `n_other_events` (events that are no 0xFF60 frame), `n_out_of_bounds_events` (events whose range runs past their record; counted and skipped), `n_hits`, `n_payload_banks`, `n_empty_payload_banks`, `n_unmapped_payload_banks`, `n_payload_words`, `non_fadc_words`, `n_flag_words`, `errors`, `error_messages` (first 50), plus `path`, `file_size`, `bytes_processed`, `tick_ns`, `workers`, `wall_seconds`. `per_roc(hist)` sums a (R, P, 16, bins) histogram to (R, bins); `hits_per_roc()` returns `{roc: hits}`.

`save(path)` writes a compressed `.npz`: one entry `meta`, a JSON string with the scalar fields, `version` and the `checks` dictionary, and one array entry per array field. `StreamStats.load(path)` opens it with `allow_pickle=False` and raises `ValueError` unless `version` equals the `STATS_VERSION` of the running pyevio (`"1"`). The full run-791 file produces a 1.6 MB `.npz`.

`pyevio stats` prints the totals, the payload words per module, the frame checks, span against live time, the fADC250 hit rate of live time, the wall time with MB/s, and a per-ROC table (module, ports, hits, other words, empty banks); `--out FILE.npz` saves the result, `--records N` stops after N records.

Measured on run 791: the converted file 00000 (15.55 GB) through `pyevio stats` with 22 workers in 30.5 s (509 MB/s), the raw file with 20 workers in 25.6 s (607 MB/s); 86,794 frames; 764,009,774 fADC250 hits = the independent reference for the complete blocks (763,992,122) + the 17,652 hits of the two frames salvaged from the cut tail, exact per ROC id; 3,065,574,754 DCRB words; 357,743 empty banks. The decoder alone runs at 966 MB/s on record 0 and 686 MB/s over 200 records on one core.

## Known file quirks and how pyevio reports them

The raw files of the SRO hardware deviate from the evio-library output in five ways. Each is read as the hardware writes it.

| quirk | what the data shows | what pyevio does |
|---|---|---|
| 8-word block headers, no file header | every block header is `{length, block number, 8, event count, 0, 4, 0, 0xc0da0100}`: version word 4 with no bit-info bits | `EvioFile.header_layout == "sro-8word"`; `header` is a stand-in `FileHeader` with `version` 6 and `container_version` 4; record headers have `header_length == 8`; `first_record_offset` is 0 |
| no last-record bit, no trailer | the bit is set on no block; the file ends after its last block | the record scan continues while a record header follows and ends at the end of the file; `EvioFile.trailer` is `None` |
| truncated tail | 3 of the 4 files end in a block cut mid-event: file 00000 declares 1,967,400 bytes and holds 531,760 (2 complete events); file 00001 declares 1,960,136 and holds 1,939,708 (10 complete events) | the block stays the last record with `is_truncated` set; its complete events are read; `EvioFile.checks` holds `("truncated_tail", declared, present, salvaged)` — `(1967400, 531760, 2)` and `(1960136, 1939708, 10)`; `record_count` counts the tail, `get_total_event_count()` counts its salvaged events (86,794 and 86,805) |
| AIS repeated entry | the AIS segment (tag 0x42) repeats one entry 24 times in every event instead of listing 24 streams (4 distinct values per file) | the decoder counts the entries and their distinct values per frame (`n_ais_entries`, `n_ais_distinct`); `stream_stats` counts `frames_with_ais` and `frames_with_degenerate_ais` (several entries, one value); the file check "AIS entries identical" shows the degenerate-frame count as `info`, `ok` when it is 0. ROC banks come from the walk. |
| empty payload banks | length 1, num 1 on ports with no hit in the frame | counted per (ROC, stream, port) in `empty_banks`; `stream_stats.empty_banks` sums them per (ROC, port) |

Two more checks exist for any file: `("bad_record_header", offset, message)` in `EvioFile.checks` when the scan meets bytes that are no record header (a byte-swapped header and a second file header are named), and `Record.errors` when an event length runs past its record. `stream_stats` counts events past their record in `n_out_of_bounds_events` and decode errors in `errors`.
