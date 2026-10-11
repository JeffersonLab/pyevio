//! Streaming, read-only reader for EVIO v6 files and JLab SRO hardware streams.
//!
//! The reader works over any [`std::io::Read`] (a file, standard input, a
//! pipe), never seeks and holds one record in memory at a time. It reads
//! the three layouts pyevio reads ([`FileLayout`]): v6 files with a file
//! header, bare v6 record streams, and the SRO hardware stream of 8-word
//! block headers. LZ4, LZ4_BEST and gzip records are decompressed. An
//! evio-library v4 file or a v1—3 file is refused with
//! [`Error::UnsupportedVersion`], which names the version.
//!
//! Words stay in file byte order inside the record buffer; [`Words`] decodes
//! them on read. Every length (record, event, bank, segment) is checked
//! against its container before it is trusted: a bad length ends the walk
//! with an error value, never a panic.
//!
//! ```no_run
//! use evio_stream::{Bank, FrameDecoder, RecordReader};
//!
//! let mut reader = RecordReader::open("run.evio")?;
//! let mut decoder = FrameDecoder::new();
//! while let Some(record) = reader.next_record()? {
//!     for event in record.iter_events() {
//!         let bank = Bank::event(event)?;
//!         if let Some(frame) = decoder.decode(event) {
//!             let hits = frame.hits().count();
//!             println!("frame {} tag 0x{:04X}: {hits} hits", frame.frame_number, bank.tag);
//!         }
//!     }
//!     reader.recycle(record);
//! }
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```
//!
//! Modules: [`header`] (file and record headers, layout detection),
//! [`record`] (one record and its events), [`reader`] (the streaming
//! iterator), [`bank`] (bank, segment and tagsegment views), [`fadc250`]
//! (the streaming fADC250 frame walk), [`checks`] (the per-file summary).

#![forbid(unsafe_code)]

pub mod bank;
pub mod checks;
pub mod error;
pub mod fadc250;
pub mod header;
pub mod reader;
pub mod record;
pub mod words;

pub use bank::{Bank, Children, StructureKind, WalkError};
pub use checks::Checks;
pub use error::Error;
pub use fadc250::{Frame, FrameDecoder, Hit, Hits, PayloadBank, RocBank};
pub use header::{Compression, FileHeader, FileLayout, RecordHeader};
pub use reader::{BadRecordHeader, RecordReader, TruncatedTail};
pub use record::{EventSpan, Record};
pub use words::{ByteOrder, Words};
