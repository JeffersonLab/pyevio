import mmap
import struct
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any

from pyevio.bank import Bank
from pyevio.buffer_reader import BufferReader


class StreamInfoBank(Bank):
    """Parses Stream Info Bank (SIB) within a ROC Time Slice Bank."""

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<'):
        """
        Initialize and parse a Stream Info Bank.

        Args:
            mm: Memory-mapped buffer
            offset: Byte offset where the SIB starts
            endian: Endianness ('<' for little endian, '>' for big endian)
        """
        super().__init__(mm, offset, endian)

        # Validate SIB tag
        if self.tag != 0xFF30:
            raise ValueError(f"Invalid Stream Info Bank tag: 0x{self.tag:04X}, expected 0xFF30")

        # Parse Stream Status
        self.error_flag = bool((self.stream_status >> 7) & 0x1)
        self.total_streams = ((self.stream_status >> 4) & 0x7)
        self.stream_mask = self.stream_status & 0xF

        # Parse Time Slice Segment (TSS)
        self._parse_time_slice_segment()

        # Parse Aggregation Info Segment (AIS)
        self._parse_aggregation_info_segment()

    def _parse_time_slice_segment(self):
        """Parse the Time Slice Segment (TSS)."""
        # TSS starts after the SIB header (2 words)
        tss_offset = self.offset + 8

        # TSS Header (first word)
        tss_header = struct.unpack(self.endian + 'I', self.mm[tss_offset:tss_offset+4])[0]
        self.tss_tag = (tss_header >> 24) & 0xFF  # Should be 0x31
        self.tss_type = (tss_header >> 16) & 0xFF  # Should be 0x01
        self.tss_length = tss_header & 0xFFFF

        # Validate TSS
        if self.tss_tag != 0x31:
            raise ValueError(f"Invalid Time Slice Segment tag: 0x{self.tss_tag:02X}, expected 0x31")

        # Frame Number (second word)
        self.frame_number = struct.unpack(self.endian + 'I', self.mm[tss_offset+4:tss_offset+8])[0]

        # Timestamp (two words: 64 bits total)
        timestamp_low = struct.unpack(self.endian + 'I', self.mm[tss_offset+8:tss_offset+12])[0]
        timestamp_high = struct.unpack(self.endian + 'I', self.mm[tss_offset+12:tss_offset+16])[0]

        if self.endian == '<':
            # Little endian: low word first, then high word
            self.timestamp = (timestamp_high << 32) | timestamp_low
        else:
            # Big endian: high word first, then low word
            self.timestamp = (timestamp_high << 32) | timestamp_low

        # A segment's length counts the words after its one header word.
        self.tss_end_offset = tss_offset + (self.tss_length + 1) * 4

    def _parse_aggregation_info_segment(self):
        """Parse the Aggregation Info Segment (AIS)."""
        # AIS starts after the TSS
        ais_offset = self.tss_end_offset

        # AIS Header (first word)
        ais_header = struct.unpack(self.endian + 'I', self.mm[ais_offset:ais_offset+4])[0]
        self.ais_tag = (ais_header >> 24) & 0xFF  # Should be 0x41
        self.ais_type_info = (ais_header >> 16) & 0xFF  # Should be 0x85
        self.ais_length = ais_header & 0xFFFF

        # Validate AIS
        if self.ais_tag != 0x41:
            raise ValueError(f"Invalid Aggregation Info Segment tag: 0x{self.ais_tag:02X}, expected 0x41")

        # Extract type info components
        self.padding = (self.ais_type_info >> 6) & 0x3  # Top 2 bits
        self.ais_data_type = self.ais_type_info & 0x3F  # Lower 6 bits (should be 5 = unsigned short)

        # Parse payload infos
        self.payload_infos = []

        # Number of 16-bit payloads
        num_payloads = self.ais_length

        # Calculate number of 32-bit words needed to store the payloads
        words_needed = (num_payloads + 1) // 2

        # Read payload infos
        for i in range(words_needed):
            word_offset = ais_offset + 4 + (i * 4)
            word = struct.unpack(self.endian + 'I', self.mm[word_offset:word_offset+4])[0]

            # Each word contains two 16-bit payloads (unless it's the last word with odd num_payloads)
            # Extract payloads in order (depending on endianness)
            if self.endian == '<':
                # Little endian: lower 16 bits first, then upper 16 bits
                payload1 = word & 0xFFFF
                payload2 = (word >> 16) & 0xFFFF

                # Add first payload
                if i * 2 < num_payloads:
                    self._add_payload_info(payload1)

                # Add second payload
                if i * 2 + 1 < num_payloads:
                    self._add_payload_info(payload2)
            else:
                # Big endian: upper 16 bits first, then lower 16 bits
                payload1 = (word >> 16) & 0xFFFF
                payload2 = word & 0xFFFF

                # Add first payload
                if i * 2 < num_payloads:
                    self._add_payload_info(payload1)

                # Add second payload
                if i * 2 + 1 < num_payloads:
                    self._add_payload_info(payload2)

        # Store AIS end offset for later use
        self.ais_end_offset = ais_offset + 4 + (words_needed * 4)

    def _add_payload_info(self, payload_info: int):
        """Parse and add a payload info to the list."""
        module_id = (payload_info >> 8) & 0xF  # bits 11-8
        bond = bool((payload_info >> 7) & 0x1)  # bit 7
        lane_id = (payload_info >> 5) & 0x3     # bits 6-5
        port_num = payload_info & 0x1F          # bits 4-0

        self.payload_infos.append({
            'module_id': module_id,
            'bond': bond,
            'lane_id': lane_id,
            'port_num': port_num,
            'raw_value': payload_info
        })


class PayloadBank(Bank):
    """Parses Payload Banks within a ROC Time Slice Bank."""

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<', payload_info: Dict = None):
        """
        Initialize and parse a Payload Bank.

        Args:
            mm: Memory-mapped buffer
            offset: Byte offset where the Payload Bank starts
            endian: Endianness ('<' for little endian, '>' for big endian)
            payload_info: Optional payload info dictionary from AIS
        """
        super().__init__(mm, offset, endian)
        self.payload_info = payload_info

        # For FADC250 data, we expect unsigned shorts (16-bit)
        # But we need to know the data format to interpret correctly
        self._analyze_data()

    def _analyze_data(self):
        """Analyze the payload data to determine its structure."""
        # For now, assume FADC250 data is stored as 16-bit unsigned shorts
        data_size = self.data_length

        if data_size % 2 != 0:
            # Odd number of bytes - adjust for padding
            data_size -= (data_size % 2)

        # Calculate number of samples
        self.num_samples = data_size // 2  # 2 bytes per sample for 16-bit data

        # Calculate expected channel count based on typical FADC250 configuration
        # This is a heuristic and may need adjustment based on actual data
        if self.num_samples > 0:
            # Try to detect if we have multiple channels
            # Typical sample counts per channel might be 100, 200, 250, etc.
            common_sample_counts = [100, 200, 250, 256, 512, 1000, 1024]

            # Find a channel count that gives a reasonable sample count
            self.channels = 1  # Default to 1 channel
            for count in common_sample_counts:
                if self.num_samples % count == 0:
                    self.channels = self.num_samples // count
                    self.samples_per_channel = count
                    break

            # If no clean division, just use a default value
            if not hasattr(self, 'samples_per_channel'):
                # Try to make an educated guess
                if self.num_samples < 100:
                    self.samples_per_channel = self.num_samples
                    self.channels = 1
                else:
                    self.samples_per_channel = 100  # Default arbitrary value
                    self.channels = self.num_samples // self.samples_per_channel

    def get_waveform_data(self, channel: int = None) -> List[int]:
        """
        Get waveform data for a specific channel or all channels.

        Args:
            channel: Channel number (None for all channels)

        Returns:
            List of data samples
        """
        if not hasattr(self, 'num_samples') or self.num_samples == 0:
            return []

        # Read all data samples as 16-bit values
        data = []
        for i in range(self.num_samples):
            sample_offset = self.data_offset + (i * 2)
            sample = struct.unpack(self.endian + 'H', self.mm[sample_offset:sample_offset+2])[0]
            data.append(sample)

        # If specific channel requested, extract only that channel
        if channel is not None and self.channels > 1:
            if channel >= self.channels:
                raise ValueError(f"Channel {channel} out of range (0-{self.channels-1})")

            # Extract samples for the specified channel
            # Channels are typically interleaved
            channel_data = []
            for i in range(channel, len(data), self.channels):
                channel_data.append(data[i])

            return channel_data

        return data

    def to_numpy(self, reshape=True):
        """
        Convert payload data to NumPy array.

        Args:
            reshape: If True, reshape array to (channels, samples_per_channel)
                    If False, return flat array

        Returns:
            NumPy array containing the data
        """
        import numpy as np

        # Read all data samples as 16-bit values
        data = np.frombuffer(
            self.mm[self.data_offset:self.data_offset + self.data_length],
            dtype=np.uint16
        )

        # Reshape if requested and possible
        if reshape and hasattr(self, 'channels') and hasattr(self, 'samples_per_channel'):
            if len(data) == self.channels * self.samples_per_channel:
                # Try to reshape into (channels, samples_per_channel)
                return data.reshape(self.channels, self.samples_per_channel)

        return data


class RocTimeSliceBank(Bank):
    """
    Class for parsing and analyzing ROC Time Slice Bank data.

    A ROC Time Slice Bank contains:
    1. A Stream Info Bank (SIB)
    2. Multiple Payload Banks
    """

    # Tag value for ROC Time Slice Banks
    TAG = 0xFF30

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<', limit: Optional[int] = None):
        """
        Initialize a RocTimeSliceBank parser.

        Args:
            mm: Memory-mapped buffer
            offset: Byte offset where the bank starts
            endian: Endianness ('<' for little endian, '>' for big endian)
            limit: Byte offset the bank must not reach past (see Bank)
        """
        super().__init__(mm, offset, endian, limit)

        # Validate ROC Time Slice Bank
        if self.data_type != 0x10:
            raise ValueError(f"Invalid ROC Time Slice Bank type: 0x{self.data_type:02X}, expected 0x10")

        # Parse Stream Status
        self.error_flag = bool((self.stream_status >> 7) & 0x1)
        self.total_streams = ((self.stream_status >> 4) & 0x7)
        self.stream_mask = self.stream_status & 0xF

        # Parse Stream Info Bank (starts after ROC TS Bank header)
        self.sib = StreamInfoBank(mm, offset + 8, endian)

        # Parse Payload Banks
        self._parse_payload_banks()

    def _parse_payload_banks(self):
        """Parse all Payload Banks within the ROC Time Slice Bank."""
        self.payload_banks = []

        # Start after Stream Info Bank
        current_offset = self.sib.ais_end_offset
        end_offset = self.end_offset

        # Match payload banks with payload infos
        for i, payload_info in enumerate(self.sib.payload_infos):
            if current_offset >= end_offset:
                # Reached the end of the bank
                break

            try:
                payload_bank = PayloadBank(self.mm, current_offset, self.endian, payload_info)
                self.payload_banks.append(payload_bank)

                # Move to next bank
                current_offset += payload_bank.size
            except Exception as e:
                # If there's an error parsing a payload bank, log it and continue
                if self.payload_banks:
                    break
                raise

    def get_timestamp(self) -> int:
        """Get the timestamp from the Stream Info Bank."""
        return self.sib.timestamp

    def get_formatted_timestamp(self) -> str:
        """Get a human-readable timestamp string."""
        timestamp_seconds = self.sib.timestamp / 1e9  # Convert to seconds (assuming nanoseconds)
        return datetime.fromtimestamp(timestamp_seconds).strftime('%Y-%m-%d %H:%M:%S.%f')

    def get_payload_data(self, payload_index: int = 0, channel: int = None) -> List[int]:
        """
        Get waveform data for a specific payload and channel.

        Args:
            payload_index: Index of the payload bank (default: 0)
            channel: Channel number (None for all channels)

        Returns:
            List of data samples
        """
        if payload_index < 0 or payload_index >= len(self.payload_banks):
            raise ValueError(f"Payload index {payload_index} out of range (0-{len(self.payload_banks)-1})")

        return self.payload_banks[payload_index].get_waveform_data(channel)

    def get_all_data_numpy(self):
        """
        Get all payload data as NumPy arrays.

        Returns:
            List of NumPy arrays, one per payload bank
        """
        import numpy as np

        result = []
        for payload in self.payload_banks:
            result.append(payload.to_numpy())

        return result