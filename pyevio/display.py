"""
Display utilities for rendering EVIO structures in a user-friendly way.
"""
from rich.tree import Tree
from pyevio.record import Record
from pyevio.roc_time_slice_bank import RocTimeSliceBank


def get_bank_type_name(bank):
    """
    Determine a human-readable bank type name based on tag and data type.

    Args:
        bank: Bank object to identify

    Returns:
        String with human-readable bank type name
    """
    bank_type = "Unknown"

    if hasattr(bank, 'is_container') and bank.is_container():
        bank_type = "Container"

    if hasattr(bank, 'data_type'):
        if bank.data_type == 0x10:
            bank_type = "Bank of banks"
        elif bank.data_type == 0x20:
            bank_type = "Segment"

    if hasattr(bank, 'tag') and (bank.tag & 0xFF00) == 0xFF00:
        tag_type = bank.tag & 0x00FF
        if (tag_type & 0x10) == 0x10:
            bank_type = "ROC Raw Data Record"
        elif tag_type == 0x30:
            bank_type = "Stream Info Bank"
        elif tag_type == 0x31:
            bank_type = "Time Slice Segment"
        elif tag_type == 0x41 or tag_type == 0x85:
            bank_type = "Aggregation Info Segment"

    if isinstance(bank, RocTimeSliceBank):
        bank_type = "ROC Time Slice Bank"

    return bank_type


def display_bank_tree(tree, bank, level=0, max_depth=10, expand_all=False):
    """
    Display bank structure in a hierarchical tree.

    Args:
        tree: Rich Tree object to add nodes to
        bank: Bank object to display
        level: Current depth level
        max_depth: Maximum depth to display
        expand_all: Whether to expand all nodes by default
    """
    # Create a node for this bank
    bank_node = tree.add(f"Bank 0x{bank.tag:04X} ({get_bank_type_name(bank)})")

    # Add basic information
    bank_node.add(f"Offset: 0x{bank.offset:X}[{bank.offset//4}], Length: {bank.length} words ({bank.data_length} bytes)")
    bank_node.add(f"Tag: 0x{bank.tag:04X}, Type: 0x{bank.data_type:02X}, Num: {bank.num}")

    # For ROC Time Slice Banks, add special handling
    if isinstance(bank, RocTimeSliceBank):
        ts_node = bank_node.add(f"Timestamp: {bank.get_formatted_timestamp()}")
        if hasattr(bank, 'sib') and bank.sib:
            ts_node.add(f"Frame Number: {bank.sib.frame_number}")

        # Add payload info
        if hasattr(bank, 'payload_banks') and bank.payload_banks:
            payloads_node = bank_node.add(f"Payload Banks ({len(bank.payload_banks)})")
            for i, payload in enumerate(bank.payload_banks[:min(5, len(bank.payload_banks))]):
                payload_node = payloads_node.add(f"Payload {i}")
                payload_node.add(f"Offset: 0x{payload.offset:X}[{payload.offset//4}], Length: {payload.length} words")

                if hasattr(payload, 'num_samples') and payload.num_samples:
                    payload_node.add(f"Samples: {payload.num_samples}, Channels: {getattr(payload, 'channels', 1)}")

    # If this is a container bank and we're not at max depth, show children
    if bank.is_container() and level < max_depth:
        children = bank.get_children()

        if children:
            child_count = len(children)
            children_node = bank_node.add(f"Child Banks ({child_count})")

            # Display each child (up to a reasonable limit)
            display_limit = min(10, child_count)
            for i, child in enumerate(children[:display_limit]):
                display_bank_tree(children_node, child, level + 1, max_depth, expand_all)

            # Note if we're hiding some children
            if child_count > display_limit:
                children_node.add(f"... {child_count - display_limit} more banks ...")
        else:
            bank_node.add("No child banks")
    elif not bank.is_container():
        # Add data preview for non-container banks
        data = bank.to_numpy()
        if data is not None and len(data) > 0:
            preview_count = min(10, len(data))
            data_preview = ", ".join([f"{x}" for x in data[:preview_count]])
            if len(data) > preview_count:
                data_preview += f", ... ({len(data) - preview_count} more values)"
            bank_node.add(f"Data: [{data_preview}]")

        # Try string conversion for string banks
        string_data = bank.to_string()
        if string_data is not None:
            bank_node.add(f"String: {string_data}")


def create_bank_tree(bank, title="Bank Structure"):
    """
    Create a rich Tree object showing the bank structure.

    Args:
        bank: Root bank to display
        title: Title for the tree

    Returns:
        Rich Tree object displaying the bank structure
    """
    tree = Tree(f"[bold]{title}[/bold]")
    display_bank_tree(tree, bank)
    return tree


COMPRESSION_NAMES = {Record.COMPRESSION_LZ4: "LZ4", Record.COMPRESSION_LZ4_BEST: "LZ4_BEST",
                     Record.COMPRESSION_GZIP: "gzip"}


def offset_scope_note(record):
    """The line that says what the event and bank offsets of ``record`` refer to.

    For a compressed record every event and bank offset is an offset in the
    decompressed record buffer (see ``Record``), so a reader who takes it for
    a file offset lands on compressed bytes. Returns ``None`` for an
    uncompressed record, whose offsets are file offsets.
    """
    if record.compression_type == record.COMPRESSION_NONE:
        return None
    name = COMPRESSION_NAMES.get(record.compression_type, f"type {record.compression_type}")
    # Kept under 80 columns so rich prints it on one line in a default terminal.
    return f"[yellow]{name} compressed record: offsets below are in the decompressed record buffer[/yellow]"
