//! The bank tree of one event flattened into owned rows.
//!
//! An event is small, so the whole tree is walked at once with
//! [`Bank::event`] and [`Bank::children`]; every level is a row with its
//! depth. A length that does not fit its container ends that container's
//! walk with an [`TreeItem::Error`] row at the offending word.

use evio_stream::{Bank, StructureKind, Words};

/// One row of the flattened tree.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TreeRow {
    /// Nesting depth, 0 for the event bank.
    pub depth: usize,
    pub item: TreeItem,
}

/// A structure, or the walk error that stopped a container.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TreeItem {
    Node {
        kind: StructureKind,
        tag: u16,
        data_type: u8,
        num: u8,
        /// The length field: words after the length word.
        length: u32,
        /// Word offset of the structure inside the event.
        start: usize,
        /// Header words plus the words that follow (length + 1).
        total_words: usize,
        is_container: bool,
    },
    Error {
        /// Word offset inside the event where the bad structure starts.
        start: usize,
        message: String,
    },
}

impl TreeRow {
    /// Word offset of the row's structure inside the event.
    pub fn start(&self) -> usize {
        match &self.item {
            TreeItem::Node { start, .. } | TreeItem::Error { start, .. } => *start,
        }
    }

    /// Words the row covers: the whole structure, or nothing known for an
    /// error (`None`).
    pub fn total_words(&self) -> Option<usize> {
        match &self.item {
            TreeItem::Node { total_words, .. } => Some(*total_words),
            TreeItem::Error { .. } => None,
        }
    }

    /// True for an error row.
    pub fn is_error(&self) -> bool {
        matches!(self.item, TreeItem::Error { .. })
    }
}

/// Flatten the bank tree of `event` (the words of one event).
pub fn flatten(event: Words<'_>) -> Vec<TreeRow> {
    let mut rows = Vec::new();
    match Bank::event(event) {
        Ok(bank) => walk(&bank, 0, 0, &mut rows),
        Err(e) => rows.push(TreeRow {
            depth: 0,
            item: TreeItem::Error {
                start: e.word,
                message: e.message,
            },
        }),
    }
    rows
}

fn walk(bank: &Bank<'_>, depth: usize, start: usize, rows: &mut Vec<TreeRow>) {
    rows.push(TreeRow {
        depth,
        item: TreeItem::Node {
            kind: bank.kind(),
            tag: bank.tag,
            data_type: bank.data_type,
            num: bank.num,
            length: bank.length,
            start,
            total_words: bank.total_words(),
            is_container: bank.is_container(),
        },
    });
    if !bank.is_container() {
        return;
    }
    let data_start = start + bank.kind().header_words();
    let mut pos = 0;
    for child in bank.children() {
        match child {
            Ok(child) => {
                walk(&child, depth + 1, data_start + pos, rows);
                pos += child.total_words();
            }
            // The iterator reports the word relative to the parent's data.
            Err(e) => rows.push(TreeRow {
                depth: depth + 1,
                item: TreeItem::Error {
                    start: data_start + e.word,
                    message: e.message,
                },
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use evio_stream::ByteOrder;

    fn le(words: &[u32]) -> Vec<u8> {
        words.iter().flat_map(|w| w.to_le_bytes()).collect()
    }

    #[test]
    fn flattens_every_level_with_offsets() {
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
        let rows = flatten(Words::new(&bytes, ByteOrder::Little));
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[0].depth, 0);
        assert_eq!(rows[0].start(), 0);
        assert_eq!(rows[0].total_words(), Some(10));
        assert_eq!(rows[1].depth, 1);
        assert_eq!(rows[1].start(), 2);
        assert_eq!(rows[2].depth, 2);
        assert_eq!(rows[2].start(), 4);
        assert_eq!(rows[2].total_words(), Some(3));
        match &rows[2].item {
            TreeItem::Node { kind, tag, .. } => {
                assert_eq!(*kind, StructureKind::Segment);
                assert_eq!(*tag, 0x32);
            }
            other => panic!("segment expected, got {other:?}"),
        }
        assert_eq!(rows[3].depth, 1);
        assert_eq!(rows[3].start(), 7);
        assert!(rows.iter().all(|r| !r.is_error()));
    }

    #[test]
    fn bad_length_becomes_an_error_row() {
        // first child fine (2 words), second declares 10 words inside a 5-word parent
        let bytes = le(&[5, 0x0001_1000, 1, 0x0002_0100, 10, 0x0003_0100]);
        let rows = flatten(Words::new(&bytes, ByteOrder::Little));
        assert_eq!(rows.len(), 3);
        assert!(rows[2].is_error());
        assert_eq!(rows[2].depth, 1);
        // the second child starts at word 2 (header) + 2 (first child) = 4
        assert_eq!(rows[2].start(), 4);
        // too short for a bank header
        let rows = flatten(Words::new(&bytes[..4], ByteOrder::Little));
        assert_eq!(rows.len(), 1);
        assert!(rows[0].is_error());
    }
}
