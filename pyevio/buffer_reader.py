import mmap
import struct
from typing import Union, Tuple, Optional
from pyevio.utils import make_hex_dump


class BufferReader:
    """
    A helper class to read values from a buffer with a base offset.
    Automatically detects endianness and provides convenient methods
    for reading various data types.
    """

    def __init__(self, buffer: Union[mmap.mmap, bytearray, bytes], base_offset: int = 0):
        """
        Initialize a BufferReader with a buffer and base offset.
        Automatically detects endianness based on the magic number.

        Args:
            buffer: Memory-mapped buffer or byte buffer
            base_offset: Starting offset in bytes

        Raises:
            ValueError: If magic number is invalid or the buffer is too small
        """
        self.buffer = buffer
        self.offset = base_offset
        self.endian = self._detect_endianness()

    def _detect_endianness(self) -> str:
        """
        Auto-detect endianness based on the magic number (0xc0da0100).

        Returns:
            Endianness string ('<' for little endian, '>' for big endian)

        Raises:
            ValueError: If magic number doesn't match in either endianness
        """
        # Magic number is at word 7 (offset 0x1C = 28 bytes from start)
        magic_pos = self.offset + (7 * 4)

        # Try little endian first
        try:
            magic = struct.unpack('<I', self.buffer[magic_pos:magic_pos+4])[0]
            if magic == 0xc0da0100:
                return '<'

            # Try big endian if little endian doesn't match
            magic = struct.unpack('>I', self.buffer[magic_pos:magic_pos+4])[0]
            if magic == 0xc0da0100:
                return '>'

        except (IndexError, struct.error) as e:
            raise ValueError(f"Buffer too small or invalid at offset {magic_pos}: {e}")

        # If we get here, neither endianness matched
        raise ValueError(f"Invalid magic number at offset 0x{magic_pos:X}: 0x{magic:08X}, expected 0xc0da0100")

    def read_uint32(self, offset_words: int = 0) -> int:
        """
        Read a 32-bit unsigned int at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            32-bit unsigned integer value

        Raises:
            IndexError: If reading beyond buffer bounds
            struct.error: If unable to unpack the data
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'I', self.buffer[pos:pos+4])[0]

    def read_uint64(self, offset_words: int = 0) -> int:
        """
        Read a 64-bit unsigned int at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            64-bit unsigned integer value

        Raises:
            IndexError: If reading beyond buffer bounds
            struct.error: If unable to unpack the data
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'Q', self.buffer[pos:pos+8])[0]

    def read_int32(self, offset_words: int = 0) -> int:
        """
        Read a 32-bit signed int at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            32-bit signed integer value
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'i', self.buffer[pos:pos+4])[0]

    def read_int64(self, offset_words: int = 0) -> int:
        """
        Read a 64-bit signed int at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            64-bit signed integer value
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'q', self.buffer[pos:pos+8])[0]

    def read_float(self, offset_words: int = 0) -> float:
        """
        Read a 32-bit float at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            32-bit float value
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'f', self.buffer[pos:pos+4])[0]

    def read_double(self, offset_words: int = 0) -> float:
        """
        Read a 64-bit double at the specified word offset.

        Args:
            offset_words: Offset in 32-bit words from the base offset

        Returns:
            64-bit double value
        """
        pos = self.offset + (offset_words * 4)
        return struct.unpack(self.endian + 'd', self.buffer[pos:pos+8])[0]

    def get_bytes(self, start_word: int, end_word: int) -> bytes:
        """
        Get a slice of bytes between word offsets.

        Args:
            start_word: Starting offset in 32-bit words from the base offset
            end_word: Ending offset in 32-bit words from the base offset

        Returns:
            Bytes between the specified word offsets
        """
        start_pos = self.offset + (start_word * 4)
        end_pos = self.offset + (end_word * 4)
        return self.buffer[start_pos:end_pos]

    def hex_dump(self, word_count: int = 14, title: Optional[str] = None) -> str:
        """
        Generate a hex dump starting at the base offset.

        Args:
            word_count: Number of 32-bit words to include in the dump
            title: Optional title for the hex dump

        Returns:
            String containing formatted hexdump
        """
        data = self.buffer[self.offset:self.offset+(word_count*4)]
        return make_hex_dump(data, title=title or f"Hex dump at offset 0x{self.offset:X}")