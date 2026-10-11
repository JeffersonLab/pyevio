import mmap
import struct
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any
from pyevio.record_header import RecordHeader
from pyevio.utils import make_hex_dump
import numpy as np


class Record:
    """
    Represents a record in an EVIO file.

    A record contains a header followed by an event index array, optional user header,
    and multiple events. This class provides methods to access and parse the record
    structure efficiently.
    """

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<', index=-1):
        """
        Initialize a Record object.

        Args:
            mm: Memory-mapped file containing the record
            offset: Byte offset in the file where the record starts
            endian: Endianness ('<' for little endian, '>' for big endian)
        """
        self.mm = mm
        self.offset = offset
        self.endian = endian

        # Parse the record header
        self.header = RecordHeader.parse(mm, offset)

        # Calculate key positions in the record
        self.header_size = self.header.header_length * 4
        self.index_array_start = self.offset + self.header_size
        self.index_array_end = self.index_array_start + self.header.index_array_length
        self.data_start = self.index_array_end + self.header.user_header_length
        self.data_end = self.offset + (self.header.record_length * 4)
        self.size = self.header.record_length * 4
        self.index = index

        # Cache for events (will be populated on demand)
        self._events = None
        self._event_count = None

    @property
    def event_count(self) -> int:
        """Get the number of events in this record."""
        if self._event_count is None:
            self._event_count = self.header.event_count
        return self._event_count

    def scan_events(self) -> List[Tuple[int, int]]:
        """
        Scan and parse all events in this record.

        Returns:
            List of tuples (offset, length) for all events in the record
        """
        event_info = []

        if self.header.index_array_length > 0:
            # Parse events from index array
            event_count = self.header.index_array_length // 4
            current_offset = self.data_start

            for i in range(event_count):
                length_offset = self.index_array_start + (i * 4)
                event_length = struct.unpack(self.endian + 'I', self.mm[length_offset:length_offset+4])[0]

                # Store event offset and length
                event_info.append((current_offset, event_length))

                # Update cumulative offset for next event
                current_offset += event_length

        elif self.header.event_count > 0:
            # No index array: walk events by the length word of each event bank
            current_offset = self.data_start
            while current_offset + 8 <= self.data_end and len(event_info) < self.header.event_count:
                bank_length = struct.unpack(self.endian + 'I', self.mm[current_offset:current_offset+4])[0]
                event_length = (bank_length + 1) * 4
                event_info.append((current_offset, event_length))
                current_offset += event_length

        return event_info

    def get_events(self, start_event: Optional[int] = None, end_event: Optional[int] = None):
        """
        Get events in the specified range.

        Args:
            start_event: Starting event index (default: 0)
            end_event: Ending event index, exclusive (default: all events)

        Returns:
            List of Event objects
        """
        # Scan events only if needed
        if self._events is None:
            from pyevio.event import Event  # Import here to avoid circular import
            event_info = self.scan_events()
            self._events = [
                Event(self.mm, offset, length, self.endian, i)
                for i, (offset, length) in enumerate(event_info)
            ]

        # Apply range filter
        if start_event is None:
            start_event = 0
        else:
            start_event = max(0, min(start_event, len(self._events)))

        if end_event is None:
            end_event = len(self._events)
        else:
            end_event = max(start_event, min(end_event, len(self._events)))

        return self._events[start_event:end_event]

    def get_event_offsets(self, start_event: Optional[int] = None, end_event: Optional[int] = None) -> List[Tuple[int, int]]:
        """
        Get offsets and lengths of events in the specified range without creating Event objects.

        Args:
            start_event: Starting event index (default: 0)
            end_event: Ending event index, exclusive (default: all events)

        Returns:
            List of tuples (offset, length) for each event
        """
        event_info = self.scan_events()

        # Apply range filter
        if start_event is None:
            start_event = 0
        else:
            start_event = max(0, min(start_event, len(event_info)))

        if end_event is None:
            end_event = len(event_info)
        else:
            end_event = max(start_event, min(end_event, len(event_info)))

        return event_info[start_event:end_event]

    def events_to_numpy_direct(self, start_event: Optional[int] = None, end_event: Optional[int] = None,
                               signature: Optional[int] = None, event_size_words: Optional[int] = None) -> np.ndarray:
        """
        Convert events directly to a NumPy array without creating Event objects.

        This optimized method loads events directly from memory into a structured NumPy array.

        Args:
            start_event: Starting event index (default: 0)
            end_event: Ending event index, exclusive (default: all events)
            signature: Optional integer signature to match in the second word of events (e.g., 0xFF60)
            event_size_words: If all events have the same size in words, specify it for optimal processing

        Returns:
            2D NumPy array where rows are events and columns are 32-bit words
        """
        # Get event offsets and lengths
        event_info = self.get_event_offsets(start_event, end_event)

        if not event_info:
            return np.array([], dtype=np.uint32)

        if signature is not None:
            # Filter events by signature
            filtered_info = []
            signature_mask = signature & 0xFFFF  # Use only top 16 bits

            for offset, length in event_info:
                if length >= 8:  # Need at least 2 words
                    # Get the second word
                    second_word = struct.unpack(self.endian + 'I', self.mm[offset + 4:offset + 8])[0]
                    if (second_word >> 16) == signature_mask:
                        filtered_info.append((offset, length))

            event_info = filtered_info

        if not event_info:
            return np.array([], dtype=np.uint32)

        if event_size_words is not None:
            # All events have the same size, we can create a regular 2D array
            num_events = len(event_info)
            result = np.zeros((num_events, event_size_words), dtype=np.uint32)

            for i, (offset, _) in enumerate(event_info):
                # Read event data directly into result array with correct endianness
                event_data = np.frombuffer(
                    self.mm[offset:offset + (event_size_words * 4)],
                    dtype=np.dtype(np.uint32).newbyteorder('>' if self.endian == '>' else '<')
                )
                result[i, :len(event_data)] = event_data

            return result
        else:
            # Variable-sized events, create a list of arrays
            event_arrays = []

            for offset, length in event_info:
                word_count = length // 4
                event_data = np.frombuffer(
                    self.mm[offset:offset + length],
                    dtype=np.uint32
                )
                event_arrays.append(event_data)

            # Try to determine if all events are the same size after filtering
            if all(len(arr) == len(event_arrays[0]) for arr in event_arrays):
                # Convert to regular 2D array
                return np.vstack(event_arrays)
            else:
                # Return as object array of different sized arrays
                return np.array(event_arrays, dtype=object)

    def get_event(self, index: int):
        """
        Get an event by index within this record.

        Args:
            index: Event index (0-based)

        Returns:
            Event object

        Raises:
            IndexError: If index is out of range
        """
        events = self.get_events()

        if index < 0 or index >= len(events):
            raise IndexError(f"Event index {index} out of range (0-{len(events)-1})")

        return events[index]

    import numpy as np

    def events_to_numpy(self, signature: int = 0xFF60, start_event: Optional[int] = None, end_event: Optional[int] = None, dtype=np.uint32) -> np.ndarray:
        """
        Convert events with a specific signature to a NumPy array.

        This method efficiently converts events where the second word matches the given signature
        to a NumPy array. It avoids slow event-by-event Python loops by taking a vectorized approach
        where possible.

        Args:
            signature: Integer signature to match in the second word of events (default: 0xFF60)
            start_event: Starting event index (default: 0)
            end_event: Ending event index, exclusive (default: all events)
            dtype: NumPy data type for the resulting array (default: np.uint32)

        Returns:
            NumPy array containing all event data matching the signature
        """
        # Get all events or subset based on start/end parameters
        events = self.get_events(start_event, end_event)

        if not events:
            return np.array([], dtype=dtype)

        # Extract offsets and lengths for all events
        offsets = np.array([event.offset for event in events], dtype=np.int64)
        lengths = np.array([event.length for event in events], dtype=np.int64)

        # Filter out events that are too small
        valid_indices = lengths >= 8
        offsets = offsets[valid_indices]
        lengths = lengths[valid_indices]

        if len(offsets) == 0:
            return np.array([], dtype=dtype)

        # Check which events match the signature
        signatures = np.zeros(len(offsets), dtype=np.uint32)

        for i in range(len(offsets)):
            # Extract the second word (bytes 4-8)
            second_word = int.from_bytes(
                self.mm[offsets[i] + 4:offsets[i] + 8],
                byteorder='little' if self.endian == '<' else 'big'
            )
            signatures[i] = second_word >> 16

        # Create a mask for events that match our signature
        mask = signatures == (signature & 0xFFFF)
        matching_offsets = offsets[mask]
        matching_lengths = lengths[mask]

        if len(matching_offsets) == 0:
            return np.array([], dtype=dtype)

        # Calculate the total size and preallocate array
        total_words = np.sum(matching_lengths) // 4
        result = np.zeros(total_words, dtype=dtype)

        # Copy each matching event's data into the result array
        current_idx = 0
        for i in range(len(matching_offsets)):
            offset = matching_offsets[i]
            length = matching_lengths[i]
            words = length // 4

            # Use np.frombuffer for efficient memory-to-array conversion
            event_data = np.frombuffer(
                self.mm[offset:offset + length],
                dtype=dtype
            )

            result[current_idx:current_idx + words] = event_data
            current_idx += words

        return result
    def get_hex_dump(self, word_count: int = 14, title: Optional[str] = None) -> str:
        """
        Generate a hex dump of the record header.

        Args:
            word_count: Number of 32-bit words to include in the dump
            title: Optional title for the hex dump

        Returns:
            String containing formatted hexdump
        """
        data = self.mm[self.offset:self.offset + min(word_count * 4, self.size)]
        return make_hex_dump(data, title=title or f"Record Header at offset 0x{self.offset:X}")

    def __str__(self) -> str:
        """Return a string representation of this record."""
        return f"Record at offset 0x{self.offset:X} with {self.event_count} events"

    def __repr__(self) -> str:
        """Return a string representation for debugging."""
        return f"Record(offset=0x{self.offset:X}, size={self.size}, events={self.event_count})"