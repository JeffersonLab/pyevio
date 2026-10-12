"""Headless-browser smoke tests of the viewer, one per tab.

The FastAPI app runs in a uvicorn thread on a free port; Playwright drives Chromium against it. Each test
asserts that the tab renders what the API returns: tile values equal /summary, charts hold a path, the records
table has one row per record, the tree expands to a payload leaf and the hex view equals /words.

Skipped when Playwright or its Chromium is missing. Install both with
``uv add --dev playwright`` and ``uv run playwright install chromium``.

Screenshots of each tab go to ``$PYEVIO_SCREENSHOT_DIR`` (default /data/pyevio/plots/phase4) when that folder
can be created; a machine without it loses nothing.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from conftest import TOKEN, open_and_wait

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

SCREENSHOT_DIR = Path(os.environ.get("PYEVIO_SCREENSHOT_DIR", "/data/pyevio/plots/phase4"))
LOAD_TIMEOUT_MS = 30_000


@pytest.fixture(scope="session")
def browser():
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as error:  # the package is installed but Chromium is not: `playwright install chromium`
            pytest.skip(f"Chromium not available for Playwright: {error}")
        yield browser
        browser.close()


@pytest.fixture
def server(app):
    """The app on 127.0.0.1 with a free port; uvicorn skips signal handlers when it runs off the main thread."""
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    port = instance.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(5)


@pytest.fixture
def page(browser, server):
    """A page on the viewer with the token in the URL; console and page errors fail the test."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    # Chromium logs every non-2xx response as "Failed to load resource"; a 400 the page displays is not a page error.
    page.on("console", lambda message: errors.append(f"console.error: {message.text}")
            if message.type == "error" and not message.text.startswith("Failed to load resource") else None)
    page.goto(f"{server}/?token={TOKEN}")
    page.wait_for_selector(".bitem", timeout=LOAD_TIMEOUT_MS)
    yield page
    page.close()
    assert errors == [], errors


def open_in_viewer(page, relative_path):
    """Open a file through the folder browser and wait until the tabs are enabled for it."""
    if page.get_attribute("#browser", "hidden") is not None:
        page.click("#browseBtn")
    while page.inner_text("#brPath") != "/":
        shown = page.inner_text("#brPath")
        page.click("#brUp")
        page.wait_for_function("previous => document.querySelector('#brPath').textContent !== previous", arg=shown)
    *folders, name = relative_path.split("/")
    for folder in folders:
        page.click(f".bitem.dir:has-text('{folder}')")
        page.wait_for_function("folder => document.querySelector('#brPath').textContent.endsWith(folder)", arg=folder)
    page.click(f".bitem.file:has-text('{name}')")
    page.wait_for_selector("body[data-file-id]", timeout=LOAD_TIMEOUT_MS)
    return page.get_attribute("body", "data-file-id")


def show_tab(page, name):
    page.click(f".tab[data-tab={name}]")
    page.wait_for_selector(f"#v-{name}:not([hidden])")


def screenshot(page, name):
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"), full_page=True)


def test_overview_tiles_equal_summary(page, client, served_root):
    file_id = open_in_viewer(page, served_root["synthetic"])
    api_file_id, _ = open_and_wait(client, served_root["synthetic"])
    assert file_id == api_file_id
    summary = client.get(f"/api/files/{file_id}/summary").json()
    show_tab(page, "overview")
    tiles = {tile.get_attribute("data-key"): json.loads(tile.get_attribute("data-value")) for tile in page.query_selector_all(".tile")}
    for key in ("n_records", "n_events", "n_frames", "n_hits", "hit_rate_hz", "hits_per_frame", "errors"):
        assert tiles[key] == summary["totals"][key], key
    assert page.inner_text(".tile[data-key=n_hits] .v") == str(served_root["expected_hits"])
    statuses = [row.get_attribute("data-status") for row in page.query_selector_all("#checks .ck")]
    assert statuses == [check["status"] for check in summary["checks"]]
    standout = page.inner_text("#standout")
    assert "missing frames" in standout and "duplicate frames" in standout
    assert "little-endian" in page.inner_text("#fileFacts")
    screenshot(page, "overview")


def test_plots_render_charts_and_heatmap(page, served_root):
    open_in_viewer(page, served_root["synthetic"])
    show_tab(page, "plots")
    page.wait_for_selector("#chartChCharge path.series, #chartChCharge text", timeout=LOAD_TIMEOUT_MS)
    for chart in ("chartHitsPerFrame", "chartRate", "chartRocRate", "chartRocCharge", "chartRocTime", "chartChRate", "chartChCharge", "chartChTime"):
        assert page.query_selector(f"#{chart} path.series") is not None, chart
    assert "time since the first frame (s)" in page.text_content("#chartRate")
    live = page.query_selector_all("#chartOccupancy rect.cell")
    dead = page.query_selector_all("#chartOccupancy rect.cell-dead")
    assert len(live) + len(dead) == 2 * 5 and len(live) == 5  # ROCs 7 and 44 × ports 1, 3, 4, 5, 6; each ROC maps 2—3 ports
    assert [option.inner_text() for option in page.query_selector_all("#selRoc option")] == ["7 (fADC250)", "44 (DCRB)"]
    page.select_option("#selRoc", "44")
    page.wait_for_function("() => document.querySelector('#rocInfo').textContent.includes('non-fADC')")
    assert "60 non-fADC words" in page.inner_text("#rocInfo")
    screenshot(page, "plots")


def test_records_table_rows_and_click(page, client, served_root):
    file_id = open_in_viewer(page, served_root["synthetic"])
    open_and_wait(client, served_root["synthetic"])
    records = client.get(f"/api/files/{file_id}/records").json()
    show_tab(page, "records")
    page.wait_for_selector("#recordsTable tbody tr")
    rows = page.query_selector_all("#recordsTable tbody tr")
    assert len(rows) == records["total"] == 4
    assert [row.get_attribute("data-id") for row in rows] == ["r0", "r1", "r2", "t"]
    assert "(trailer)" in rows[3].inner_text() and "set" in rows[3].inner_text()
    screenshot(page, "records")
    rows[1].click()
    page.wait_for_selector("#v-structure:not([hidden]) .node[data-id='r1/e0']")
    assert "record_number" in page.inner_text("#detail")


def test_structure_expands_to_hits_and_hex_equals_words(page, client, served_root):
    file_id = open_in_viewer(page, served_root["synthetic"])
    open_and_wait(client, served_root["synthetic"])
    show_tab(page, "structure")
    page.click(".node[data-id=r1] > .row")
    page.click(".node[data-id='r1/e0'] > .row")
    page.click(".node[data-role=roc_bank] > .row")
    page.click(".node[data-role=payload_hits] > .row")
    page.wait_for_selector(".node[data-id$='/h15']")
    hits = page.query_selector_all(".node[data-role=payload_hits] .node")
    assert len(hits) == 16 and "ch 15" in hits[15].inner_text()
    payload_id = page.get_attribute(".node[data-role=payload_hits]", "data-id")
    payload = client.get(f"/api/files/{file_id}/structure", params={"node": payload_id}).json()["node"]
    words = client.get(f"/api/files/{file_id}/words", params={"offset": payload["offset"], "count": payload["size"] // 4, "record": 1}).json()
    page.wait_for_selector("#detail .hex")
    shown = [cell.inner_text() for cell in page.query_selector_all("#detail .hex span:not(.a)") if cell.inner_text()]
    assert shown == words["words"]
    assert len(page.query_selector_all("#detail .hex span.h")) == 2  # a bank's two header words are highlighted
    page.click(".node[data-role=roc_stream_info] > .row")
    page.click(".node[data-role=payload_map] > .row")
    page.wait_for_selector("#detail table.t")
    assert [row.inner_text().split("\t") for row in page.query_selector_all("#detail table.t tbody tr")] == [
        ["0", "1", "0", "fADC250"], ["1", "3", "0", "fADC250"], ["2", "5", "0", "fADC250"]]
    screenshot(page, "structure")


def test_golden_file_shows_na_and_empty_plots(page, served_root):
    if not served_root["golden_lz4"]:
        pytest.skip("golden set absent")
    open_in_viewer(page, served_root["golden_lz4"])
    show_tab(page, "overview")
    assert page.inner_text(".tile[data-key=hit_rate_hz] .v") == "n/a"
    assert page.inner_text(".tile[data-key=n_frames] .v") == "0"
    show_tab(page, "plots")
    page.wait_for_function("() => document.querySelector('#chartOccupancy').textContent.includes('no stream frames')")
    assert page.query_selector("#chartRate path.series") is None
    show_tab(page, "structure")
    page.click(".node[data-id=r0] > .row")
    page.click(".node[data-id='r0/e0'] > .row")
    page.wait_for_selector("#detail .hex")
    assert "compressed\tyes" in page.inner_text("#detail")
    assert "in record 0's buffer" in page.inner_text("#detail")


def test_failed_open_is_reported(page, served_root):
    """A file the server cannot read shows its error; the tabs stay disabled."""
    (served_root["root"] / "runs" / "garbage.evio").write_bytes(b"\0" * 64)
    page.click(".bitem.dir:has-text('runs')")
    page.click(".bitem.file:has-text('garbage.evio')")
    page.wait_for_selector("#stErr:not([hidden])")
    assert "EVIO" in page.inner_text("#stErr")
    assert page.inner_text("#stMsg") == "Open failed."
    assert all(button.get_attribute("disabled") is not None for button in page.query_selector_all(".tab"))


def test_second_open_replaces_the_first(page, served_root):
    """The page shows the file opened last; the earlier file's content is gone."""
    first = open_in_viewer(page, served_root["synthetic"])
    if not served_root["golden"]:
        pytest.skip("golden set absent")
    second = open_in_viewer(page, served_root["golden"])
    assert second != first
    show_tab(page, "overview")
    assert page.inner_text(".tile[data-key=n_frames] .v") == "0"
    assert page.inner_text("#stFile") == served_root["golden"]
