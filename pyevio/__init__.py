"""
pyevio - A Python library for reading and introspecting EVIO v6 files.
"""

# Export core classes
from pyevio.evio_file import EvioFile
from pyevio.file_header import FileHeader
from pyevio.record_header import RecordHeader
from pyevio.bank import Bank
from pyevio.roc_time_slice_bank import RocTimeSliceBank, StreamInfoBank, PayloadBank

# Export utility functions
from pyevio.utils import make_hex_dump


__version__ = "1.0.0"
