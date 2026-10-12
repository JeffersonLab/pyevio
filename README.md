# pyevio

A Python library and command-line tool for reading and introspecting EVIO (Event Input/Output) files from Jefferson Lab.

## Overview

`pyevio` provides both a Python API and a command-line interface for:

- Reading and parsing EVIO v6 files efficiently
- Handling both compressed and uncompressed records
- Inspecting file structure, records, and events
- Navigating hierarchical bank structures
- Converting data to NumPy arrays for analysis
- Specialized handling of ROC Time Slice Banks with fADC250 data

## Installation

```bash
pip install pyevio
```

## Requirements

- Python 3.9+
- Dependencies (automatically installed):
   - click
   - lz4
   - rich
   - numpy
   - matplotlib

## Command-Line Usage

The `pyevio` command provides several subcommands for file analysis:

### Basic File Information

```bash
# Display file metadata
pyevio info sample.evio

# Or simply (default command is info)
pyevio sample.evio
```

This shows general file information including:
- Magic number and format version
- Endianness
- Record count
- User header details
- Record summary

### Examining Records

```bash
# Detailed view of record #5
pyevio record sample.evio 5

# Show all records without truncation
pyevio info sample.evio --full

# Show hexdump of file header
pyevio info sample.evio --hexdump
```

### Inspecting Record Structure

```bash
# Dump structure of record #3
pyevio dump sample.evio 3

# Limit tree display depth
pyevio dump sample.evio 3 --depth=3

# Show data preview (first 5 elements)
pyevio dump sample.evio 3 --preview=5

# Include hex dumps of data
pyevio dump sample.evio 3 --hexdump
```

### Examining Individual Events

```bash
# Display event #2 from record #3
pyevio event sample.evio 2 --record=3

# Alternatively, access by global event index
pyevio event sample.evio 25

# Show event data as hex dump
pyevio event sample.evio 2 --record=3 --hexdump
```

### Advanced Debugging

```bash
# Debug record #1
pyevio debug sample.evio --record=1

# Focus on a specific event within the record
pyevio debug sample.evio --record=1 --event=0

# Focus on a specific payload (for ROC Time Slice Banks)
pyevio debug sample.evio --record=1 --payload=0

# Show hex dumps
pyevio debug sample.evio --record=1 --hexdump
```

### Raw Hexadecimal View

```bash
# Display 30 words (default) from byte offset 0x100
pyevio hex sample.evio 0x100 --bytes

# Display 50 words starting at word 200
pyevio hex sample.evio 200 --size=50

# Specify endianness
pyevio hex sample.evio 0x100 --bytes --endian='>'
```

### Experimental UI

```bash
# Launch textual UI (requires textual package)
pyevio ui sample.evio
```

### GUI Viewer

```bash
# Serve the viewer at http://localhost:8765, file browser starts in /path/to/runs
pyevio gui /path/to/runs

# File-based EVIO is experimental: skip the streaming decode, show structure tree only
pyevio gui /path/to/runs --mode=file
```

Browse a file in the browser: file checks, channel occupancy, rate, charge and timing plots for CODA streaming fADC250 data, and a structure tree down to single hit words. Decoding runs in the browser. Only CODA streaming fADC250 EVIO is tested; file-based EVIO may not decode well yet. See [docs/web_viewer.md](docs/web_viewer.md).

![GUI viewer overview](docs/images/web_viewer_overview.png)

## Understanding EVIO File Structure

EVIO files are organized hierarchically:

1. **File Header**: Contains metadata about the file
2. **Records**: Containers for events (may be compressed in v6)
3. **Events**: Contains banks of data
4. **Banks**: Hierarchical containers that can hold other banks or data
   - **Container Banks**: Hold other banks
   - **Data Banks**: Hold actual data (integers, floats, etc.)

Special bank types include:
- **ROC Time Slice Banks** (tag 0xFF30): Contain fADC250 data with timestamps
- **Physics Events** (tag 0xFF31): Contain physics data
- **ROC Raw Data Records** (tag & 0xFF10): Contains raw detector data

## Python API Usage

For more advanced use cases, you can use the Python API:

```python
from pyevio import EvioFile
import numpy as np

# Open an EVIO file
with EvioFile("sample.evio") as evio_file:
    # Get file info
    print(f"Records: {evio_file.record_count}")
    print(f"Events: {evio_file.get_total_event_count()}")
    
    # Get a specific record
    record = evio_file.get_record(3)
    
    # Get events from the record
    events = record.get_events()
    
    # Process an event
    event = events[0]
    bank = event.get_bank()
    
    # For ROC Time Slice Banks
    if event.is_roc_time_slice_bank():
        ts_bank = event.get_bank()
        timestamp = ts_bank.get_formatted_timestamp()
        print(f"Timestamp: {timestamp}")
        
        # Get waveform data
        waveform_data = ts_bank.get_payload_data(0)
        
        # Convert to NumPy
        np_data = np.array(waveform_data)
```

## License

[License information]

## Contributors

[Contributor information]