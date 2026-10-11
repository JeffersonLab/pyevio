import click
from rich.console import Console
from rich import box
from rich.table import Table

import numpy as np

from pyevio.evio_file import EvioFile
from pyevio.decoders import fadc250_stream
from pyevio.roc_time_slice_bank import RocTimeSliceBank
from pyevio.utils import print_offset_hex

# Hit words printed per payload bank before the output is cut; SRO payload banks hold thousands.
MAX_HIT_WORDS_SHOWN = 32


def display_record_info(console, record_obj, record_index):
    """Display record header information."""
    console.print(f"[bold cyan]Record #{record_index} Analysis[/bold cyan]")
    console.print(f"[bold]Offset: [green]0x{record_obj.offset:X}[{record_obj.offset//4}][/green], Length: [green]{record_obj.header.record_length}[/green] words[/bold]")
    console.print(f"[bold]Type: [green]{record_obj.header.event_type}[/green], Events: [green]{record_obj.event_count}[/green][/bold]")


def display_bank_header(console, bank, title=None):
    """Display basic information about a bank."""
    bank_title = title or "Bank Information"
    console.print(f"[bold]{bank_title}:[/bold]")
    console.print(f"Tag: 0x{bank.tag:04X}, Data Type: 0x{bank.data_type:02X}, Offset: 0x{bank.offset:X}[{bank.offset//4}]")
    console.print(f"Length: {bank.length} words ({bank.data_length} bytes of data)")


def get_bank_type_name(bank):
    """Determine a human-readable bank type name."""
    bank_type = "Unknown"
    if bank.data_type == 0x10:
        bank_type = "Bank of banks"
    elif bank.data_type == 0x20:
        bank_type = "Segment"
    elif (bank.tag & 0xFF00) == 0xFF00:
        tag_type = bank.tag & 0x00FF
        if (tag_type & 0x10) == 0x10:
            bank_type = "ROC Raw Data Record"
        elif tag_type == 0x30:
            bank_type = "Stream Info Bank"
        elif tag_type == 0x31:
            bank_type = "Time Slice Segment"
        elif tag_type == 0x41 or tag_type == 0x85:
            bank_type = "Aggregation Info Segment"
    return bank_type


def display_child_banks(console, parent_bank, evio_file, verbose, hexdump, level=0, max_level=2):
    """Recursively display information about child banks."""
    if not parent_bank.is_container() or level > max_level:
        return

    try:
        children = parent_bank.get_children()
        if not children:
            console.print(f"[bold]No child banks found[/bold]")
            return

        console.print(f"[bold]Contains {len(children)} child banks:[/bold]")

        # Create a table for child banks
        child_table = Table(box=box.SIMPLE)
        child_table.add_column("#", style="cyan")
        child_table.add_column("Offset", style="green")
        child_table.add_column("Tag", style="yellow")
        child_table.add_column("Data Type", style="magenta")
        child_table.add_column("Length", style="blue")
        child_table.add_column("Type", style="white")

        for i, child in enumerate(children[:min(20, len(children))]):
            try:
                bank_type = get_bank_type_name(child)

                child_table.add_row(
                    str(i),
                    f"0x{child.offset:X}[{child.offset//4}]",
                    f"0x{child.tag:04X}",
                    f"0x{child.data_type:02X}",
                    f"{child.length} words",
                    bank_type
                )
            except Exception as e:
                child_table.add_row(
                    str(i),
                    f"0x{child.offset:X}[{child.offset//4}]",
                    "ERROR",
                    "ERROR",
                    f"Parse Error: {str(e)}",
                    "Invalid Bank"
                )

        if len(children) > 20:
            child_table.add_row("...", "...", "...", "...", f"{len(children) - 20} more banks", "...")

        console.print(child_table)

        # Display detailed info for each child if requested
        if verbose or hexdump:
            for i, child in enumerate(children[:min(10, len(children))]):
                try:
                    console.print(f"\n[bold]Child Bank #{i} (Tag 0x{child.tag:04X}, Type 0x{child.data_type:02X}):[/bold]")

                    # Use print_offset_hex for displaying hex data
                    if hexdump:
                        print_offset_hex(evio_file.mm, child.offset, min(16, child.length),
                                         f"Child #{i} at offset 0x{child.offset:X}[{child.offset//4}]")

                    # Show length details for better debugging
                    console.print(f"[dim]Length: {child.length} words, Data Length: {child.data_length} bytes[/dim]")
                    # The length word counts the words after it: a bank spans (length + 1) words.
                    console.print(f"[dim]Offset Range: 0x{child.offset:X} - 0x{child.offset + (child.length + 1) * 4:X}[/dim]")

                    if not child.is_container():
                        display_data_bank(console, child, evio_file, hexdump)
                    # Recurse for container banks
                    elif child.is_container() and level < max_level:
                        display_child_banks(console, child, evio_file, verbose, hexdump, level + 1, max_level)
                except Exception as e:
                    console.print(f"[red]Error analyzing child {i}: {str(e)}[/red]")
                    if verbose:
                        import traceback
                        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    except Exception as e:
        console.print(f"[red]Error accessing child banks: {str(e)}[/red]")

def display_data_bank(console, bank, evio_file, hexdump=False):
    """
    Display detailed information about a data bank, including bit-field analysis.

    Args:
        console: Rich console for output
        bank: Bank object representing a data bank
        evio_file: EvioFile object for accessing raw data
        hexdump: Whether to show hex dumps
    """
    # Get data value
    if bank.length == 1:
        # For a length-1 bank, there's no additional data - the bank is just the length word
        console.print(f"[bold]Data Bank (Length 1, No Data)[/bold]")
        return

    # The length word counts the words that follow it, so the payload is (length - 1) words after the 2-word header.
    data_offset = bank.offset + 8
    data_end = min(data_offset + 4 * (bank.length - 1), len(evio_file.mm))
    data_words = fadc250_stream.event_words(evio_file.mm, data_offset, max(data_end - data_offset, 0), bank.endian)

    if len(data_words) == 0:
        console.print(f"[bold]Data Bank (No Valid Data)[/bold]")
        return

    console.print(f"[bold]Data Bank (Data Words: {len(data_words)})[/bold]")

    # Interpret the words as streaming fADC250 hits; the layout lives in the decoder module.
    shown = data_words[:MAX_HIT_WORDS_SHOWN]
    channels, times_raw, charges = fadc250_stream.unpack_hit_words(shown)
    for word_i, (word, chan, time_raw, charge) in enumerate(zip(shown.tolist(), channels, times_raw, charges)):
        console.print(f"  Word {word_i}: 0x{word:08X}")
        console.print(f"    [dim]Channel: {chan}, Charge: {charge}, Time: {int(time_raw) * fadc250_stream.HIT_TIME_TICK_NS}[/dim]")
    if len(data_words) > len(shown):
        console.print(f"  [dim]... {len(data_words) - len(shown)} more words[/dim]")

    # Show hexdump if requested
    if hexdump and bank.data_length > 0:
        print_offset_hex(evio_file.mm, bank.data_offset, min(bank.data_length // 4, 16),
                         f"Data Bank at 0x{bank.offset:X}[{bank.offset//4}]")

def display_roc_timeslice_info(console, bank, evio_file, payload_filter=None, hexdump=False):
    """Display detailed information about a ROC Time Slice Bank."""
    console.print(f"[bold]ROC Time Slice Bank (ROC ID: {bank.roc_id})[/bold]")

    # Show timestamp information
    console.print(f"Timestamp: {bank.get_formatted_timestamp()}")
    console.print(f"Frame Number: {bank.sib.frame_number}")

    # Display Stream Info Bank details
    console.print(f"\n[bold]Stream Info Bank (0xFF30):[/bold]")
    console.print(f"Error Flag: {bank.error_flag}, Total Streams: {bank.total_streams}, Stream Mask: 0x{bank.stream_mask:X}")

    # Display payload info
    console.print(f"\n[bold]Payload Information ({len(bank.sib.payload_infos)} entries):[/bold]")

    payload_table = Table(box=box.SIMPLE)
    payload_table.add_column("#", style="cyan")
    payload_table.add_column("Module ID", style="green")
    payload_table.add_column("Bond", style="yellow")
    payload_table.add_column("Lane ID", style="magenta")
    payload_table.add_column("Port #", style="blue")

    for i, payload_info in enumerate(bank.sib.payload_infos):
        payload_table.add_row(
            str(i),
            str(payload_info['module_id']),
            str(payload_info['bond']),
            str(payload_info['lane_id']),
            str(payload_info['port_num'])
        )

    console.print(payload_table)

    # Display payload banks
    console.print(f"\n[bold]Payload Banks ({len(bank.payload_banks)}):[/bold]")

    # Filter payloads if requested
    payload_indices = range(len(bank.payload_banks))
    if payload_filter is not None:
        if payload_filter < 0 or payload_filter >= len(bank.payload_banks):
            console.print(f"[yellow]Warning: Payload {payload_filter} out of range (0-{len(bank.payload_banks)-1})[/yellow]")
        else:
            payload_indices = [payload_filter]

    # Display each payload bank
    for p_idx in payload_indices:
        if p_idx >= len(bank.payload_banks):
            continue

        payload_bank = bank.payload_banks[p_idx]
        payload_info = bank.sib.payload_infos[p_idx] if p_idx < len(bank.sib.payload_infos) else None

        console.print(f"\n[bold]Payload {p_idx}:[/bold]")
        console.print(f"Offset: 0x{payload_bank.offset:X}[{payload_bank.offset//4}], Length: {payload_bank.length} words")
        console.print(f"Tag: 0x{payload_bank.tag:04X}, Data Type: 0x{payload_bank.data_type:02X}")

        if payload_info:
            console.print(f"Module ID: {payload_info['module_id']}, Lane ID: {payload_info['lane_id']}, Port: {payload_info['port_num']}")

        # Show waveform information if available
        if hasattr(payload_bank, 'num_samples'):
            console.print(f"Total Samples: {payload_bank.num_samples}")
            if hasattr(payload_bank, 'channels'):
                console.print(f"Channels: {payload_bank.channels}")
            if hasattr(payload_bank, 'samples_per_channel'):
                console.print(f"Samples/Channel: {payload_bank.samples_per_channel}")

            # Show data preview
            try:
                data = payload_bank.get_waveform_data()
                if data:
                    console.print(f"Data Range: Min={min(data)}, Max={max(data)}, Mean={(sum(data)/len(data)):.2f}")

                    # Show hexdump of waveform data
                    if hexdump:
                        print_offset_hex(evio_file.mm, payload_bank.data_offset, min(16, payload_bank.data_length//4),
                                         f"Payload {p_idx} Data at 0x{payload_bank.data_offset:X}[{payload_bank.data_offset//4}]")
            except Exception as e:
                console.print(f"[red]Error analyzing waveform data: {str(e)}[/red]")


def display_stream_frame(console, evio_file, event_obj):
    """Decode a streaming time frame (0xFF60) and show its hits per ROC bank."""
    words = fadc250_stream.event_words(evio_file.mm, event_obj.offset, event_obj.length, event_obj.endian)
    frame = fadc250_stream.decode_frame(words)
    console.print(f"[bold]Streaming time frame {frame.frame_number}[/bold], timestamp {frame.timestamp}, "
                  f"{frame.n_hits} hits in {frame.n_roc_banks} ROC banks, "
                  f"{frame.n_payload_banks} payload banks ({frame.n_empty_payload_banks} empty), "
                  f"{frame.non_fadc_words} words of other modules")
    for message in frame.error_messages:
        console.print(f"[red]Decode error: {message}[/red]")

    module_names = {fadc250_stream.MODULE_FADC250: "fADC250", fadc250_stream.MODULE_DCRB: "DCRB",
                    fadc250_stream.MODULE_UNKNOWN: "unmapped"}
    roc_table = Table(box=box.SIMPLE)
    roc_table.add_column("ROC", style="cyan")
    roc_table.add_column("Stream", style="green")
    roc_table.add_column("Module", style="white")
    roc_table.add_column("Payload banks", style="blue")
    roc_table.add_column("Empty", style="yellow")
    roc_table.add_column("Hits", style="magenta")
    roc_table.add_column("Other words", style="magenta")
    roc_table.add_column("Ports", style="white")
    for roc_bank in frame.roc_banks:
        same_bank = lambda table: table[(table["roc"] == roc_bank["roc"]) & (table["stream"] == roc_bank["stream"])]
        hit_slice = slice(int(roc_bank["first_hit_index"]), int(roc_bank["first_hit_index"] + roc_bank["n_hits"]))
        ports = sorted(set(frame.port[hit_slice].tolist()) | set(same_bank(frame.empty_banks)["port"].tolist())
                       | set(same_bank(frame.non_fadc_banks)["port"].tolist()))
        module_id = int(roc_bank["module_id"])
        roc_table.add_row(str(roc_bank["roc"]), str(roc_bank["stream"]), module_names.get(module_id, str(module_id)),
                          str(roc_bank["n_payload_banks"]), str(roc_bank["n_empty_payload_banks"]), str(roc_bank["n_hits"]),
                          str(roc_bank["non_fadc_words"]), ", ".join(map(str, ports)))
    console.print(roc_table)


def display_event_info(console, evio_file, record_obj, event_idx, payload_filter, hexdump, verbose):
    """Display detailed information about an event."""
    console.print(f"[bold yellow]Event #{event_idx}[/bold yellow]")

    try:
        event_obj = record_obj.get_event(event_idx)
        console.print(f"[bold]Offset: [green]0x{event_obj.offset:X}[{event_obj.offset//4}][/green], Size: [green]{event_obj.length}[/green] bytes[/bold]")

        # Show hexdump if requested
        if hexdump:
            console.print()
            print_offset_hex(evio_file.mm, event_obj.offset, min(30, event_obj.length//4), f"Event #{event_idx} at 0x{event_obj.offset:X}[{event_obj.offset//4}]")


        # Get bank information
        bank_info = event_obj.get_bank_info()
        if bank_info:
            console.print(f"[bold]Bank Type: {bank_info.get('bank_type', 'Unknown')} (Tag: 0x{bank_info.get('tag', 0):04X})[/bold]")

        if bank_info and bank_info.get("tag") == fadc250_stream.STREAM_EVENT_TAG:
            display_stream_frame(console, evio_file, event_obj)


        # Try to parse the event as a bank
        try:
            # Get the bank object
            bank = event_obj.get_bank()

            # Show basic bank information
            display_bank_header(console, bank)

            # Handle different bank types
            if isinstance(bank, RocTimeSliceBank):
                # Display ROC Time Slice Bank information
                display_roc_timeslice_info(console, bank, evio_file, payload_filter, hexdump)
            elif bank.is_container():
                # Display child banks for container banks
                display_child_banks(console, bank, evio_file, verbose, hexdump)
            else:
                # For leaf banks, show data preview
                data = bank.to_numpy()
                if data is not None:
                    preview = ", ".join([f"{x}" for x in data[:min(5, len(data))]])
                    if len(data) > 5:
                        preview += f", ... ({len(data)} elements)"
                    console.print(f"[bold]Data Preview:[/bold] {preview}")

                # Show hexdump of bank data if requested
                if hexdump:
                    print_offset_hex(evio_file.mm, bank.data_offset, min(16, bank.data_length//4),
                                     f"Bank Data at 0x{bank.data_offset:X}[{bank.data_offset//4}]")

        except Exception as e:
            console.print(f"[red]Error parsing bank: {str(e)}[/red]")
            if verbose:
                import traceback
                console.print(f"[dim]{traceback.format_exc()}[/dim]")

    except Exception as e:
        console.print(f"[red]Error accessing event: {str(e)}[/red]")


@click.command(name="debug")
@click.argument("filename", type=click.Path(exists=True))
@click.option("--record", "-r", "record_index", type=int, required=True, help="Record number to debug")
@click.option("--event", "-e", type=int, help="Event number within the record (if omitted, scans first few events)")
@click.option("--payload", "-p", type=int, help="Payload number to focus on (if omitted, shows all payloads)")
@click.option("--hexdump/--no-hexdump", default=False, help="Show hex dump of data structures")
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.pass_context
def debug_command(ctx, filename, record_index, event, payload, hexdump, verbose):
    """
    Debug EVIO file structure at a detailed level.

    Analyzes a specific record and optionally a specific event within that record.
    Shows the internal structure of ROC Time Slice Banks, Stream Info Banks, and Payload Banks.
    """
    # Use either the command-specific verbose flag or the global one
    verbose = verbose or ctx.obj.get('VERBOSE', False)
    console = Console()

    with EvioFile(filename, verbose) as evio_file:
        # Validate record index
        if record_index < 0 or record_index >= evio_file.record_count:
            raise click.BadParameter(f"Record {record_index} out of range (0-{evio_file.record_count-1})")

        # Get the record object
        record_obj = evio_file.get_record(record_index)

        # Display record header information
        display_record_info(console, record_obj, record_index)

        # Show hexdump of record header if requested
        if hexdump:
            console.print()
            print_offset_hex(evio_file.mm, record_obj.offset, record_obj.header.header_length, "Record Header")

        # Handle event parameter
        events_to_scan = []
        if event is not None:
            if event < 0 or event >= record_obj.event_count:
                raise click.BadParameter(f"Event {event} out of range (0-{record_obj.event_count-1})")
            events_to_scan = [event]
        else:
            # Limit to first few events by default if not specified
            events_to_scan = list(range(min(3, record_obj.event_count)))

        # Scan each event
        for evt_idx in events_to_scan:
            console.print()
            display_event_info(console, evio_file, record_obj, evt_idx, payload, hexdump, verbose)