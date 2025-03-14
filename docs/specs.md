# **pyevio - EVIO v6 Reader Specification (v1.0)**

**A Python library and CLI toolkit for reading/introspecting EVIO v6 files**

**Goal**:  
Create a pure-Python package (**pyevio**) for **reading** and **introspecting** **non-compressed
EVIO v6** files, with optional **NumPy** conversion of certain data banks (“ROC Time Slice Bank”).
Provide a command-line interface (CLI) for file and record-level inspection.


---

## **1. Core Objectives**

- Read non-compressed EVIO v6 files efficiently (GB+ scale)
- Provide human-friendly CLI inspection tools
- Convert "ROC Time Slice Bank" data to NumPy arrays
- Zero-write capability in v1 (read-only implementation)

---

## **2. Technical Specifications**

### **2.1 File Format Support**

- **EVIO Version**: Strictly v6 (magic number `0x4556494F`)
- **Compression**: None supported initially
- **Endianness**: Auto-detected from file header
- **Special Handling**:
    - ROC Time Slice Bank ( *0xFF30*) with FADC250 data
    - ROC Raw Data Record 0xFF1X
    - 64-bit timestamp extraction
    - 16-bit waveform data arrays

## **Functional Requirements**

1. **File Header Parsing**

    - Support **EVIO v6** header structure, including:
        - File Type ID, File Number, Header Length, Record Count, Index Array Length, Bit Info, User
          Header Length, Magic Number, User Register (64 bits), Trailer Position (64 bits), User
          Integers.
    - Validate **Magic Number** (“EVIO”) and **Version** (must be 6).
    - Provide a method to read & expose these fields as Python attributes.
2. **Record Parsing**

    - Identify each record’s **header** and basic metadata (length in 32-bit words, data type, bit
      info, etc.).
    - Maintain offsets for each record to allow **sequential** or **random access**.
3. **Bank & Sub-Bank Hierarchy**

    - Parse nested **Banks/Segments/TagSegments**.
    - Maintain a **tree-like** object structure reflecting the nested hierarchy.
    - Expose key fields (`tag`, `data_type`, `length`, offsets).
4. **ROC Time Slice Bank Support**

    - Specifically handle banks with known tags (e.g., `0x10C0`) containing FADC250 data.
    - Extract timestamps and channel data in a structured way.
5. **CLI Tools**

    - **`pyevio info FILE`**
        - Show **file header** details, record count, and offset/length information for each record.
    - **`pyevio dump FILE N`**
        - **Tree-like** dump of record N, including tags, data types, lengths, offsets.
        - Optional data previews (e.g., first 3–5 elements for numeric arrays).
        - Support indentation and ANSI color (toggleable).
6. **NumPy Conversion**

    - Provide an **API** to read bank data as a NumPy array (`bank.to_numpy()`).
    - Handle known data types (e.g., `int32`, `uint32`, `float32`) via a lookup table.
    - For large data, parse **lazily** (using `mmap`/`memoryview` to avoid full-file loading).

---

## **Non-Functional Requirements**

1. **Performance**

    - **Mmap-based** reading for zero-copy I/O.
    - **Incremental** parsing of records (do not load the entire file at once).
    - Avoid Python loops for large numeric data—use `numpy.frombuffer` wherever possible.
2. **Memory Usage**

    - Must handle **gigabyte-scale** files without excessive memory overhead.
    - Keep data in `memoryview` or `mmap` slices until explicitly requested for conversion.
3. **Compatibility**

    - **Operating Systems**: Linux, macOS, Windows.
    - **Python Versions**: 3.8+ (test in 3.8, 3.9, 3.10, 3.11, etc.).
4. **Extensibility**

    - Clean internal APIs (e.g., distinct classes for header, record, bank).
    - Subcommands for CLI are modular, allowing future addition.

-

---

## **Distribution & Versioning**

1. **PyPI Package**:
    - Distributed as `pyevio` (`pip install pyevio`).
2. **Semantic Versioning**:
    - **Major.Minor.Patch** (e.g., `1.0.0` for initial stable release).
3. **Release Process**:
    - Tag in GitHub triggers CI build & PyPI publish.

## **Documentation**

### **Structure**

- **`docs/`** folder with Markdown files:
    - **`getting_started.md`**: Installation, basic usage steps, environment requirements.
    - **`cli_reference.md`**: Detailed subcommand usage, flags, examples.
    - **`api_reference.md`**: Explanation of major classes (`FileHeader`, `RecordHeader`, `Bank`)
      and methods.
    - **`internals.md`**: EVIO v6 binary structure, design rationale, extension points.

### **Rendering & Publishing**

- Use a static site generator (e.g., **VitePress**) to build from Markdown.
- Deploy to GitHub Pages on release tags.

### **Architecture**

```
pyevio/
├── parser.py       # mmap-based low-level parsing
├── headers.py      # FileHeader/RecordHeader implementation
├── banks.py        # Bank hierarchy handling
└── exceptions.py   # Custom error classes
├── utils/
│   ├── convert.py      # NumPy conversion logic
│   └── hexdump.py      # Binary formatting utilities
├── cli/
│   ├── info.py         # File header inspection
│   ├── dump.py         # Record structure visualization
│   └── __init__.py     # Click command group
└── tests/...           # Test helper classes if needed
tests/                  # Test suite
```

### **Key Data Structures**

```python
class FileHeader:
    magic: bytes  # 4-byte "EVIO"
    version: int  # 6 for EVIO v6
    record_count: int  # Total records in file
    index_array: List[int]  # Record start offsets (bytes)
    trailer_position: int  # 64-bit offset to trailer
    user_register: int  # 64-bit user value
    # ... other header fields


class BankNode:
    tag: int  # Bank identifier
    data_type: int  # EVIO data type code
    length: int  # Data length in 32-bit words
    offset: int  # Byte position in file
    children: List[BankNode]  # Sub-banks
    _buffer: Optional[memoryview] = None

    @property
    def numpy_shape(self) -> tuple:
        """Derive array shape from bank length/type"""

    def to_numpy(self, mm: mmap) -> np.ndarray:
        """Convert bank data to NumPy array"""
```

### **Memory Management**

- **Memory Mapping**:  
  All file access via `mmap` for zero-copy operations
- **Lazy Loading**:
    - Headers parsed immediately on file open
    - Bank data loaded on-demand during navigation
- **Efficient Array Conversion for numpy**:
  ```python
  def waveform_to_array(mm: mmap, bank: BankNode) -> np.ndarray:
      return np.frombuffer(
          mm[bank.offset : bank.offset + bank.data_length],
          dtype=np.uint16
      )
  ```

---

## **4. CLI Implementation**

### **Command Structure**

```bash
$ pyevio --help
Usage: pyevio [OPTIONS] COMMAND [ARGS]...

Options:
  --version  Show version
  --help     Show help

Commands:
  info   Show file metadata
  dump   Inspect record structure
```

### **`pyevio info` Command**

**Function**: Display file header and global metadata  
**Output**:

```
EVIO File Inspection: experiment_005.evio
══════════════════════════════════════════════════
╭──────────────────────┬─────────────────────────╮
│ Magic Number         │ EVIO (0x4556494F)       │
│ Format Version       │ 6                       │
│ Endianness           │ Little                  │
│ Record Count         │ 142                     │
│ Index Array Size     │ 256 entries             │
│ User Header Length   │ 0 bytes                 │
│ Trailer Position     │ 0x1af340 (1.67 MB)     │
│ Creation Timestamp   │ 2024-03-15 14:22:01 UTC │
╰──────────────────────┴─────────────────────────╯
```

### ** `pyevio dump` Command**

**Function**: Show hierarchical record structure  
**Flags**:

- `--depth=5`: Limit tree depth
- `--color=yes|no`: Toggle ANSI colors
- `--format=text|json`: Output format
- `--preview=3`: Data sample length

**Output**:

```
Record #12 [Offset: 0x1D4F00, Length: 8192 words]
├─ █ Bank 0x10C0 (ROC Time Slice) [Offset: +0x10]
│  ├─ Timestamp: 1710453205.882352941 (2024-03-14 09:53:25.882)
│  ├─ Channel 0: [0x3FF, 0x1A4, 0x0, ...] (uint16[256])
│  ├─ Channel 1: [0x255, 0x3E8, 0x12F, ...]
│  └─ ... 6 more channels
└─ █ Bank 0x201 (Trigger Data) [Offset: +0x1A4]
   ├─ Trigger ID: 0x1A4B3C
   └─ █ Sub-Bank 0x301 (Waveform Metadata)
      ├─ Samples: 1024
      └─ Clock Rate: 250 MHz
```

## **Summary**

The **pyevio** project will deliver a Python-based solution for **reading, parsing, and
introspecting** EVIO v6 files with a focus on performance, memory efficiency, and flexibility. The
core library provides Pythonic classes to represent headers and banks, along with lazy-loading
techniques for large data. A CLI built on **click** (and rendered with **rich**) offers **info** and
**dump** commands, enabling users to quickly inspect EVIO file structure or produce more detailed
hierarchical dumps. Optional **NumPy** conversion allows advanced analysis workflows.