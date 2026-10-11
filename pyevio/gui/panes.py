"""The base of the four tab panes and the contract between the app and a pane.

The app owns the :class:`~pyevio.gui.session.Session` and drives every pane
through three hooks, always from the main thread:

- :meth:`Pane.file_closed` — the previous file is going away: drop every row, node
  and plot that refers to it. Called before the session closes the file.
- :meth:`Pane.file_opened` — a file is open; its analysis may still be running.
  Records and Structure load now, because they read the file, not the statistics.
- :meth:`Pane.stats_ready` — the analysis finished; ``summary`` is the dict of
  ``pyevio.web.report.build_summary``. Overview and Plots load now.

A pane keeps only what the session's model functions return (dicts, lists,
numbers), never a ``Record``, ``Event`` or ``Bank``: a live view of the file
mmap makes ``EvioFile.close()`` raise ``BufferError`` on the next file switch.
Anything slow runs through the session on demand (a page of 500 rows, one node's
children, one window of words), never the whole file.
"""
from typing import Optional

from textual.containers import Vertical


class Pane(Vertical):
    """One tab's content; see the module docstring for the hooks."""

    DEFAULT_CSS = """
    Pane {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, session, **kwargs):
        super().__init__(**kwargs)
        self.session = session
        self.summary: Optional[dict] = None  # the last build_summary dict, None until stats_ready

    def file_closed(self) -> None:
        self.summary = None

    def file_opened(self) -> None:
        pass

    def stats_ready(self, summary: dict) -> None:
        self.summary = summary
