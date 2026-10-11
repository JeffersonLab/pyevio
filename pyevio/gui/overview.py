"""Overview tab: totals, what stands out, the file checks, file facts and one row per ROC.

Everything here is rendered from the ``build_summary`` dict the app passes to
:meth:`OverviewPane.stats_ready`; the pane keeps that dict and nothing else.
Six ``Static`` widgets in a ``VerticalScroll``: the state line, totals, "what
stands out", the checks table, the file facts and the per-ROC table. Text
sections are Textual markup (values escaped); the two tables are Rich
``Table`` renderables with the status word coloured by a Rich style.
"""
import math
from typing import List

from rich.table import Table
from rich.text import Text
from textual.containers import VerticalScroll
from textual.markup import escape
from textual.widgets import Static

from pyevio.gui.panes import Pane

STATUS_STYLE = {"error": "red", "warn": "yellow", "info": "blue", "ok": "green"}
STANDOUT_ORDER = ("error", "warn")  # the web viewer's rule: info checks stay in the checks table
NO_FILE_TEXT = "No file open — press o to open an EVIO file."
SECTION_IDS = ("overview-totals", "overview-standout", "overview-checks", "overview-file", "overview-rocs")


def fmt(value) -> str:
    """One value as the web viewer prints it: ``n/a`` as given, lists joined, booleans as yes/no, numbers with separators."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(fmt(item) for item in value) if value else "none"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".") if math.isfinite(value) else str(value)
    return f"{value:,}"


def seconds(value) -> str:
    return value if isinstance(value, str) else f"{value:.3f} s"


def facts_markup(title: str, rows: List[tuple]) -> str:
    """A titled key/value list; the keys are padded to one column."""
    width = max((len(name) for name, _ in rows), default=0)
    lines = [f"[b]{escape(title)}[/b]"]
    lines += [f"  {escape(name):<{width}}  {escape(fmt(value))}" for name, value in rows]
    return "\n".join(lines)


def status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLE.get(status, ""))


class OverviewPane(Pane):
    """See the module docstring; the hooks are those of :class:`~pyevio.gui.panes.Pane`."""

    DEFAULT_CSS = """
    OverviewPane VerticalScroll {
        height: 1fr;
    }
    OverviewPane Static {
        margin-bottom: 1;
    }
    OverviewPane #overview-state {
        color: $text-muted;
    }
    """

    def compose(self):
        with VerticalScroll(id="overview-scroll"):
            yield Static(NO_FILE_TEXT, id="overview-state")
            for section_id in SECTION_IDS:
                yield Static("", id=section_id)

    # ---------------------------------------------------------------- hooks
    def file_closed(self) -> None:
        super().file_closed()
        self._set_state(NO_FILE_TEXT)
        self._clear_sections()

    def file_opened(self) -> None:
        name = self.session.path.name if self.session.path else "the file"
        self._set_state(f"Analyzing {escape(name)} — the totals and checks appear when the analysis is done.")
        self._clear_sections()

    def stats_ready(self, summary: dict) -> None:
        super().stats_ready(summary)
        stats = summary["stats"]
        source = "from cache" if stats["cached"] else f"computed in {stats['wall_seconds']:.1f} s on {stats['workers']} workers"
        self._set_state(f"{escape(summary['file']['path'])} — analysis {source}, stats version {stats['version']}")
        self.query_one("#overview-totals", Static).update(self.totals_markup(summary))
        self.query_one("#overview-standout", Static).update(self.standout_markup(summary["checks"], summary["totals"].get("error_messages", [])))
        self.query_one("#overview-checks", Static).update(self.checks_table(summary["checks"]))
        self.query_one("#overview-file", Static).update(self.file_markup(summary))
        self.query_one("#overview-rocs", Static).update(self.roc_table(summary["rocs"]))
        self.query_one("#overview-scroll", VerticalScroll).scroll_home(animate=False)

    # ---------------------------------------------------------------- sections
    def _set_state(self, markup: str) -> None:
        self.query_one("#overview-state", Static).update(markup)

    def _clear_sections(self) -> None:
        for section_id in SECTION_IDS:
            self.query_one(f"#{section_id}", Static).update("")

    @staticmethod
    def totals_markup(summary: dict) -> str:
        totals = summary["totals"]
        modules = ", ".join(f"{module['name']} ({fmt(module['words'])} words)" for module in totals["modules"]) or "none"
        first_error = f" — first: {totals['error_messages'][0]}" if totals["error_messages"] else ""
        first_warning = f" — first: {totals['warning_messages'][0]}" if totals["warning_messages"] else ""
        rows = [
            ("records", f"{fmt(totals['n_records'])} + {fmt(totals['n_trailer_records'])} trailer"),
            ("events", f"{fmt(totals['n_events'])} ({fmt(totals['n_frames'])} stream frames, {fmt(totals['n_other_events'])} other, "
                       f"{fmt(totals['n_out_of_bounds_events'])} out of bounds)"),
            ("fADC250 hits", totals["n_hits"]),
            ("hit rate", totals["hit_rate_hz"] if isinstance(totals["hit_rate_hz"], str) else f"{totals['hit_rate_hz']:,.1f} Hz"),
            ("hits per frame", totals["hits_per_frame"]),
            ("duration / live time", f"{seconds(totals['duration_s'])} / {seconds(totals['live_time_s'])}"),
            ("modules", modules),
            ("decode errors", f"{fmt(totals['errors'])}{first_error}"),
            ("decode warnings", f"{fmt(totals['warnings'])}{first_warning}"),
        ]
        return facts_markup("Totals", rows)

    @staticmethod
    def standout_markup(checks: List[dict], messages: List[str] = ()) -> str:
        """The error checks, then the warn checks, then the first decode messages — the web viewer's rule."""
        lines = ["[b]What stands out[/b]"]
        notable = [check for status in STANDOUT_ORDER for check in checks if check["status"] == status]
        for check in notable:
            style = STATUS_STYLE[check["status"]]
            detail = f" — {escape(check['detail'])}" if check["detail"] else ""
            lines.append(f"  [{style}]{check['status']:<5}[/{style}] {escape(check['name'])}: {escape(fmt(check['value']))}{detail}")
        for message in list(messages)[:3]:
            lines.append(f"  [red]{escape(message)}[/red]")
        if len(lines) == 1:
            lines.append("  [green]all checks pass[/green]")
        return "\n".join(lines)

    @staticmethod
    def checks_table(checks: List[dict]) -> Table:
        table = Table(title="Checks", title_justify="left", expand=False, pad_edge=False)
        for column in ("check", "status", "value", "detail"):
            table.add_column(column)
        for check in checks:
            table.add_row(check["name"], status_text(check["status"]), fmt(check["value"]), check["detail"] or "")
        return table

    @staticmethod
    def file_markup(summary: dict) -> str:
        file = summary["file"]
        rows = [
            ("path", file["path"]),
            ("size", f"{fmt(file['size'])} B ({file['size'] / 1e6:.1f} MB)"),
            ("header layout", file["header_layout"]),
            ("byte order", "little-endian" if file["endian"] == "<" else "big-endian"),
            ("EVIO version", file["version"]),
            ("file type", file["file_type"]),
            ("records", f"{fmt(file['header_record_count'])} declared in the file header, {fmt(file['record_count'])} scanned"),
            ("trailer", f"{'present' if file['has_trailer'] else 'absent'}, position {fmt(file['trailer_position'])} in the file header"),
            ("first record offset", file["first_record_offset"]),
            ("index array length", f"{fmt(file['index_array_length'])} B"),
            ("user header length", f"{fmt(file['user_header_length'])} B"),
        ]
        for words in (OverviewPane.scan_check_words(check) for check in summary["scan_checks"]):
            rows.append(("record scan", words))
        if not summary["scan_checks"]:
            rows.append(("record scan", "every record header read cleanly"))
        return facts_markup("File", rows)

    @staticmethod
    def scan_check_words(check: list) -> str:
        """An ``EvioFile.checks`` tuple in words."""
        if check[0] == "truncated_tail":
            _, declared, present, salvaged = check
            return (f"truncated tail: the last record declares {fmt(declared)} B but {fmt(present)} B are present; "
                    f"{fmt(salvaged)} complete events kept")
        if check[0] == "bad_record_header":
            _, offset, reason = check
            return f"bad record header at 0x{offset:X}: the scan stopped there ({reason})"
        return f"{check[0]}: {', '.join(str(item) for item in check[1:])}"

    @staticmethod
    def roc_table(rocs: List[dict]) -> Table:
        table = Table(title="Per ROC", title_justify="left", expand=False, pad_edge=False)
        for column, justify in (("ROC", "right"), ("modules", "left"), ("ports", "left"), ("hits", "right"),
                                ("other-module words", "right"), ("empty banks", "right")):
            table.add_column(column, justify=justify)
        for roc in rocs:
            ports = f"{len(roc['ports'])}: {fmt(roc['ports'])}" if roc["ports"] else "none"
            table.add_row(str(roc["roc"]), fmt(roc["modules"]), ports, fmt(roc["hits"]), fmt(roc["non_fadc_words"]), fmt(roc["empty_banks"]))
        if not rocs:
            table.add_row("—", "no ROC banks in the stream frames", "", "", "", "")
        return table
