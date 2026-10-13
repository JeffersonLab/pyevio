//! The per-file summary: counts and the checks of pyevio's `EvioFile.checks`.

use std::io::Read;

use crate::error::Error;
use crate::header::FileLayout;
use crate::reader::{BadRecordHeader, RecordReader, TruncatedTail};
use crate::record::Record;
use crate::words::ByteOrder;

/// What one pass over a stream found.
#[derive(Clone, Debug)]
pub struct Checks {
    pub layout: FileLayout,
    pub order: ByteOrder,
    /// Data records read, a cut tail included, the trailer excluded.
    pub records: u64,
    /// Events found, the salvaged events of a cut tail included.
    pub events: u64,
    /// True when the stream ended in a trailer record.
    pub trailer: bool,
    pub truncated_tail: Option<TruncatedTail>,
    pub bad_record_header: Option<BadRecordHeader>,
    /// Records whose event walk or decompression reported an error.
    pub records_with_errors: u64,
    /// The first [`Checks::MAX_MESSAGES`] record error messages, prefixed
    /// with the record index.
    pub error_messages: Vec<String>,
    /// Bytes consumed from the stream.
    pub bytes: u64,
}

impl Checks {
    /// Error messages kept.
    pub const MAX_MESSAGES: usize = 50;

    /// Start a summary for `reader`.
    pub fn start<R: Read>(reader: &RecordReader<R>) -> Checks {
        Checks {
            layout: reader.layout(),
            order: reader.order(),
            records: 0,
            events: 0,
            trailer: false,
            truncated_tail: None,
            bad_record_header: None,
            records_with_errors: 0,
            error_messages: Vec::new(),
            bytes: 0,
        }
    }

    /// Count one record.
    pub fn add(&mut self, record: &Record) {
        if record.is_trailer() {
            self.trailer = true;
        } else {
            self.records += 1;
            self.events += record.event_count() as u64;
        }
        if !record.errors.is_empty() {
            self.records_with_errors += 1;
            for message in &record.errors {
                if self.error_messages.len() >= Self::MAX_MESSAGES {
                    break;
                }
                self.error_messages
                    .push(format!("record {}: {message}", record.index));
            }
        }
    }

    /// Copy the end-of-stream state from the reader.
    pub fn finish<R: Read>(&mut self, reader: &RecordReader<R>) {
        self.truncated_tail = reader.truncated_tail();
        self.bad_record_header = reader.bad_record_header().cloned();
        self.trailer = reader.trailer_seen();
        self.bytes = reader.bytes_read();
    }

    /// Read every record of `reader`, calling `visit` on each, and return
    /// the summary. The reader's record buffers are recycled.
    pub fn scan_with<R: Read, F: FnMut(&Record)>(
        mut reader: RecordReader<R>,
        mut visit: F,
    ) -> Result<Checks, Error> {
        let mut checks = Checks::start(&reader);
        while let Some(record) = reader.next_record()? {
            checks.add(&record);
            visit(&record);
            reader.recycle(record);
        }
        checks.finish(&reader);
        Ok(checks)
    }

    /// Read every record of `reader` and return the summary.
    pub fn scan<R: Read>(reader: RecordReader<R>) -> Result<Checks, Error> {
        Self::scan_with(reader, |_| {})
    }

    /// True when no check fired and no record reported an error.
    pub fn is_clean(&self) -> bool {
        self.truncated_tail.is_none()
            && self.bad_record_header.is_none()
            && self.records_with_errors == 0
    }
}
