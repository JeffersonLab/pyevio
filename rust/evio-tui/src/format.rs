//! Text for every cell, line and panel the screens show. Nothing here
//! touches the terminal, so unit tests check the strings directly.

use evio_stream::bank::data_type;
use evio_stream::fadc250::{module_name, STREAM_EVENT_TAG};
use evio_stream::{Frame, StructureKind};

use crate::scan::{RecordSummary, ScanStart};
use crate::state::{EventRow, ScanProgress};
use crate::tree::{TreeItem, TreeRow};

/// Words shown per hex row.
pub const HEX_WORDS_PER_ROW: usize = 8;

/// Column titles of the records table; see [`record_cells`].
pub const RECORD_COLUMNS: [&str; 9] = [
    "index", "offset", "bytes", "type", "events", "found", "compress", "trunc", "errors",
];

/// Column titles of the events table; see [`event_cells`].
pub const EVENT_COLUMNS: [&str; 6] = ["event", "words", "tag", "type", "num", "note"];

/// Column titles of the bank tree table; see [`tree_cells`].
pub const TREE_COLUMNS: [&str; 6] = ["structure", "tag", "type", "num", "length", "words"];

/// `1.23 GB`, `456.7 MB`, `12.3 kB`, `99 B`.
pub fn human_bytes(bytes: u64) -> String {
    let b = bytes as f64;
    if bytes >= 1_000_000_000 {
        format!("{:.2} GB", b / 1e9)
    } else if bytes >= 1_000_000 {
        format!("{:.1} MB", b / 1e6)
    } else if bytes >= 1_000 {
        format!("{:.1} kB", b / 1e3)
    } else {
        format!("{bytes} B")
    }
}

/// The cells of one records-table row.
pub fn record_cells(s: &RecordSummary) -> [String; 9] {
    let h = &s.header;
    [
        s.index.to_string(),
        format!("0x{:X}", s.offset),
        s.declared_bytes().to_string(),
        s.kind_name().to_string(),
        h.event_count.to_string(),
        s.events_found.to_string(),
        h.compression.name(),
        if s.is_truncated { "yes" } else { "" }.to_string(),
        if s.error_count > 0 {
            s.error_count.to_string()
        } else {
            String::new()
        },
    ]
}

/// The side panel of a record: every header field.
pub fn record_header_lines(s: &RecordSummary) -> Vec<String> {
    let h = &s.header;
    let mut lines = vec![
        format!("record {} at 0x{:X}", s.index, s.offset),
        format!(
            "record length    {} words ({} bytes)",
            h.record_length,
            s.declared_bytes()
        ),
        format!("present          {} bytes", s.present_bytes),
        format!("record number    {}", h.record_number),
        format!("header length    {} words", h.header_length),
        format!("header type      {} ({})", h.header_type, s.kind_name()),
        format!("version          {}", h.version),
        format!(
            "events           {} declared, {} found",
            h.event_count, s.events_found
        ),
        format!("index array      {} bytes", h.index_array_length),
        format!("user header      {} bytes", h.user_header_length),
        format!("uncompressed     {} bytes", h.uncompressed_data_length),
        format!(
            "compression      {} ({} words)",
            h.compression.name(),
            h.compressed_data_length
        ),
        format!("bit info         0x{:05X}", h.bit_info),
        format!("event type       {}", h.event_type_name()),
        format!(
            "last / dict / first   {} / {} / {}",
            h.is_last_record, h.has_dictionary, h.has_first_event
        ),
        format!("pad1/2/3         {} {} {}", h.pad1, h.pad2, h.pad3),
        format!("user register 1  0x{:016x}", h.user_register1),
        format!("user register 2  0x{:016x}", h.user_register2),
        format!("truncated        {}", s.is_truncated),
    ];
    if s.error_count > 0 {
        lines.push(format!("errors           {}", s.error_count));
        for e in &s.errors {
            lines.push(format!("  {e}"));
        }
    }
    lines
}

/// The file line under the title: layout, byte order, file type.
pub fn file_line(start: Option<&ScanStart>) -> String {
    match start {
        Some(s) => {
            let h = &s.file_header;
            let mut line = format!(
                "layout {}  byte order {}  file type {}",
                s.layout, s.order, h.file_type
            );
            if h.header_length > 0 {
                line.push_str(&format!(
                    "  records declared {}  dict {}  first event {}  trailer index {}",
                    h.record_count, h.has_dictionary, h.has_first_event, h.has_trailer
                ));
            }
            line
        }
        None => "opening...".to_string(),
    }
}

/// The status line: progress while the scan runs, the checks once it ends.
pub fn status_line(scan: &ScanProgress) -> String {
    if let Some(failed) = &scan.failed {
        return format!("scan failed: {failed}");
    }
    let elapsed = scan.elapsed_s();
    let rate = scan.bytes as f64 / 1e9 / elapsed.max(1e-9);
    let base = format!(
        "{}  {} events  {}  {elapsed:.2} s  {rate:.2} GB/s",
        scan.records_label(),
        scan.events,
        human_bytes(scan.bytes)
    );
    match &scan.end {
        None => format!("scanning: {base}"),
        Some(end) => {
            let mut parts = vec![format!("done: {base}")];
            parts.push(format!(
                "trailer {}",
                if end.trailer { "yes" } else { "no" }
            ));
            match end.truncated_tail {
                Some(t) => parts.push(format!(
                    "truncated_tail ({}, {}, {})",
                    t.declared_bytes, t.present_bytes, t.salvaged_events
                )),
                None => parts.push("truncated_tail none".into()),
            }
            match &end.bad_record_header {
                Some(b) => parts.push(format!(
                    "bad_record_header at 0x{:X}: {}",
                    b.offset, b.message
                )),
                None => parts.push("bad_record_header none".into()),
            }
            if let Some(e) = &end.error {
                parts.push(format!("reader error: {e}"));
            }
            parts.join("  ")
        }
    }
}

/// The cells of one events-table row.
pub fn event_cells(e: &EventRow) -> [String; 6] {
    let note = if !e.header_ok {
        "no bank header"
    } else if e.tag == STREAM_EVENT_TAG {
        "stream frame"
    } else {
        ""
    };
    [
        e.index.to_string(),
        e.words.to_string(),
        format!("0x{:04X}", e.tag),
        format!("0x{:02X} {}", e.data_type, data_type::name(e.data_type)),
        e.num.to_string(),
        note.to_string(),
    ]
}

/// The cells of one bank-tree row, and whether the row is an error.
pub fn tree_cells(row: &TreeRow) -> ([String; 6], bool) {
    let indent = "  ".repeat(row.depth);
    match &row.item {
        TreeItem::Node {
            kind,
            tag,
            data_type: dt,
            num,
            length,
            total_words,
            ..
        } => (
            [
                format!("{indent}{}", kind.name()),
                format!("0x{tag:04X}"),
                format!("0x{dt:02X} {}", data_type::name(*dt)),
                match kind {
                    StructureKind::Bank => num.to_string(),
                    _ => String::new(),
                },
                length.to_string(),
                total_words.to_string(),
            ],
            false,
        ),
        TreeItem::Error { start, message } => (
            [
                format!("{indent}ERROR word {start}: {message}"),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
            ],
            true,
        ),
    }
}

/// Hex rows of `words`, [`HEX_WORDS_PER_ROW`] per row, the word offset of
/// each row at the left (`first_word` is the offset of `words[0]`).
pub fn hex_lines(first_word: usize, words: &[u32]) -> Vec<String> {
    words
        .chunks(HEX_WORDS_PER_ROW)
        .enumerate()
        .map(|(i, chunk)| {
            let offset = first_word + i * HEX_WORDS_PER_ROW;
            let hex: Vec<String> = chunk.iter().map(|w| format!("{w:08x}")).collect();
            format!("{offset:>6}  {}", hex.join(" "))
        })
        .collect()
}

/// The fADC250 frame summary of one stream event, as text.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FrameSummary {
    pub lines: Vec<String>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

/// Summarize a decoded frame: frame number, timestamp, totals, the per-ROC
/// table, then the decoder's error and warning messages.
pub fn frame_summary(frame: &Frame<'_>) -> FrameSummary {
    let mut lines = vec![
        format!(
            "frame {}  timestamp {} (0x{:X})",
            frame.frame_number, frame.timestamp, frame.timestamp
        ),
        format!(
            "ROC banks {}  payload banks {}  empty {}  unmapped {}",
            frame.roc_banks.len(),
            frame.n_payload_banks,
            frame.n_empty_payload_banks,
            frame.n_unmapped_payload_banks
        ),
        format!(
            "fADC250 hits {}  other-module words {}  payload words {}",
            frame.n_hits, frame.non_fadc_words, frame.n_payload_words
        ),
        format!(
            "AIS entries {} ({} distinct)  errors {}  warnings {}",
            frame.n_ais_entries, frame.n_ais_distinct, frame.n_errors, frame.n_warnings
        ),
        format!(
            "{:>5} {:>6} {:>8} {:>8} {:>6} {:>10} {:>12}",
            "roc", "stream", "module", "payload", "empty", "hits", "other words"
        ),
    ];
    for roc in frame.roc_banks {
        lines.push(format!(
            "{:>5} {:>6} {:>8} {:>8} {:>6} {:>10} {:>12}",
            roc.roc,
            roc.stream,
            module_name(roc.module),
            roc.n_payload_banks,
            roc.n_empty_payload_banks,
            roc.n_hits,
            roc.non_fadc_words
        ));
    }
    FrameSummary {
        lines,
        errors: frame.errors.to_vec(),
        warnings: frame.warnings.to_vec(),
    }
}

/// The one-screen key help.
pub const HELP_LINES: [&str; 15] = [
    "evio-tui keys",
    "",
    "  Up/Down, PgUp/PgDn      move the selection",
    "  Home/End, g/G           first / last row",
    "  Enter (or Right)        open the record / the event",
    "  Esc (or Left, Backspace) back one level (closes the frame view first)",
    "  Ctrl-C                  quit, like q",
    "  n / p                   next / previous 64 words of the selected structure",
    "  f                       fADC250 frame summary of a stream event (tag 0xFF60)",
    "  ?                       this help",
    "  q                       quit",
    "",
    "Screens: records of the file (the scan fills the list while it runs),",
    "events of one record (read from the file at the record's offset),",
    "the bank tree and words of one event.",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bytes_and_hex() {
        assert_eq!(human_bytes(12), "12 B");
        assert_eq!(human_bytes(1_500), "1.5 kB");
        assert_eq!(human_bytes(2_500_000), "2.5 MB");
        assert_eq!(human_bytes(15_550_062_464), "15.55 GB");
        let lines = hex_lines(16, &[1, 2, 3, 4, 5, 6, 7, 8, 9]);
        assert_eq!(lines.len(), 2);
        assert_eq!(
            lines[0],
            "    16  00000001 00000002 00000003 00000004 00000005 00000006 00000007 00000008"
        );
        assert_eq!(lines[1], "    24  00000009");
        assert!(hex_lines(0, &[]).is_empty());
    }

    #[test]
    fn event_and_tree_cells() {
        let e = EventRow {
            index: 3,
            words: 7,
            tag: 0xFF60,
            data_type: 0x10,
            num: 0,
            header_ok: true,
        };
        let cells = event_cells(&e);
        assert_eq!(cells[2], "0xFF60");
        assert_eq!(cells[3], "0x10 banks");
        assert_eq!(cells[5], "stream frame");
        let row = TreeRow {
            depth: 2,
            item: TreeItem::Error {
                start: 9,
                message: "too long".into(),
            },
        };
        let (cells, is_error) = tree_cells(&row);
        assert!(is_error);
        assert_eq!(cells[0], "    ERROR word 9: too long");
        let row = TreeRow {
            depth: 1,
            item: TreeItem::Node {
                kind: StructureKind::Segment,
                tag: 0x32,
                data_type: 1,
                num: 0,
                length: 3,
                start: 4,
                total_words: 4,
                is_container: false,
            },
        };
        let (cells, is_error) = tree_cells(&row);
        assert!(!is_error);
        assert_eq!(cells[0], "  Segment");
        assert_eq!(cells[3], "");
        assert_eq!(cells[5], "4");
    }
}
