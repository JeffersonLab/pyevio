//! File header, record header and layout detection.

use std::fmt;

use crate::words::{ByteOrder, Words};

/// The magic word at word 7 of every EVIO header, which fixes the byte order.
pub const MAGIC: u32 = 0xc0da_0100;
/// File type id "EVIO" in word 0 of a v6 file header.
pub const FILE_TYPE_EVIO: u32 = 0x4556_494F;
/// File type id "HIPO" in word 0 of a v6 file header.
pub const FILE_TYPE_HIPO: u32 = 0x4849_504F;

/// Byte length of the short (8-word) header every layout starts with.
pub const SHORT_HEADER_BYTES: usize = 32;
/// Byte length of the full 14-word v6 header.
pub const FULL_HEADER_BYTES: usize = 56;

/// Name of a file type id read in either byte order ("EVIO", "HIPO"), else
/// `None`. HIPO-flavored writers store the ASCII bytes in order whatever
/// the file's byte order, so both orders are accepted.
pub fn file_type_name(word: u32) -> Option<&'static str> {
    match word {
        FILE_TYPE_EVIO => Some("EVIO"),
        FILE_TYPE_HIPO => Some("HIPO"),
        _ => match word.swap_bytes() {
            FILE_TYPE_EVIO => Some("EVIO"),
            FILE_TYPE_HIPO => Some("HIPO"),
            _ => None,
        },
    }
}

/// Byte order of the header whose first 32 bytes are `head`, from the magic
/// word at word 7. `None` when neither order matches or `head` is short.
pub fn detect_byte_order(head: &[u8]) -> Option<ByteOrder> {
    let m = head.get(28..32)?;
    let bytes = [m[0], m[1], m[2], m[3]];
    if u32::from_le_bytes(bytes) == MAGIC {
        Some(ByteOrder::Little)
    } else if u32::from_be_bytes(bytes) == MAGIC {
        Some(ByteOrder::Big)
    } else {
        None
    }
}

/// A byte length rounded up to whole words, as user headers are stored.
pub fn padded_to_words(bytes: u64) -> u64 {
    (bytes + 3) & !3
}

/// How a file starts; mirrors `EvioFile.header_layout` of pyevio.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileLayout {
    /// A 14-word file header followed by 14-word record headers.
    V6,
    /// A bare stream of 14-word v6 records with no file header (a writer
    /// buffer dump).
    V6Records,
    /// The JLab SRO hardware stream: no file header, 8-word block headers
    /// `{length, block number, 8, event count, 0, 4, 0, magic}` whose
    /// version word is exactly 4, no index arrays, no trailer.
    Sro8Word,
}

impl FileLayout {
    /// The pyevio name of the layout.
    pub fn name(self) -> &'static str {
        match self {
            FileLayout::V6 => "v6",
            FileLayout::V6Records => "v6-records",
            FileLayout::Sro8Word => "sro-8word",
        }
    }
}

impl fmt::Display for FileLayout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// The v6 file header (14 words), or a stand-in for the layouts without one.
#[derive(Clone, Debug)]
pub struct FileHeader {
    pub layout: FileLayout,
    pub order: ByteOrder,
    /// "EVIO", "HIPO", "EVIO records" or "SRO".
    pub file_type: &'static str,
    pub file_type_id: u32,
    pub file_number: u32,
    /// Words in the header; 0 for a stand-in.
    pub header_length: u32,
    /// Record count the header declares; 0 for a stand-in (count the records).
    pub record_count: u32,
    pub index_array_length: u32,
    /// Bits 8—31 of word 5.
    pub bit_info: u32,
    /// Content version: 6 for every layout this crate reads.
    pub version: u8,
    /// Version word of the container headers: 6, or 4 for the SRO blocks.
    pub container_version: u8,
    pub user_header_length: u32,
    pub user_register: u64,
    pub trailer_position: u64,
    pub user_integer1: u32,
    pub user_integer2: u32,
    pub has_dictionary: bool,
    pub has_first_event: bool,
    pub has_trailer: bool,
}

impl FileHeader {
    /// Parse a 14-word file header; `head` holds at least `header_length * 4`
    /// bytes (and at least 32). Words 8—13 read as 0 when absent.
    pub(crate) fn parse(head: &[u8], order: ByteOrder) -> Result<FileHeader, String> {
        let w = Words::new(head, order);
        let word = |i: usize| w.get(i).unwrap_or(0);
        let file_type_id = word(0);
        let file_type = file_type_name(file_type_id)
            .ok_or_else(|| format!("file type id 0x{file_type_id:08x}, expected EVIO or HIPO"))?;
        let bit_info_version = word(5);
        let bit_info = bit_info_version >> 8;
        let version = (bit_info_version & 0xFF) as u8;
        let u64_at = |i: usize| (word(i) as u64) | ((word(i + 1) as u64) << 32);
        Ok(FileHeader {
            layout: FileLayout::V6,
            order,
            file_type,
            file_type_id,
            file_number: word(1),
            header_length: word(2),
            record_count: word(3),
            index_array_length: word(4),
            bit_info,
            version,
            container_version: version,
            user_header_length: word(6),
            user_register: u64_at(8),
            trailer_position: u64_at(10),
            user_integer1: word(12),
            user_integer2: word(13),
            has_dictionary: bit_info & 1 != 0,
            has_first_event: bit_info & 2 != 0,
            has_trailer: bit_info & 4 != 0,
        })
    }

    /// The stand-in header of a layout without a file header.
    pub(crate) fn stand_in(layout: FileLayout, order: ByteOrder) -> FileHeader {
        let (file_type, container_version) = match layout {
            FileLayout::Sro8Word => ("SRO", 4),
            _ => ("EVIO records", 6),
        };
        FileHeader {
            layout,
            order,
            file_type,
            file_type_id: 0,
            file_number: 0,
            header_length: 0,
            record_count: 0,
            index_array_length: 0,
            bit_info: 0,
            version: 6,
            container_version,
            user_header_length: 0,
            user_register: 0,
            trailer_position: 0,
            user_integer1: 0,
            user_integer2: 0,
            has_dictionary: false,
            has_first_event: false,
            has_trailer: false,
        }
    }
}

/// Compression type of a record, bits 28—31 of header word 9.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Compression {
    None,
    Lz4,
    Lz4Best,
    Gzip,
    Unknown(u8),
}

impl Compression {
    fn from_code(code: u8) -> Compression {
        match code {
            0 => Compression::None,
            1 => Compression::Lz4,
            2 => Compression::Lz4Best,
            3 => Compression::Gzip,
            other => Compression::Unknown(other),
        }
    }

    /// Short name: "none", "lz4", "lz4_best", "gzip" or "unknown(N)".
    pub fn name(self) -> String {
        match self {
            Compression::None => "none".into(),
            Compression::Lz4 => "lz4".into(),
            Compression::Lz4Best => "lz4_best".into(),
            Compression::Gzip => "gzip".into(),
            Compression::Unknown(c) => format!("unknown({c})"),
        }
    }
}

/// Header species, bits 28—31 of record header word 5.
pub const HEADER_TYPE_RECORD: u8 = 0;
/// Header species of a v6 file trailer.
pub const HEADER_TYPE_TRAILER: u8 = 3;

/// Word 5 of an SRO hardware block header: version 4 and no bit-info bits.
pub const SRO_BLOCK_WORD: u32 = 0x0000_0004;

/// A record header: 14 words, or the 8-word SRO block header whose words
/// 8—13 read as 0.
///
/// ```text
/// 0 record length (words)      7 magic 0xc0da0100
/// 1 record number              8 uncompressed data length (bytes, events only)
/// 2 header length (words)      9 compression type (bits 28—31) | compressed length (words)
/// 3 event count               10—11 user register 1
/// 4 index array length (bytes) 12—13 user register 2
/// 5 species (28—31) | bit-info (8—27) | version (0—7)
/// 6 user header length (bytes)
/// ```
#[derive(Clone, Debug)]
pub struct RecordHeader {
    pub record_length: u32,
    pub record_number: u32,
    pub header_length: u32,
    pub event_count: u32,
    pub index_array_length: u32,
    pub version: u8,
    /// Bits 8—27 of word 5.
    pub bit_info: u32,
    /// Bits 28—31 of word 5: 0 record, 3 trailer.
    pub header_type: u8,
    pub pad1: u8,
    pub pad2: u8,
    pub pad3: u8,
    pub user_header_length: u32,
    pub uncompressed_data_length: u32,
    pub compression: Compression,
    /// Padded length of the compressed data in words.
    pub compressed_data_length: u32,
    pub user_register1: u64,
    pub user_register2: u64,
    pub has_dictionary: bool,
    pub is_last_record: bool,
    pub has_first_event: bool,
    /// CODA event type code, bits 10—13 of word 5.
    pub event_type: u8,
}

impl RecordHeader {
    /// Parse the header from `head`, which holds `header_length * 4` bytes
    /// (32 for an 8-word header, 56 for a 14-word one). The caller has
    /// already run [`record_header_problem`] on the first 32 bytes.
    pub(crate) fn parse(head: &[u8], order: ByteOrder) -> RecordHeader {
        let w = Words::new(head, order);
        let word = |i: usize| w.get(i).unwrap_or(0);
        let header_length = word(2);
        let bit_info_version = word(5);
        let bit_info = (bit_info_version >> 8) & 0xF_FFFF;
        let full = header_length >= 14 && head.len() >= FULL_HEADER_BYTES;
        let compression_word = if full { word(9) } else { 0 };
        let u64_at = |i: usize| (word(i) as u64) | ((word(i + 1) as u64) << 32);
        RecordHeader {
            record_length: word(0),
            record_number: word(1),
            header_length,
            event_count: word(3),
            index_array_length: word(4),
            version: (bit_info_version & 0xFF) as u8,
            bit_info,
            header_type: ((bit_info_version >> 28) & 0xF) as u8,
            pad1: ((bit_info_version >> 20) & 3) as u8,
            pad2: ((bit_info_version >> 22) & 3) as u8,
            pad3: ((bit_info_version >> 24) & 3) as u8,
            user_header_length: word(6),
            uncompressed_data_length: if full { word(8) } else { 0 },
            compression: Compression::from_code(((compression_word >> 28) & 0xF) as u8),
            compressed_data_length: compression_word & 0x0FFF_FFFF,
            user_register1: if full { u64_at(10) } else { 0 },
            user_register2: if full { u64_at(12) } else { 0 },
            has_dictionary: bit_info & 1 != 0,
            is_last_record: bit_info & 2 != 0,
            has_first_event: bit_info & (1 << 6) != 0,
            event_type: ((bit_info >> 2) & 0xF) as u8,
        }
    }

    /// True for the trailer record that closes a file (species 3). Its index
    /// array lists records, never events.
    pub fn is_trailer(&self) -> bool {
        self.header_type == HEADER_TYPE_TRAILER
    }

    /// True for the SRO hardware block header shape: 8 words, version 4, no
    /// bit-info bits.
    pub fn is_sro_block(&self) -> bool {
        self.header_length == 8 && self.version == 4 && self.bit_info == 0 && self.header_type == 0
    }

    /// Byte length of the header itself.
    pub fn header_bytes(&self) -> u64 {
        self.header_length as u64 * 4
    }

    /// Byte length the header declares for the whole record, header included.
    pub fn record_bytes(&self) -> u64 {
        self.record_length as u64 * 4
    }

    /// The CODA event type name of bits 10—13.
    pub fn event_type_name(&self) -> &'static str {
        match self.event_type {
            0 => "ROC Raw",
            1 => "Physics",
            2 => "Partial Physics",
            3 => "Disentangled Physics",
            4 => "User",
            5 => "Control",
            6 => "Mixed",
            8 => "ROC Raw Streaming",
            9 => "Physics Streaming",
            15 => "Other",
            _ => "Unknown",
        }
    }
}

/// Why no record header can start with the 32 bytes `head`, or `None` when
/// one can. Mirrors `EvioFile.record_header_problem` of pyevio: a record
/// header has the magic at word 7, a header length of 8 or 14, a record
/// length of at least the header, and no file type id in word 0 (that is a
/// second file header, as in two files concatenated).
pub fn record_header_problem(head: &[u8], order: ByteOrder) -> Option<String> {
    if head.len() < SHORT_HEADER_BYTES {
        return Some(format!(
            "{} bytes left, a record header needs {SHORT_HEADER_BYTES}",
            head.len()
        ));
    }
    let w = Words::new(head, order);
    let word = |i: usize| w.get(i).unwrap_or(0);
    if let Some(name) = file_type_name(word(0)) {
        return Some(format!(
            "file header ({name}) where a record header should start"
        ));
    }
    let magic = word(7);
    if magic != MAGIC {
        let hint = if magic.swap_bytes() == MAGIC {
            " (byte-swapped record header)"
        } else {
            ""
        };
        return Some(format!("no magic number at word 7 (0x{magic:08X}){hint}"));
    }
    let header_length = word(2);
    if header_length != 8 && header_length != 14 {
        return Some(format!("header length {header_length}, expected 8 or 14"));
    }
    let length_words = word(0);
    if length_words < header_length {
        return Some(format!(
            "record length {length_words} words is shorter than its {header_length}-word header"
        ));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn le_words(words: &[u32]) -> Vec<u8> {
        words.iter().flat_map(|w| w.to_le_bytes()).collect()
    }

    #[test]
    fn file_type_both_orders() {
        assert_eq!(file_type_name(FILE_TYPE_EVIO), Some("EVIO"));
        assert_eq!(file_type_name(FILE_TYPE_HIPO.swap_bytes()), Some("HIPO"));
        assert_eq!(file_type_name(0x1234), None);
    }

    #[test]
    fn detects_order_and_problems() {
        let sro = le_words(&[100, 1, 8, 11, 0, 4, 0, MAGIC]);
        assert_eq!(detect_byte_order(&sro), Some(ByteOrder::Little));
        assert_eq!(record_header_problem(&sro, ByteOrder::Little), None);
        let h = RecordHeader::parse(&sro, ByteOrder::Little);
        assert!(h.is_sro_block());
        assert_eq!(h.record_bytes(), 400);
        assert_eq!(h.compression, Compression::None);

        let be: Vec<u8> = [100u32, 1, 8, 11, 0, 4, 0, MAGIC]
            .iter()
            .flat_map(|w| w.to_be_bytes())
            .collect();
        assert_eq!(detect_byte_order(&be), Some(ByteOrder::Big));
        assert!(record_header_problem(&be, ByteOrder::Little)
            .unwrap()
            .contains("byte-swapped"));

        let short = le_words(&[5, 1, 8, 11, 0, 4, 0, MAGIC]);
        assert!(record_header_problem(&short, ByteOrder::Little)
            .unwrap()
            .contains("shorter than"));
        let bad_len = le_words(&[100, 1, 9, 11, 0, 4, 0, MAGIC]);
        assert!(record_header_problem(&bad_len, ByteOrder::Little)
            .unwrap()
            .contains("expected 8 or 14"));
        let file = le_words(&[FILE_TYPE_EVIO, 1, 14, 11, 0, 6, 0, MAGIC]);
        assert!(record_header_problem(&file, ByteOrder::Little)
            .unwrap()
            .contains("file header"));
        assert!(record_header_problem(&sro[..20], ByteOrder::Little)
            .unwrap()
            .contains("bytes left"));
        assert_eq!(detect_byte_order(&sro[..20]), None);
    }

    #[test]
    fn parses_full_header_and_trailer() {
        let trailer = le_words(&[14, 3, 14, 0, 0, 0x3000_0206, 0, MAGIC, 0, 0, 0, 0, 0, 0]);
        let h = RecordHeader::parse(&trailer, ByteOrder::Little);
        assert!(h.is_trailer());
        assert!(h.is_last_record);
        assert_eq!(h.version, 6);
        let lz4 = le_words(&[
            40,
            1,
            14,
            2,
            8,
            0x0100_0006,
            0,
            MAGIC,
            100,
            (1 << 28) | 20,
            7,
            0,
            9,
            0,
        ]);
        let h = RecordHeader::parse(&lz4, ByteOrder::Little);
        assert_eq!(h.compression, Compression::Lz4);
        assert_eq!(h.compressed_data_length, 20);
        assert_eq!(h.pad3, 1);
        assert_eq!(h.uncompressed_data_length, 100);
        assert_eq!(h.user_register1, 7);
        assert_eq!(h.user_register2, 9);
        assert!(!h.is_sro_block());
        assert_eq!(padded_to_words(5), 8);
        assert_eq!(padded_to_words(8), 8);
    }
}
