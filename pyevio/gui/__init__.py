"""Console viewer for EVIO files: a Textual app in the terminal.

``pyevio gui [PATH]`` shows the same things as the browser viewer, in the
terminal: file checks and totals, plots, the record table and the structure
tree down to single hit words. It reads through the same model modules as
``pyevio web`` (``pyevio.web.report``, ``pyevio.web.structure``,
``pyevio.web.jobs``, ``pyevio.web.cache``; none of them imports FastAPI), so
the two viewers agree on every number and share the statistics cache.

Modules, one job each:

- ``session``: the open file, its analysis job and the cached statistics. No Textual.
- ``app``: the Textual app, the status strip and the file-open screen.
- ``overview``, ``plots``, ``records``, ``structure``: one pane per tab.
- ``export``: the current plot as a PNG through matplotlib.

Textual and textual-plotext are the optional extra ``pyevio[gui]``; this
package imports them only inside the widget modules, so ``pyevio.gui.session``
works without them.
"""

INSTALL_HINT = "The console viewer needs Textual and textual-plotext: pip install 'pyevio[gui]'"
