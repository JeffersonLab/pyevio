"""Raw SRO smoke: a JLab hardware file (8-word record headers, truncated tail) opens through the API.

Runs with ``PYEVIO_SRO=1`` or ``PYEVIO_CORPUS=1``. Reference numbers, measured
independently of pyevio: 7,893 records of which the last is truncated, 86,794
events (2 salvaged from the tail), 764,009,774 fADC250 hits.
"""
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import TOKEN
from pyevio.web.app import create_app

SRO_FILE = Path("/data/sro_boyarinov_data_2026/sro_000791.evio.00000")
SRO_RECORDS = 7_893
SRO_EVENTS = 86_794
SRO_HITS = 764_009_774

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("PYEVIO_SRO") != "1" and os.environ.get("PYEVIO_CORPUS") != "1",
                                 reason="set PYEVIO_SRO=1 (or PYEVIO_CORPUS=1) to run on the raw SRO file")]


def test_raw_sro_file_opens_with_truncated_tail(tmp_path):
    if not SRO_FILE.is_file():
        pytest.skip(f"raw SRO file absent: {SRO_FILE}")
    client = TestClient(create_app(SRO_FILE.parent, TOKEN, tmp_path / "cache"), base_url="http://localhost",
                        headers={"X-Token": TOKEN})
    t_open = time.perf_counter()
    opened = client.post("/api/open", json={"path": SRO_FILE.name}).json()
    assert "file_id" in opened, opened
    while True:
        progress = client.get(f"/api/files/{opened['file_id']}/progress").json()
        if progress["state"] not in ("queued", "running"):
            break
        assert time.perf_counter() - t_open < 600
        time.sleep(0.2)
    open_to_done_s = time.perf_counter() - t_open
    assert progress["state"] == "done", progress
    print(f"\nraw SRO open-to-done: {open_to_done_s:.1f} s, records {progress['records_total']}")

    summary = client.get(f"/api/files/{opened['file_id']}/summary").json()
    assert summary["file"]["header_layout"] == "sro-8word" and summary["file"]["has_trailer"] is False
    assert summary["records"]["count"] == SRO_RECORDS and summary["records"]["truncated"] == 1
    assert summary["totals"]["n_events"] == SRO_EVENTS and summary["totals"]["n_frames"] == SRO_EVENTS
    assert summary["totals"]["n_hits"] == SRO_HITS
    checks = {row["name"]: row for row in summary["checks"]}
    assert checks["truncated tail"]["status"] == "error" and checks["truncated tail"]["value"] > 0
    assert summary["scan_checks"][0][0] == "truncated_tail" and summary["scan_checks"][0][3] == 2

    rows = client.get(f"/api/files/{opened['file_id']}/records", params={"from": SRO_RECORDS - 2, "count": 10}).json()
    assert rows["total"] == SRO_RECORDS and len(rows["rows"]) == 2
    last = rows["rows"][-1]
    assert last["is_truncated"] and last["header_length"] == 8 and last["events_found"] == 2
    assert not rows["rows"][0]["is_truncated"]
    rate = client.get(f"/api/files/{opened['file_id']}/hist/rate").json()
    assert sum(rate["counts"]) == SRO_HITS
