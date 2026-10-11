"""PNG export of the histogram dicts, without Textual."""
import pytest

pytest.importorskip("matplotlib")

from pyevio.gui.export import default_png_name, nice_ticks, occupancy_grid, rebin, save_histogram_png, series  # noqa: E402
from pyevio.stream_stats import compute_stream_stats  # noqa: E402
from pyevio.web.report import HISTOGRAM_NAMES, histogram  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def stats(tmp_path_factory):
    from test_stream_stats import write_synthetic_file

    path, _, _ = write_synthetic_file(tmp_path_factory.mktemp("export") / "synthetic.evio")
    return compute_stream_stats(str(path), workers=1)


@pytest.mark.parametrize("name", HISTOGRAM_NAMES)
def test_save_histogram_png_writes_a_png_for_every_histogram(stats, name, tmp_path):
    hist = histogram(stats, name)
    path = save_histogram_png(hist, tmp_path / f"{name}.png", f"{name} of the synthetic file")
    assert path == tmp_path / f"{name}.png"
    assert path.read_bytes()[:8] == PNG_SIGNATURE and path.stat().st_size > 1000


def test_save_histogram_png_at_roc_and_channel_level(stats, tmp_path):
    for selection in ({"roc": 7}, {"roc": 7, "port": 1, "channel": 3}, {"roc": 44}):
        for name in ("occupancy", "charge", "rate", "time_in_frame"):
            hist = histogram(stats, name, **selection)
            path = save_histogram_png(hist, tmp_path / default_png_name("synthetic.evio", name, hist["selection"]))
            assert path.read_bytes()[:8] == PNG_SIGNATURE
    assert (tmp_path / "synthetic_charge_roc7_port1_ch3.png").is_file()
    assert (tmp_path / "synthetic_occupancy_roc44.png").is_file()


def test_default_png_name():
    assert default_png_name("/data/runs/synthetic.evio", "charge", {"roc": 7, "port": 1, "ch": 3}) == "synthetic_charge_roc7_port1_ch3.png"
    assert default_png_name("synthetic.evio", "hits_per_frame", {"roc": None, "port": None, "ch": None}) == "synthetic_hits_per_frame.png"
    assert default_png_name("run_001.evio.0", "rate", {"roc": 44}) == "run_001.evio_rate_roc44.png"
    assert default_png_name(None, "occupancy", None) == "histogram_occupancy.png"


def test_occupancy_grid_marks_unmapped_ports(stats):
    grid = occupancy_grid(histogram(stats, "occupancy"))
    assert grid["rows"] == ["ROC 7", "ROC 44"] and grid["cols"] == ["1", "3", "4", "5", "6"]
    assert grid["dead"][0] == [False, False, True, False, True] and grid["dead"][1] == [True, True, False, True, False]
    assert sum(grid["values"][0]) == stats.n_hits and grid["empty"] is None
    roc = occupancy_grid(histogram(stats, "occupancy", roc=7))
    assert roc["rows"] == ["port 1", "port 3", "port 4", "port 5", "port 6"] and len(roc["cols"]) == 16
    assert roc["dead"][2] == [True] * 16 and not any(roc["dead"][0])
    channel = occupancy_grid(histogram(stats, "occupancy", roc=7, port=1, channel=3))
    assert channel["rows"] == ["port 1"] and channel["cols"] == ["3"] and channel["dead"] == [[False]]
    port = occupancy_grid(histogram(stats, "occupancy", roc=7, port=4))
    assert port["rows"] == ["port 4"] and len(port["cols"]) == 16 and port["dead"] == [[True] * 16]


def test_occupancy_without_rocs_is_empty_and_still_exports(tmp_path):
    hist = {"name": "occupancy", "rocs": [], "ports": [1, 3], "channels": 16, "module_id": [], "counts": [],
            "y_unit": "hits", "selection": {"roc": None, "port": None, "ch": None}}
    grid = occupancy_grid(hist)
    assert grid["rows"] == [] and grid["empty"] == "no stream frames"
    path = save_histogram_png(hist, tmp_path / "empty.png")
    assert path.read_bytes()[:8] == PNG_SIGNATURE


def test_series_and_rebin_keep_the_totals(stats):
    charge = histogram(stats, "charge")
    data = series(charge)
    assert len(data["edges"]) == len(data["values"]) + 1 and data["log_y"] and data["empty"] is None
    merged = rebin(charge, 20)
    assert len(merged["counts"]) <= 20 and sum(merged["counts"]) == sum(charge["counts"])
    assert merged["edges"][0] == charge["edges"][0] and merged["edges"][-1] == charge["edges"][-1]
    rate = histogram(stats, "rate")
    merged = rebin(rate, 16)
    assert len(merged["rate_hz"]) <= 16 and sum(merged["counts"]) == sum(rate["counts"])
    assert sum(merged["frames_per_bin"]) == sum(rate["frames_per_bin"])
    # The merged rate is the merged counts over the merged live time.
    live = [frames for frames in merged["frames_per_bin"]]
    for rate_hz, counts, frames in zip(merged["rate_hz"], merged["counts"], live):
        if frames:
            assert rate_hz == pytest.approx(counts / (frames * stats.frame_period_ns * 1e-9))
    assert rebin(charge, 1000) is charge
    assert series(histogram(stats, "charge", roc=44))["empty"] == "no hits"


def test_nice_ticks():
    assert nice_ticks(0, 64, 5) == [0, 20, 40, 60]
    assert nice_ticks(0, 1, 4) == pytest.approx([0, 0.5, 1.0])
    assert nice_ticks(0, 0, 4) == [0]
