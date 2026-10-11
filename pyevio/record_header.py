from pyevio.buffer_reader import BufferReader


class RecordHeader:
    """Parses and represents an EVIO v6 record header.

    Word layout (word 5 holds the header species, flags and version):

        0 record length (words)      7 magic 0xc0da0100
        1 record number              8 uncompressed data length (bytes, events only)
        2 header length (words)      9 compression type (bits 28—31) | compressed length (words)
        3 event count               10—11 user register 1
        4 index array length (bytes) 12—13 user register 2
        5 species (bits 28—31) | bit-info (bits 8—27) | version (bits 0—7)
        6 user header length (bytes)

    A 14-word header carries all of these. An 8-word header (the SRO hardware
    block layout, see ``EvioFile``) stops after the magic word; the remaining
    fields read as 0.
    """

    # EVIO record header magic number
    MAGIC_NUMBER = 0xc0da0100

    # Size of a full record header in bytes
    HEADER_SIZE = 56  # 14 words * 4 bytes

    # Header species, bits 28—31 of word 5.
    TYPE_RECORD = 0
    TYPE_FILE = 1
    TYPE_TRAILER = 3

    def __init__(self):
        """
            Initialize an empty RecordHeader object
        """
        self.record_length = -1
        self.record_number = -1
        self.header_length = -1
        self.endian = ""
        self.event_count = -1
        self.index_array_length = -1
        self.version = -1
        self.bit_info = -1
        self.header_type = -1
        self.pad1 = 0
        self.pad2 = 0
        self.pad3 = 0
        self.user_header_length = -1
        self.magic_number = -1
        self.uncompressed_data_length = 0
        self.compression_type = 0
        self.compressed_data_length = 0
        self.user_register1 = 0
        self.user_register2 = 0
        self.has_dictionary = False
        self.is_last_record = False
        self.has_first_event = False
        self.event_type = f"NeverInitialized"

    # Word 5 of an SRO hardware block header: version 4 and nothing else. A
    # v4 file written by the evio library sets bit-info bits somewhere in its
    # chain (last-block 0x200, dictionary 0x100), which is how the two are
    # told apart (EvioFile._detect_layout).
    SRO_BLOCK_WORD = 0x00000004

    @staticmethod
    def is_sro_block_word(bit_info_version: int) -> bool:
        """True when word 5 is exactly version 4 with no bit-info bits."""
        return bit_info_version == RecordHeader.SRO_BLOCK_WORD

    @property
    def is_trailer(self) -> bool:
        """True for the trailer record that closes a file (header species 3).

        A trailer's index array lists (record length, event count) pairs of the
        records before it, never events.
        """
        return self.header_type == RecordHeader.TYPE_TRAILER

    @staticmethod
    def parse(buffer, offset=0):
        """
        Parse a RecordHeader from a memory-mapped buffer.

        Args:
            buffer: Memory-mapped buffer containing EVIO data
            offset: Byte offset where the record header starts

        Returns:
            RecordHeader object with parsed data
        """
        header = RecordHeader()
        if offset + 8 * 4 > len(buffer):
            raise ValueError(f"Record header at offset 0x{offset:X} needs 8 words, "
                             f"buffer ends at 0x{len(buffer):X}")
        reader = BufferReader(buffer, offset)

        # The first eight words exist in every header layout.
        header.record_length = reader.read_uint32(0)
        header.record_number = reader.read_uint32(1)
        header.header_length = reader.read_uint32(2)
        header.endian = reader.endian
        header.event_count = reader.read_uint32(3)
        header.index_array_length = reader.read_uint32(4)

        bit_info_version = reader.read_uint32(5)
        header.version = bit_info_version & 0xFF
        header.bit_info = (bit_info_version >> 8) & 0xFFFFF
        header.header_type = (bit_info_version >> 28) & 0xF
        # Padding byte counts: pad1 after the user header, pad2 after the
        # events, pad3 after the compressed data (bits 20—25 of word 5).
        header.pad1 = (bit_info_version >> 20) & 0x3
        header.pad2 = (bit_info_version >> 22) & 0x3
        header.pad3 = (bit_info_version >> 24) & 0x3

        header.user_header_length = reader.read_uint32(6)
        header.magic_number = reader.read_uint32(7)

        # Words 8—13 exist only in the 14-word layout; reading them from an
        # 8-word header would take the first event's words as header fields.
        if header.header_length >= 14:
            if offset + 14 * 4 > len(buffer):
                raise ValueError(f"Record header at offset 0x{offset:X} declares 14 words, "
                                 f"buffer ends at 0x{len(buffer):X}")
            header.uncompressed_data_length = reader.read_uint32(8)

            compression_data = reader.read_uint32(9)
            header.compression_type = (compression_data >> 28) & 0xF
            header.compressed_data_length = compression_data & 0x0FFFFFFF

            header.user_register1 = reader.read_uint64(10)  # Words 10-11
            header.user_register2 = reader.read_uint64(12)  # Words 12-13

        if header.magic_number != RecordHeader.MAGIC_NUMBER:
            raise ValueError(f"Invalid record magic number: 0x{header.magic_number:08x}, expected 0x{RecordHeader.MAGIC_NUMBER:08x}")

        # Version 6 is the record header; the SRO hardware block header is
        # the one other shape read: exactly 8 words, version 4, no bit-info
        # bits (see RecordHeader.is_sro_block_word). Any other version is a
        # legacy block that EvioFile refuses at open.
        if header.version != 6 and not (header.header_length == 8
                                        and RecordHeader.is_sro_block_word(bit_info_version)):
            raise ValueError(f"Unsupported EVIO version in record at offset 0x{offset:X}: "
                             f"version {header.version}, expected 6")

        # Parse bit_info fields
        header.has_dictionary = bool((header.bit_info >> 0) & 1)  # Bit 8
        header.is_last_record = bool((header.bit_info >> 1) & 1)  # Bit 9

        # Extract event type (bits 10-13)
        event_type_code = (header.bit_info >> 2) & 0xF
        event_types = {
            0: "ROC Raw",
            1: "Physics",
            2: "Partial Physics",
            3: "Disentangled Physics",
            4: "User",
            5: "Control",
            6: "Mixed",
            8: "ROC Raw Streaming",
            9: "Physics Streaming",
            15: "Other"
        }
        header.event_type = event_types.get(event_type_code, f"Unknown ({event_type_code})")

        header.has_first_event = bool((header.bit_info >> 6) & 1)  # Bit 14

        return header
