//! The streaming record reader.

use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

use crate::error::Error;
use crate::header::{
    detect_byte_order, file_type_name, padded_to_words, record_header_problem, FileHeader,
    FileLayout, RecordHeader, FULL_HEADER_BYTES, SHORT_HEADER_BYTES, SRO_BLOCK_WORD,
};
use crate::record::Record;
use crate::words::{ByteOrder, Words};

/// Largest buffer reserved before a record is read (64 MB); see `read_record`.
const RESERVE_CAP: u64 = 1 << 26;

/// The `("bad_record_header", offset, message)` check of pyevio: bytes that
/// are no record header ended the scan.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BadRecordHeader {
    pub offset: u64,
    pub message: String,
}

/// The `("truncated_tail", declared, present, salvaged)` check of pyevio: the
/// last record's declared length ran past the end of the stream; its
/// complete events were kept.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TruncatedTail {
    /// Bytes the record header declares, header included.
    pub declared_bytes: u64,
    /// Bytes of the record present, header included.
    pub present_bytes: u64,
    /// Complete events read from the cut record.
    pub salvaged_events: usize,
}

/// Reads records one after another from any [`Read`], never seeking.
///
/// The reader holds one record in memory at a time. It is an iterator over
/// `Result<Record, Error>`; iteration ends at the end of the stream, after
/// the trailer, at a cut tail (the cut record is yielded with its complete
/// events) or at bytes that are no record header (recorded in
/// [`bad_record_header`](Self::bad_record_header), never an `Err`). The
/// only `Err` items are I/O errors and the version error of an evio-library
/// v4 file whose block chain only reveals itself after the first block.
pub struct RecordReader<R> {
    inner: R,
    layout: FileLayout,
    order: ByteOrder,
    file_header: FileHeader,
    file_user_header: Vec<u8>,
    /// Bytes consumed from the stream so far.
    offset: u64,
    next_index: usize,
    pending_head: Option<[u8; SHORT_HEADER_BYTES]>,
    spare: Vec<u8>,
    scratch: Vec<u8>,
    done: bool,
    after_last_bit: bool,
    bad_record_header: Option<BadRecordHeader>,
    truncated_tail: Option<TruncatedTail>,
    data_records: u64,
    trailer_seen: bool,
}

impl RecordReader<File> {
    /// Open a file. `"-"` reads standard input (see [`RecordReader::stdin`]).
    pub fn open<P: AsRef<Path>>(path: P) -> Result<RecordReader<File>, Error> {
        RecordReader::new(File::open(path)?)
    }
}

impl RecordReader<io::StdinLock<'static>> {
    /// Read standard input.
    pub fn stdin() -> Result<Self, Error> {
        RecordReader::new(io::stdin().lock())
    }
}

impl<R: Read> RecordReader<R> {
    /// Detect the layout from the first bytes and position the reader at the
    /// first record. Fails with [`Error::UnsupportedVersion`] for an EVIO v4
    /// or v1—3 file and [`Error::NotEvio`] for anything without the magic.
    pub fn new(mut inner: R) -> Result<Self, Error> {
        let mut head = [0u8; SHORT_HEADER_BYTES];
        let got = read_full(&mut inner, &mut head)?;
        if got < SHORT_HEADER_BYTES {
            return Err(Error::NotEvio(format!(
                "{got} bytes, a header needs {SHORT_HEADER_BYTES}"
            )));
        }
        let order = detect_byte_order(&head).ok_or_else(|| {
            Error::NotEvio("no magic number 0xc0da0100 at word 7 of the first header".into())
        })?;
        let w = Words::new(&head, order);
        let word = |i: usize| w.get(i).unwrap_or(0);

        let mut offset = 0u64;
        let mut file_user_header = Vec::new();
        let (layout, file_header, pending_head) = if file_type_name(word(0)).is_some() {
            let header_length = word(2) as usize;
            let header_bytes = header_length * 4;
            let mut full = head.to_vec();
            if header_bytes > SHORT_HEADER_BYTES {
                full.resize(header_bytes, 0);
                let more = read_full(&mut inner, &mut full[SHORT_HEADER_BYTES..])?;
                if more < header_bytes - SHORT_HEADER_BYTES {
                    return Err(Error::NotEvio(format!(
                        "file header declares {header_length} words, the file ends inside it"
                    )));
                }
            } else if header_length < 8 {
                return Err(Error::NotEvio(format!(
                    "file header length {header_length} words, expected 14"
                )));
            }
            let header = FileHeader::parse(&full, order).map_err(Error::NotEvio)?;
            if header.version != 6 {
                return Err(Error::UnsupportedVersion {
                    version: header.version,
                    detail: "file header".into(),
                });
            }
            offset += header_bytes as u64;
            // The file-level index array (record lengths) is not used; the
            // user header (dictionary, first event) is kept, padded to words.
            let skip = header.index_array_length as u64;
            offset += skip_bytes(&mut inner, skip)?;
            let user_padded = padded_to_words(header.user_header_length as u64);
            file_user_header.resize(user_padded as usize, 0);
            let got = read_full(&mut inner, &mut file_user_header)?;
            file_user_header.truncate(got.min(header.user_header_length as usize));
            offset += got as u64;
            (FileLayout::V6, header, None)
        } else {
            let header_length = word(2);
            let bit_info_version = word(5);
            let version = (bit_info_version & 0xFF) as u8;
            if header_length == 14 && version == 6 {
                let header = FileHeader::stand_in(FileLayout::V6Records, order);
                (FileLayout::V6Records, header, Some(head))
            } else if header_length == 8 && bit_info_version == SRO_BLOCK_WORD {
                let header = FileHeader::stand_in(FileLayout::Sro8Word, order);
                (FileLayout::Sro8Word, header, Some(head))
            } else {
                let detail = if header_length == 8 && version == 4 {
                    format!(
                        "block header at offset 0 has bit-info 0x{:X}, an evio-library v4 file rather than an SRO stream",
                        bit_info_version >> 8
                    )
                } else {
                    "block header at offset 0, no file header".to_string()
                };
                return Err(Error::UnsupportedVersion { version, detail });
            }
        };

        Ok(RecordReader {
            inner,
            layout,
            order,
            file_header,
            file_user_header,
            offset,
            next_index: 0,
            pending_head,
            spare: Vec::new(),
            scratch: Vec::new(),
            done: false,
            after_last_bit: false,
            bad_record_header: None,
            truncated_tail: None,
            data_records: 0,
            trailer_seen: false,
        })
    }

    /// The layout detected from the first bytes.
    pub fn layout(&self) -> FileLayout {
        self.layout
    }

    /// The byte order of the file.
    pub fn order(&self) -> ByteOrder {
        self.order
    }

    /// The file header, or a stand-in for the layouts without one.
    pub fn file_header(&self) -> &FileHeader {
        &self.file_header
    }

    /// The file header's user header (dictionary and first event as a
    /// record), empty for the layouts without one.
    pub fn file_user_header(&self) -> &[u8] {
        &self.file_user_header
    }

    /// Bytes consumed from the stream so far.
    pub fn bytes_read(&self) -> u64 {
        self.offset
    }

    /// Data records yielded so far (the trailer excluded).
    pub fn data_records(&self) -> u64 {
        self.data_records
    }

    /// True once the trailer has been read.
    pub fn trailer_seen(&self) -> bool {
        self.trailer_seen
    }

    /// Set when the scan ended on bytes that are no record header.
    pub fn bad_record_header(&self) -> Option<&BadRecordHeader> {
        self.bad_record_header.as_ref()
    }

    /// Set when the last record was cut by the end of the stream.
    pub fn truncated_tail(&self) -> Option<TruncatedTail> {
        self.truncated_tail
    }

    /// Give a record's buffer back so the next record reuses the allocation.
    pub fn recycle(&mut self, record: Record) {
        let buffer = record.into_buffer();
        if buffer.capacity() > self.spare.capacity() {
            self.spare = buffer;
        }
    }

    /// Read the next record, `Ok(None)` at the end. See the type docs for
    /// what ends the iteration.
    pub fn next_record(&mut self) -> Result<Option<Record>, Error> {
        if self.done {
            return Ok(None);
        }
        match self.read_record() {
            Ok(Some(record)) => Ok(Some(record)),
            Ok(None) => {
                self.done = true;
                Ok(None)
            }
            Err(e) => {
                self.done = true;
                Err(e)
            }
        }
    }

    fn stop_with(&mut self, message: String) -> Result<Option<Record>, Error> {
        // After a record with the last-record bit, bytes that are no record
        // header end the scan quietly, as pyevio's scan does; otherwise they
        // are a bad_record_header check.
        if !self.after_last_bit {
            self.bad_record_header = Some(BadRecordHeader {
                offset: self.offset,
                message,
            });
        }
        Ok(None)
    }

    fn read_record(&mut self) -> Result<Option<Record>, Error> {
        let mut head = [0u8; FULL_HEADER_BYTES];
        let got = match self.pending_head.take() {
            Some(pending) => {
                head[..SHORT_HEADER_BYTES].copy_from_slice(&pending);
                SHORT_HEADER_BYTES
            }
            None => read_full(&mut self.inner, &mut head[..SHORT_HEADER_BYTES])?,
        };
        if got == 0 {
            return Ok(None);
        }
        if let Some(problem) = record_header_problem(&head[..got], self.order) {
            return self.stop_with(problem);
        }
        let header_length = Words::new(&head, self.order).get(2).unwrap_or(0) as usize;
        let header_bytes = header_length * 4;
        if header_bytes > SHORT_HEADER_BYTES {
            let more = read_full(&mut self.inner, &mut head[SHORT_HEADER_BYTES..header_bytes])?;
            if more < header_bytes - SHORT_HEADER_BYTES {
                let left = SHORT_HEADER_BYTES + more;
                return self.stop_with(format!(
                    "{left} bytes left, the {header_length}-word header needs {header_bytes}"
                ));
            }
        }
        let header = RecordHeader::parse(&head[..header_bytes], self.order);

        // Version 6 is the record header; the SRO block header (8 words,
        // version 4, no bit-info bits) is the one other shape read. In an SRO
        // stream any other 8-word header means an evio-library v4 file, which
        // the whole-chain check of pyevio rejects at open; without seeks it is
        // rejected where it shows.
        if header.version != 6 && !header.is_sro_block() {
            if self.layout == FileLayout::Sro8Word && header.header_length == 8 {
                let detail = if header.version == 4 {
                    format!(
                        "block header at offset 0x{:X} has bit-info 0x{:X}, an evio-library v4 file rather than an SRO stream",
                        self.offset,
                        (header.bit_info | ((header.header_type as u32) << 20))
                    )
                } else {
                    format!("block header at offset 0x{:X}, no file header", self.offset)
                };
                return Err(Error::UnsupportedVersion {
                    version: header.version,
                    detail,
                });
            }
            return self.stop_with(format!(
                "Unsupported EVIO version in record at offset 0x{:X}: version {}, expected 6",
                self.offset, header.version
            ));
        }

        let body_len = header.record_bytes() - header.header_bytes();
        let mut body = std::mem::take(&mut self.spare);
        body.clear();
        // Reserve the declared size (no zeroing: read_to_end fills spare
        // capacity), capped so a corrupt length word cannot demand 16 GB up
        // front; a larger record grows the buffer as it is read.
        body.reserve(body_len.min(RESERVE_CAP) as usize);
        (&mut self.inner).take(body_len).read_to_end(&mut body)?;
        let is_truncated = (body.len() as u64) < body_len;
        let present = header.header_bytes() + body.len() as u64;
        let is_trailer = header.is_trailer();
        let is_last = header.is_last_record;
        let record_offset = self.offset;
        let record = Record::build(
            self.next_index,
            record_offset,
            header,
            self.order,
            body,
            is_truncated,
            &mut self.scratch,
        );
        self.next_index += 1;
        self.offset += present;
        self.after_last_bit = is_last;
        if is_trailer {
            self.trailer_seen = true;
            self.done = true;
        } else {
            self.data_records += 1;
        }
        if is_truncated {
            self.truncated_tail = Some(TruncatedTail {
                declared_bytes: record.declared_bytes,
                present_bytes: present,
                salvaged_events: record.event_count(),
            });
            self.done = true;
        }
        Ok(Some(record))
    }
}

impl<R: Read> Iterator for RecordReader<R> {
    type Item = Result<Record, Error>;

    fn next(&mut self) -> Option<Self::Item> {
        self.next_record().transpose()
    }
}

/// Fill `buf` as far as the stream allows; returns the bytes read (less than
/// `buf.len()` only at the end of the stream).
fn read_full<R: Read>(reader: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(filled)
}

/// Discard `n` bytes; returns how many were there.
fn skip_bytes<R: Read>(reader: &mut R, n: u64) -> io::Result<u64> {
    io::copy(&mut reader.take(n), &mut io::sink())
}
