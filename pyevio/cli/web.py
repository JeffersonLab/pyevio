import click


@click.command(name="web")
@click.argument("start_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.option('--port', '-p', default=8765, show_default=True, help="Port to serve on (localhost only)")
@click.option('--no-browser', is_flag=True, help="Do not open a browser tab")
def web_command(start_dir, port, no_browser):
    """Browse EVIO files in a web page: checks, plots and structure tree.

    START_DIR is the folder the file browser opens in (default: current directory).
    """
    from pyevio.web import serve
    serve(start_dir, port, open_browser=not no_browser)
