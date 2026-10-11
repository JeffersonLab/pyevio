//! The corpus and SRO rulers (15 GB files; run with `EVIO_CORPUS=1`).
//!
//! `tests/data/corpus.tsv` is generated from
//! `evio-optim/space-evio-lib-23/golden_manifest/corpus.yaml`: the eight
//! converted v6 files (plain and LZ4) with their record and event counts.
//! The raw SRO hardware files of run 791 are checked against the reference
//! measured with pyevio and two independent decoders (`docs/stream_decoding.md`).

use std::path::Path;

use evio_stream::{Checks, FileLayout, FrameDecoder, RecordReader};

fn enabled() -> bool {
    std::env::var("EVIO_CORPUS")
        .map(|v| v == "1")
        .unwrap_or(false)
}

struct Stream {
    checks: Checks,
    frames: u64,
    hits: u64,
    tail_hits: u64,
    non_fadc_words: u64,
    empty_payload_banks: u64,
    decode_errors: u64,
    seconds: f64,
}

fn scan(path: &Path, fadc250: bool) -> Stream {
    let start = std::time::Instant::now();
    let reader = RecordReader::open(path).unwrap();
    let mut decoder = FrameDecoder::new();
    let (mut frames, mut hits, mut tail_hits, mut other, mut empty, mut errors) =
        (0, 0, 0, 0, 0, 0);
    let checks = Checks::scan_with(reader, |record| {
        if !fadc250 {
            return;
        }
        for event in record.iter_events() {
            if let Some(frame) = decoder.decode(event) {
                frames += 1;
                hits += frame.n_hits;
                if record.is_truncated {
                    tail_hits += frame.n_hits;
                }
                other += frame.non_fadc_words;
                empty += frame.n_empty_payload_banks as u64;
                errors += frame.n_errors as u64;
            }
        }
    })
    .unwrap();
    Stream {
        checks,
        frames,
        hits,
        tail_hits,
        non_fadc_words: other,
        empty_payload_banks: empty,
        decode_errors: errors,
        seconds: start.elapsed().as_secs_f64(),
    }
}

#[test]
fn converted_corpus() {
    if !enabled() {
        eprintln!("EVIO_CORPUS not set, skipping");
        return;
    }
    for line in include_str!("data/corpus.tsv").lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split('\t').collect();
        let (path, records, events) = (
            f[0],
            f[2].parse::<u64>().unwrap(),
            f[3].parse::<u64>().unwrap(),
        );
        let s = scan(Path::new(path), false);
        eprintln!(
            "{path}: {} records, {} events, trailer {}, {:.1} s, {:.2} GB/s",
            s.checks.records,
            s.checks.events,
            s.checks.trailer,
            s.seconds,
            s.checks.bytes as f64 / 1e9 / s.seconds
        );
        assert_eq!(s.checks.layout, FileLayout::V6, "{path}");
        assert_eq!(s.checks.records, records, "{path}: records");
        assert_eq!(s.checks.events, events, "{path}: events");
        assert!(s.checks.is_clean(), "{path}: {:?}", s.checks);
    }
}

#[test]
fn raw_sro_files() {
    if !enabled() {
        eprintln!("EVIO_CORPUS not set, skipping");
        return;
    }
    // (path, records incl. the cut tail, events incl. salvaged, (declared, present, salvaged),
    //  fADC250 hits in the complete blocks)
    let expected = [
        (
            "/data/sro_boyarinov_data_2026/sro_000791.evio.00000",
            7893u64,
            86_794u64,
            (1_967_400u64, 531_760u64, 2usize),
            763_992_122u64,
            Some(764_009_774u64),
        ),
        (
            "/data/sro_boyarinov_data_2026/sro_000791.evio.00001",
            7893,
            86_805,
            (1_960_136, 1_939_708, 10),
            764_052_526,
            // Sum of the '+salvage' payload words of the fADC250 ROC ids in
            // /data/pyevio/sro_reference/tables.md (independent reference).
            Some(764_140_261),
        ),
    ];
    for (path, records, events, tail, hits_complete, hits_with_salvage) in expected {
        let s = scan(Path::new(path), true);
        eprintln!(
            "{path}: {} records, {} events, tail {:?}, {} frames, {} hits ({} in the tail), {} other words, {} empty banks, {} decode errors, {:.1} s, {:.2} GB/s",
            s.checks.records, s.checks.events, s.checks.truncated_tail, s.frames, s.hits, s.tail_hits,
            s.non_fadc_words, s.empty_payload_banks, s.decode_errors, s.seconds,
            s.checks.bytes as f64 / 1e9 / s.seconds
        );
        assert_eq!(s.checks.layout, FileLayout::Sro8Word, "{path}");
        assert_eq!(s.checks.records, records, "{path}: records");
        assert_eq!(s.checks.events, events, "{path}: events");
        let t = s.checks.truncated_tail.expect("cut tail");
        assert_eq!(
            (t.declared_bytes, t.present_bytes, t.salvaged_events),
            tail,
            "{path}: tail"
        );
        assert!(s.checks.bad_record_header.is_none(), "{path}");
        assert_eq!(s.frames, events, "{path}: every event is a frame");
        assert_eq!(
            s.hits - s.tail_hits,
            hits_complete,
            "{path}: hits in the complete blocks"
        );
        if let Some(total) = hits_with_salvage {
            assert_eq!(s.hits, total, "{path}: hits with the salvaged frames");
        }
        assert_eq!(s.decode_errors, 0, "{path}: decode errors");
    }
}
