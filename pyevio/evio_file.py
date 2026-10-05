import mmap
import os
import struct
from typing import List, Tuple, Optional, Dict, Any, Iterator
from datetime import datetime

from pyevio.file_header import FileHeader
from pyevio.record import Record
from pyevio.record_header import RecordHeader
from pyevio.event import Event


class EvioFile:
    """
    Main class for handling EVIO v6 files.

    Manages file resources and provides methods for navigating through
    the file structure using Records and Events.
    """

    def __init__(self, filename: str, verbose: bool = False):
        """
        Initialize EvioFile object with a file path.

        Args:
            filename: Path to the EVIO file
            verbose: Enable verbose output
        """
        self.verbose = verbose
        self.filename = filename
        self.file = open(filename, 'rb')
        self.file_size = os.path.getsize(filename)

        # Memory map the file for efficient access
        self.mm = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)

        # Parse file header
        self.header = FileHeader.from_buffer(self.mm, 0)

        # Calculate initial offset after file header
        self.first_record_offset = self.header.header_length * 4

        # Skip index array if present
        if self.header.index_array_length > 0:
            self.first_record_offset += self.header.index_array_length

        # Skip user header if present
        if self.header.user_header_length > 0:
            self.first_record_offset += self.header.user_header_length

        # Record objects (will be created on demand)
        self._records = {}

        # Scan record offsets
        self._record_offsets = self._scan_record_offsets()

        # Total event count cache
        self._total_event_count = None

    def __del__(self):
        """Cleanup resources when object is destroyed"""
        try:
            if hasattr(self, 'mm') and self.mm and not getattr(self.mm, 'closed', True):
                self.mm.close()
            if hasattr(self, 'file') and self.file and not self.file.closed:
                self.file.close()
        except (ValueError, AttributeError, BufferError):
            # Ignore errors during cleanup
            pass

    def __enter__(self):
        """Support for context manager protocol"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Cleanup when exiting context"""
        self.__del__()

    def _scan_record_offsets(self) -> List[int]:
        """
        Scan the file to find all record offsets.

        Returns:
            List of record offsets in bytes
        """
        record_offsets = []
        offset = self.first_record_offset

        while offset < self.file_size:
            try:
                # Store this record's position
                record_offsets.append(offset)

                # Create a temporary Record object to get its size
                record = Record(self.mm, offset, self.header.endian)

                # Move to next record
                offset += record.size

                # Stop if this was the last record. Some CODA writers set the
                # last-record bit on every record, so keep going if another
                # record header follows.
                if record.header.is_last_record and not self._is_record_at(offset):
                    break

            except Exception as e:
                if self.verbose:
                    print(f"Error scanning record at offset 0x{offset:X}: {e}")
                raise

        return record_offsets

    def _is_record_at(self, offset: int) -> bool:
        """Check whether a record header (magic word in word 7) starts at offset."""
        if offset + RecordHeader.HEADER_SIZE > self.file_size:
            return False
        magic = struct.unpack(self.header.endian + 'I', self.mm[offset + 28:offset + 32])[0]
        return magic == RecordHeader.MAGIC_NUMBER

    @property
    def record_count(self) -> int:
        """Get the number of records in this file."""
        return len(self._record_offsets)

    def get_record(self, index: int) -> Record:
        """
        Get a record by index.

        Args:
            index: Record index (0-based)

        Returns:
            Record object

        Raises:
            IndexError: If index is out of range
        """
        if index < 0 or index >= len(self._record_offsets):
            raise IndexError(f"Record index {index} out of range (0-{len(self._record_offsets)-1})")

        # Check if record is already cached
        if index not in self._records:
            offset = self._record_offsets[index]
            self._records[index] = Record(self.mm, offset, self.header.endian, index=index)

        return self._records[index]

    def get_records(self) -> List[Record]:
        """
        Get all records in the file.

        Returns:
            List of Record objects
        """
        return [self.get_record(i) for i in range(len(self._record_offsets))]

    def iter_records(self) -> Iterator[Record]:
        """
        Iterate through all records in the file.

        Yields:
            Record objects
        """
        for i in range(len(self._record_offsets)):
            yield self.get_record(i)

    def get_total_event_count(self) -> int:
        """
        Get the total number of events across all records.

        Returns:
            Total event count
        """
        if self._total_event_count is None:
            self._total_event_count = 0
            for record in self.iter_records():
                self._total_event_count += record.event_count

        return self._total_event_count

    def get_record_and_event(self, global_index: int) -> Tuple[Record, Event]:
        """
        Get an event by global index across all records.

        Args:
            global_index: Global event index (0-based) across all records

        Returns:
            Tuple of (Record, Event) objects

        Raises:
            IndexError: If global_index is out of range
        """
        if global_index < 0:
            raise IndexError(f"Global event index {global_index} cannot be negative")

        # Scan through records to find the record containing this event
        current_index = 0

        for record_idx in range(len(self._record_offsets)):
            record = self.get_record(record_idx)
            event_count = record.event_count

            # Check if this event is in the current record
            if current_index + event_count > global_index:
                # Found the record containing this event
                local_event_index = global_index - current_index
                event = record.get_event(local_event_index)
                return record, event

            # Move to next record
            current_index += event_count

        # If we get here, the global_index is out of range
        raise IndexError(f"Global event index {global_index} out of range (0-{current_index-1})")

    def get_event(self, global_index: int) -> Event:
        """Gets event by the global index, automatically finds record, where it is located

        Description:
            - Same as `get_record_and_event`, but instead of tuple only returns the event object
            - If you need to get a bulk of events, this might be not the best performance for this

        Returns: found events
        """
        _, _, event, _ = self.get_record_and_event(global_index)
        return event

    def iter_events(self) -> Iterator[Tuple[Record, Event]]:
        """
        Iterate through all events in the file.

        Yields:
            Tuples of (Record, Event) objects
        """
        for record in self.iter_records():
            for event in record.get_events():
                yield record, event
