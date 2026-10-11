//! What is shown and what is selected: the state machine behind the
//! screens, with no terminal in sight.
//!
//! [`App`] takes scan messages ([`App::apply`]) and keys ([`App::key`]).
//! Opening a record reads it from the file ([`crate::access`]); everything
//! else is bookkeeping, so tests drive the whole flow with keys.

use std::path::{Path, PathBuf};
use std::time::Instant;

use evio_stream::fadc250::STREAM_EVENT_TAG;
use evio_stream::{FrameDecoder, Record};

use crate::access;
use crate::format::{frame_summary, FrameSummary};
use crate::scan::{RecordSummary, ScanEnd, ScanMessage, ScanStart};
use crate::tree::{flatten, TreeRow};

/// Words shown at once of the selected structure.
pub const WORDS_PER_PAGE: usize = 64;

/// A key press, independent of the terminal library.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    Up,
    Down,
    PageUp,
    PageDown,
    Home,
    End,
    Enter,
    Esc,
    Char(char),
}

/// Which screen is shown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Screen {
    Records,
    Events,
    Event,
    Help,
}

/// Selection and scroll of one list. `height` is the number of rows the
/// last draw had room for; the drawing code sets it before it reads
/// `first`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ListCursor {
    pub selected: usize,
    /// First visible row.
    pub first: usize,
    pub len: usize,
    pub height: usize,
}

impl ListCursor {
    /// A list of `len` rows, the first selected.
    pub fn new(len: usize) -> ListCursor {
        ListCursor {
            selected: 0,
            first: 0,
            len,
            height: 0,
        }
    }

    /// Set the row count; the selection is clamped.
    pub fn set_len(&mut self, len: usize) {
        self.len = len;
        self.clamp();
    }

    /// Set the rows the view has room for; the window follows the selection.
    pub fn set_height(&mut self, height: usize) {
        self.height = height.max(1);
        self.clamp();
    }

    /// Move the selection by `delta` rows, clamped to the list.
    pub fn move_by(&mut self, delta: isize) {
        let target = self.selected as isize + delta;
        self.selected = target.clamp(0, self.len.saturating_sub(1) as isize) as usize;
        self.clamp();
    }

    /// Select `index` (clamped).
    pub fn select(&mut self, index: usize) {
        self.selected = index;
        self.clamp();
    }

    /// Select the last row.
    pub fn select_last(&mut self) {
        self.select(self.len.saturating_sub(1));
    }

    /// Apply a navigation key; `true` when it was one.
    pub fn navigate(&mut self, key: Key) -> bool {
        let page = self.height.max(1) as isize;
        match key {
            Key::Up => self.move_by(-1),
            Key::Down => self.move_by(1),
            Key::PageUp => self.move_by(-page),
            Key::PageDown => self.move_by(page),
            Key::Home | Key::Char('g') => self.select(0),
            Key::End | Key::Char('G') => self.select_last(),
            _ => return false,
        }
        true
    }

    /// Rows visible: `first..first + height`, clipped to the list.
    pub fn visible(&self) -> std::ops::Range<usize> {
        let end = (self.first + self.height.max(1)).min(self.len);
        self.first.min(end)..end
    }

    /// Selected row relative to `first`, for the drawing code.
    pub fn selected_in_view(&self) -> Option<usize> {
        (self.len > 0 && self.selected >= self.first).then(|| self.selected - self.first)
    }

    fn clamp(&mut self) {
        if self.len == 0 {
            self.selected = 0;
            self.first = 0;
            return;
        }
        self.selected = self.selected.min(self.len - 1);
        let height = self.height.max(1);
        if self.selected < self.first {
            self.first = self.selected;
        } else if self.selected >= self.first + height {
            self.first = self.selected + 1 - height;
        }
        self.first = self.first.min(self.len.saturating_sub(height));
    }
}

/// Progress of the scan as the messages arrive.
#[derive(Debug)]
pub struct ScanProgress {
    pub start: Option<ScanStart>,
    /// Every record seen, the trailer included (it is a row of the list).
    pub records: Vec<RecordSummary>,
    /// Data records seen so far (the trailer excluded, as `evio-count` counts).
    pub data_records: u64,
    /// True once the trailer record has arrived.
    pub trailer_seen: bool,
    /// Events found so far.
    pub events: u64,
    /// Bytes consumed so far.
    pub bytes: u64,
    pub end: Option<ScanEnd>,
    /// Set when the file could not be opened as EVIO.
    pub failed: Option<String>,
    started_at: Instant,
}

impl ScanProgress {
    fn new() -> ScanProgress {
        ScanProgress {
            start: None,
            records: Vec::new(),
            data_records: 0,
            trailer_seen: false,
            events: 0,
            bytes: 0,
            end: None,
            failed: None,
            started_at: Instant::now(),
        }
    }

    /// `"7891 records + trailer"`, `"7893 records"`: the data record count
    /// with the trailer named separately.
    pub fn records_label(&self) -> String {
        format!(
            "{} records{}",
            self.data_records,
            if self.trailer_seen { " + trailer" } else { "" }
        )
    }

    /// Seconds the scan has run, frozen at its end.
    pub fn elapsed_s(&self) -> f64 {
        match &self.end {
            Some(end) => end.elapsed_s,
            None => self.started_at.elapsed().as_secs_f64(),
        }
    }

    /// True once the scan has finished or failed.
    pub fn is_done(&self) -> bool {
        self.end.is_some() || self.failed.is_some()
    }
}

/// One row of the events table.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EventRow {
    pub index: usize,
    /// Length of the event in words.
    pub words: usize,
    pub tag: u16,
    pub data_type: u8,
    pub num: u8,
    /// False when the event is too short for a bank header.
    pub header_ok: bool,
}

impl EventRow {
    fn new(index: usize, record: &Record) -> EventRow {
        let words = record.event(index);
        let header = words.and_then(|w| w.get(1));
        match (words, header) {
            (Some(w), Some(h)) => EventRow {
                index,
                words: w.len(),
                tag: (h >> 16) as u16,
                data_type: ((h >> 8) & 0x3F) as u8,
                num: (h & 0xFF) as u8,
                header_ok: true,
            },
            _ => EventRow {
                index,
                words: words.map(|w| w.len()).unwrap_or(0),
                tag: 0,
                data_type: 0,
                num: 0,
                header_ok: false,
            },
        }
    }

    /// True for a CODA stream frame (tag 0xFF60).
    pub fn is_stream_frame(&self) -> bool {
        self.header_ok && self.tag == STREAM_EVENT_TAG
    }
}

/// The record held in memory: the one whose events are shown.
#[derive(Debug)]
pub struct LoadedRecord {
    /// Index and offset from the scan (the record's own are relative to
    /// the reader that read it).
    pub index: usize,
    pub offset: u64,
    pub record: Record,
    pub events: Vec<EventRow>,
}

/// The event whose tree is shown.
#[derive(Debug)]
pub struct EventView {
    pub event_index: usize,
    pub rows: Vec<TreeRow>,
    /// First word of the hex window, relative to the selected structure.
    pub word_window: usize,
    /// The frame summary, decoded on `f`; `None` for a non-frame event.
    pub frame: Option<FrameSummary>,
    pub show_frame: bool,
}

/// The whole application state.
#[derive(Debug)]
pub struct App {
    path: PathBuf,
    pub screen: Screen,
    help_from: Screen,
    pub scan: ScanProgress,
    pub records: ListCursor,
    pub loaded: Option<LoadedRecord>,
    pub events: ListCursor,
    pub tree: ListCursor,
    pub view: Option<EventView>,
    /// A one-line message for the status area (a failed load, say).
    pub message: Option<String>,
    pub quit: bool,
}

impl App {
    /// State for `path` before any scan message.
    pub fn new(path: impl Into<PathBuf>) -> App {
        App {
            path: path.into(),
            screen: Screen::Records,
            help_from: Screen::Records,
            scan: ScanProgress::new(),
            records: ListCursor::new(0),
            loaded: None,
            events: ListCursor::new(0),
            tree: ListCursor::new(0),
            view: None,
            message: None,
            quit: false,
        }
    }

    /// The file shown.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Take one scan message in.
    pub fn apply(&mut self, message: ScanMessage) {
        match message {
            ScanMessage::Started(start) => self.scan.start = Some(start),
            ScanMessage::Record(summary) => {
                if summary.header.is_trailer() {
                    self.scan.trailer_seen = true;
                } else {
                    self.scan.data_records += 1;
                    self.scan.events += summary.events_found as u64;
                }
                self.scan.bytes = summary.bytes_read;
                self.scan.records.push(summary);
                self.records.set_len(self.scan.records.len());
            }
            ScanMessage::Finished(end) => {
                self.scan.bytes = end.bytes;
                self.scan.end = Some(end);
            }
            ScanMessage::Failed(e) => self.scan.failed = Some(e),
        }
    }

    /// Take one key in.
    pub fn key(&mut self, key: Key) {
        self.message = None;
        if key == Key::Char('q') {
            self.quit = true;
            return;
        }
        if key == Key::Char('?') {
            if self.screen == Screen::Help {
                self.screen = self.help_from;
            } else {
                self.help_from = self.screen;
                self.screen = Screen::Help;
            }
            return;
        }
        match self.screen {
            Screen::Help => {
                if key == Key::Esc {
                    self.screen = self.help_from;
                }
            }
            Screen::Records => {
                if !self.records.navigate(key) && key == Key::Enter {
                    self.open_selected_record();
                }
            }
            Screen::Events => {
                if self.events.navigate(key) {
                    return;
                }
                match key {
                    Key::Enter => self.open_selected_event(),
                    Key::Esc => self.screen = Screen::Records,
                    _ => {}
                }
            }
            Screen::Event => {
                if self.tree.navigate(key) {
                    if let Some(view) = &mut self.view {
                        view.word_window = 0;
                    }
                    return;
                }
                match key {
                    Key::Esc => {
                        let frame_shown = self.view.as_ref().is_some_and(|v| v.show_frame);
                        if frame_shown {
                            if let Some(view) = &mut self.view {
                                view.show_frame = false;
                            }
                        } else {
                            self.screen = Screen::Events;
                        }
                    }
                    Key::Char('n') => self.scroll_words(1),
                    Key::Char('p') => self.scroll_words(-1),
                    Key::Char('f') => self.toggle_frame(),
                    _ => {}
                }
            }
        }
    }

    /// The record summary under the cursor.
    pub fn selected_record(&self) -> Option<&RecordSummary> {
        self.scan.records.get(self.records.selected)
    }

    /// The event row under the cursor of the loaded record.
    pub fn selected_event(&self) -> Option<&EventRow> {
        self.loaded.as_ref()?.events.get(self.events.selected)
    }

    /// The tree row under the cursor.
    pub fn selected_tree_row(&self) -> Option<&TreeRow> {
        self.view.as_ref()?.rows.get(self.tree.selected)
    }

    /// Read the selected record from the file and show its events.
    pub fn open_selected_record(&mut self) {
        let Some(summary) = self.selected_record() else {
            return;
        };
        let (index, offset) = (summary.index, summary.offset);
        match access::load_record(&self.path, offset) {
            Ok(record) => {
                let events = (0..record.event_count())
                    .map(|i| EventRow::new(i, &record))
                    .collect::<Vec<_>>();
                self.events = ListCursor::new(events.len());
                self.loaded = Some(LoadedRecord {
                    index,
                    offset,
                    record,
                    events,
                });
                self.view = None;
                self.screen = Screen::Events;
            }
            Err(e) => self.message = Some(format!("record {index}: {e}")),
        }
    }

    /// Build the bank tree of the selected event and show it.
    pub fn open_selected_event(&mut self) {
        let Some(loaded) = &self.loaded else {
            return;
        };
        let Some(row) = loaded.events.get(self.events.selected) else {
            return;
        };
        let event_index = row.index;
        let Some(words) = loaded.record.event(event_index) else {
            return;
        };
        let rows = flatten(words);
        self.tree = ListCursor::new(rows.len());
        self.view = Some(EventView {
            event_index,
            rows,
            word_window: 0,
            frame: None,
            show_frame: false,
        });
        self.screen = Screen::Event;
    }

    /// The hex window of the selected structure: the absolute word offset
    /// of its first word and up to [`WORDS_PER_PAGE`] words. For an error
    /// row, the words from the offending word to the end of the event.
    pub fn selected_words(&self) -> Option<(usize, Vec<u32>)> {
        let view = self.view.as_ref()?;
        let row = view.rows.get(self.tree.selected)?;
        let event = self.loaded.as_ref()?.record.event(view.event_index)?;
        let start = row.start();
        let end = match row.total_words() {
            Some(total) => (start + total).min(event.len()),
            None => event.len(),
        };
        let first = (start + view.word_window).min(end);
        let last = (first + WORDS_PER_PAGE).min(end);
        let words = event.slice(first, last)?.to_vec();
        Some((first, words))
    }

    /// Words of the selected structure beyond the current window.
    pub fn words_after_window(&self) -> usize {
        let Some(view) = &self.view else {
            return 0;
        };
        let Some(row) = view.rows.get(self.tree.selected) else {
            return 0;
        };
        let Some(event) = self
            .loaded
            .as_ref()
            .and_then(|l| l.record.event(view.event_index))
        else {
            return 0;
        };
        let start = row.start();
        let end = match row.total_words() {
            Some(total) => (start + total).min(event.len()),
            None => event.len(),
        };
        (end - start.min(end)).saturating_sub(view.word_window + WORDS_PER_PAGE)
    }

    fn scroll_words(&mut self, direction: isize) {
        let more = self.words_after_window();
        let Some(view) = &mut self.view else {
            return;
        };
        if direction > 0 {
            if more > 0 {
                view.word_window += WORDS_PER_PAGE;
            }
        } else {
            view.word_window = view.word_window.saturating_sub(WORDS_PER_PAGE);
        }
    }

    /// Show or hide the fADC250 frame summary of the shown event. A
    /// non-frame event gets a message instead.
    pub fn toggle_frame(&mut self) {
        let Some(loaded) = &self.loaded else {
            return;
        };
        let Some(view) = &mut self.view else {
            return;
        };
        if view.show_frame {
            view.show_frame = false;
            return;
        }
        if view.frame.is_none() {
            let Some(words) = loaded.record.event(view.event_index) else {
                return;
            };
            let mut decoder = FrameDecoder::new();
            match decoder.decode(words) {
                Some(frame) => view.frame = Some(frame_summary(&frame)),
                None => {
                    self.message = Some(format!(
                        "event {} is no stream frame (tag 0x{:04X} expected)",
                        view.event_index, STREAM_EVENT_TAG
                    ));
                    return;
                }
            }
        }
        view.show_frame = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursor_scrolls_with_the_selection() {
        let mut c = ListCursor::new(100);
        c.set_height(10);
        assert_eq!(c.visible(), 0..10);
        c.navigate(Key::Down);
        assert_eq!(c.selected, 1);
        c.navigate(Key::PageDown);
        assert_eq!(c.selected, 11);
        assert_eq!(c.visible(), 2..12);
        assert_eq!(c.selected_in_view(), Some(9));
        c.navigate(Key::End);
        assert_eq!(c.selected, 99);
        assert_eq!(c.visible(), 90..100);
        c.navigate(Key::Char('g'));
        assert_eq!(c.visible(), 0..10);
        c.navigate(Key::Up);
        assert_eq!(c.selected, 0);
        c.navigate(Key::Char('G'));
        c.navigate(Key::PageUp);
        assert_eq!(c.selected, 89);
        assert!(!c.navigate(Key::Enter));
        // shrinking the list clamps the selection and the window
        c.set_len(5);
        assert_eq!(c.selected, 4);
        assert_eq!(c.visible(), 0..5);
        c.set_len(0);
        assert_eq!(c.selected_in_view(), None);
        // a list that grows while the cursor sits at the top stays put
        c.set_len(50);
        assert_eq!(c.visible(), 0..10);
    }

    #[test]
    fn help_and_quit_from_anywhere() {
        let mut app = App::new("/nonexistent");
        assert_eq!(app.screen, Screen::Records);
        app.key(Key::Char('?'));
        assert_eq!(app.screen, Screen::Help);
        app.key(Key::Esc);
        assert_eq!(app.screen, Screen::Records);
        app.key(Key::Char('?'));
        app.key(Key::Char('?'));
        assert_eq!(app.screen, Screen::Records);
        // Enter with nothing scanned does nothing; Esc stays
        app.key(Key::Enter);
        app.key(Key::Esc);
        assert_eq!(app.screen, Screen::Records);
        assert!(!app.quit);
        app.key(Key::Char('q'));
        assert!(app.quit);
    }

    #[test]
    fn scan_messages_fill_the_list() {
        let mut app = App::new("/nonexistent");
        app.apply(ScanMessage::Failed("not evio".into()));
        assert!(app.scan.is_done());
        assert!(crate::format::status_line(&app.scan).contains("not evio"));
        // a failed open then Enter: a message, no screen change
        app.key(Key::Enter);
        assert_eq!(app.screen, Screen::Records);
    }
}
