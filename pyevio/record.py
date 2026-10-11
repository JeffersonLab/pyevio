import mmap
import struct
import zlib
from typing import List, Tuple, Optional, Union

import lz4.block
import numpy as np

from pyevio.record_header import RecordHeader
from pyevio.utils import make_hex_dump


def padded_to_words(length_bytes: int) -> int:
    """A byte length rounded up to whole 32-bit words, as user headers are stored."""
    return (length_bytes + 3) & ~3


class Record:
    """
    Represents a record in an EVIO file.

    A record contains a header followed by an event index array, optional user header,
    and multiple events. This class provides methods to access and parse the record
    structure efficiently.

    Buffers: ``mm`` is the file; ``buffer`` is where the index array, user
    header and events live. For an uncompressed record ``buffer`` is ``mm``
    and all offsets are file offsets. For a compressed record (LZ4, LZ4_BEST
    or gzip, header word 10) ``buffer`` is the decompressed bytes, built on
    first access, and ``index_array_start``, ``data_start``, ``data_end`` and
    every event offset are offsets into that buffer. Read event bytes through
    ``buffer``, never through ``mm``.
    """

    COMPRESSION_NONE = 0
    COMPRESSION_LZ4 = 1
    COMPRESSION_LZ4_BEST = 2
    COMPRESSION_GZIP = 3

    def __init__(self, mm: mmap.mmap, offset: int, endian: str = '<', index=-1):
        """
        Initialize a Record object.

        Args:
            mm: Memory-mapped file containing the record
            offset: Byte offset in the file where the record starts
            endian: Endianness ('<' for little endian, '>' for big endian)
            index: Record index within the file (for reference)
        """
        self.mm = mm
        self.offset = offset
        self.endian = endian
        self.index = index

        # Parse the record header
        self.header = RecordHeader.parse(mm, offset)

        self.header_size = self.header.header_length * 4
        self.size = self.header.record_length * 4
        self.compression_type = self.header.compression_type

        # Declared record end against the file: a record that runs past the
        # end of the file is truncated, and reads stop at the present bytes.
        self.declared_end = self.offset + self.size
        self.file_end = min(self.declared_end, len(mm))
        self.is_truncated = self.declared_end > len(mm)

        # Why an event walk stopped early (short strings with offsets); empty
        # when every event length fit inside the record.
        self.errors: List[str] = []

        # The user header is stored padded to whole words; the unpadded length
        # in the header word shifts every event by the padding otherwise.
        user_header_padded = padded_to_words(self.header.user_header_length)

        self._buffer = None
        if self.compression_type == self.COMPRESSION_NONE:
            self._buffer = mm
            self.index_array_start = self.offset + self.header_size
            self.index_array_end = self.index_array_start + self.header.index_array_length
            self.data_start = self.index_array_end + user_header_padded
            self.data_end = self.file_end
        else:
            # Layout of the decompressed buffer: index array, user header, events.
            self.index_array_start = 0
            self.index_array_end = self.header.index_array_length
            self.data_start = self.index_array_end + user_header_padded
            self.data_end = self.data_start + self.header.uncompressed_data_length

        # Cache for events (will be populated on demand)
        self._events = None
        self._event_info = None

    @property
    def buffer(self) -> Union[mmap.mmap, bytes]:
        """The buffer that holds the index array, user header and events (see class docstring)."""
        if self._buffer is None:
            self._buffer = self._decompress()
        return self._buffer

    def release(self) -> None:
        """Drop the decompressed buffer and the Event cache; the next access rebuilds them."""
        if self.compression_type != self.COMPRESSION_NONE:
            self._buffer = None
        self._events = None

    def _decompress(self) -> bytes:
        """Decompress index array + user header + events into one bytes buffer.

        Header facts (verified on the evio C++ and Java writers): the
        compressed region starts right after the header; word 10 holds the
        compression type in bits 28—31 and the compressed length in words
        below; the last word carries pad3 (bits 24—25 of word 5) filler bytes
        that are not part of the stream. LZ4 and LZ4_BEST are the LZ4 block
        format (no frame) and need the decompressed size up front: word 9
        counts the events only, so the index array and padded user header are
        added to it.
        """
        compressed_words = self.header.compressed_data_length
        compressed_start = self.offset + self.header_size
        compressed_end = compressed_start + compressed_words * 4 - self.header.pad3
        if compressed_end > self.file_end:
            self.errors.append(f"compressed data declares {compressed_words} words at 0x{compressed_start:X}, "
                               f"record ends at 0x{self.file_end:X}")
            compressed_end = self.file_end
        compressed = self.mm[compressed_start:compressed_end]

        try:
            if self.compression_type in (self.COMPRESSION_LZ4, self.COMPRESSION_LZ4_BEST):
                decompressed = lz4.block.decompress(compressed, uncompressed_size=self.data_end)
            elif self.compression_type == self.COMPRESSION_GZIP:
                decompressed = zlib.decompress(compressed, wbits=47)  # gzip or zlib wrapper
            else:
                self.errors.append(f"unknown compression type {self.compression_type}")
                decompressed = b""
        except (lz4.block.LZ4BlockError, zlib.error, ValueError) as error:
            self.errors.append(f"decompression failed (type {self.compression_type}): {error}")
            decompressed = b""

        if len(decompressed) < self.data_end:
            self.errors.append(f"decompressed {len(decompressed)} bytes, header declares {self.data_end}")
            self.data_end = len(decompressed)
        return decompressed

    @property
    def event_count(self) -> int:
        """Number of events the header declares. A trailer declares 0."""
        return self.header.event_count

    @property
    def is_trailer(self) -> bool:
        """True for the trailer record that closes a file; it holds no events."""
        return self.header.is_trailer

    def scan_events(self) -> List[Tuple[int, int]]:
        """
        Scan and parse all events in this record.

        Returns:
            List of tuples (offset, length) for all events in the record;
            offsets are into ``buffer`` (file offsets for uncompressed records)
        """
        if self._event_info is None:
            self._event_info = self._scan_events()
        return self._event_info

    def _scan_events(self) -> List[Tuple[int, int]]:
        # A trailer's index array lists records, never events: reading it as
        # event lengths yields ghost events past the end of the file.
        if self.header.is_trailer:
            return []

        buffer = self.buffer
        event_info = []
        current_offset = self.data_start

        if self.header.index_array_length > 0:
            # The index array holds one byte length per event. The header's
            # event count and the index size are two claims about the same
            # number; trust neither beyond the other, nor beyond the bytes
            # present (a truncated record may cut the index array itself).
            index_entries = self.header.index_array_length // 4
            index_present = max(0, min(self.index_array_end, len(buffer)) - self.index_array_start) // 4
            event_count = min(self.header.event_count, index_entries, index_present)

            for event_i in range(event_count):
                length_offset = self.index_array_start + event_i * 4
                event_length = struct.unpack(self.endian + 'I', buffer[length_offset:length_offset + 4])[0]
                if event_length < 8 or current_offset + event_length > self.data_end:
                    self.errors.append(f"event {event_i} at 0x{current_offset:X} declares {event_length} bytes, "
                                       f"record data ends at 0x{self.data_end:X}")
                    break
                event_info.append((current_offset, event_length))
                current_offset += event_length

        else:
            # No index array: walk events by the length word of each event
            # bank (length + 1 words, see Bank). Stop at the header's event
            # count or at the end of the record data, whichever comes first.
            while len(event_info) < self.header.event_count:
                if current_offset + 8 > self.data_end:
                    self.errors.append(f"event {len(event_info)} at 0x{current_offset:X} has no room for a bank header, "
                                       f"record data ends at 0x{self.data_end:X}")
                    break
                bank_length = struct.unpack(self.endian + 'I', buffer[current_offset:current_offset + 4])[0]
                event_length = (bank_length + 1) * 4
                if bank_length == 0 or current_offset + event_length > self.data_end:
                    self.errors.append(f"event {len(event_info)} at 0x{current_offset:X} declares {event_length} bytes, "
                                       f"record data ends at 0x{self.data_end:X}")
                    break
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
            self._events = [
                Event(self.buffer, offset, length, self.endian, i)
                for i, (offset, length) in enumerate(self.scan_events())
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
            List of tuples (offset, length) for each event; offsets are into ``buffer``
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

    def _second_word(self, offset: int) -> int:
        return struct.unpack(self.endian + 'I', self.buffer[offset + 4:offset + 8])[0]

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

        if signature is not None:
            # Filter events by the tag in the second word (every event has 8+ bytes)
            signature_mask = signature & 0xFFFF
            event_info = [(offset, length) for offset, length in event_info
                          if (self._second_word(offset) >> 16) == signature_mask]

        if not event_info:
            return np.array([], dtype=np.uint32)

        dtype = np.dtype(np.uint32).newbyteorder(self.endian)
        buffer = self.buffer

        if event_size_words is not None:
            # All events have the same size, we can create a regular 2D array
            result = np.zeros((len(event_info), event_size_words), dtype=np.uint32)

            for i, (offset, length) in enumerate(event_info):
                # Never read past the event: a shorter event leaves zeros.
                word_count = min(event_size_words, length // 4)
                result[i, :word_count] = np.frombuffer(buffer, dtype=dtype, count=word_count, offset=offset)

            return result
        else:
            # Variable-sized events, create a list of arrays
            event_arrays = [np.frombuffer(buffer, dtype=dtype, count=length // 4, offset=offset)
                            for offset, length in event_info]

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

    def events_to_numpy(self, signature: int = 0xFF60, start_event: Optional[int] = None, end_event: Optional[int] = None, dtype=np.uint32) -> np.ndarray:
        """
        Concatenate the words of every event whose tag matches the signature into one NumPy array.

        Args:
            signature: Integer signature to match in the second word of events (default: 0xFF60)
            start_event: Starting event index (default: 0)
            end_event: Ending event index, exclusive (default: all events)
            dtype: NumPy data type for the resulting array (default: np.uint32)

        Returns:
            NumPy array containing all event data matching the signature
        """
        signature_mask = signature & 0xFFFF
        matching = [(offset, length) for offset, length in self.get_event_offsets(start_event, end_event)
                    if (self._second_word(offset) >> 16) == signature_mask]

        if not matching:
            return np.array([], dtype=dtype)

        total_words = sum(length // 4 for _, length in matching)
        result = np.zeros(total_words, dtype=dtype)
        buffer = self.buffer
        source_dtype = np.dtype(np.uint32).newbyteorder(self.endian)

        current_idx = 0
        for offset, length in matching:
            words = length // 4
            result[current_idx:current_idx + words] = np.frombuffer(buffer, dtype=source_dtype, count=words, offset=offset)
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
        kind = "Trailer" if self.is_trailer else "Record"
        return f"{kind} at offset 0x{self.offset:X} with {self.event_count} events"

    def __repr__(self) -> str:
        """Return a string representation for debugging."""
        return f"Record(offset=0x{self.offset:X}, size={self.size}, events={self.event_count})"
