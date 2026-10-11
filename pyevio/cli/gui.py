import sys
from pathlib import Path

import click


@click.command(name="gui")
@click.argument("path", type=click.Path(exists=True), default=".")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None,
              help="Where analysis results (.npz) are cached (default: ~/.cache/pyevio/stats, shared with pyevio web)")
@click.option("--workers", "-w", type=int, default=None, help="Worker processes for the analysis (default: cores minus two)")
def gui_command(path, cache_dir, workers):
    """Browse an EVIO file in the terminal: checks, plots, records and structure.

    PATH is a file to open, or a folder (default: the current one) to pick a
    file from. The analysis runs in the background and its result is cached,
    so a file analyzed here opens at once in `pyevio web`, and the other way round.
    """
    try:
        from pyevio.gui.app import PyEvioGui
    except ImportError:
        from pyevio.gui import INSTALL_HINT
        click.echo(INSTALL_HINT, err=True)
        sys.exit(1)
    from pyevio.gui.session import Session

    target = Path(path)
    session = Session(cache_dir=Path(cache_dir) if cache_dir else None, workers=workers)
    app = PyEvioGui(session, start_path=target if target.is_file() else None, start_dir=target if target.is_dir() else target.parent)
    app.run()
