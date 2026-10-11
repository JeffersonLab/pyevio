import mmap
import os
import struct
from typing import List, Tuple, Optional, Dict, Any, Iterator
from datetime import datetime

from pyevio.file_header import FileHeader, detect_endian, file_type_name, unsupported_version_error
from pyevio.record import Record, padded_to_words
from pyevio.record_header import RecordHeader
from pyevio.event import Event


class EvioFile:
    """
    Main class for handling EVIO v6 files.

    Manages file resources and provides methods for navigating through
    the file structure using Records and Events.

    Records: ``record_count``, ``get_record()`` and ``iter_records()`` cover
    the data records. The trailer record that closes a file (header species
    3) holds no events and is exposed separately as ``trailer`` (None when
    the file ends in a plain record).

    Layouts: ``header_layout`` names how the file starts. ``"v6"`` is a
    14-word file header followed by 14-word record headers. Files without a
    file header (EVIO v4 blocks, v1—3 blocks) raise ``ValueError`` naming
    the version at open.

    Checks: ``checks`` lists what the record scan found wrong with the file
    as tuples whose first item names the check: ``("truncated_tail",
    declared_bytes, present_bytes, events_salvaged)`` for a final record
    that runs past the end of the file (its complete events are kept),
    ``("bad_record_header", offset, message)`` where the scan stopped on
    bytes that are no record header.

    Resources: ``close()`` (or the ``with`` block) releases the mmap and the
    file. A numpy array or memoryview that still refers to the mmap makes
    ``close()`` raise ``BufferError`` after closing the file; drop the views
    and call ``close()`` again.
    """

    def __init__(self, filename: str, verbose: bool = False):
        """
        Initialize EvioFile object with a file path.

        Args:
            filename: Path to the EVIO file
            verbose: Enable verbose output

        Raises:
            ValueError: the file is not EVIO, or an EVIO version pyevio does not read
        """
        self.verbose = verbose
        self.filename = filename
        self.file = open(filename, 'rb')
        self.file_size = os.path.getsize(filename)

        # Memory map the file for efficient access
        self.mm = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)

        self.header_layout, self.header = self._detect_layout()

        # The first record follows the file header, its index array and its
        # user header (dictionary, first event), which is stored padded to
        # whole words although the header gives the unpadded byte count.
        self.first_record_offset = (self.header.header_length * 4
                                    + self.header.index_array_length
                                    + padded_to_words(self.header.user_header_length))

        # One Record is cached: the one accessed last. A Record owns its
        # decompressed buffer and Event objects, so caching every record
        # would hold a whole compressed file in memory.
        self._cached_record: Optional[Record] = None
        self._trailer = None

        # Scan record offsets
        self._trailer_offset: Optional[int] = None
        self.checks: List[tuple] = []
        self._record_offsets = self._scan_record_offsets()

        # Total event count cache
        self._total_event_count = None

    def _detect_layout(self) -> Tuple[str, FileHeader]:
        """Decide how the file starts and parse its header.

        Every EVIO header layout carries the magic word at word 7, so the byte
        order comes from there. A file type id in word 0 means a v6 file
        header. Anything else is a bare block or record header at offset 0:
        its version sits in the low byte of word 5. Only version 6 with a
        file header is read here; other versions raise the one unsupported-
        version error.
        """
        endian = detect_endian(self.mm, 0)
        if endian is None:
            raise ValueError(f"Not an EVIO file: no magic number 0x{RecordHeader.MAGIC_NUMBER:08x} "
                             f"at word 7 of {self.filename}")

        first_word = struct.unpack(endian + 'I', self.mm[0:4])[0]
        if file_type_name(first_word) is not None:
            return "v6", FileHeader.from_buffer(self.mm, 0)

        block_version = struct.unpack(endian + 'I', self.mm[20:24])[0] & 0xFF
        raise unsupported_version_error(block_version, f"block header at offset 0, no file header: {self.filename}")

    def close(self) -> None:
        """Release the mmap and the file.

        The file is closed whatever happens to the mmap. ``mmap.close()``
        raises ``BufferError`` while a numpy array or memoryview exported
        from it is alive; that error is re-raised with the cause named, and
        the mmap stays open until those views are dropped and ``close()`` is
        called again.
        """
        mm = getattr(self, 'mm', None)
        file = getattr(self, 'file', None)
        try:
            if mm is not None and not mm.closed:
                mm.close()
        except BufferError as error:
            raise BufferError(f"cannot close the mmap of {self.filename}: a numpy array or memoryview "
                              f"still refers to it; drop those views, then call close() again") from error
        finally:
            if file is not None and not file.closed:
                file.close()

    def __del__(self):
        """Release resources at garbage collection; errors are silent here."""
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        """Support for context manager protocol"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release resources; a BufferError from a live view propagates (see close)."""
        self.close()

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
                record = Record(self.mm, offset, self.header.endian)
            except ValueError as error:
                # Bytes that are no record header end the scan (F6 tightens this).
                self.checks.append(("bad_record_header", offset, str(error)))
                break

            # The trailer closes the file: it lists records, holds no events
            # and is exposed as `trailer`, never as a data record.
            if record.header.is_trailer:
                self._trailer_offset = offset
                break

            # A record shorter than its header (or an 8-word-short header)
            # cannot be stepped over: the scan would stand still.
            if record.header.header_length < 8 or record.size < record.header_size:
                self.checks.append(("bad_record_header", offset,
                                    f"record declares {record.header.record_length} words "
                                    f"with a {record.header.header_length}-word header"))
                break

            record_offsets.append(offset)

            # A final record cut by the end of the file: keep it, walk its
            # complete events and report the byte counts.
            if record.is_truncated:
                record.index = len(record_offsets) - 1
                salvaged = len(record.scan_events())
                self.checks.append(("truncated_tail", record.declared_end - offset, self.file_size - offset, salvaged))
                break

            offset += record.size

            # Stop if this was the last record. Some CODA writers set the
            # last-record bit on every record, so keep going if another
            # record header follows.
            if record.header.is_last_record and not self._is_record_at(offset):
                break

        return record_offsets

    @property
    def trailer(self) -> Optional[Record]:
        """The trailer record that closes the file, or None when the file ends in a plain record."""
        if self._trailer_offset is None:
            return None
        if self._trailer is None:
            self._trailer = Record(self.mm, self._trailer_offset, self.header.endian)
        return self._trailer

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

        if self._cached_record is None or self._cached_record.index != index:
            self._cached_record = Record(self.mm, self._record_offsets[index], self.header.endian, index=index)

        return self._cached_record

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
            self._total_event_count = sum(self.record_event_count(record) for record in self.iter_records())

        return self._total_event_count

    @staticmethod
    def record_event_count(record: Record) -> int:
        """Events a record contributes: the header count, or the complete events present in a truncated tail."""
        if record.is_truncated:
            return len(record.scan_events())
        return record.event_count

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
            event_count = self.record_event_count(record)

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
        _, event = self.get_record_and_event(global_index)
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
