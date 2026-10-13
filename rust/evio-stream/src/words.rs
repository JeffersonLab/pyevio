//! A bounds-checked view of 32-bit words over bytes kept in file byte order.

use std::fmt;

/// Byte order of a file, decided by the magic word at word 7 of its first
/// header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ByteOrder {
    Little,
    Big,
}

impl ByteOrder {
    /// Decode one word.
    #[inline(always)]
    pub fn u32(self, bytes: [u8; 4]) -> u32 {
        match self {
            ByteOrder::Little => u32::from_le_bytes(bytes),
            ByteOrder::Big => u32::from_be_bytes(bytes),
        }
    }

    /// Decode one half-word.
    #[inline(always)]
    pub fn u16(self, bytes: [u8; 2]) -> u16 {
        match self {
            ByteOrder::Little => u16::from_le_bytes(bytes),
            ByteOrder::Big => u16::from_be_bytes(bytes),
        }
    }

    /// Decode a 64-bit value stored as two words, low word first.
    #[inline]
    pub fn u64(self, bytes: [u8; 8]) -> u64 {
        match self {
            ByteOrder::Little => u64::from_le_bytes(bytes),
            ByteOrder::Big => u64::from_be_bytes(bytes),
        }
    }
}

impl fmt::Display for ByteOrder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ByteOrder::Little => "little",
            ByteOrder::Big => "big",
        })
    }
}

/// Words of a byte slice in the file's byte order.
///
/// The bytes are never swapped in place: every read decodes one word with
/// the slice's [`ByteOrder`], which is a single instruction on the common
/// path (a little-endian file on a little-endian host). All accessors are
/// bounds checked and return `Option`; nothing here panics on short input.
#[derive(Clone, Copy)]
pub struct Words<'a> {
    bytes: &'a [u8],
    order: ByteOrder,
}

impl<'a> Words<'a> {
    /// View `bytes` as words. A trailing partial word is ignored.
    #[inline]
    pub fn new(bytes: &'a [u8], order: ByteOrder) -> Self {
        Words { bytes, order }
    }

    /// The bytes behind the view.
    #[inline]
    pub fn bytes(&self) -> &'a [u8] {
        self.bytes
    }

    /// The byte order used to decode words.
    #[inline]
    pub fn order(&self) -> ByteOrder {
        self.order
    }

    /// Number of whole words.
    #[inline]
    pub fn len(&self) -> usize {
        self.bytes.len() / 4
    }

    /// True when the view holds no whole word.
    #[inline]
    pub fn is_empty(&self) -> bool {
        self.bytes.len() < 4
    }

    /// Word `i`, or `None` past the end.
    #[inline(always)]
    pub fn get(&self, i: usize) -> Option<u32> {
        let start = i.checked_mul(4)?;
        let chunk = self.bytes.get(start..start.checked_add(4)?)?;
        Some(self.order.u32([chunk[0], chunk[1], chunk[2], chunk[3]]))
    }

    /// Half-word `i` in memory order (half-word 0 is the first two bytes),
    /// or `None` past the end.
    #[inline]
    pub fn get_u16(&self, i: usize) -> Option<u16> {
        let start = i.checked_mul(2)?;
        let chunk = self.bytes.get(start..start.checked_add(2)?)?;
        Some(self.order.u16([chunk[0], chunk[1]]))
    }

    /// The words `start..end`, or `None` when the range is not inside the view.
    #[inline]
    pub fn slice(&self, start: usize, end: usize) -> Option<Words<'a>> {
        if start > end {
            return None;
        }
        let bytes = self.bytes.get(start.checked_mul(4)?..end.checked_mul(4)?)?;
        Some(Words {
            bytes,
            order: self.order,
        })
    }

    /// Every whole word in order.
    #[inline]
    pub fn iter(&self) -> impl Iterator<Item = u32> + 'a {
        let order = self.order;
        self.bytes
            .chunks_exact(4)
            .map(move |c| order.u32([c[0], c[1], c[2], c[3]]))
    }

    /// The words copied into a `Vec` in host order.
    pub fn to_vec(&self) -> Vec<u32> {
        self.iter().collect()
    }
}

impl fmt::Debug for Words<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Words({} words, {} endian)", self.len(), self.order)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_both_orders_and_bounds() {
        let bytes = [1u8, 0, 0, 0, 0, 0, 0, 2, 9];
        let le = Words::new(&bytes, ByteOrder::Little);
        assert_eq!(le.len(), 2);
        assert_eq!(le.get(0), Some(1));
        assert_eq!(le.get(1), Some(0x0200_0000));
        assert_eq!(le.get(2), None);
        assert_eq!(le.get(usize::MAX), None);
        let be = Words::new(&bytes, ByteOrder::Big);
        assert_eq!(be.get(0), Some(0x0100_0000));
        assert_eq!(be.get(1), Some(2));
        assert_eq!(le.get_u16(1), Some(0));
        assert_eq!(be.get_u16(3), Some(2));
        assert_eq!(le.get_u16(4), None);
        assert!(le.slice(1, 3).is_none());
        assert_eq!(le.slice(1, 2).unwrap().get(0), Some(0x0200_0000));
        assert!(le.slice(2, 1).is_none());
        assert_eq!(le.iter().count(), 2);
    }
}
