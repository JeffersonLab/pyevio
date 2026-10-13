"""Corpus smoke: a 15.5 GB SRO file opens through the API with progress, and a second open is a cache hit.

Runs only with ``PYEVIO_CORPUS=1``; the first open analyzes the file with every
spare core (about half a minute on 22 workers), the second returns at once.
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

from conftest import CORPUS_FILE, TOKEN
from pyevio.web.app import create_app

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("PYEVIO_CORPUS") != "1", reason="set PYEVIO_CORPUS=1 to run on the SRO corpus")]

REFERENCE_FRAMES = 86_794  # frames in sro_000791.evio.00000.v6, counted independently of pyevio


def test_corpus_open_progress_cache(tmp_path):
    if not CORPUS_FILE.is_file():
        pytest.skip(f"corpus file absent: {CORPUS_FILE}")
    client = TestClient(create_app(CORPUS_FILE.parent, TOKEN, tmp_path / "cache"), base_url="http://localhost",
                        headers={"X-Token": TOKEN})
    listing = client.get("/api/ls").json()
    assert CORPUS_FILE.name in [entry["name"] for entry in listing["files"]]

    t_open = time.perf_counter()
    opened = client.post("/api/open", json={"path": CORPUS_FILE.name}).json()
    assert opened["state"] in ("queued", "running")
    seen_running = False
    while True:
        progress = client.get(f"/api/files/{opened['file_id']}/progress").json()
        if progress["state"] == "running" and 0 < progress["fraction"] < 1:
            seen_running = True
            assert progress["eta_s"] is not None and progress["records_total"] > 0
        if progress["state"] not in ("queued", "running"):
            break
        assert time.perf_counter() - t_open < 600
        time.sleep(0.2)
    open_to_done_s = time.perf_counter() - t_open
    assert progress["state"] == "done", progress
    assert seen_running, "progress never reported a running fraction between 0 and 1"
    print(f"\ncorpus open-to-done: {open_to_done_s:.1f} s, records {progress['records_total']}")

    summary = client.get(f"/api/files/{opened['file_id']}/summary").json()
    assert summary["totals"]["n_frames"] == REFERENCE_FRAMES and summary["frames"]["stride"] == 4
    assert summary["totals"]["n_hits"] > 0 and summary["records"]["count"] == progress["records_total"]
    rows = client.get(f"/api/files/{opened['file_id']}/records", params={"from": 7880, "count": 100}).json()
    assert rows["total"] == summary["records"]["count"] + 1 and rows["rows"][-1]["is_trailer"]
    hits = client.get(f"/api/files/{opened['file_id']}/hist/rate").json()
    assert sum(hits["counts"]) == summary["totals"]["n_hits"]

    t_again = time.perf_counter()
    again = client.post("/api/open", json={"path": CORPUS_FILE.name}).json()
    assert again["file_id"] == opened["file_id"] and again["state"] == "done"
    assert time.perf_counter() - t_again < 5
    fresh = TestClient(create_app(CORPUS_FILE.parent, TOKEN, tmp_path / "cache"), base_url="http://localhost",
                       headers={"X-Token": TOKEN})
    cached = fresh.post("/api/open", json={"path": CORPUS_FILE.name}).json()
    assert cached["state"] == "done" and cached["cached"]
