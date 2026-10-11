import click

from pyevio import __version__

# Import and register commands
from pyevio.cli.info import info_command
from pyevio.cli.dump import dump_command
from pyevio.cli.debug import debug_command
from pyevio.cli.record import record_command
from pyevio.cli.event import event_command
from pyevio.cli.hex import hex_command
from pyevio.cli.gui import gui_command
from pyevio.cli.ana import ana_command
from pyevio.cli.web import web_command
from pyevio.cli.stats import stats_command


class EvioCli(click.Group):
    """The pyevio command group: a file pyevio cannot read ends the command with its message.

    ``EvioFile`` raises ``ValueError`` for a file that is not EVIO or an EVIO
    version pyevio does not read. Converting it here, once, turns the message
    into click's ``Error: ...`` on stderr with exit code 1 for every command.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ValueError as error:
            raise click.ClickException(str(error)) from error


@click.group(cls=EvioCli)
@click.version_option(version=__version__)
@click.option('--verbose', '-v', is_flag=True, help="Enable verbose output")
@click.pass_context
def cli(ctx, verbose):
    """EVIO v6 file inspection toolkit."""
    # Create a context object to pass data between commands
    ctx.ensure_object(dict)
    ctx.obj['VERBOSE'] = verbose


# Register commands with the CLI
cli.add_command(info_command)
cli.add_command(dump_command)
cli.add_command(debug_command)
cli.add_command(record_command)
cli.add_command(event_command)
cli.add_command(hex_command)
cli.add_command(gui_command)
cli.add_command(ana_command)
cli.add_command(web_command)
cli.add_command(stats_command)


# Entry point for the CLI
def main():
    """Entry point for the CLI when installed via pip."""
    cli(prog_name="pyevio")


if __name__ == "__main__":
    main()
