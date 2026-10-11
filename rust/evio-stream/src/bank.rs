//! Bank, segment and tagsegment views with every length checked against the
//! container.

use std::fmt;

use crate::words::Words;

/// The three EVIO structure kinds. A bank has a two-word header, a segment
/// and a tagsegment a one-word header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StructureKind {
    Bank,
    Segment,
    TagSegment,
}

impl StructureKind {
    /// Header words of the kind.
    pub fn header_words(self) -> usize {
        match self {
            StructureKind::Bank => 2,
            _ => 1,
        }
    }

    /// "Bank", "Segment" or "TagSegment".
    pub fn name(self) -> &'static str {
        match self {
            StructureKind::Bank => "Bank",
            StructureKind::Segment => "Segment",
            StructureKind::TagSegment => "TagSegment",
        }
    }
}

/// EVIO content types.
pub mod data_type {
    pub const UNKNOWN32: u8 = 0x0;
    pub const UINT32: u8 = 0x1;
    pub const FLOAT32: u8 = 0x2;
    pub const STRING: u8 = 0x3;
    pub const INT16: u8 = 0x4;
    pub const UINT16: u8 = 0x5;
    pub const INT8: u8 = 0x6;
    pub const UINT8: u8 = 0x7;
    pub const FLOAT64: u8 = 0x8;
    pub const INT64: u8 = 0x9;
    pub const UINT64: u8 = 0xa;
    pub const INT32: u8 = 0xb;
    pub const TAGSEGMENT: u8 = 0xc;
    pub const SEGMENT: u8 = 0xd;
    pub const BANK: u8 = 0xe;
    pub const COMPOSITE: u8 = 0xf;
    pub const BANK2: u8 = 0x10;
    pub const SEGMENT2: u8 = 0x20;

    /// Name of a content type code.
    pub fn name(code: u8) -> &'static str {
        match code {
            UNKNOWN32 => "unknown32",
            UINT32 => "uint32",
            FLOAT32 => "float32",
            STRING => "string",
            INT16 => "int16",
            UINT16 => "uint16",
            INT8 => "int8",
            UINT8 => "uint8",
            FLOAT64 => "float64",
            INT64 => "int64",
            UINT64 => "uint64",
            INT32 => "int32",
            TAGSEGMENT => "tagsegments",
            SEGMENT | SEGMENT2 => "segments",
            BANK | BANK2 => "banks",
            COMPOSITE => "composite",
            _ => "unknown",
        }
    }

    /// The kind of the children of a container type, `None` for a leaf type.
    pub fn child_kind(code: u8) -> Option<super::StructureKind> {
        match code {
            BANK | BANK2 => Some(super::StructureKind::Bank),
            SEGMENT | SEGMENT2 => Some(super::StructureKind::Segment),
            TAGSEGMENT => Some(super::StructureKind::TagSegment),
            _ => None,
        }
    }
}

/// A length that does not fit its container. The walk that met it stops.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WalkError {
    /// Word offset of the offending structure inside its container.
    pub word: usize,
    pub message: String,
}

impl fmt::Display for WalkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "word {}: {}", self.word, self.message)
    }
}

impl std::error::Error for WalkError {}

/// A view of one structure: its header words and the words that follow.
///
/// Every length word counts the words after it, so a structure with length
/// N occupies N + 1 words. Children are typed by the parent's content type:
/// a bank of banks holds banks, a bank of segments holds segments (one-word
/// headers), a bank of tagsegments holds tagsegments.
#[derive(Clone, Copy)]
pub struct Bank<'a> {
    words: Words<'a>,
    kind: StructureKind,
    /// Words that follow the length word (the length field).
    pub length: u32,
    pub tag: u16,
    pub data_type: u8,
    /// Bank num byte; 0 for segments and tagsegments.
    pub num: u8,
    /// Padding bytes for 8- and 16-bit types; 0 for tagsegments.
    pub pad: u8,
}

impl<'a> Bank<'a> {
    /// Parse the structure of `kind` that starts at word 0 of `words`; the
    /// view bounds it. The error names a header that has no room or a
    /// length that runs past the view.
    pub fn parse(words: Words<'a>, kind: StructureKind) -> Result<Bank<'a>, WalkError> {
        let header_words = kind.header_words();
        if words.len() < header_words {
            return Err(WalkError {
                word: 0,
                message: format!(
                    "{} header needs {header_words} words, {} present",
                    kind.name(),
                    words.len()
                ),
            });
        }
        let (length, tag, data_type, num, pad) = match kind {
            StructureKind::Bank => {
                let length = words.get(0).unwrap_or(0);
                let info = words.get(1).unwrap_or(0);
                (
                    length,
                    (info >> 16) as u16,
                    ((info >> 8) & 0x3F) as u8,
                    (info & 0xFF) as u8,
                    ((info >> 14) & 3) as u8,
                )
            }
            StructureKind::Segment => {
                let h = words.get(0).unwrap_or(0);
                (
                    h & 0xFFFF,
                    (h >> 24) as u16,
                    ((h >> 16) & 0x3F) as u8,
                    0,
                    ((h >> 22) & 3) as u8,
                )
            }
            StructureKind::TagSegment => {
                let h = words.get(0).unwrap_or(0);
                (
                    h & 0xFFFF,
                    ((h >> 20) & 0xFFF) as u16,
                    ((h >> 16) & 0xF) as u8,
                    0,
                    0,
                )
            }
        };
        let total = length as usize + 1;
        if total < header_words {
            return Err(WalkError {
                word: 0,
                message: format!(
                    "{} declares length {length}, no room for its {header_words}-word header",
                    kind.name()
                ),
            });
        }
        if total > words.len() {
            return Err(WalkError {
                word: 0,
                message: format!(
                    "{} declares {length} words, ends at word {total} past its limit {}",
                    kind.name(),
                    words.len()
                ),
            });
        }
        let words = words.slice(0, total).ok_or_else(|| WalkError {
            word: 0,
            message: "structure range out of view".into(),
        })?;
        Ok(Bank {
            words,
            kind,
            length,
            tag,
            data_type,
            num,
            pad,
        })
    }

    /// Parse an event: the top-level bank of `words`.
    pub fn event(words: Words<'a>) -> Result<Bank<'a>, WalkError> {
        Bank::parse(words, StructureKind::Bank)
    }

    /// Bank, segment or tagsegment.
    pub fn kind(&self) -> StructureKind {
        self.kind
    }

    /// Header words plus the words that follow (length + 1).
    pub fn total_words(&self) -> usize {
        self.words.len()
    }

    /// The whole structure, header included.
    pub fn words(&self) -> Words<'a> {
        self.words
    }

    /// The words after the header: child structures or leaf data.
    pub fn data(&self) -> Words<'a> {
        self.words
            .slice(self.kind.header_words(), self.words.len())
            .unwrap_or(Words::new(&[], self.words.order()))
    }

    /// Leaf data bytes without the trailing `pad` bytes.
    pub fn data_bytes(&self) -> &'a [u8] {
        let bytes = self.data().bytes();
        let end = bytes.len().saturating_sub(self.pad as usize);
        &bytes[..end]
    }

    /// True for a bank of banks, segments or tagsegments.
    pub fn is_container(&self) -> bool {
        data_type::child_kind(self.data_type).is_some()
    }

    /// The kind of the children, `None` for a leaf.
    pub fn child_kind(&self) -> Option<StructureKind> {
        data_type::child_kind(self.data_type)
    }

    /// Name of the content type.
    pub fn type_name(&self) -> &'static str {
        data_type::name(self.data_type)
    }

    /// The children of a container, in order; empty for a leaf. A child
    /// whose length runs past this structure ends the iteration with one
    /// `Err` item.
    pub fn children(&self) -> Children<'a> {
        Children {
            words: self.data(),
            pos: 0,
            kind: self.child_kind(),
            failed: false,
        }
    }
}

impl fmt::Debug for Bank<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}(tag=0x{:04X}, type=0x{:02X}, num={}, length={})",
            self.kind.name(),
            self.tag,
            self.data_type,
            self.num,
            self.length
        )
    }
}

/// Iterator over the children of a container (see [`Bank::children`]).
pub struct Children<'a> {
    words: Words<'a>,
    pos: usize,
    kind: Option<StructureKind>,
    failed: bool,
}

impl<'a> Iterator for Children<'a> {
    type Item = Result<Bank<'a>, WalkError>;

    fn next(&mut self) -> Option<Self::Item> {
        let kind = self.kind?;
        if self.failed || self.pos >= self.words.len() {
            return None;
        }
        let rest = self.words.slice(self.pos, self.words.len())?;
        match Bank::parse(rest, kind) {
            Ok(child) => {
                self.pos += child.total_words();
                Some(Ok(child))
            }
            Err(mut e) => {
                self.failed = true;
                e.word += self.pos;
                Some(Err(e))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::words::ByteOrder;

    fn le(words: &[u32]) -> Vec<u8> {
        words.iter().flat_map(|w| w.to_le_bytes()).collect()
    }

    #[test]
    fn walks_banks_segments_and_tagsegments() {
        // bank of banks: [ bank of segments: [ segment len 2 ], leaf uint32 len 1 ]
        let bytes = le(&[
            9,
            0xFF60_1000,
            4,
            0xFF31_2000,
            0x3201_0002,
            11,
            22,
            2,
            0x0001_0100,
            99,
        ]);
        let event = Bank::event(Words::new(&bytes, ByteOrder::Little)).unwrap();
        assert_eq!(event.tag, 0xFF60);
        assert_eq!(event.data_type, 0x10);
        assert!(event.is_container());
        let children: Vec<_> = event.children().map(Result::unwrap).collect();
        assert_eq!(children.len(), 2);
        assert_eq!(children[0].tag, 0xFF31);
        assert_eq!(children[0].child_kind(), Some(StructureKind::Segment));
        let segs: Vec<_> = children[0].children().map(Result::unwrap).collect();
        assert_eq!(segs.len(), 1);
        assert_eq!(segs[0].tag, 0x32);
        assert_eq!(segs[0].data().to_vec(), vec![11, 22]);
        assert_eq!(children[1].tag, 1);
        assert_eq!(children[1].num, 0);
        assert_eq!(children[1].data().to_vec(), vec![99]);
        assert!(children[1].children().next().is_none());
    }

    #[test]
    fn bad_lengths_are_errors_not_panics() {
        // child declares 10 words inside a 3-word parent
        let bytes = le(&[3, 0x0001_1000, 10, 0x0002_0100]);
        let event = Bank::event(Words::new(&bytes, ByteOrder::Little)).unwrap();
        let items: Vec<_> = event.children().collect();
        assert_eq!(items.len(), 1);
        assert!(items[0].is_err());
        assert_eq!(items[0].as_ref().unwrap_err().word, 0);

        // top-level bank longer than the event
        let bytes = le(&[u32::MAX, 0x0001_1000]);
        assert!(Bank::event(Words::new(&bytes, ByteOrder::Little)).is_err());
        // bank length 0: no room for the second header word
        let bytes = le(&[2, 0x0001_1000, 0, 0x0002_0100, 5]);
        let event = Bank::event(Words::new(&bytes, ByteOrder::Little)).unwrap();
        assert!(event.children().next().unwrap().is_err());
        // too short for a header
        assert!(Bank::event(Words::new(&bytes[..4], ByteOrder::Little)).is_err());
        // a tagsegment of length 0 is fine
        let bytes = le(&[2, 0x0001_0C00, 0x1230_0000]);
        let event = Bank::event(Words::new(&bytes, ByteOrder::Little)).unwrap();
        let ts = event.children().next().unwrap().unwrap();
        assert_eq!(ts.kind(), StructureKind::TagSegment);
        assert_eq!(ts.tag, 0x123);
        assert_eq!(ts.length, 0);
    }
}
