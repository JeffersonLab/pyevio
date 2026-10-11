"""Fixtures for the console viewer tests: a synthetic stream file, a session with a temporary cache.

``tests/`` is added to ``sys.path`` so the synthetic-file builder of
``test_stream_stats.py`` can be reused; pytest puts only this folder on the path.
The app tests need Textual (``pyevio[gui]``); the session tests do not.
"""
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_stream_stats import write_synthetic_file  # noqa: E402

GOLDEN_FILE = Path("/data/evio-lib/golden/cases/cpp/basic_le.evio")
GOLDEN_LZ4_FILE = GOLDEN_FILE.with_name("lz4_le.evio")
WAIT_S = 60


@pytest.fixture
def synthetic(tmp_path):
    """``runs/synthetic.evio`` (12 stream frames, one other event, a trailer) and a non-EVIO file next to it."""
    folder = tmp_path / "runs"
    folder.mkdir()
    path, expected_hits, frame_numbers = write_synthetic_file(folder / "synthetic.evio")
    (folder / "notes.txt").write_text("not an evio file")
    (folder / "garbage.evio").write_bytes(b"\0" * 64)
    return {"path": Path(path), "folder": folder, "expected_hits": expected_hits, "frame_numbers": frame_numbers,
            "garbage": folder / "garbage.evio", "cache_dir": tmp_path / "cache",
            "golden": GOLDEN_FILE if GOLDEN_FILE.is_file() else None,
            "golden_lz4": GOLDEN_LZ4_FILE if GOLDEN_LZ4_FILE.is_file() else None}


@pytest.fixture
def session(synthetic):
    from pyevio.gui.session import Session

    session = Session(cache_dir=synthetic["cache_dir"], workers=1)
    yield session
    try:
        session.close()
    except BufferError:
        pass


def wait_until(predicate, timeout_s=WAIT_S, what="condition"):
    """Block (outside the app) until ``predicate()`` is true."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, f"{what} did not happen in {timeout_s} s"
        time.sleep(0.02)


async def pilot_wait(pilot, predicate, timeout_s=WAIT_S, what="condition"):
    """Pause the pilot until ``predicate()`` is true, letting the app's timers run."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, f"{what} did not happen in {timeout_s} s"
        await pilot.pause(0.05)
