//! The headless harness on the big files: the converted v6 corpus file
//! (15.5 GB, `v6` layout) and the raw SRO stream (`sro-8word`). Each test
//! scans the file inline, times the scan, opens record 7000 by its offset
//! through the state machine, times that, and checks the events, the bank
//! tree and the frame summary. Skipped when the file is absent. Run with
//! `--nocapture` to see the timings.

use std::path::Path;
use std::sync::mpsc;
use std::time::Instant;

use evio_tui::scan::{self, ScanMessage};
use evio_tui::state::{App, Key, Screen};
use evio_tui::tree::TreeItem;

const CORPUS_V6: &str = "/data/evio-lib/corpus/sro_000791.evio.00000.v6";
const RAW_SRO: &str = "/data/sro_boyarinov_data_2026/sro_000791.evio.00000";

struct Expect {
    layout: &'static str,
    records: usize,
    events: u64,
    trailer: bool,
    salvaged: Option<usize>,
    record_7000_offset: u64,
}

fn drive(path: &str, expect: &Expect) {
    let path = Path::new(path);
    if !path.is_file() {
        eprintln!("skipped: {} is absent", path.display());
        return;
    }
    let (tx, rx) = mpsc::channel();
    let t0 = Instant::now();
    scan::scan(path, &tx);
    drop(tx);
    let scan_s = t0.elapsed().as_secs_f64();
    let mut app = App::new(path);
    let mut messages = 0usize;
    for message in rx {
        messages += 1;
        app.apply(message);
    }
    let end = app.scan.end.clone().expect("the scan finished");
    println!(
        "{}: scan {scan_s:.2} s, {} records, {} events, {:.2} GB/s, {messages} messages",
        path.display(),
        app.scan.records.len(),
        app.scan.events,
        end.bytes as f64 / 1e9 / end.elapsed_s
    );
    assert_eq!(
        app.scan.start.as_ref().unwrap().layout.name(),
        expect.layout
    );
    assert_eq!(app.scan.records.len(), expect.records);
    assert_eq!(app.scan.events, expect.events);
    assert_eq!(end.trailer, expect.trailer);
    assert_eq!(
        end.truncated_tail.map(|t| t.salvaged_events),
        expect.salvaged
    );
    assert!(end.bad_record_header.is_none());
    assert!(end.error.is_none(), "{:?}", end.error);

    // record 7000: select it and open it through the state machine
    app.records.select(7000);
    let summary = app.selected_record().unwrap().clone();
    assert_eq!(summary.index, 7000);
    assert_eq!(summary.offset, expect.record_7000_offset);
    assert_eq!(summary.header.record_number, 7001);
    let t1 = Instant::now();
    app.key(Key::Enter);
    let open_s = t1.elapsed().as_secs_f64();
    assert_eq!(app.screen, Screen::Events, "{:?}", app.message);
    let loaded = app.loaded.as_ref().unwrap();
    assert_eq!(loaded.index, 7000);
    assert_eq!(loaded.offset, summary.offset);
    assert_eq!(loaded.record.header.record_number, 7001);
    assert_eq!(loaded.events.len(), summary.events_found);
    assert_eq!(loaded.events.len(), 11);
    assert!(loaded.events.iter().all(|e| e.is_stream_frame()));
    println!(
        "record 7000 at 0x{:X}: opened in {:.4} s ({} bytes, {} events)",
        loaded.offset,
        open_s,
        loaded.record.present_bytes,
        loaded.events.len()
    );
    assert!(open_s < 1.0, "opening record 7000 took {open_s} s");

    // the tree of event 0 walks without an error; the frame summary has hits
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Event);
    let view = app.view.as_ref().unwrap();
    let errors = view.rows.iter().filter(|r| r.is_error()).count();
    assert_eq!(errors, 0);
    // depth 1 holds the SIB (tag 0xFF31) and the ROC banks
    let depth1: Vec<u16> = view
        .rows
        .iter()
        .filter(|r| r.depth == 1)
        .filter_map(|r| match r.item {
            TreeItem::Node { tag, .. } => Some(tag),
            TreeItem::Error { .. } => None,
        })
        .collect();
    assert_eq!(depth1.first(), Some(&0xFF31));
    assert!(depth1.len() >= 2, "{depth1:?}");
    let (first, words) = app.selected_words().unwrap();
    assert_eq!(first, 0);
    assert_eq!(words.len(), 64);
    assert_eq!(words[1] >> 16, 0xFF60);
    app.key(Key::Char('n'));
    let (first, _) = app.selected_words().unwrap();
    assert_eq!(first, 64);
    app.key(Key::Char('p'));
    assert_eq!(app.selected_words().unwrap().0, 0);
    app.key(Key::Char('f'));
    let view = app.view.as_ref().unwrap();
    assert!(view.show_frame);
    let frame = view.frame.as_ref().unwrap();
    println!("frame summary: {}", frame.lines.join(" | "));
    assert!(frame.lines[0].starts_with("frame "));
    assert!(frame.lines[2].contains("fADC250 hits"));
    assert!(frame.lines.len() > 5, "per-ROC rows expected");
    assert!(frame.errors.is_empty(), "{:?}", frame.errors);
    app.key(Key::Esc);
    assert!(!app.view.as_ref().unwrap().show_frame);
    assert_eq!(app.screen, Screen::Event);

    // the last record opens too (the cut tail of the raw file, the trailer
    // of the converted one)
    app.key(Key::Esc);
    app.key(Key::Esc);
    assert_eq!(app.screen, Screen::Records);
    app.key(Key::End);
    let last = app.selected_record().unwrap().clone();
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Events, "{:?}", app.message);
    let loaded = app.loaded.as_ref().unwrap();
    assert_eq!(loaded.index, last.index);
    assert_eq!(loaded.events.len(), last.events_found);
    assert_eq!(loaded.record.is_truncated, last.is_truncated);
    println!(
        "last record {} at 0x{:X}: {} events, truncated {}",
        last.index,
        last.offset,
        loaded.events.len(),
        loaded.record.is_truncated
    );
}

#[test]
fn converted_v6_corpus_file() {
    drive(
        CORPUS_V6,
        &Expect {
            layout: "v6",
            records: 7892,
            events: 86794,
            trailer: true,
            salvaged: None,
            record_7000_offset: 0x3364DC560,
        },
    );
}

#[test]
fn raw_sro_stream() {
    drive(
        RAW_SRO,
        &Expect {
            layout: "sro-8word",
            records: 7893,
            events: 86794,
            trailer: false,
            salvaged: Some(2),
            record_7000_offset: 0x33610BA10,
        },
    );
}

/// The scan runs on its own thread and the receiver drains as messages
/// come: the first records arrive long before the end.
#[test]
fn background_scan_streams_records() {
    let path = Path::new(CORPUS_V6);
    if !path.is_file() {
        eprintln!("skipped: {} is absent", path.display());
        return;
    }
    let t0 = Instant::now();
    let rx = scan::spawn(path.to_path_buf());
    let mut app = App::new(path);
    let mut first_record_at = None;
    for message in rx {
        if first_record_at.is_none() && matches!(message, ScanMessage::Record(_)) {
            first_record_at = Some(t0.elapsed().as_secs_f64());
        }
        app.apply(message);
    }
    let total = t0.elapsed().as_secs_f64();
    let first = first_record_at.unwrap();
    println!("first record after {first:.4} s, scan done after {total:.2} s");
    assert!(app.scan.is_done());
    assert!(first < total / 2.0);
}
