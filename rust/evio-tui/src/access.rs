//! Random access to one record of a file.
//!
//! The library reader is forward-only, but every layout it reads starts
//! each record with a record header: a 14-word v6 header (which a fresh
//! reader takes for the `v6-records` layout) or an 8-word SRO block header
//! (`sro-8word`). So a file seeked to a record offset noted during the scan
//! and handed to [`RecordReader::new`] yields that record first, with no
//! layout hint and no second pass over the file.

use std::fs::File;
use std::io::{Seek, SeekFrom};
use std::path::Path;

use evio_stream::{Record, RecordReader};

/// Read the record whose header starts at `offset`. The returned record's
/// own `index` and `offset` are relative to the fresh reader (0); callers
/// keep the scan's numbers.
pub fn load_record(path: &Path, offset: u64) -> Result<Record, String> {
    let mut file = File::open(path).map_err(|e| format!("open {}: {e}", path.display()))?;
    file.seek(SeekFrom::Start(offset))
        .map_err(|e| format!("seek to 0x{offset:X}: {e}"))?;
    let mut reader =
        RecordReader::new(file).map_err(|e| format!("no record header at 0x{offset:X}: {e}"))?;
    match reader.next_record() {
        Ok(Some(record)) => Ok(record),
        Ok(None) => Err(match reader.bad_record_header() {
            Some(bad) => format!("no record at 0x{offset:X}: {}", bad.message),
            None => format!("no record at 0x{offset:X}"),
        }),
        Err(e) => Err(format!("record at 0x{offset:X}: {e}")),
    }
}
