//! The background scan: the whole file streamed once from the start with
//! [`RecordReader`], one small [`RecordSummary`] per record sent over a
//! channel so the user interface never waits on the file.
//!
//! [`scan`] is synchronous and takes the sender; [`spawn`] runs it on a
//! thread. Tests call `scan` inline and drain the receiver.

use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::Instant;

use evio_stream::{
    BadRecordHeader, ByteOrder, FileHeader, FileLayout, Record, RecordHeader, RecordReader,
    TruncatedTail,
};

/// Record error messages kept per summary (the count is always exact).
pub const MAX_ERRORS_KEPT: usize = 4;

/// What one record looked like when the scan passed it. The full header is
/// kept (it is small) so the side panel needs no second look at the file.
#[derive(Clone, Debug)]
pub struct RecordSummary {
    /// Position in the stream, from 0 (the trailer included).
    pub index: usize,
    /// Byte offset of the record header in the file.
    pub offset: u64,
    pub header: RecordHeader,
    /// Bytes of the record present in the file, header included.
    pub present_bytes: u64,
    /// Events found (the complete ones for a cut tail).
    pub events_found: usize,
    pub is_truncated: bool,
    /// Record errors (event walk, decompression); see [`error_count`](Self::error_count).
    pub errors: Vec<String>,
    pub error_count: usize,
    /// Bytes the scan had consumed after this record.
    pub bytes_read: u64,
}

impl RecordSummary {
    /// Summarize a record just read by a reader that has consumed
    /// `bytes_read` bytes so far.
    pub fn new(record: &Record, bytes_read: u64) -> RecordSummary {
        RecordSummary {
            index: record.index,
            offset: record.offset,
            header: record.header.clone(),
            present_bytes: record.present_bytes,
            events_found: record.event_count(),
            is_truncated: record.is_truncated,
            errors: record
                .errors
                .iter()
                .take(MAX_ERRORS_KEPT)
                .cloned()
                .collect(),
            error_count: record.errors.len(),
            bytes_read,
        }
    }

    /// Byte length the header declares for the record.
    pub fn declared_bytes(&self) -> u64 {
        self.header.record_bytes()
    }

    /// "trailer" or "record".
    pub fn kind_name(&self) -> &'static str {
        if self.header.is_trailer() {
            "trailer"
        } else {
            "record"
        }
    }
}

/// What the first bytes of the file said.
#[derive(Clone, Debug)]
pub struct ScanStart {
    pub layout: FileLayout,
    pub order: ByteOrder,
    pub file_header: FileHeader,
}

/// How the scan ended.
#[derive(Clone, Debug)]
pub struct ScanEnd {
    pub truncated_tail: Option<TruncatedTail>,
    pub bad_record_header: Option<BadRecordHeader>,
    pub trailer: bool,
    /// Data records read (the trailer excluded).
    pub data_records: u64,
    pub bytes: u64,
    pub elapsed_s: f64,
    /// Set when the reader returned an error (an evio-library v4 file that
    /// revealed itself late, an I/O error); the records before it stand.
    pub error: Option<String>,
}

/// One message from the scan.
#[derive(Clone, Debug)]
pub enum ScanMessage {
    Started(ScanStart),
    Record(RecordSummary),
    Finished(ScanEnd),
    /// The file could not be opened as EVIO; nothing else follows.
    Failed(String),
}

/// Stream `path` from the start and send one message per record. Returns
/// when the file ends, when the reader fails or when the receiver is gone.
pub fn scan(path: &Path, tx: &Sender<ScanMessage>) {
    let start = Instant::now();
    let mut reader = match RecordReader::open(path) {
        Ok(reader) => reader,
        Err(e) => {
            let _ = tx.send(ScanMessage::Failed(e.to_string()));
            return;
        }
    };
    let started = ScanStart {
        layout: reader.layout(),
        order: reader.order(),
        file_header: reader.file_header().clone(),
    };
    if tx.send(ScanMessage::Started(started)).is_err() {
        return;
    }
    let mut error = None;
    loop {
        match reader.next_record() {
            Ok(Some(record)) => {
                let summary = RecordSummary::new(&record, reader.bytes_read());
                let delivered = tx.send(ScanMessage::Record(summary)).is_ok();
                reader.recycle(record);
                if !delivered {
                    return;
                }
            }
            Ok(None) => break,
            Err(e) => {
                error = Some(e.to_string());
                break;
            }
        }
    }
    let end = ScanEnd {
        truncated_tail: reader.truncated_tail(),
        bad_record_header: reader.bad_record_header().cloned(),
        trailer: reader.trailer_seen(),
        data_records: reader.data_records(),
        bytes: reader.bytes_read(),
        elapsed_s: start.elapsed().as_secs_f64(),
        error,
    };
    let _ = tx.send(ScanMessage::Finished(end));
}

/// Run [`scan`] on a background thread. Dropping the receiver ends the
/// thread at its next record.
pub fn spawn(path: PathBuf) -> Receiver<ScanMessage> {
    let (tx, rx) = mpsc::channel();
    thread::Builder::new()
        .name("evio-scan".into())
        .spawn(move || scan(&path, &tx))
        .expect("spawn the scan thread");
    rx
}
