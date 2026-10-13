//! The screens drawn into ratatui's `TestBackend` for the small golden files
//! (`/data/evio-lib/golden`, read-only; skipped when absent): the records
//! screen text after a full scan, then the events and the event screens
//! driven by keys.

use std::path::{Path, PathBuf};
use std::sync::mpsc;

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use evio_tui::scan::{self, ScanMessage};
use evio_tui::state::{App, Key, Screen};
use evio_tui::ui;

fn golden(name: &str) -> Option<PathBuf> {
    let root = std::env::var_os("EVIO_GOLDEN_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/data/evio-lib/golden"));
    let path = root.join("cases/cpp").join(name);
    if path.is_file() {
        Some(path)
    } else {
        eprintln!("skipped: {} is absent", path.display());
        None
    }
}

/// Scan `path` inline and feed every message to a new `App`.
fn scanned_app(path: &Path) -> App {
    let (tx, rx) = mpsc::channel();
    scan::scan(path, &tx);
    drop(tx);
    let mut app = App::new(path);
    let mut finished = false;
    for message in rx {
        finished |= matches!(message, ScanMessage::Finished(_));
        app.apply(message);
    }
    assert!(finished, "the scan must end with Finished");
    app
}

fn screen_text(terminal: &mut Terminal<TestBackend>, app: &mut App) -> String {
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    ui::buffer_lines(terminal.backend().buffer()).join("\n")
}

#[test]
fn records_screen_of_basic_le() {
    let Some(path) = golden("basic_le.evio") else {
        return;
    };
    let mut app = scanned_app(&path);
    assert_eq!(app.scan.records.len(), 2);
    assert_eq!(app.scan.events, 18);
    let end = app.scan.end.as_ref().unwrap();
    assert!(end.trailer);
    assert!(end.truncated_tail.is_none());
    assert!(end.bad_record_header.is_none());

    let mut terminal = Terminal::new(TestBackend::new(140, 30)).unwrap();
    let text = screen_text(&mut terminal, &mut app);
    // the title, the table header, the two record rows and the status line
    assert!(text.contains("basic_le.evio"), "{text}");
    assert!(text.contains("[records]"), "{text}");
    assert!(text.contains(" 1 records + trailer "), "{text}");
    assert!(text.contains(" index "), "{text}");
    assert!(text.contains("0x38"), "{text}");
    assert!(text.contains(" 752 "), "{text}");
    assert!(text.contains("record "), "{text}");
    assert!(text.contains("18"), "{text}");
    assert!(text.contains("0x328"), "{text}");
    assert!(text.contains("trailer"), "{text}");
    assert!(text.contains("none"), "{text}");
    // the side panel shows the selected record's header fields
    assert!(text.contains("layout v6"), "{text}");
    assert!(text.contains("record number    1"), "{text}");
    assert!(text.contains("index array      72 bytes"), "{text}");
    // the status line shows the finished scan and the checks
    assert!(
        text.contains("done: 1 records + trailer  18 events"),
        "{text}"
    );
    assert!(text.contains("trailer yes"), "{text}");
    assert!(text.contains("truncated_tail none"), "{text}");
    assert!(text.contains("bad_record_header none"), "{text}");

    // a narrow terminal drops the side panel but keeps the table
    let mut narrow = Terminal::new(TestBackend::new(80, 12)).unwrap();
    let text = screen_text(&mut narrow, &mut app);
    assert!(text.contains("0x38"), "{text}");
    assert!(!text.contains("record number"), "{text}");
}

#[test]
fn events_and_event_screens_of_lz4_le() {
    let Some(path) = golden("lz4_le.evio") else {
        return;
    };
    let mut app = scanned_app(&path);
    assert_eq!(app.scan.records.len(), 2);
    assert_eq!(app.scan.records[0].header.compression.name(), "lz4");
    let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
    let text = screen_text(&mut terminal, &mut app);
    assert!(text.contains("lz4"), "{text}");

    // Enter opens record 0: 18 events read from the file at offset 0x38
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Events, "{:?}", app.message);
    let loaded = app.loaded.as_ref().unwrap();
    assert_eq!(loaded.index, 0);
    assert_eq!(loaded.offset, 0x38);
    assert_eq!(loaded.events.len(), 18);
    assert_eq!(loaded.record.header.record_number, 1);
    let text = screen_text(&mut terminal, &mut app);
    assert!(text.contains("[events]"), "{text}");
    assert!(text.contains("record 0 at 0x38: 18 events"), "{text}");
    assert!(text.contains("0x0101"), "{text}");
    assert!(text.contains("0x01 uint32"), "{text}");

    // event 16 (tag 0x010E, a bank of banks): the tree has more than one row
    app.key(Key::End);
    app.key(Key::Up);
    assert_eq!(app.selected_event().unwrap().tag, 0x010E);
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Event);
    let view = app.view.as_ref().unwrap();
    assert_eq!(view.event_index, 16);
    assert!(view.rows.len() > 1, "{:?}", view.rows);
    assert!(view.rows.iter().all(|r| !r.is_error()), "{:?}", view.rows);
    let text = screen_text(&mut terminal, &mut app);
    assert!(text.contains("[event]"), "{text}");
    assert!(text.contains("Bank"), "{text}");
    assert!(text.contains("0x010E"), "{text}");
    assert!(text.contains("words 0.."), "{text}");
    // the first hex row starts at word 0 with the length word of the event
    assert!(text.contains("     0  00000007"), "{text}");

    // f on a non-frame event leaves a message, not a frame panel
    app.key(Key::Char('f'));
    assert!(app
        .message
        .as_deref()
        .unwrap_or("")
        .contains("no stream frame"));
    assert!(!app.view.as_ref().unwrap().show_frame);

    // Esc walks back: event -> events -> records
    app.key(Key::Esc);
    assert_eq!(app.screen, Screen::Events);
    app.key(Key::Esc);
    assert_eq!(app.screen, Screen::Records);

    // the trailer opens too: a record with no events
    app.key(Key::End);
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Events, "{:?}", app.message);
    assert_eq!(app.loaded.as_ref().unwrap().events.len(), 0);
    assert!(app.loaded.as_ref().unwrap().record.is_trailer());
    app.key(Key::Enter);
    assert_eq!(app.screen, Screen::Events);
}

#[test]
fn a_missing_file_fails_the_scan() {
    let (tx, rx) = mpsc::channel();
    scan::scan(Path::new("/nonexistent/run.evio"), &tx);
    drop(tx);
    let messages: Vec<ScanMessage> = rx.iter().collect();
    assert_eq!(messages.len(), 1);
    assert!(matches!(messages[0], ScanMessage::Failed(_)));
    let mut app = App::new("/nonexistent/run.evio");
    app.apply(messages[0].clone());
    let mut terminal = Terminal::new(TestBackend::new(100, 10)).unwrap();
    let text = screen_text(&mut terminal, &mut app);
    assert!(text.contains("scan failed"), "{text}");
}
