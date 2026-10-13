import mmap
import struct
from typing import List, Tuple, Optional, Dict, Any, Union
import numpy as np
from datetime import datetime

from pyevio.utils import make_hex_dump


class BankHeader:
    """Base class for bank headers."""
    def __init__(self):
        self.length = None
        self.tag = None
        self.pad = None
        self.data_type = None
        self.num = None
        self.offset = None
        self.type_name = "Unknown"


class Bank(BankHeader):
    """
    Represents a Bank with a full 2-word header.

    Structure:
    - Word 1: length
    - Word 2: tag (16 bits) | pad (2 bits) | type (6 bits) | num (8 bits)

    Banks can contain data or other banks, depending on the data_type.

    Every length word counts the words that follow it, so a structure with
    length N occupies N + 1 words. Children are typed by the parent's data
    type: a bank of banks (0xe/0x10) holds ``Bank`` children, a bank of
    segments (0xd/0x20) holds ``Segment`` children (1-word header) and a bank
    of tagsegments (0xc) holds ``TagSegment`` children (1-word header).
    Reading a segment with the 2-word bank header takes its first payload
    word as a bank header.
    """

    # Data type constants
    TYPE_UNKNOWN32 = 0x0
    TYPE_UINT32 = 0x1
    TYPE_FLOAT32 = 0x2
    TYPE_STRING = 0x3
    TYPE_INT16 = 0x4
    TYPE_UINT16 = 0x5
    TYPE_INT8 = 0x6
    TYPE_UINT8 = 0x7
    TYPE_FLOAT64 = 0x8
    TYPE_INT64 = 0x9
    TYPE_UINT64 = 0xa
    TYPE_INT32 = 0xb
    TYPE_TAGSEGMENT = 0xc
    TYPE_SEGMENT = 0xd
    TYPE_BANK = 0xe
    TYPE_COMPOSITE = 0xf
    TYPE_BANK2 = 0x10
    TYPE_SEGMENT2 = 0x20

    # Mapping from data types to numpy dtypes
    _NUMPY_DTYPES = {
        TYPE_UNKNOWN32: np.uint32,
        TYPE_UINT32: np.uint32,
        TYPE_FLOAT32: np.float32,
        TYPE_INT16: np.int16,
        TYPE_UINT16: np.uint16,
        TYPE_INT8: np.int8,
        TYPE_UINT8: np.uint8,
        TYPE_FLOAT64: np.float64,
        TYPE_INT64: np.int64,
        TYPE_UINT64: np.uint64,
        TYPE_INT32: np.int32
    }

    # Header words of this structure kind; subclasses with a 1-word header override it.
    HEADER_WORDS = 2
    STRUCTURE_NAME = "Bank"

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<', limit: Optional[int] = None):
        """
        Initialize a Bank object.

        Args:
            mm: Memory-mapped file (or bytes buffer) containing the bank
            offset: Byte offset in the buffer where the bank starts
            endian: Endianness ('<' for little endian, '>' for big endian)
            limit: Byte offset this bank must not reach past (the end of its
                parent, event or buffer). Default: the end of the buffer.
                A declared length past the limit is recorded in ``errors`` and
                the bank is clamped to the limit, so no read runs past it.
        """
        super().__init__()
        self.mm = mm
        self.offset = offset
        self.endian = endian

        # Why a child walk stopped early or this bank was clamped (short
        # strings with offsets); empty when every length fit.
        self.errors: List[str] = []

        self.header_size = self.HEADER_WORDS * 4
        if offset < 0 or offset + self.header_size > len(mm):
            raise ValueError(f"{self.STRUCTURE_NAME} header at 0x{offset:X} needs {self.header_size} bytes, "
                             f"buffer ends at 0x{len(mm):X}")
        self._parse_header()

        # The length word counts the words that follow it, so a structure with
        # length N occupies N + 1 words. Counting the length word itself drops
        # the last payload word of every leaf.
        self.data_offset = self.offset + self.header_size
        self.size = (self.length + 1) * 4
        self.end_offset = self.offset + self.size

        if limit is None:
            limit = len(mm)
        if self.end_offset > limit:
            self.errors.append(f"{self.STRUCTURE_NAME} at 0x{self.offset:X} declares {self.length} words, "
                               f"ends at 0x{self.end_offset:X} past its limit 0x{limit:X}")
            self.end_offset = limit
            self.size = max(0, limit - self.offset)

        self.data_length = max(0, self.end_offset - self.data_offset)

        # Child banks (will be parsed on demand)
        self._children = None

    def _parse_header(self):
        """Parse the 2-word bank header from the buffer."""
        self.length = struct.unpack(self.endian + 'I', self.mm[self.offset:self.offset+4])[0]
        bank_info = struct.unpack(self.endian + 'I', self.mm[self.offset+4:self.offset+8])[0]

        # Unpack bank info
        self.tag = (bank_info >> 16) & 0xFFFF
        self.pad = (bank_info >> 14) & 0x3
        self.data_type = (bank_info >> 8) & 0x3F
        self.num = bank_info & 0xFF

        # Determine special bank types by tag
        if (self.tag & 0xFF00) == 0xFF00:
            tag_type = self.tag & 0x00FF
            if (tag_type & 0x10) == 0x10:
                self.type_name = "RocRawDataRecord"
            elif tag_type == 0x30:
                self.type_name = "RocTimeSliceBank"
            elif tag_type == 0x31:
                self.type_name = "PhysicsEvent"

    @classmethod
    def read_length(cls, mm, offset: int, endian: str) -> int:
        """The length field (words that follow the length word) of the structure at offset."""
        return struct.unpack(endian + 'I', mm[offset:offset + 4])[0]

    @classmethod
    def from_buffer(cls, buffer: mmap.mmap, offset: int, endian: str = '<', limit: Optional[int] = None) -> 'Bank':
        """
        Create a Bank object from a memory-mapped buffer.

        Args:
            buffer: Memory-mapped buffer containing the bank
            offset: Byte offset in the buffer where the bank starts
            endian: Endianness ('<' for little endian, '>' for big endian)
            limit: Byte offset the bank must not reach past (see ``__init__``)

        Returns:
            Bank object
        """
        return cls(buffer, offset, endian, limit)

    def is_container(self) -> bool:
        """
        Check if this bank is a container for other banks.

        Returns:
            True if this bank contains other banks, False otherwise
        """
        return self.data_type in (self.TYPE_BANK, self.TYPE_BANK2, self.TYPE_SEGMENT, self.TYPE_SEGMENT2,
                                  self.TYPE_TAGSEGMENT)

    def child_class(self) -> Optional[type]:
        """The structure class of this container's children, or None for a leaf."""
        if self.data_type in (self.TYPE_BANK, self.TYPE_BANK2):
            return Bank
        if self.data_type in (self.TYPE_SEGMENT, self.TYPE_SEGMENT2):
            return Segment
        if self.data_type == self.TYPE_TAGSEGMENT:
            return TagSegment
        return None

    def get_children(self) -> List['Bank']:
        """
        Get child banks if this is a container bank.

        Returns:
            List of Bank objects
        """
        # Return empty list for non-container banks
        if not self.is_container():
            return []

        # Return cached children if already parsed
        if self._children is not None:
            return self._children

        self._children = []
        child_class = self.child_class()

        # Walk the children by their length words. Every length is checked
        # against this bank's end before it is trusted: a child that has no
        # room for its header words, declares length 0 with a 2-word header
        # (no room for the second header word) or runs past the parent is
        # corrupt data. The walk stops there, keeps the children found so far
        # and records why.
        current_offset = self.data_offset
        while current_offset < self.end_offset:
            if current_offset + child_class.HEADER_WORDS * 4 > self.end_offset:
                self.errors.append(f"{child_class.STRUCTURE_NAME} header at 0x{current_offset:X} needs "
                                   f"{child_class.HEADER_WORDS * 4} bytes, parent ends at 0x{self.end_offset:X}")
                break

            child_length = child_class.read_length(self.mm, current_offset, self.endian)
            child_end = current_offset + (child_length + 1) * 4
            if child_length + 1 < child_class.HEADER_WORDS or child_end > self.end_offset:
                self.errors.append(f"{child_class.STRUCTURE_NAME} at 0x{current_offset:X} declares {child_length} words, "
                                   f"ends at 0x{child_end:X}, parent ends at 0x{self.end_offset:X}")
                break

            self._children.append(child_class(self.mm, current_offset, self.endian, self.end_offset))
            current_offset = child_end

        return self._children

    def get_data(self) -> bytes:
        """
        Get the raw data for this bank.

        Returns:
            Bytes object containing the raw bank data
        """
        return self.mm[self.data_offset:self.data_offset + self.data_length]

    def to_numpy(self) -> Optional[np.ndarray]:
        """
        Convert bank data to a NumPy array if possible.

        Returns:
            NumPy array containing the data, or None if not convertible
        """
        if self.is_container():
            return None

        dtype = self._NUMPY_DTYPES.get(self.data_type)
        if dtype is None:
            return None

        # Calculate the number of elements
        element_size = np.dtype(dtype).itemsize
        num_elements = self.data_length // element_size

        # Create NumPy array from buffer
        return np.frombuffer(
            self.mm[self.data_offset:self.data_offset + (num_elements * element_size)],
            dtype=np.dtype(dtype).newbyteorder(self.endian)
        )

    def to_string(self) -> Optional[str]:
        """
        Convert bank data to a string if it's a string type.

        Returns:
            String representation of the data, or None if not a string
        """
        if self.data_type != self.TYPE_STRING:
            return None

        # Extract string data (null-terminated)
        data = self.mm[self.data_offset:self.data_offset + self.data_length]

        # Find null terminator
        null_pos = data.find(b'\0')
        if null_pos >= 0:
            data = data[:null_pos]

        # Decode as ASCII
        try:
            return data.decode('ascii')
        except UnicodeDecodeError:
            return None

    def get_hex_dump(self, max_bytes: int = 64, title: Optional[str] = None) -> str:
        """
        Generate a hex dump of the bank data.

        Args:
            max_bytes: Maximum number of bytes to include in the dump
            title: Optional title for the hex dump

        Returns:
            String containing formatted hexdump
        """
        display_len = min(max_bytes, self.data_length)
        data = self.mm[self.data_offset:self.data_offset + display_len]
        return make_hex_dump(data, title=title or f"Bank Data at offset 0x{self.data_offset:X}")

    def __str__(self) -> str:
        """Return string representation of the bank header."""
        return f"""{self.STRUCTURE_NAME} (0x{self.tag:04x}, {self.type_name}):
  Offset:    0x{self.offset:08x}
  Length:    {self.length} words ({self.size} bytes with the length word)
  Tag:       0x{self.tag:04x}
  Pad:       {self.pad}
  Data Type: 0x{self.data_type:02x}
  Num:       {self.num}"""

    def __repr__(self) -> str:
        """Return a string representation for debugging."""
        return f"{self.STRUCTURE_NAME}(offset=0x{self.offset:X}, tag=0x{self.tag:04X}, type=0x{self.data_type:02X})"


class Segment(Bank):
    """A segment: one header word ``tag (8) | pad (2) | type (6) | length (16)``, no num.

    Children of a bank of segments (data type 0xd/0x20). Shares every
    accessor with ``Bank``; ``num`` is None.
    """

    HEADER_WORDS = 1
    STRUCTURE_NAME = "Segment"

    def _parse_header(self):
        header = struct.unpack(self.endian + 'I', self.mm[self.offset:self.offset + 4])[0]
        self.tag = (header >> 24) & 0xFF
        self.pad = (header >> 22) & 0x3
        self.data_type = (header >> 16) & 0x3F
        self.length = header & 0xFFFF
        self.num = None

    @classmethod
    def read_length(cls, mm, offset: int, endian: str) -> int:
        return struct.unpack(endian + 'I', mm[offset:offset + 4])[0] & 0xFFFF


class TagSegment(Bank):
    """A tagsegment: one header word ``tag (12) | type (4) | length (16)``, no pad, no num.

    Children of a bank of tagsegments (data type 0xc).
    """

    HEADER_WORDS = 1
    STRUCTURE_NAME = "TagSegment"

    def _parse_header(self):
        header = struct.unpack(self.endian + 'I', self.mm[self.offset:self.offset + 4])[0]
        self.tag = (header >> 20) & 0xFFF
        self.pad = 0
        self.data_type = (header >> 16) & 0xF
        self.length = header & 0xFFFF
        self.num = None

    @classmethod
    def read_length(cls, mm, offset: int, endian: str) -> int:
        return struct.unpack(endian + 'I', mm[offset:offset + 4])[0] & 0xFFFF
