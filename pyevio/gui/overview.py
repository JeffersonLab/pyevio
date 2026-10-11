"""Overview tab: totals, file checks, what stands out, file facts and one row per ROC."""
from textual.widgets import Static

from pyevio.gui.panes import Pane


class OverviewPane(Pane):
    def compose(self):
        yield Static("Open a file to see its checks and totals.", id="overview-placeholder")

    def file_closed(self) -> None:
        super().file_closed()
        self.query_one("#overview-placeholder", Static).update("Open a file to see its checks and totals.")

    def file_opened(self) -> None:
        self.query_one("#overview-placeholder", Static).update("Analyzing…")

    def stats_ready(self, summary: dict) -> None:
        super().stats_ready(summary)
        totals = summary["totals"]
        self.query_one("#overview-placeholder", Static).update(
            f"{summary['file']['path']}: {totals['n_records']:,} records, {totals['n_events']:,} events, {totals['n_hits']:,} hits")
