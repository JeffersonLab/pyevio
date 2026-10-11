"""The Plots tab: the three levels, the keys, a ROC without hits, the PNG save."""
import pytest

textual = pytest.importorskip("textual")
pytest.importorskip("textual_plotext")

from conftest import pilot_wait  # noqa: E402
from pyevio.gui.app import PyEvioGui  # noqa: E402
from pyevio.gui.plots import HistogramPlot, PlotsPane  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


async def open_plots(app, pilot):
    """Wait for the analysis, switch to the Plots tab (the pane takes the focus) and return the pane."""
    await pilot_wait(pilot, lambda: app.plots.summary is not None, what="stats_ready")
    await pilot.press("2")
    await pilot.pause()
    return app.query_one(PlotsPane)


def rendered(plot: HistogramPlot) -> str:
    return plot.render().plain


async def test_file_level_shows_hits_per_frame_rate_and_occupancy(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        titles = pane.titles()
        assert len(titles) == 3
        assert "Hits per frame" in titles[0] and "Hit rate" in titles[1] and "Occupancy" in titles[2]
        assert all("file" in title for title in titles)
        hits, rate, occupancy = pane.plots[:3]
        assert rate.hist["name"] == "rate" and rate.hist["selection"] == {"roc": None, "port": None, "ch": None}
        assert "hits per frame" in rendered(hits) and "frames" in rendered(hits)
        assert "hit rate (Hz)" in rendered(rate) and "(s)" in rendered(rate)
        text = rendered(occupancy)
        assert "ROC 7" in text and "ROC 44" in text and "payload port" in text
        assert "–" in text  # ROC 44 has ports its map does not list
        assert not pane.plots[3].display
        assert isinstance(app.focused, HistogramPlot)


async def test_roc_level_has_four_plots_named_after_the_roc(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("r")
        await pilot.pause()
        titles = pane.titles()
        assert len(titles) == 4 and pane.level == "roc" and pane.roc == 7
        assert "Occupancy" in titles[0] and "Hit rate" in titles[1] and "Charge" in titles[2] and "Time in frame" in titles[3]
        assert all("ROC 7" in title for title in titles)
        charge = pane.plots[2]
        assert charge.hist["selection"]["roc"] == 7 and sum(charge.hist["counts"]) > 0
        assert "charge (13-bit ADC units)" in rendered(charge)
        assert "channel" in rendered(pane.plots[0]) and "port 1" in rendered(pane.plots[0])
        assert str(app.query_one("#status-error").render()) == ""


async def test_channel_level_steps_to_port_1_channel_3(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("plus")
            await pilot.pause()
        assert pane.level == "channel" and (pane.roc, pane.port, pane.channel) == (7, 1, 3)
        titles = pane.titles()
        assert len(titles) == 3 and all("ROC 7 port 1 ch 3" in title for title in titles)
        assert pane.plots[1].hist["selection"] == {"roc": 7, "port": 1, "ch": 3}
        # Stepping past the last channel of port 1 rolls over into port 3; the port keys move one port.
        pane.channel = 15
        await pilot.press("plus")
        await pilot.pause()
        assert (pane.port, pane.channel) == (3, 0)
        await pilot.press("minus")
        await pilot.pause()
        assert (pane.port, pane.channel) == (1, 15)
        await pilot.press("right_curly_bracket")
        await pilot.pause()
        assert pane.port == 3 and "port 3 ch 15" in pane.titles()[0]
        # The Select widgets follow the keys.
        from textual.widgets import Select

        assert app.query_one("#plots-port", Select).value == 3
        assert app.query_one("#plots-channel", Select).value == 15
        assert app.query_one("#plots-level", Select).value == "channel"
        # And the pane follows the Select widgets (the mouse user's path).
        app.query_one("#plots-channel", Select).value = 9
        await pilot.pause()
        assert pane.channel == 9 and "port 3 ch 9" in pane.titles()[0]
        app.query_one("#plots-roc", Select).value = 44
        await pilot.pause()
        assert (pane.roc, pane.port, pane.channel) == (44, 4, 0) and "ROC 44 port 4 ch 0" in pane.titles()[0]


async def test_keys_do_not_crash_on_a_roc_without_hits(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("r")
        await pilot.press("right_square_bracket")  # ROC 44, the DCRB
        await pilot.pause()
        assert pane.roc == 44 and all("ROC 44" in title for title in pane.titles())
        assert "no hits" in rendered(pane.plots[2])
        assert "–" in rendered(pane.plots[0])  # ports the DCRB's map does not list
        for key in ("c", "plus", "plus", "minus", "right_curly_bracket", "left_curly_bracket", "right_square_bracket",
                    "left_square_bracket", "f", "r", "c"):
            await pilot.press(key)
            await pilot.pause()
            for plot in pane.plots:
                if plot.display:
                    rendered(plot)
        assert pane.roc == 7 and pane.level == "channel"
        assert str(app.query_one("#status-error").render()) == ""


async def test_s_saves_a_png_and_notifies(session, synthetic, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = PyEvioGui(session, start_path=synthetic["path"])
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        path = tmp_path / "synthetic_hits_per_frame.png"
        assert path.is_file() and path.read_bytes()[:8] == PNG_SIGNATURE
        assert notices and str(path) in notices[-1]
        # The focused plot is the one saved: Tab moves to the rate plot.
        await pilot.press("tab")
        await pilot.press("s")
        await pilot.pause()
        assert (tmp_path / "synthetic_rate.png").is_file()
        # S saves every plot of the level; at channel level the names carry the selection.
        await pilot.press("c")
        await pilot.press("S")
        await pilot.pause()
        for name in ("rate", "charge", "time_in_frame"):
            assert (tmp_path / f"synthetic_{name}_roc7_port1_ch0.png").read_bytes()[:8] == PNG_SIGNATURE
        assert "3 PNGs" in notices[-1]
        assert pane.level == "channel"


async def test_file_closed_clears_the_plots(session, synthetic):
    app = PyEvioGui(session, start_path=synthetic["path"])
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await open_plots(app, pilot)
        pane.file_closed()
        await pilot.pause()
        assert pane.summary is None and all(plot.hist is None for plot in pane.plots)
        assert not pane.query_one("#plots-grid").display
        assert "Open a file" in str(pane.query_one("#plots-message").render())
