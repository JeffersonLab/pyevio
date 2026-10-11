import mmap
import struct

import pytest

from pyevio.bank import Bank


class MockMemoryMap:
    """A mock memory map for testing bank scanning."""

    def __init__(self, data):
        """
        Initialize with byte data.

        Args:
            data: Bytes to use for the mock memory map
        """
        self.data = data

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            stop = key.stop or len(self.data)
            return self.data[start:stop]
        return self.data[key]

    def __len__(self):
        return len(self.data)


def create_mock_bank(length, tag, data_type, num, data=None, endian='<'):
    """
    Create a mock bank with the specified parameters.

    Args:
        length: Bank length in words (including header)
        tag: Bank tag
        data_type: Bank data type
        num: Bank number
        data: Optional data (bytes) to include in the bank
        endian: Endianness ('<' for little endian, '>' for big endian)

    Returns:
        Bytes object representing the bank
    """
    # Create bank header (2 words)
    header_word1 = struct.pack(f"{endian}I", length)
    header_word2 = struct.pack(f"{endian}I",
                               ((tag & 0xFFFF) << 16) |
                               ((data_type & 0xFF) << 8) |
                               (num & 0xFF))

    # If data is None, create empty data of appropriate length
    if data is None:
        # Length includes header, so subtract 2 words for data
        data_size = max(0, (length - 2) * 4)
        data = bytes(data_size)

    # Combine header and data
    return header_word1 + header_word2 + data


def evio_bank(tag, data_type, num, payload, endian='<'):
    """Build a bank the EVIO way: the length word counts the words that follow it.

    `create_mock_bank` above takes a length that includes the length word
    itself, which is the convention `Bank` uses today (see
    test_bank_length_counts_words_that_follow). Use this builder for data
    that must match what the evio writers produce.
    """
    assert len(payload) % 4 == 0, "payload is whole 32-bit words"
    length = len(payload) // 4 + 1
    header = struct.pack(f"{endian}II", length,
                         ((tag & 0xFFFF) << 16) | ((data_type & 0xFF) << 8) | (num & 0xFF))
    return header + payload


def print_hex_dump(data, title=None):
    """Helper function to print a hex dump of data for debugging."""
    if title:
        print(f"--- {title} ---")

    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_values = ' '.join([f"{b:02X}" for b in chunk])
        ascii_values = ''.join([chr(b) if 32 <= b < 127 else '.' for b in chunk])
        print(f"{i:04X}: {hex_values:<48} {ascii_values}")


class TestBankScanning:
    """Tests for the Bank scanning logic."""

    def test_basic_bank_structure(self):
        """Test parsing of a simple container bank with child banks."""
        # Create a container bank with two child banks
        child1 = create_mock_bank(4, 0x0101, 0x01, 0x01, struct.pack("<II", 1, 2))
        child2 = create_mock_bank(4, 0x0202, 0x02, 0x02, struct.pack("<II", 3, 4))

        # Container bank length = 2 (header) + len(child1)/4 + len(child2)/4
        container_length = 2 + len(child1)//4 + len(child2)//4
        container_data = child1 + child2
        container = create_mock_bank(container_length, 0xFFAA, 0x10, 0x00, container_data)

        # Create mock memory map
        mm = MockMemoryMap(container)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Verify bank properties
        assert bank.tag == 0xFFAA
        assert bank.data_type == 0x10
        assert bank.num == 0x00
        assert bank.length == container_length

        # Get children
        children = bank.get_children()

        # Verify children count
        assert len(children) == 2

        # Verify first child
        assert children[0].tag == 0x0101
        assert children[0].data_type == 0x01
        assert children[0].num == 0x01
        assert children[0].length == 4

        # Verify second child
        assert children[1].tag == 0x0202
        assert children[1].data_type == 0x02
        assert children[1].num == 0x02
        assert children[1].length == 4

    def test_minimal_length_banks(self):
        """Test parsing of banks with minimal lengths (1 and 2)."""
        # Create a length-1 bank (just the length word)
        bank1 = struct.pack("<I", 1)

        # Create a length-2 bank (length word + header word)
        bank2 = create_mock_bank(2, 0x000F, 0x00, 0x01)

        # Create a container with these banks
        container_data = bank1 + bank2
        container_length = 2 + len(container_data)//4
        container = create_mock_bank(container_length, 0xFF60, 0x10, 0x01, container_data)

        # Create mock memory map
        mm = MockMemoryMap(container)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Get children
        children = bank.get_children()

        # Verify children count
        assert len(children) == 2

        # Verify first child (length-1 bank)
        assert children[0].length == 1
        assert children[0].offset == 8  # After container header

        # Verify second child (length-2 bank)
        assert children[1].length == 2
        assert children[1].tag == 0x000F
        assert children[1].data_type == 0x00
        assert children[1].num == 0x01

    def test_nonstandard_tag_patterns(self):
        """Test parsing of banks with non-standard tag patterns."""
        # Create a ROC bank with 0x0002 for ROC_ID and 0x11 for status
        roc_bank = create_mock_bank(3, 0x0002, 0x10, 0x11, struct.pack("<I", 0))

        # Create a container with this bank
        container_data = roc_bank
        container_length = 2 + len(container_data)//4
        container = create_mock_bank(container_length, 0xFF60, 0x10, 0x01, container_data)

        # Create mock memory map
        mm = MockMemoryMap(container)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Get children
        children = bank.get_children()

        # Verify children count
        assert len(children) == 1

        # Verify ROC bank
        assert children[0].tag == 0x0002
        assert children[0].data_type == 0x10
        assert children[0].num == 0x11
        assert children[0].length == 3

    def test_complex_event_structure(self):
        """A ROC time-slice event: segment, bank of banks, leaf bank, all EVIO-consistent.

        Every length word counts the words that follow it (EVIO rule), so a
        bank occupies length + 1 words. The three top-level banks fill the
        event exactly:

            offset 0x00  event bank   length 20  -> words 0..20 (21 words)
            offset 0x08  0xFF31 segment, 4 payload words, length 5 -> 6 words
            offset 0x20  0x0002 bank of banks, length 9 -> 10 words
            offset 0x28    0xFF30 segment, 3 payload words, length 4 -> 5 words
            offset 0x3C    3 zero words of padding inside the ROC bank
            offset 0x48  0x000F leaf bank, 1 payload word, length 2 -> 3 words
        """
        segment_bank = evio_bank(0xFF31, 0x20, 0x01, struct.pack("<IIII", 0x32010003, 0, 0, 0x42010001))
        stream_info = evio_bank(0xFF30, 0x20, 0x11, struct.pack("<III", 0x31010003, 0, 0))
        roc_bank = evio_bank(0x0002, 0x10, 0x11, stream_info + bytes(12))
        data_bank = evio_bank(0x000F, 0x00, 0x01, struct.pack("<I", 0x000F0001))
        event_data = evio_bank(0xFF60, 0x10, 0x01, segment_bank + roc_bank + data_bank)

        assert len(segment_bank) == 24 and len(roc_bank) == 40 and len(data_bank) == 12
        assert len(event_data) == 84

        bank = Bank(MockMemoryMap(event_data), 0, '<')
        assert bank.length == 20
        children = bank.get_children()

        assert [(c.offset, c.tag, c.data_type, c.length) for c in children] == [
            (0x08, 0xFF31, 0x20, 5),
            (0x20, 0x0002, 0x10, 9),
            (0x48, 0x000F, 0x00, 2),
        ]

        # The stream-info segment is a child of the ROC bank; the padding words are no banks.
        roc_children = children[1].get_children()
        assert [(c.offset, c.tag, c.data_type, c.length) for c in roc_children] == [
            (0x28, 0xFF30, 0x20, 4),
        ]
        assert children[2].get_children() == []

    def test_edge_cases(self):
        """Test handling of edge cases (zero-length banks, boundary conditions)."""
        # Create valid banks
        valid_bank1 = create_mock_bank(3, 0x0101, 0x01, 0x01, struct.pack("<I", 1))
        valid_bank2 = create_mock_bank(3, 0x0202, 0x02, 0x02, struct.pack("<I", 2))

        # Create invalid data (not a proper bank)
        invalid_data = struct.pack("<II", 0, 0)  # Zero length, invalid

        # Create container with valid and invalid data
        container_data = valid_bank1 + invalid_data + valid_bank2
        container_length = 2 + len(container_data)//4
        container = create_mock_bank(container_length, 0xFF60, 0x10, 0x01, container_data)

        # Create mock memory map
        mm = MockMemoryMap(container)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Get children - should skip invalid data
        children = bank.get_children()

        # Verify children count - should only get the valid banks
        assert len(children) == 2

        # Verify first valid bank
        assert children[0].tag == 0x0101
        assert children[0].data_type == 0x01

        # Verify second valid bank
        assert children[1].tag == 0x0202
        assert children[1].data_type == 0x02

    def test_error_recovery(self):
        """Test recovery from corrupted or invalid bank data."""
        # Create valid bank
        valid_bank = create_mock_bank(3, 0x0101, 0x01, 0x01, struct.pack("<I", 1))

        # Create bank with invalid length (would go beyond parent)
        invalid_bank = struct.pack("<I", 1000) + struct.pack("<I", 0x02020202)

        # Create another valid bank after the invalid one
        valid_bank2 = create_mock_bank(3, 0x0303, 0x03, 0x03, struct.pack("<I", 3))

        # Create container with mix of valid and invalid
        container_data = valid_bank + invalid_bank + valid_bank2
        container_length = 2 + len(container_data)//4
        container = create_mock_bank(container_length, 0xFF60, 0x10, 0x01, container_data)

        # Create mock memory map
        mm = MockMemoryMap(container)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Get children - should skip invalid bank and recover
        children = bank.get_children()

        # Verify we found at least the first valid bank
        assert len(children) >= 1
        assert children[0].tag == 0x0101
        assert children[0].data_type == 0x01




    # The 22-word stream of a ROC time-slice event as dumped from a DAQ file.
    # Length words count the words that follow, so the containment is:
    #
    #   word  0  0x00  length 21        event bank 0xFF60, words 0..21 (the whole stream)
    #   word  2  0x08  length 7         0xFF31 segment, words 2..9
    #   word  9  0x24  0x00020011       last payload word of the 0xFF31 segment
    #   word 10  0x28  length 11        0x0002 bank of banks, words 10..21: reaches the end
    #   word 12  0x30    length 7       0xFF30 segment, words 12..19
    #   word 20  0x50    length 2       a bank header that needs words 20..22; the
    #                                   parent ends at word 21, so it is a truncated tail
    #
    # The 0x000F header at word 20 lies inside the 0x0002 bank
    # (0x28 + (11 + 1) * 4 = 0x58 > 0x50), so the event has two top-level banks.
    REAL_WORLD_EVENT_WORDS = (
        21, 0xFF601001,
        7, 0xFF312001, 0x32010003, 0, 0, 0, 0x42010001,
        0x00020011,
        11, 0x00021011,
        7, 0xFF302011, 0x31010003, 0, 0, 0, 0x41850001,
        0,
        2, 0x000F0001,
    )

    def test_real_world_event_structure(self, tmp_path):
        """Top-level banks of the dumped event follow the length words, with the 0x000F header nested."""
        path = tmp_path / "real_world_event.bin"
        path.write_bytes(struct.pack("<22I", *self.REAL_WORLD_EVENT_WORDS))

        with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            event_bank = Bank(mm, 0, '<')
            assert (event_bank.length, event_bank.tag, event_bank.data_type) == (21, 0xFF60, 0x10)

            children = event_bank.get_children()
            assert [(c.offset, c.tag, c.data_type, c.num, c.length) for c in children] == [
                (0x08, 0xFF31, 0x20, 0x01, 7),
                (0x28, 0x0002, 0x10, 0x11, 11),
            ]

            # Inside the ROC bank: the 0xFF30 segment, then the truncated header, which is no bank.
            roc_children = children[1].get_children()
            assert [(c.offset, c.tag, c.data_type, c.length) for c in roc_children] == [
                (0x30, 0xFF30, 0x20, 7),
            ]
            assert all(c.tag != 0x000F for c in children + roc_children)

    @pytest.mark.xfail(strict=True, reason="F38: Bank counts the length word as part of its own "
                                           "length; size, end_offset and data_length are one word short")
    def test_bank_length_counts_words_that_follow(self):
        """A leaf bank with three uint32 payload words occupies five words and yields all three."""
        bank_bytes = evio_bank(0x0101, Bank.TYPE_UINT32, 0x01, struct.pack("<III", 1, 2, 3))
        assert len(bank_bytes) == 20

        bank = Bank(MockMemoryMap(bank_bytes), 0, '<')
        assert bank.length == 4
        assert bank.size == 20
        assert bank.end_offset == 20
        assert bank.data_length == 12
        assert bank.to_numpy().tolist() == [1, 2, 3]

    def test_data_payload_decoding(self):
        """Test decoding of FADC hit data in payload banks."""
        # Create a data payload with a single hit
        # Format: (channel << 13) | (charge & 0x1FFF) | ((time/4) << 17)
        channel = 5      # Channel 5
        charge = 1024    # ADC value of 1024
        time = 400       # Time of 400

        # Encode according to the bit pattern
        encoded_hit = (channel << 13) | (charge & 0x1FFF) | ((time // 4) << 17)

        # CRITICAL FIX: Use create_mock_bank to create a proper bank structure
        data_bank = create_mock_bank(2, 0x000F, 0x00, 0x01, struct.pack("<I", encoded_hit))

        # Debug output
        print("\nDEBUG: Data Bank Structure")
        print_hex_dump(data_bank, "Data bank raw bytes")

        # Create mock memory map
        mm = MockMemoryMap(data_bank)

        # Create bank object
        bank = Bank(mm, 0, '<')

        # Debug output
        print(f"Bank: offset={hex(bank.offset)}, length={bank.length}, data_offset={hex(bank.data_offset)}")

        # We don't have direct access to the decoded fields in Bank object,
        # but we can verify the encoding by manually decoding the data
        data_offset = bank.data_offset

        # Verify the data_offset is within bounds
        assert data_offset + 4 <= len(mm), f"Data offset {data_offset} + 4 exceeds buffer size {len(mm)}"

        # Now it's safe to decode
        data_word = struct.unpack("<I", mm[data_offset:data_offset+4])[0]

        decoded_channel = (data_word >> 13) & 0x000F
        decoded_charge = data_word & 0x1FFF
        decoded_time = ((data_word >> 17) & 0x3FFF) * 4

        # Verify decoded values match what we encoded
        assert decoded_channel == channel
        assert decoded_charge == charge
        assert decoded_time == time