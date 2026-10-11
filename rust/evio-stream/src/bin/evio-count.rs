//! `evio-count FILE|- [--fadc250]`: records, events, trailer, layout and the
//! file checks of a stream, with throughput; `--fadc250` adds the streaming
//! fADC250 totals (frames, ROC banks, hits, other-module words, empty
//! payload banks) with every hit word decoded.

use std::collections::BTreeMap;
use std::process::ExitCode;
use std::time::Instant;

use evio_stream::fadc250::{module_name, MODULE_UNKNOWN};
use evio_stream::{Checks, Error, FrameDecoder, RecordReader};

const USAGE: &str = "usage: evio-count FILE|- [--fadc250]\n\
  Reads the stream once and prints records, events, trailer, layout, the file checks\n\
  and the throughput. With --fadc250 every 0xFF60 stream frame is walked and every\n\
  fADC250 hit word decoded; the per-ROC table and the totals follow.";

#[derive(Default)]
struct Fadc250Totals {
    frames: u64,
    other_events: u64,
    roc_banks: u64,
    payload_banks: u64,
    empty_payload_banks: u64,
    unmapped_payload_banks: u64,
    payload_words: u64,
    hits: u64,
    hits_decoded: u64,
    non_fadc_words: u64,
    flag_words: u64,
    charge_overflow: u64,
    charge_sum: u64,
    max_time_raw: u16,
    errors: u64,
    warnings: u64,
    frames_with_ais: u64,
    frames_with_degenerate_ais: u64,
    first_frame: Option<u32>,
    last_frame: u32,
    first_timestamp: u64,
    last_timestamp: u64,
    error_messages: Vec<String>,
    /// Per ROC id: module, hits, other words, empty banks, ROC banks.
    per_roc: BTreeMap<u16, (u8, u64, u64, u64, u64)>,
    /// Hits and words in the salvaged events of a cut tail.
    tail_hits: u64,
    tail_frames: u64,
}

fn run(path: &str, fadc250: bool) -> Result<(), Error> {
    let start = Instant::now();
    let mut decoder = FrameDecoder::new();
    let mut totals = Fadc250Totals::default();

    let visit = |record: &evio_stream::Record| {
        if !fadc250 {
            return;
        }
        for event in record.iter_events() {
            let Some(frame) = decoder.decode(event) else {
                totals.other_events += 1;
                continue;
            };
            totals.frames += 1;
            totals.roc_banks += frame.roc_banks.len() as u64;
            totals.payload_banks += frame.n_payload_banks as u64;
            totals.empty_payload_banks += frame.n_empty_payload_banks as u64;
            totals.unmapped_payload_banks += frame.n_unmapped_payload_banks as u64;
            totals.payload_words += frame.n_payload_words;
            totals.hits += frame.n_hits;
            totals.non_fadc_words += frame.non_fadc_words;
            totals.errors += frame.n_errors as u64;
            totals.warnings += frame.n_warnings as u64;
            if frame.n_ais_entries > 0 {
                totals.frames_with_ais += 1;
                if frame.n_ais_entries > 1 && frame.n_ais_distinct == 1 {
                    totals.frames_with_degenerate_ais += 1;
                }
            }
            if totals.first_frame.is_none() {
                totals.first_frame = Some(frame.frame_number);
                totals.first_timestamp = frame.timestamp;
            }
            totals.last_frame = frame.frame_number;
            totals.last_timestamp = frame.timestamp;
            if record.is_truncated {
                totals.tail_hits += frame.n_hits;
                totals.tail_frames += 1;
            }
            for message in frame.errors {
                if totals.error_messages.len() < 20 {
                    totals.error_messages.push(format!(
                        "record {} frame {}: {message}",
                        record.index, frame.frame_number
                    ));
                }
            }
            for roc in frame.roc_banks {
                let entry = totals
                    .per_roc
                    .entry(roc.roc)
                    .or_insert((MODULE_UNKNOWN, 0, 0, 0, 0));
                if entry.0 == MODULE_UNKNOWN {
                    entry.0 = roc.module;
                }
                entry.1 += roc.n_hits;
                entry.2 += roc.non_fadc_words;
                entry.3 += roc.n_empty_payload_banks as u64;
                entry.4 += 1;
            }
            // Decode every hit word: the checks below depend on each one.
            let mut decoded = 0u64;
            let mut charge_sum = 0u64;
            let mut flags = 0u64;
            let mut overflow = 0u64;
            let mut max_time = totals.max_time_raw;
            for hit in frame.hits() {
                decoded += 1;
                charge_sum += hit.charge as u64;
                flags += hit.flag as u64;
                overflow += (hit.charge == 0x1FFF) as u64;
                max_time = max_time.max(hit.time_raw);
            }
            totals.hits_decoded += decoded;
            totals.charge_sum += charge_sum;
            totals.flag_words += flags;
            totals.charge_overflow += overflow;
            totals.max_time_raw = max_time;
        }
    };

    let checks = if path == "-" {
        Checks::scan_with(RecordReader::stdin()?, visit)?
    } else {
        Checks::scan_with(RecordReader::open(path)?, visit)?
    };
    let elapsed = start.elapsed().as_secs_f64();

    println!("file:               {path}");
    println!("layout:             {}", checks.layout);
    println!("byte order:         {}", checks.order);
    println!("records:            {}", checks.records);
    println!("events:             {}", checks.events);
    println!(
        "trailer:            {}",
        if checks.trailer { "yes" } else { "no" }
    );
    match checks.truncated_tail {
        Some(t) => println!(
            "truncated_tail:     declared {} bytes, present {}, salvaged {} events  ({}, {}, {})",
            t.declared_bytes,
            t.present_bytes,
            t.salvaged_events,
            t.declared_bytes,
            t.present_bytes,
            t.salvaged_events
        ),
        None => println!("truncated_tail:     none"),
    }
    match &checks.bad_record_header {
        Some(b) => println!("bad_record_header:  offset 0x{:X}: {}", b.offset, b.message),
        None => println!("bad_record_header:  none"),
    }
    println!("record errors:      {} records", checks.records_with_errors);
    for message in &checks.error_messages {
        println!("  {message}");
    }
    println!(
        "bytes:              {}  time {:.2} s  {:.2} GB/s",
        checks.bytes,
        elapsed,
        checks.bytes as f64 / 1e9 / elapsed.max(1e-9)
    );

    if fadc250 {
        println!();
        println!("fADC250 stream decode (every hit word read)");
        println!("frames:             {}", totals.frames);
        println!("other events:       {}", totals.other_events);
        match totals.first_frame {
            Some(first) => println!(
                "frame range:        {first}..{}  timestamps {}..{}",
                totals.last_frame, totals.first_timestamp, totals.last_timestamp
            ),
            None => println!("frame range:        none"),
        }
        println!("ROC banks:          {}", totals.roc_banks);
        println!("payload banks:      {}", totals.payload_banks);
        println!("empty payload banks: {}", totals.empty_payload_banks);
        println!("unmapped payload banks: {}", totals.unmapped_payload_banks);
        println!("payload words:      {}", totals.payload_words);
        println!(
            "fADC250 hits:       {}  (complete records {}, salvaged tail {} in {} frames)",
            totals.hits,
            totals.hits - totals.tail_hits,
            totals.tail_hits,
            totals.tail_frames
        );
        println!("hits decoded:       {}", totals.hits_decoded);
        println!("other-module words: {}", totals.non_fadc_words);
        println!("flag words (bit 31): {}", totals.flag_words);
        println!("charge overflow:    {}", totals.charge_overflow);
        println!("charge sum:         {}", totals.charge_sum);
        println!(
            "max time_raw:       {} ({} ns)",
            totals.max_time_raw,
            totals.max_time_raw as u32 * 4
        );
        println!(
            "AIS:                {} frames with an AIS, {} with one value repeated",
            totals.frames_with_ais, totals.frames_with_degenerate_ais
        );
        println!("decode errors:      {}", totals.errors);
        println!("decode warnings:    {}", totals.warnings);
        for message in &totals.error_messages {
            println!("  {message}");
        }
        let consistent = totals.hits + totals.non_fadc_words == totals.payload_words
            && totals.hits == totals.hits_decoded;
        println!(
            "consistency:        hits + other words == payload words, hits decoded == hits: {}",
            if consistent { "ok" } else { "FAILED" }
        );
        println!();
        println!(
            "{:>6} {:>8} {:>10} {:>14} {:>14} {:>12}",
            "roc", "module", "ROC banks", "hits", "other words", "empty banks"
        );
        for (roc, (module, hits, other, empty, banks)) in &totals.per_roc {
            let name = module_name(*module);
            println!("{roc:>6} {name:>8} {banks:>10} {hits:>14} {other:>14} {empty:>12}");
        }
        println!(
            "decode:             {:.2} s  {:.2} GB/s",
            elapsed,
            checks.bytes as f64 / 1e9 / elapsed.max(1e-9)
        );
    }
    Ok(())
}

fn main() -> ExitCode {
    let mut path = None;
    let mut fadc250 = false;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "--fadc250" => fadc250 = true,
            "-h" | "--help" => {
                println!("{USAGE}");
                return ExitCode::SUCCESS;
            }
            "-" => path = Some(arg),
            a if a.starts_with('-') => {
                eprintln!("unknown option {a}\n{USAGE}");
                return ExitCode::from(2);
            }
            _ => {
                if path.is_some() {
                    eprintln!("one file only\n{USAGE}");
                    return ExitCode::from(2);
                }
                path = Some(arg);
            }
        }
    }
    let Some(path) = path else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    match run(&path, fadc250) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("evio-count: {e}");
            ExitCode::FAILURE
        }
    }
}
