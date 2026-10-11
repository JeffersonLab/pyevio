"""Plots tab: hits per frame, rate, occupancy, charge and time in frame, at file, ROC and channel level."""
from textual.widgets import Static

from pyevio.gui.panes import Pane


class PlotsPane(Pane):
    def compose(self):
        yield Static("Plots appear when the analysis is done.", id="plots-placeholder")

    def stats_ready(self, summary: dict) -> None:
        super().stats_ready(summary)
        self.query_one("#plots-placeholder", Static).update(f"{summary['totals']['n_hits']:,} hits to plot")
