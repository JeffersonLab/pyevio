import mmap
import struct

import pytest

from pyevio.bank import Bank, Segment, TagSegment


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


def evio_bank(tag, data_type, num, payload, endian='<'):
    """Build a bank the EVIO way: the length word counts the words that follow it.

    A bank with N payload words gets length N + 1 (the second header word
    plus the payload) and occupies N + 2 words. This is what the evio
    writers produce and what `Bank` reads.
    """
    assert len(payload) % 4 == 0, "payload is whole 32-bit words"
    length = len(payload) // 4 + 1
    header = struct.pack(f"{endian}II", length,
                         ((tag & 0xFFFF) << 16) | ((data_type & 0xFF) << 8) | (num & 0xFF))
    return header + payload


def evio_segment(tag, data_type, payload, endian='<'):
    """Build a segment: one header word tag (8) | pad (2) | type (6) | length (16), then the payload."""
    assert len(payload) % 4 == 0
    return struct.pack(f"{endian}I", ((tag & 0xFF) << 24) | ((data_type & 0x3F) << 16) | (len(payload) // 4)) + payload


def evio_tagsegment(tag, data_type, payload, endian='<'):
    """Build a tagsegment: one header word tag (12) | type (4) | length (16), then the payload."""
    assert len(payload) % 4 == 0
    return struct.pack(f"{endian}I", ((tag & 0xFFF) << 20) | ((data_type & 0xF) << 16) | (len(payload) // 4)) + payload


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
        child1 = evio_bank(0x0101, 0x01, 0x01, struct.pack("<II", 1, 2))
        child2 = evio_bank(0x0202, 0x02, 0x02, struct.pack("<II", 3, 4))
        container = evio_bank(0xFFAA, 0x10, 0x00, child1 + child2)

        bank = Bank(MockMemoryMap(container), 0, '<')

        assert bank.tag == 0xFFAA
        assert bank.data_type == 0x10
        assert bank.num == 0x00
        assert bank.length == 9  # 1 header word + two 4-word children
        assert bank.size == len(container)

        children = bank.get_children()
        assert len(children) == 2
        assert bank.errors == []

        assert children[0].tag == 0x0101
        assert children[0].data_type == 0x01
        assert children[0].num == 0x01
        assert children[0].length == 3
        assert children[0].to_numpy().tolist() == [1, 2]

        assert children[1].tag == 0x0202
        assert children[1].data_type == 0x02
        assert children[1].num == 0x02
        assert children[1].length == 3
        assert children[1].offset == 8 + 16

    def test_minimal_length_banks(self):
        """A header-only bank (length 1, two words) is a valid empty leaf.

        SRO payload banks with no hits look like this: length 1, num 1.
        """
        empty_bank = evio_bank(0x000F, 0x00, 0x01, b"")
        data_bank = evio_bank(0x0010, 0x01, 0x00, struct.pack("<I", 7))
        container = evio_bank(0xFF60, 0x10, 0x01, empty_bank + data_bank)

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()

        assert len(children) == 2
        assert bank.errors == []

        assert children[0].length == 1
        assert children[0].offset == 8  # After container header
        assert children[0].size == 8
        assert children[0].data_length == 0
        assert children[0].tag == 0x000F
        assert children[0].num == 0x01
        assert children[0].to_numpy().tolist() == []

        assert children[1].offset == 16
        assert children[1].length == 2
        assert children[1].to_numpy().tolist() == [7]

    def test_nonstandard_tag_patterns(self):
        """Test parsing of banks with non-standard tag patterns."""
        # A ROC bank with 0x0002 for ROC_ID and 0x11 for status
        roc_bank = evio_bank(0x0002, 0x10, 0x11, struct.pack("<I", 0))
        container = evio_bank(0xFF60, 0x10, 0x01, roc_bank)

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()

        assert len(children) == 1
        assert children[0].tag == 0x0002
        assert children[0].data_type == 0x10
        assert children[0].num == 0x11
        assert children[0].length == 2

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
        assert bank.errors == []

        # The stream-info segment is a child of the ROC bank; the zero padding
        # words are no bank (length 0), so the walk stops there and says so.
        roc_children = children[1].get_children()
        assert [(c.offset, c.tag, c.data_type, c.length) for c in roc_children] == [
            (0x28, 0xFF30, 0x20, 4),
        ]
        assert len(children[1].errors) == 1 and "0x3C" in children[1].errors[0]
        assert children[2].get_children() == []

    def test_child_walk_stops_at_zero_length(self):
        """A length-0 word between two banks is corrupt: the walk keeps the banks before it and stops."""
        valid_bank1 = evio_bank(0x0101, 0x01, 0x01, struct.pack("<I", 1))
        invalid_data = struct.pack("<II", 0, 0)
        valid_bank2 = evio_bank(0x0202, 0x02, 0x02, struct.pack("<I", 2))
        container = evio_bank(0xFF60, 0x10, 0x01, valid_bank1 + invalid_data + valid_bank2)

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()

        assert [(c.offset, c.tag) for c in children] == [(8, 0x0101)]
        assert len(bank.errors) == 1
        assert "0x14" in bank.errors[0] and "declares 0 words" in bank.errors[0]

    def test_child_walk_stops_at_overrun(self):
        """A child length that runs past the parent is corrupt: the walk stops before it."""
        valid_bank = evio_bank(0x0101, 0x01, 0x01, struct.pack("<I", 1))
        invalid_bank = struct.pack("<II", 1000, 0x02020202)
        valid_bank2 = evio_bank(0x0303, 0x03, 0x03, struct.pack("<I", 3))
        container = evio_bank(0xFF60, 0x10, 0x01, valid_bank + invalid_bank + valid_bank2)

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()

        assert [(c.offset, c.tag) for c in children] == [(8, 0x0101)]
        assert len(bank.errors) == 1
        assert "declares 1000 words" in bank.errors[0]

        # Repeated calls return the cached children and keep one error entry.
        assert bank.get_children() == children
        assert len(bank.errors) == 1

    def test_child_walk_stops_at_short_header(self):
        """One trailing word cannot hold a bank header: the walk stops and reports it."""
        valid_bank = evio_bank(0x0101, 0x01, 0x01, struct.pack("<I", 1))
        container = evio_bank(0xFF60, 0x10, 0x01, valid_bank + struct.pack("<I", 5))

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()

        assert [(c.offset, c.tag) for c in children] == [(8, 0x0101)]
        assert len(bank.errors) == 1 and "needs 8 bytes" in bank.errors[0]

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
            assert event_bank.size == 88 and event_bank.end_offset == 88

            children = event_bank.get_children()
            assert [(c.offset, c.tag, c.data_type, c.num, c.length) for c in children] == [
                (0x08, 0xFF31, 0x20, 0x01, 7),
                (0x28, 0x0002, 0x10, 0x11, 11),
            ]
            assert event_bank.errors == []

            # Inside the ROC bank: the 0xFF30 segment, then a zero word, which is no bank.
            roc_children = children[1].get_children()
            assert [(c.offset, c.tag, c.data_type, c.length) for c in roc_children] == [
                (0x30, 0xFF30, 0x20, 7),
            ]
            assert len(children[1].errors) == 1 and "0x50" in children[1].errors[0]
            assert all(c.tag != 0x000F for c in children + roc_children)

    def test_sro_event_segments_are_one_word_structures(self):
        """The SIB of an SRO stream frame holds segments (1-word headers), typed by the parent.

        Layout as the hardware writes it: event 0xFF60 (bank of banks) ->
        SIB 0xFF31 (bank of segments: TSS 0x32 with frame, ts_lo, ts_hi; AIS
        0x42 with 24 identical words) + ROC bank (bank of banks: stream-info
        bank 0xFF30 of segments 0x31 and 0x41 (ushort16 port map), then
        payload banks of type 0x00, an empty one with num 1).
        """
        tss = evio_segment(0x32, Bank.TYPE_UINT32, struct.pack("<III", 7, 0x70000, 0))
        ais = evio_segment(0x42, Bank.TYPE_UINT32, struct.pack("<24I", *([0x00070021] * 24)))
        sib = evio_bank(0xFF31, Bank.TYPE_SEGMENT2, 0, tss + ais)
        roc_tss = evio_segment(0x31, Bank.TYPE_UINT32, struct.pack("<III", 7, 0x70000, 0))
        port_map = evio_segment(0x41, Bank.TYPE_UINT16, struct.pack("<I", (3 << 16) | 1))
        stream_info = evio_bank(0xFF30, Bank.TYPE_SEGMENT2, 0x21, roc_tss + port_map)
        payload_1 = evio_bank(1, Bank.TYPE_UNKNOWN32, 0, struct.pack("<II", 0x1234, 0x5678))
        payload_3 = evio_bank(3, Bank.TYPE_UNKNOWN32, 1, b"")
        roc = evio_bank(7, Bank.TYPE_BANK2, 0x21, stream_info + payload_1 + payload_3)
        event = evio_bank(0xFF60, Bank.TYPE_BANK2, 0, sib + roc)

        root = Bank(MockMemoryMap(event), 0, '<')
        sib_bank, roc_bank = root.get_children()
        assert root.errors == []
        assert (sib_bank.tag, sib_bank.data_type, sib_bank.length) == (0xFF31, 0x20, 30)

        segments = sib_bank.get_children()
        assert sib_bank.errors == []
        assert all(isinstance(s, Segment) for s in segments)
        assert [(s.offset, s.tag, s.data_type, s.length, s.size, s.num) for s in segments] == [
            (sib_bank.data_offset, 0x32, 0x01, 3, 16, None),
            (sib_bank.data_offset + 16, 0x42, 0x01, 24, 100, None),
        ]
        assert segments[0].to_numpy().tolist() == [7, 0x70000, 0]
        assert len(segments[1].to_numpy()) == 24 and segments[1].get_children() == []

        info, p1, p3 = roc_bank.get_children()
        assert roc_bank.errors == []
        assert isinstance(info, Bank) and info.tag == 0xFF30
        roc_segments = info.get_children()
        assert [(s.tag, s.data_type, s.length) for s in roc_segments] == [(0x31, 0x01, 3), (0x41, 0x05, 1)]
        assert roc_segments[1].to_numpy().tolist() == [1, 3]
        assert (p1.tag, p1.num, p1.to_numpy().tolist()) == (1, 0, [0x1234, 0x5678])
        assert (p3.tag, p3.num, p3.length, p3.data_length) == (3, 1, 1, 0)

    def test_tagsegment_container_children(self):
        """A bank of tagsegments (type 0xc) holds 1-word-header children with 12-bit tags."""
        first = evio_tagsegment(0xABC, Bank.TYPE_UINT32, struct.pack("<II", 5, 6))
        second = evio_tagsegment(0x001, Bank.TYPE_INT32, struct.pack("<i", -1))
        container = evio_bank(0x0100, Bank.TYPE_TAGSEGMENT, 0, first + second)

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()
        assert bank.errors == []
        assert all(isinstance(c, TagSegment) for c in children)
        assert [(c.offset, c.tag, c.data_type, c.length) for c in children] == [(8, 0xABC, 0x1, 2), (20, 0x001, 0xb, 1)]
        assert children[0].to_numpy().tolist() == [5, 6]
        assert children[1].to_numpy().tolist() == [-1]

    def test_segment_walk_stops_at_overrun(self):
        """A segment whose length runs past its parent stops the walk with an error."""
        good = evio_segment(0x32, Bank.TYPE_UINT32, struct.pack("<I", 1))
        bad = struct.pack("<I", (0x42 << 24) | (0x01 << 16) | 500)
        container = evio_bank(0xFF31, Bank.TYPE_SEGMENT2, 0, good + bad + bytes(8))

        bank = Bank(MockMemoryMap(container), 0, '<')
        children = bank.get_children()
        assert [(c.tag, c.length) for c in children] == [(0x32, 1)]
        assert len(bank.errors) == 1 and "Segment at 0x10 declares 500 words" in bank.errors[0]

    def test_bank_clamped_to_limit(self):
        """A bank whose length passes the given limit is clamped there and says so."""
        bank_bytes = evio_bank(0x0101, Bank.TYPE_UINT32, 0x01, struct.pack("<III", 1, 2, 3))
        bank = Bank(MockMemoryMap(bank_bytes), 0, '<', limit=16)
        assert bank.length == 4
        assert bank.end_offset == 16 and bank.size == 16 and bank.data_length == 8
        assert bank.to_numpy().tolist() == [1, 2]
        assert len(bank.errors) == 1 and "past its limit 0x10" in bank.errors[0]

        # Without a limit the buffer end bounds the bank.
        short = Bank(MockMemoryMap(bank_bytes[:12]), 0, '<')
        assert short.end_offset == 12 and short.to_numpy().tolist() == [1]
        assert len(short.errors) == 1

        # A buffer with no room for the header is refused before any read.
        with pytest.raises(ValueError, match="needs 8 bytes"):
            Bank(MockMemoryMap(bank_bytes[:6]), 0, '<')

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
        assert bank.get_data() == struct.pack("<III", 1, 2, 3)

    def test_data_payload_decoding(self):
        """Test decoding of FADC hit data in payload banks."""
        # Create a data payload with a single hit
        # Format: (channel << 13) | (charge & 0x1FFF) | ((time/4) << 17)
        channel = 5      # Channel 5
        charge = 1024    # ADC value of 1024
        time = 400       # Time of 400

        # Encode according to the bit pattern
        encoded_hit = (channel << 13) | (charge & 0x1FFF) | ((time // 4) << 17)

        data_bank = evio_bank(0x000F, 0x00, 0x01, struct.pack("<I", encoded_hit))
        print_hex_dump(data_bank, "Data bank raw bytes")

        mm = MockMemoryMap(data_bank)
        bank = Bank(mm, 0, '<')
        assert bank.length == 2 and bank.data_length == 4

        data_offset = bank.data_offset
        assert data_offset + 4 <= len(mm), f"Data offset {data_offset} + 4 exceeds buffer size {len(mm)}"

        data_word = struct.unpack("<I", mm[data_offset:data_offset+4])[0]

        decoded_channel = (data_word >> 13) & 0x000F
        decoded_charge = data_word & 0x1FFF
        decoded_time = ((data_word >> 17) & 0x3FFF) * 4

        assert decoded_channel == channel
        assert decoded_charge == charge
        assert decoded_time == time
