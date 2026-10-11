import click
from rich.console import Console
from rich.panel import Panel
from rich import box
from rich.table import Table
from rich.tree import Tree
import os
import io
from PIL import Image
import matplotlib.pyplot as plt
from datetime import datetime

from pyevio.display import create_bank_tree, offset_scope_note
from pyevio.evio_file import EvioFile
from pyevio.roc_time_slice_bank import RocTimeSliceBank
from pyevio.utils import make_hex_dump, print_offset_hex


def display_event(console, evio_file, record_obj, event_obj, record_idx,
                  hexdump=False, verbose=False):
    """
    Display detailed information about an event.

    Args:
        console: Rich console for output
        evio_file: EvioFile object
        record_obj: Record object containing the event
        event_obj: Event object to display
        record_idx: Record index (for display)
        hexdump: Whether to show hex dumps
        verbose: Whether to show verbose output
    """
    # Display event header information
    console.print(f"[bold]Record #{record_idx} Event #{event_obj.index}[/bold]")
    note = offset_scope_note(record_obj)
    if note:
        console.print(note)
    console.print(f"[bold]Offset: [green]0x{event_obj.offset:X}[{event_obj.offset//4}][/green], Length: [green]{event_obj.length}[/green] bytes[/bold]")

    # Show hexdump of the full event if requested. The event's buffer is the
    # file for an uncompressed record and the decompressed bytes otherwise;
    # event_obj.offset is valid only in that buffer.
    if hexdump:
        console.print()
        print_offset_hex(
            event_obj.mm,
            event_obj.offset,
            event_obj.length // 4,  # Convert bytes to words
            f"Record #{record_idx} Event rel#{event_obj.index}, Length: {event_obj.length} bytes)"
        )
        console.print()

    # Try to get the bank
    try:
        bank = event_obj.get_bank()

        # Create a tree for hierarchical display
        bank_tree = create_bank_tree(bank, title="Event Structure")
        console.print(bank_tree)

    except Exception as e:
        console.print(f"[red]Error parsing bank: {str(e)}[/red]")
        if verbose:
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")


@click.command(name="event")
@click.argument("filename", type=click.Path(exists=True))
@click.argument("event_index", type=int)
@click.option("--record", "-r", "record_index", type=int, help="Record number containing the event (if omitted, EVENT is treated as global index)")
@click.option("--hexdump/--no-hexdump", "-h", default=False, help="Show hex dump of event data")
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.pass_context
def event_command(ctx, filename, event_index, record_index, hexdump, verbose):
    """
    Display detailed information about a specific event.

    If RECORD is specified using the -r/--record option, EVENT is treated as an index within that record.
    If no RECORD is specified, EVENT is treated as a global event index across all records.
    """
    verbose = verbose or ctx.obj.get('VERBOSE', False)
    console = Console()

    with (EvioFile(filename, verbose) as evio_file):
        try:
            # Get the event object (either by global index or record-specific index)
            if record_index is None:
                # Event index is global.
                record, event = evio_file.get_record_and_event(event_index)
                console.print(f"[dim]Global event {event_index} maps to: record# {record.index}, local event index: {event.index}[/dim]")
            else:
                # Record-specific event indexing
                record = evio_file.get_record(record_index)
                event = record.get_event(event_index)

            # Display the event information
            display_event(
                console,
                evio_file,
                record,
                event,
                record_index,
                hexdump=hexdump,
                verbose=verbose
            )

        except IndexError as e:
            raise click.BadParameter(str(e))
        except Exception as e:
            console.print(f"[red]Error: {str(e)}[/red]")
            if verbose:
                import traceback
                console.print(f"[dim]{traceback.format_exc()}[/dim]")