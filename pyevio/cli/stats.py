import click
from rich.console import Console
from rich import box
from rich.table import Table

from pyevio.decoders import fadc250_stream
from pyevio.stream_stats import compute_stream_stats, default_workers, DEFAULT_RATE_BINS, DEFAULT_TICK_NS

MODULE_NAMES = fadc250_stream.MODULE_NAMES


def print_stats_summary(console, stats):
    """Print totals, frame checks and a per-ROC table."""
    checks = stats.checks
    totals = Table(box=box.SIMPLE, show_header=False)
    totals.add_column("", style="bold")
    totals.add_column("")
    totals.add_row("File", f"{stats.path} ({stats.file_size / 1e9:.2f} GB)")
    totals.add_row("Records", f"{stats.n_records:,} ({stats.n_trailer_records} trailer)")
    totals.add_row("Events", f"{stats.n_events:,} ({stats.n_frames:,} stream frames, {stats.n_other_events:,} other, "
                             f"{stats.n_out_of_bounds_events:,} out of bounds)")
    totals.add_row("fADC250 hits", f"{stats.n_hits:,}")
    totals.add_row("Payload words", ", ".join(f"{MODULE_NAMES.get(int(m), str(m))}: {int(w):,}"
                                              for m, w in zip(stats.module_ids, stats.module_words)) or "none")
    totals.add_row("Payload banks", f"{stats.n_payload_banks:,} ({stats.n_empty_payload_banks:,} empty, "
                                    f"{stats.n_unmapped_payload_banks:,} unmapped)")
    totals.add_row("Frames", f"{checks.first_frame:,} — {checks.last_frame:,}, stride {checks.stride}, "
                             f"{checks.missing_frames:,} missing, {checks.duplicate_frames:,} duplicated, "
                             f"{checks.out_of_order:,} out of order, {checks.dts_outliers:,} timestamp-step outliers")
    totals.add_row("Frame period", f"{checks.frame_period_ticks:,.0f} ticks = {stats.frame_period_ns / 1000:,.3f} µs "
                                   f"at {stats.tick_ns} ns per tick")
    totals.add_row("Time", f"span {stats.duration_s:,.3f} s, live {stats.live_time_s:,.3f} s "
                           f"(stride {checks.stride}: this file holds 1 frame in {max(checks.stride, 1)})")
    if stats.live_time_s > 0:
        totals.add_row("Hit rate", f"{stats.n_hits / stats.live_time_s / 1e6:,.2f} MHz of live time (fADC250)")
    totals.add_row("Decode errors", f"{stats.errors:,}" + (f" — first: {stats.error_messages[0]}" if stats.error_messages else ""))
    totals.add_row("Decode warnings", f"{stats.warnings:,}" + (f" — first: {stats.warning_messages[0]}" if stats.warning_messages else ""))
    # F15: the SRO hardware fills every AIS slot with one stream's entry; the same check the viewer shows.
    totals.add_row("AIS", f"{stats.frames_with_ais:,} frames with an aggregation info segment, "
                          f"{stats.frames_with_degenerate_ais:,} repeat one entry in every slot")
    totals.add_row("Wall time", f"{stats.wall_seconds:.1f} s with {stats.workers} workers for "
                                f"{stats.bytes_processed / 1e6:,.1f} MB ({stats.bytes_processed / max(stats.wall_seconds, 1e-9) / 1e6:,.0f} MB/s)")
    console.print(totals)

    per_roc = Table(box=box.SIMPLE, title="Per ROC")
    for column in ("ROC", "Module", "Ports", "Hits", "Other words", "Empty banks"):
        per_roc.add_column(column, justify="right" if column not in ("Module",) else "left")
    hits_per_roc = stats.hits_per_roc()
    for roc_i, roc in enumerate(stats.rocs):
        modules = {int(m) for m in stats.module_id[roc_i] if m != fadc250_stream.MODULE_UNKNOWN}
        ports = [int(p) for p, m in zip(stats.ports, stats.module_id[roc_i]) if m != fadc250_stream.MODULE_UNKNOWN]
        per_roc.add_row(str(roc), ", ".join(MODULE_NAMES.get(m, str(m)) for m in sorted(modules)) or "unmapped",
                        str(len(ports)), f"{hits_per_roc[int(roc)]:,}", f"{int(stats.non_fadc_words_per_port[roc_i].sum()):,}",
                        f"{int(stats.empty_banks[roc_i].sum()):,}")
    console.print(per_roc)


@click.command(name="stats")
@click.argument("filename", type=click.Path(exists=True))
@click.option("--out", "-o", "out_path", type=click.Path(), help="Save the statistics as .npz")
@click.option("--workers", "-w", type=int, default=None, help=f"Worker processes (default: cores minus two = {default_workers()})")
@click.option("--rate-bins", type=int, default=DEFAULT_RATE_BINS, show_default=True, help="Time bins of the rate series")
@click.option("--tick-ns", type=float, default=DEFAULT_TICK_NS, show_default=True, help="Timestamp tick length in ns")
@click.option("--records", "-n", type=int, default=None, help="Stop after this many records")
@click.pass_context
def stats_command(ctx, filename, out_path, workers, rate_bins, tick_ns, records):
    """Histogram the streaming fADC250 hits of a file and check its frame sequence."""
    console = Console()
    stats = compute_stream_stats(filename, workers=workers, rate_bins=rate_bins, tick_ns=tick_ns, max_records=records)
    print_stats_summary(console, stats)
    if out_path:
        stats.save(out_path)
        console.print(f"Saved {out_path}")
