"""Fixtures for the web server tests: a served root with a synthetic stream file, a golden file and traps.

``tests/`` is added to ``sys.path`` so the synthetic-file builder of
``test_stream_stats.py`` can be reused; pytest puts only this folder on the path.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_stream_stats import write_synthetic_file  # noqa: E402

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pyevio.web.app import create_app  # noqa: E402

GOLDEN_FILE = Path("/data/evio-lib/golden/cases/cpp/basic_le.evio")
GOLDEN_LZ4_FILE = GOLDEN_FILE.with_name("lz4_le.evio")
CORPUS_FILE = Path("/data/evio-lib/corpus/sro_000791.evio.00000.v6")
TOKEN = "test-token-0123456789"
POLL_TIMEOUT_S = 60


@pytest.fixture
def served_root(tmp_path):
    """ROOT with ``runs/synthetic.evio`` (12 stream frames), a copy of a golden file, a secret outside and a symlink to it."""
    root = tmp_path / "root"
    (root / "runs").mkdir(parents=True)
    path, expected_hits, frame_numbers = write_synthetic_file(root / "runs" / "synthetic.evio")
    if GOLDEN_FILE.is_file():
        shutil.copy(GOLDEN_FILE, root / "basic_le.evio")
    if GOLDEN_LZ4_FILE.is_file():
        shutil.copy(GOLDEN_LZ4_FILE, root / "lz4_le.evio")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.evio").write_bytes(b"not for you")
    os.symlink(outside / "secret.evio", root / "link_outside.evio")
    os.symlink(outside, root / "dir_outside")
    return {"root": root, "synthetic": "runs/synthetic.evio", "expected_hits": expected_hits, "frame_numbers": frame_numbers,
            "golden": "basic_le.evio" if GOLDEN_FILE.is_file() else None,
            "golden_lz4": "lz4_le.evio" if GOLDEN_LZ4_FILE.is_file() else None, "cache_dir": tmp_path / "cache"}


@pytest.fixture
def app(served_root):
    return create_app(served_root["root"], TOKEN, served_root["cache_dir"], stats_workers=1)


@pytest.fixture
def client(app):
    # TestClient's default Host is "testserver", which the Host check rejects by design.
    return TestClient(app, base_url="http://localhost", headers={"X-Token": TOKEN})


def wait_done(client, file_id, timeout_s=POLL_TIMEOUT_S):
    """Poll /progress until the job leaves queued/running; return the last progress."""
    deadline = time.monotonic() + timeout_s
    while True:
        progress = client.get(f"/api/files/{file_id}/progress").json()
        if progress["state"] not in ("queued", "running"):
            return progress
        assert time.monotonic() < deadline, f"analysis did not finish in {timeout_s} s: {progress}"
        time.sleep(0.02)


def open_and_wait(client, path):
    response = client.post("/api/open", json={"path": path})
    assert response.status_code == 200, response.text
    file_id = response.json()["file_id"]
    progress = wait_done(client, file_id)
    assert progress["state"] == "done", progress
    return file_id, progress
