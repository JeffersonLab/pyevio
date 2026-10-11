import mmap
import struct
from typing import Optional

from pyevio.utils import make_hex_dump


# File type ids in word 0 of a v6 file header. The evio writers store the id
# as a 32-bit word in the file's byte order; HIPO-flavored writers store the
# ASCII bytes in order whatever the byte order, so the id must be accepted in
# both orders (see file_type_name).
FILE_TYPE_EVIO = 0x4556494F  # "EVIO"
FILE_TYPE_HIPO = 0x4849504F  # "HIPO"
FILE_TYPE_NAMES = {FILE_TYPE_EVIO: "EVIO", FILE_TYPE_HIPO: "HIPO"}

MAGIC_NUMBER = 0xc0da0100


def file_type_name(word: int) -> Optional[str]:
    """Name of a file type id ("EVIO", "HIPO") read in either byte order, else None."""
    swapped = int.from_bytes(word.to_bytes(4, "little"), "big")
    return FILE_TYPE_NAMES.get(word) or FILE_TYPE_NAMES.get(swapped)


def detect_endian(buffer, offset: int = 0) -> Optional[str]:
    """Byte order ('<' or '>') of the header at offset, from the magic word 0xc0da0100 at word 7.

    Both the v6 file header and every record or block header (v1—v6) carry
    the magic at word 7, so one probe serves all layouts. Returns None when
    neither byte order matches or the buffer is shorter than 8 words.
    """
    magic_bytes = buffer[offset + 28:offset + 32]
    if len(magic_bytes) < 4:
        return None
    for endian in ("<", ">"):
        if struct.unpack(endian + "I", magic_bytes)[0] == MAGIC_NUMBER:
            return endian
    return None


def unsupported_version_error(version: int, detail: str = "") -> ValueError:
    """The one error for files pyevio does not read, naming the EVIO version.

    The golden harness pins this message: it names "version N" and points
    at the evio tools.
    """
    message = (f"EVIO version {version} file is not supported: pyevio reads EVIO version 6 files "
               f"and SRO streaming files. Convert the file with the evio tools (evio2xml, eviocopy) "
               f"or read it with the evio library.")
    if detail:
        message += f" ({detail})"
    return ValueError(message)


class FileHeader:
    """
    Parses and represents an EVIO v6 file header.

    ``layout`` names the container the header describes:

    - ``"v6"`` — the 14-word file header read from the file, followed by
      14-word record headers.
    - ``"sro-8word"`` — the JLab SRO hardware stream: no file header at all,
      8-word block headers ``{length, block number, 8, event count, 0, 4,
      0, magic}`` whose version word says 4 with no bit-info bits. The
      events are EVIO v6 streaming content (tag 0xFF60 frames), so
      ``version`` reports 6 and ``container_version`` keeps the 4 the block
      headers carry. Every field that only a file header could supply
      (file number, user register, trailer position) is 0;
      ``record_count`` is filled from the record scan.
    """

    # EVIO magic number and bytes
    MAGIC_BYTES = b'EVIO'
    MAGIC_NUMBER = MAGIC_NUMBER

    # Size of file header in bytes
    HEADER_SIZE = 56  # 14 words * 4 bytes

    LAYOUT_V6 = "v6"
    LAYOUT_SRO = "sro-8word"

    def __init__(self):
        """Initialize an empty FileHeader object"""
        self.layout = self.LAYOUT_V6
        self.container_version = None
        self.file_type_id = None
        self.file_type = None
        self.file_number = None
        self.header_length = None
        self.record_count = None
        self.index_array_length = None
        self.bit_info = None
        self.version = None
        self.user_header_length = None
        self.magic_number = None
        self.user_register = None
        self.trailer_position = None
        self.user_integer1 = None
        self.user_integer2 = None

        # Derived properties
        self.endian = '<'  # Default to little endian
        self.has_dictionary = False
        self.has_first_event = False
        self.has_trailer = False

    @staticmethod
    def from_buffer(buffer: mmap.mmap, offset: int = 0) -> 'FileHeader':
        """
        Parse FileHeader from a memory-mapped buffer.

        Args:
            buffer: Memory-mapped buffer
            offset: Byte offset where the header starts

        Returns:
            FileHeader object

        Raises:
            ValueError: no magic word, no file type id, or a version other than 6
        """
        header = FileHeader()

        endian = detect_endian(buffer, offset)
        if endian is None:
            raise ValueError(f"Not an EVIO file: no magic number 0x{MAGIC_NUMBER:08x} at word 7 of offset 0x{offset:X}")
        header.endian = endian

        header.file_type_id = struct.unpack(endian + 'I', buffer[offset:offset+4])[0]
        header.file_type = file_type_name(header.file_type_id)
        if header.file_type is None:
            raise ValueError(f"Invalid EVIO file header: file_type_id = 0x{header.file_type_id:08x}, "
                             f"expected 0x{FILE_TYPE_EVIO:08X} (EVIO) or 0x{FILE_TYPE_HIPO:08X} (HIPO)")

        header.file_number = struct.unpack(endian + 'I', buffer[offset+4:offset+8])[0]
        header.header_length = struct.unpack(endian + 'I', buffer[offset+8:offset+12])[0]
        header.record_count = struct.unpack(endian + 'I', buffer[offset+12:offset+16])[0]
        header.index_array_length = struct.unpack(endian + 'I', buffer[offset+16:offset+20])[0]

        bit_info_version = struct.unpack(endian + 'I', buffer[offset+20:offset+24])[0]
        header.bit_info = bit_info_version >> 8
        header.version = bit_info_version & 0xFF

        header.user_header_length = struct.unpack(endian + 'I', buffer[offset+24:offset+28])[0]
        header.magic_number = struct.unpack(endian + 'I', buffer[offset+28:offset+32])[0]

        # 64-bit values
        header.user_register = struct.unpack(endian + 'Q', buffer[offset+32:offset+40])[0]
        header.trailer_position = struct.unpack(endian + 'Q', buffer[offset+40:offset+48])[0]

        header.user_integer1 = struct.unpack(endian + 'I', buffer[offset+48:offset+52])[0]
        header.user_integer2 = struct.unpack(endian + 'I', buffer[offset+52:offset+56])[0]

        if header.version != 6:
            raise unsupported_version_error(header.version, "file header")
        header.container_version = header.version

        # Parse bit_info fields
        header.has_dictionary = bool((header.bit_info >> 0) & 1)  # Bit 8
        header.has_first_event = bool((header.bit_info >> 1) & 1)  # Bit 9
        header.has_trailer = bool((header.bit_info >> 2) & 1)  # Bit 10

        return header

    @staticmethod
    def for_sro_stream(endian: str) -> 'FileHeader':
        """The stand-in header of an SRO hardware stream (layout ``"sro-8word"``, see the class docstring)."""
        header = FileHeader()
        header.layout = FileHeader.LAYOUT_SRO
        header.endian = endian
        header.file_type_id = 0
        header.file_type = "SRO"
        header.file_number = 0
        header.header_length = 0
        header.record_count = 0
        header.index_array_length = 0
        header.bit_info = 0
        header.version = 6
        header.container_version = 4
        header.user_header_length = 0
        header.magic_number = MAGIC_NUMBER
        header.user_register = 0
        header.trailer_position = 0
        header.user_integer1 = 0
        header.user_integer2 = 0
        return header

    def get_hex_dump(self, buffer: mmap.mmap, offset: int = 0) -> str:
        """ Generate a hex dump of the raw header bytes.

        Args:
            buffer: Memory-mapped buffer containing the header
            offset: Byte offset where the header starts

        Returns:
            String containing formatted hexdump
        """
        # Extract the bytes for the header
        header_bytes = buffer[offset:offset + self.HEADER_SIZE]
        return make_hex_dump(header_bytes, chunk_size=4, title="File Header Hex Dump")

    def __str__(self) -> str:
        """Return string representation of the header"""
        endian_str = "Little Endian" if self.endian == '<' else "Big Endian"

        return f"""EVIO File Header:
  Layout:            {self.layout}
  Magic Number:      0x{self.magic_number:08x}
  Version:           {self.version} (container {self.container_version})
  Endianness:        {endian_str}
  File Type ID:      0x{self.file_type_id:08x} ({self.file_type or "Unknown"})
  File Number:       {self.file_number}
  Header Length:     {self.header_length} words ({self.header_length * 4} bytes)
  Record Count:      {self.record_count}
  Index Array Len:   {self.index_array_length} bytes
  User Header Len:   {self.user_header_length} bytes
  Has Dictionary:    {self.has_dictionary}
  Has First Event:   {self.has_first_event}
  Has Trailer:       {self.has_trailer}
  Trailer Position:  0x{self.trailer_position:016x}
  User Register:     0x{self.user_register:016x}
  User Integer 1:    {self.user_integer1}
  User Integer 2:    {self.user_integer2}"""
