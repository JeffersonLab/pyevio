import click
from rich.console import Console
from rich.table import Table
from rich import box
from datetime import datetime

from pyevio.evio_file import EvioFile
from pyevio.utils import print_offset_hex


@click.command(name="info")
@click.argument("filename", type=click.Path(exists=True))
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.option('--hexdump/--no-hexdump', default=False, help="Show hex dump of file header")
@click.option('--full', '-f', is_flag=True, help="Show all records without truncation")
@click.pass_context
def info_command(ctx, filename, verbose, hexdump, full):
    """Show file metadata and structure."""
    # Use either the command-specific verbose flag or the global one
    verbose = verbose or ctx.obj.get('VERBOSE', False)
    console = Console()

    with EvioFile(filename, verbose) as evio_file:
        # Create a table for file header
        table = Table(title=f"EVIO File: {filename}", box=box.DOUBLE)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")

        header = evio_file.header

        # If hexdump mode is enabled, display hex dump of the header
        if hexdump:
            # Calculate how many words to display (up to 50 or file_size // 4 if smaller)
            words_to_display = min(50, evio_file.file_size // 4)
            print_offset_hex(evio_file.mm, 0, words_to_display,
                             title=f"File Header Hex Dump (first {words_to_display} words)",
                             endian=header.endian)
            console.print()

        # Add header fields to the table
        table.add_row("Layout", header.layout)
        table.add_row("Magic Number", f"EVIO (0x{header.magic_number:08X})")
        version = str(header.version)
        if header.container_version != header.version:
            version += f" (content; block headers say {header.container_version})"
        table.add_row("Format Version", version)
        table.add_row("Endianness", "Little" if header.endian == '<' else "Big")
        table.add_row("Record Count", str(evio_file.record_count))
        table.add_row("Index Array Size", f"{header.index_array_length // 8} entries")
        table.add_row("User Header Length", f"{header.user_header_length} bytes")

        if header.trailer_position > 0:
            trailer_pos_str = f"0x{header.trailer_position:X} ({header.trailer_position / (1024*1024):.2f} MB)"
        else:
            trailer_pos_str = "Not present (0x0)"
        table.add_row("Trailer Position", trailer_pos_str)

        # Print the table
        console.print(table)

        # Print record information
        console.print("\n[bold]Record Information:[/bold]")

        records_table = Table(box=box.SIMPLE)
        records_table.add_column("Record #", style="cyan")
        records_table.add_column("Offset[hex]", style="green")
        records_table.add_column("(words)", style="green")
        records_table.add_column("Length (words)", style="yellow")
        records_table.add_column("Events", style="magenta")
        records_table.add_column("Type", style="blue")
        records_table.add_column("Last?", style="red")

        # Iterate through records using the new object-oriented structure
        for i in range(evio_file.record_count):
            # Display all records if --full flag is provided, otherwise use truncation
            if full or i < 10 or i >= evio_file.record_count - 5 or evio_file.record_count <= 15:
                try:
                    record = evio_file.get_record(i)
                    records_table.add_row(
                        str(i),
                        f"0x{record.offset:X}",
                        str(record.offset//4),
                        str(record.header.record_length),
                        str(record.event_count),
                        record.header.event_type,
                        "✓" if record.header.is_last_record else ""
                    )
                except Exception as e:
                    records_table.add_row(
                        str(i),
                        f"0x{evio_file._record_offsets[i]:X}",
                        "Error", "", f"[red]{str(e)}[/red]", ""
                    )
            elif i == 10 and evio_file.record_count > 15 and not full:
                records_table.add_row("...", "...", "...", "...", "...", "")

        console.print(records_table)

        # Print summary statistics
        console.print("\n[bold]Summary Statistics:[/bold]")
        console.print(f"Total Records: {evio_file.record_count}")
        total_events = evio_file.get_total_event_count()
        console.print(f"Total Events: {total_events}")
        console.print(f"File Size: {evio_file.file_size / (1024*1024):.2f} MB")

        console.print("\n[bold]File Checks:[/bold]")
        if not evio_file.checks:
            console.print("none")
        for check in evio_file.checks:
            console.print(describe_check(check))


def describe_check(check: tuple) -> str:
    """One line for an EvioFile.checks entry (see EvioFile for the tuple shapes)."""
    name = check[0]
    if name == "truncated_tail":
        _, declared, present, salvaged = check
        return (f"truncated_tail: the last record is cut by the end of the file — "
                f"declared {declared} bytes, {present} present, {salvaged} complete events kept")
    if name == "bad_record_header":
        _, offset, reason = check
        return f"bad_record_header: record scan stopped at offset 0x{offset:X} — {reason}"
    return f"{name}: {check[1:]}"