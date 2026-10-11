import sys

import click


@click.command(name="web")
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--port", "-p", default=8765, show_default=True, help="Port on 127.0.0.1")
@click.option("--no-browser", is_flag=True, help="Do not open a browser tab")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None,
              help="Where analysis results (.npz) are cached (default: ~/.cache/pyevio/stats)")
@click.option("--workers", "-w", type=int, default=None, help="Worker processes for the analysis (default: cores minus two)")
@click.option("--verbose", "-v", is_flag=True, help="Log every request")
def web_command(root, port, no_browser, cache_dir, workers, verbose):
    """Browse EVIO files under ROOT in a web page: checks, plots, records and structure.

    The server listens on 127.0.0.1 and prints a URL with a one-time token; only
    files under ROOT (default: the current folder) can be opened.
    """
    try:
        from pyevio.web.app import serve
    except ImportError:
        from pyevio.web import INSTALL_HINT
        click.echo(INSTALL_HINT, err=True)
        sys.exit(1)
    serve(root, port, open_browser=not no_browser, cache_dir=cache_dir, workers=workers, verbose=verbose)
