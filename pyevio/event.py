import mmap
import struct
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime
from pyevio.bank import Bank  # Import here to avoid circular import

from pyevio.utils import make_hex_dump


class Event:
    """
    Represents an event within a record in an EVIO file.

    An event contains a bank structure with a hierarchy of data.
    This class provides methods to access and parse the event
    structure efficiently.
    """

    def __init__(self, mm: mmap.mmap, offset: int, length: int, endian: str = '<', index: int = -1):
        """
        Initialize an Event object.

        Args:
            mm: Memory-mapped file containing the event
            offset: Byte offset in the file where the event starts
            length: Length of the event in bytes
            endian: Endianness ('<' for little endian, '>' for big endian)
            index: Event index within its parent record (for reference)
        """
        self.mm = mm
        self.offset = offset
        self.length = length
        self.endian = endian
        self.index = index

        # End offset for the event
        self.end_offset = offset + length

        # Root bank (will be parsed on demand)
        self._root_bank = None
        self._bank_info = None

    def get_bank_info(self) -> Dict[str, Any]:
        """
        Get information about the root bank of this event.

        Returns:
            Dictionary containing bank information
        """
        if self._bank_info is None:
            try:
                # Parse first two words to identify bank type
                first_word = struct.unpack(self.endian + 'I',
                                           self.mm[self.offset:self.offset+4])[0]
                second_word = struct.unpack(self.endian + 'I',
                                            self.mm[self.offset+4:self.offset+8])[0]

                # Extract bank information
                bank_length = first_word
                tag = (second_word >> 16) & 0xFFFF
                data_type = (second_word >> 8) & 0xFF
                num = second_word & 0xFF

                # Determine bank type based on tag and data_type
                bank_type = "Unknown"
                if data_type == 0x10:
                    bank_type = "ROC Time Slice Bank"
                elif (tag & 0xFF00) == 0xFF00:
                    tag_type = tag & 0x00FF
                    if (tag_type & 0x10) == 0x10:
                        bank_type = "ROC Raw Data Record"
                    elif tag_type == 0x30:
                        bank_type = "ROC Time Slice Bank"
                    elif tag_type == 0x31:
                        bank_type = "Physics Event"

                self._bank_info = {
                    "length": bank_length,
                    "tag": tag,
                    "data_type": data_type,
                    "num": num,
                    "bank_type": bank_type,
                    "offset": self.offset
                }
            except Exception as e:
                self._bank_info = {"error": str(e)}

        return self._bank_info

    def get_bank(self)->Bank:
        """
        Get the root bank of this event.

        Returns:
            Bank object for this event's root bank
        """
        if self._root_bank is None:
            # The root bank may not reach past the event's bytes (its length
            # word is file data); the event end is the limit.
            self._root_bank = Bank.from_buffer(self.mm, self.offset, self.endian, self.end_offset)

            # For special bank types, return appropriate subclass
            bank_info = self.get_bank_info()

            if bank_info.get("bank_type") == "ROC Time Slice Bank":
                from pyevio.roc_time_slice_bank import RocTimeSliceBank
                try:
                    self._root_bank = RocTimeSliceBank(self.mm, self.offset, self.endian, self.end_offset)
                except Exception as e:
                    # If parsing as specialized bank fails, fall back to generic Bank
                    pass

        return self._root_bank

    def is_roc_time_slice_bank(self) -> bool:
        """
        Check if this event contains a ROC Time Slice Bank.

        Returns:
            True if this event contains a ROC Time Slice Bank, False otherwise
        """
        bank_info = self.get_bank_info()
        return bank_info.get("bank_type") == "ROC Time Slice Bank"

    def get_hex_dump(self, max_bytes: int = 256, title: Optional[str] = None) -> str:
        """
        Generate a hex dump of the event data.

        Args:
            max_bytes: Maximum number of bytes to include in the dump
            title: Optional title for the hex dump

        Returns:
            String containing formatted hexdump
        """
        display_len = min(max_bytes, self.length)
        data = self.mm[self.offset:self.offset + display_len]
        return make_hex_dump(data, title=title or f"Event Data at offset 0x{self.offset:X}")

    def get_data(self) -> bytes:
        """
        Get the raw data for this event.

        Returns:
            Bytes object containing the raw event data
        """
        return self.mm[self.offset:self.end_offset]

    def __str__(self) -> str:
        """Return a string representation of this event."""
        bank_info = self.get_bank_info()
        bank_type = bank_info.get("bank_type", "Unknown")
        tag = bank_info.get("tag", 0)
        return f"Event {self.index} at offset 0x{self.offset:X}, length {self.length} bytes, type {bank_type} (0x{tag:04X})"

    def __repr__(self) -> str:
        """Return a string representation for debugging."""
        return f"Event(offset=0x{self.offset:X}, length={self.length}, index={self.index})"