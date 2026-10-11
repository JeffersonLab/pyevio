//! One record: its header, its bytes and the events inside.

use std::io::Read;

use crate::header::{padded_to_words, Compression, RecordHeader};
use crate::words::{ByteOrder, Words};

/// Where one event lives inside [`Record::buffer`], in bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EventSpan {
    pub offset: usize,
    pub length: usize,
}

/// A record read from the stream, with its bytes.
///
/// The record owns one buffer that holds the index array, the user header
/// and the events: the bytes as they came from the file for a plain record,
/// the decompressed bytes for an LZ4, LZ4_BEST or gzip record. Event spans
/// index that buffer. Words stay in file byte order; [`Words`] decodes them.
#[derive(Debug)]
pub struct Record {
    /// Position of the record in the stream: data records and the trailer,
    /// in the order read, from 0.
    pub index: usize,
    /// Byte offset of the record header in the stream.
    pub offset: u64,
    pub header: RecordHeader,
    order: ByteOrder,
    buffer: Vec<u8>,
    /// True when the record's declared length runs past the end of the
    /// stream; `events` then holds the complete events present.
    pub is_truncated: bool,
    /// Byte length the header declares, header included.
    pub declared_bytes: u64,
    /// Bytes of the record present in the stream, header included.
    pub present_bytes: u64,
    data_start: usize,
    data_end: usize,
    events: Vec<EventSpan>,
    /// Why an event walk or a decompression stopped early; empty when every
    /// length fit.
    pub errors: Vec<String>,
}

impl Record {
    /// Build a record from its header and the body bytes that follow the
    /// header (`body` has `record_bytes - header_bytes` bytes, fewer for a
    /// cut tail). `scratch` receives the compressed bytes of a compressed
    /// record so the caller can recycle the allocation.
    pub(crate) fn build(
        index: usize,
        offset: u64,
        header: RecordHeader,
        order: ByteOrder,
        body: Vec<u8>,
        is_truncated: bool,
        scratch: &mut Vec<u8>,
    ) -> Record {
        let declared_bytes = header.record_bytes();
        let present_bytes = header.header_bytes() + body.len() as u64;
        let mut errors = Vec::new();
        let index_len = header.index_array_length as usize;
        let user_padded = padded_to_words(header.user_header_length as u64) as usize;
        let data_start = index_len + user_padded;

        let (buffer, data_end) = if header.compression == Compression::None {
            let end = body.len();
            (body, end)
        } else {
            let declared_end = data_start + header.uncompressed_data_length as usize;
            let out = decompress(&header, &body, declared_end, &mut errors);
            *scratch = body;
            let end = declared_end.min(out.len());
            (out, end)
        };

        let mut record = Record {
            index,
            offset,
            header,
            order,
            buffer,
            is_truncated,
            declared_bytes,
            present_bytes,
            data_start,
            data_end,
            events: Vec::new(),
            errors,
        };
        record.scan_events();
        record
    }

    /// Walk the events: by the index array when the record has one, else by
    /// the length word of each event bank. A trailer yields no events.
    fn scan_events(&mut self) {
        if self.header.is_trailer() {
            return;
        }
        let w = Words::new(&self.buffer, self.order);
        let data_end = self.data_end.min(self.buffer.len());
        let mut offset = self.data_start;
        let declared = self.header.event_count as usize;
        let index_len = self.header.index_array_length as usize;

        if index_len > 0 {
            // The header's event count, the index size and the bytes present
            // are three claims about the same number; trust none beyond the
            // others (a cut tail may cut the index array itself).
            let index_present = index_len.min(self.buffer.len()) / 4;
            let count = declared.min(index_len / 4).min(index_present);
            for i in 0..count {
                let length = w.get(i).unwrap_or(0) as usize;
                if length < 8 || offset.saturating_add(length) > data_end {
                    self.errors.push(format!(
                        "event {i} at 0x{offset:X} declares {length} bytes, record data ends at 0x{data_end:X}"
                    ));
                    break;
                }
                self.events.push(EventSpan { offset, length });
                offset += length;
            }
        } else {
            while self.events.len() < declared {
                let i = self.events.len();
                if offset.saturating_add(8) > data_end {
                    self.errors.push(format!(
                        "event {i} at 0x{offset:X} has no room for a bank header, record data ends at 0x{data_end:X}"
                    ));
                    break;
                }
                let bank_length = w.get(offset / 4).unwrap_or(0) as usize;
                let length = (bank_length + 1) * 4;
                if bank_length == 0 || offset.saturating_add(length) > data_end {
                    self.errors.push(format!(
                        "event {i} at 0x{offset:X} declares {length} bytes, record data ends at 0x{data_end:X}"
                    ));
                    break;
                }
                self.events.push(EventSpan { offset, length });
                offset += length;
            }
        }
    }

    /// Byte order of the file.
    pub fn order(&self) -> ByteOrder {
        self.order
    }

    /// True for the trailer that closes a file; it holds no events.
    pub fn is_trailer(&self) -> bool {
        self.header.is_trailer()
    }

    /// The events found; for a cut tail, the complete ones.
    pub fn events(&self) -> &[EventSpan] {
        &self.events
    }

    /// Number of events found (see [`Record::events`]).
    pub fn event_count(&self) -> usize {
        self.events.len()
    }

    /// The words of event `i`.
    pub fn event(&self, i: usize) -> Option<Words<'_>> {
        let span = self.events.get(i)?;
        let bytes = self.buffer.get(span.offset..span.offset + span.length)?;
        Some(Words::new(bytes, self.order))
    }

    /// The words of every event in order.
    pub fn iter_events(&self) -> impl Iterator<Item = Words<'_>> + '_ {
        self.events.iter().filter_map(move |span| {
            let bytes = self.buffer.get(span.offset..span.offset + span.length)?;
            Some(Words::new(bytes, self.order))
        })
    }

    /// Index array, user header and events (file bytes, or decompressed).
    pub fn buffer(&self) -> &[u8] {
        &self.buffer
    }

    /// The index array bytes (one word per event length), empty without one.
    pub fn index_array(&self) -> &[u8] {
        let end = (self.header.index_array_length as usize).min(self.buffer.len());
        &self.buffer[..end]
    }

    /// The user header bytes, unpadded.
    pub fn user_header(&self) -> &[u8] {
        let start = (self.header.index_array_length as usize).min(self.buffer.len());
        let end = (start + self.header.user_header_length as usize).min(self.buffer.len());
        &self.buffer[start..end]
    }

    /// The event bytes present (from the first event to the data end).
    pub fn data(&self) -> &[u8] {
        let start = self.data_start.min(self.buffer.len());
        let end = self.data_end.min(self.buffer.len()).max(start);
        &self.buffer[start..end]
    }

    /// Take the buffer back for reuse (see [`RecordReader::recycle`](crate::RecordReader::recycle)).
    pub fn into_buffer(self) -> Vec<u8> {
        self.buffer
    }
}

/// Decompress a record body. The compressed bytes start right after the
/// header and run `compressed_data_length * 4 - pad3` bytes; LZ4 needs the
/// output size up front, which is index array + padded user header + the
/// uncompressed event bytes (`declared_end`).
fn decompress(
    header: &RecordHeader,
    body: &[u8],
    declared_end: usize,
    errors: &mut Vec<String>,
) -> Vec<u8> {
    let compressed_bytes = header.compressed_data_length as usize * 4;
    let mut end = compressed_bytes.saturating_sub(header.pad3 as usize);
    if end > body.len() {
        errors.push(format!(
            "compressed data declares {} words, {} bytes present",
            header.compressed_data_length,
            body.len()
        ));
        end = body.len();
    }
    let compressed = &body[..end];
    let result: Result<Vec<u8>, String> = match header.compression {
        Compression::Lz4 | Compression::Lz4Best => {
            lz4_flex::block::decompress(compressed, declared_end).map_err(|e| e.to_string())
        }
        Compression::Gzip => {
            let mut out = Vec::with_capacity(declared_end);
            let gzip_magic =
                compressed.len() >= 2 && compressed[0] == 0x1f && compressed[1] == 0x8b;
            let r = if gzip_magic {
                flate2::read::MultiGzDecoder::new(compressed).read_to_end(&mut out)
            } else {
                flate2::read::ZlibDecoder::new(compressed).read_to_end(&mut out)
            };
            r.map(|_| out).map_err(|e| e.to_string())
        }
        Compression::None => unreachable!("plain records are not decompressed"),
        Compression::Unknown(code) => Err(format!("unknown compression type {code}")),
    };
    let out = match result {
        Ok(out) => out,
        Err(message) => {
            errors.push(format!(
                "decompression failed (type {}): {message}",
                header.compression.name()
            ));
            Vec::new()
        }
    };
    if out.len() < declared_end {
        errors.push(format!(
            "decompressed {} bytes, header declares {declared_end}",
            out.len()
        ));
    }
    out
}
