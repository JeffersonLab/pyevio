import os
import sys

import click

# Import and register commands
from pyevio.cli.info import info_command
from pyevio.cli.dump import dump_command
from pyevio.cli.debug import debug_command
from pyevio.cli.record import record_command
from pyevio.cli.event import event_command
from pyevio.cli.hex import hex_command
from pyevio.cli.ui import ui_command
from pyevio.cli.ana import ana_command
from pyevio.cli.web import web_command


@click.group(invoke_without_command=True)
@click.version_option(version="0.1.0")
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.pass_context
def cli(ctx, verbose):
    """EVIO v6 file inspection toolkit."""
    # Create a context object to pass data between commands
    ctx.ensure_object(dict)
    ctx.obj['VERBOSE'] = verbose

    # If no command was provided, and there are arguments, check if the first one is a file
    if ctx.invoked_subcommand is None and len(sys.argv) > 1:
        potential_file = sys.argv[1]
        if os.path.isfile(potential_file):
            # Treat this as 'info' command
            ctx.invoke(info_command, filename=potential_file, verbose=verbose)


# Register commands with the CLI
cli.add_command(info_command)
cli.add_command(dump_command)
cli.add_command(debug_command)
cli.add_command(record_command)
cli.add_command(event_command)
cli.add_command(hex_command)
cli.add_command(ui_command)
cli.add_command(ana_command)
cli.add_command(web_command)


# Entry point for the CLI
def main():
    """Entry point for the CLI when installed via pip."""
    cli(prog_name="pyevio")


if __name__ == "__main__":
    main()
