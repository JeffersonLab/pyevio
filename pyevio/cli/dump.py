import click
from rich.console import Console
import struct
from typing import List, Tuple, Optional, Dict, Any

from rich.table import Table

from pyevio.evio_file import EvioFile


def fetch_events(evio_file, count, start, record, verbose=False):
    """
    Fetch events based on the parameters.

    Args:
        evio_file: EvioFile object
        count: Number of events to fetch
        start: Starting event index
        record: Record index (if specified)
        verbose: Whether to print verbose messages

    Returns:
        List of (Event, index) tuples
    """
    events = []

    if record is not None:
        # Get events from the specified record
        try:
            record_obj = evio_file.get_record(record)
            # Adjust start index to be relative to the record
            start = max(0, min(start, record_obj.event_count - 1))
            # Fetch events from the record
            record_events = record_obj.get_events(start, start + count)
            events = [(event, f"{record}:{i + start}") for i, event in enumerate(record_events)]
        except Exception as e:
            if verbose:
                print(f"Error accessing record {record}: {e}")
            return []
    else:
        # Get events by global index
        total_events = evio_file.get_total_event_count()
        # Adjust start index
        start = max(0, min(start, total_events - 1))
        # Fetch events
        for i in range(start, min(start + count, total_events)):
            try:
                record, event = evio_file.get_record_and_event(i)
                events.append((event, i))
            except Exception as e:
                if verbose:
                    print(f"Error fetching event {i}: {e}")
                break

    return events


def format_events(events, format_as, only):
    """
    Format events for display.

    Args:
        events: List of (Event, index) tuples
        format_as: Output format ("hex" or "dec")
        only: Comma-separated list of word indices to include

    Returns:
        List of formatted rows, one per event
    """
    rows = []

    # Parse the "only" parameter if provided
    selected_indices = None
    if only:
        try:
            selected_indices = [int(idx.strip()) for idx in only.split(",")]
            selected_indices.sort()  # Ensure indices are in order
        except ValueError:
            print(f"Warning: Invalid format for --only parameter: {only}")

    # Process events
    for event, idx in events:
        try:
            # Get raw event data
            data = event.get_data()

            # Parse into 32-bit words
            row = [f"{idx}"]
            for i in range(0, len(data), 4):
                if i + 4 <= len(data):  # Only process complete words
                    word_bytes = data[i:i+4]
                    word_value = struct.unpack(event.endian + "I", word_bytes)[0]

                    # Only include words in selected_indices if specified
                    word_idx = i // 4
                    if selected_indices is None or word_idx in selected_indices:
                        if format_as == "hex":
                            row.append(f"0x{word_value:08X}")
                        elif format_as == "dec":
                            row.append(f"{int(word_value)}")

            # Add formatted row for this event
            rows.append(row)

        except Exception as e:
            print(f"Event #{idx}", f"Error: {str(e)}")
            raise

    return rows


def render_rows(rows, output_format="text"):
    """
    Render formatted rows for output.

    Args:
        rows: List of formatted rows
        output_format: Output format (currently only "text")

    Returns:
        String containing rendered output
    """
    if not rows:
        return "No events to display"

    if output_format == "text":
        # Find the maximum width of each column
        max_widths = {}
        for row in rows:
            for i, cell in enumerate(row):
                max_widths[i] = max(max_widths.get(i, 0), len(str(cell)))

        # # Format each row with proper padding
        # if not rows ||
        #
        # table = Table(title=None, box=None)
        # table.add_column("Evt", style="cyan")
        #
        formatted_rows = []
        for row in rows:
            formatted_cells = []
            for i, cell in enumerate(row):

                # Left-align the event number, right-align the values
                if i == 0:  # Event number
                    formatted_cells.append(str(cell).ljust(max_widths[i]))
                else:  # Data values
                    formatted_cells.append(str(cell).rjust(max_widths[i]))

            formatted_rows.append(" ".join(formatted_cells))

        return "\n".join(formatted_rows)

    # Add support for other formats (JSON, CSV, etc.) in the future
    return "Unsupported output format"


@click.command(name="dump")
@click.argument("filename", type=click.Path(exists=True))
@click.argument("count", type=int, required=False, default=1)
@click.option("--start", "-s", type=int, default=2, help="Starting event index (default: 2)")
@click.option("--record", "-r", type=int, help="Record number to use for event indexing")
@click.option("--as", "format_as", type=click.Choice(["hex", "dec"]), default="hex", help="Output format")
@click.option("--only", help="Only show specific word indices (comma-separated)")
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.pass_context
def dump_command(ctx, filename, count, start, record, format_as, only, verbose):
    """Dump event data in tabular format.

    FILENAME: Path to EVIO file
    COUNT: Number of events to dump (default: 1)

    Examples:
      pyevio dump file              # Dump the 3rd event (index 2)
      pyevio dump file 100          # Dump 100 events starting from the 3rd event
      pyevio dump file -s 200 100   # Dump 100 events starting from event id=200
      pyevio dump file -r 5 100     # Dump 100 events from record 5, starting from index 0
      pyevio dump file --as=dec     # Dump in decimal format instead of hex
      pyevio dump file --only=0,1,2 # Only show words 0, 1, and 2 from each event
    """
    # Use either the command-specific verbose flag or the global one
    verbose = verbose or ctx.obj.get('VERBOSE', False)
    console = Console()

    # The open sits outside the catch-all: a file pyevio cannot read raises
    # ValueError, which the command group reports with exit code 1.
    with EvioFile(filename, verbose) as evio_file:
        try:
            # Validate record if specified
            if record is not None:
                if record < 0 or record >= evio_file.record_count:
                    console.print(f"[red]Record {record} out of range (0-{evio_file.record_count-1})[/red]")
                    return 1

                # If record is specified and start is default (2), adjust to 0
                if start == 2 and not ctx.params.get("start"):
                    start = 0

            # When using default start value for global index, print info
            if record is None and start == 2 and not ctx.params.get("start"):
                console.print(f"[dim]Using default global event index: {start}[/dim]")

            # Fetch events based on parameters
            events = fetch_events(evio_file, count, start, record, verbose)

            if not events:
                console.print("[yellow]No events found with the specified parameters[/yellow]")
                return

            # Format events for display
            formatted_rows = format_events(events, format_as, only)

            # Render and display the formatted rows
            rendered_output = render_rows(formatted_rows)
            print(rendered_output)

        except Exception as e:
            console.print(f"[red]Error: {str(e)}[/red]")
            if verbose:
                import traceback
                console.print(traceback.format_exc())