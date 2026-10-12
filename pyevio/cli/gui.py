import click


@click.command(name="gui")
@click.argument("start_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.option('--port', '-p', default=8765, show_default=True, help="Port to serve on (localhost only)")
@click.option('--no-browser', is_flag=True, help="Do not open a browser tab")
@click.option('--mode', type=click.Choice(["stream", "file"]), default="stream", show_default=True,
              help="EVIO flavor to assume: 'stream' for CODA streaming fADC250 data (tested), "
                   "'file' for triggered/file-based EVIO (experimental, structure tree only)")
def gui_command(start_dir, port, no_browser, mode):
    """Browse EVIO files in a browser page: checks, plots and structure tree.

    START_DIR is the folder the file browser opens in (default: current directory).

    Only CODA streaming fADC250 EVIO is fully tested. File-based EVIO uses a
    different decoding path and may not work well; pass --mode=file to skip
    the streaming-specific decoding and show the structure tree only.
    """
    from pyevio.gui import serve
    serve(start_dir, port, open_browser=not no_browser, mode=mode)
