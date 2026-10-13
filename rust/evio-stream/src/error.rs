//! The error type of the reader.

use std::fmt;
use std::io;

/// Why a stream could not be opened or read further.
///
/// Malformed content inside a record (a bad event or bank length, a failed
/// decompression) is never an `Error`: it is recorded in
/// [`Record::errors`](crate::Record::errors) or returned by the bank walk as a
/// [`WalkError`](crate::WalkError), and reading goes on with what is sound.
#[derive(Debug)]
pub enum Error {
    /// The underlying reader failed.
    Io(io::Error),
    /// The first bytes are no EVIO header at all (no magic word, too short).
    NotEvio(String),
    /// An EVIO version this crate does not read: an evio-library v4 file or a
    /// v1—3 file. `version` is the version word of the header that gave it
    /// away; `detail` says where.
    UnsupportedVersion { version: u8, detail: String },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "I/O error: {e}"),
            Error::NotEvio(m) => write!(f, "Not an EVIO file: {m}"),
            Error::UnsupportedVersion { version, detail } => write!(
                f,
                "EVIO version {version} file is not supported: evio-stream reads EVIO version 6 \
                 files and SRO streaming files. Convert the file with the evio tools (evio2xml, \
                 eviocopy) or read it with the evio library. ({detail})"
            ),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}
