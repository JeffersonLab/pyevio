"""Session: open, analyze, cache, switch and refuse files, without Textual."""
import pytest

from conftest import wait_until
from pyevio import stream_stats
from pyevio.gui.session import STATE_EMPTY, Session
from pyevio.web import ApiError


def test_open_analyze_and_read_the_model(session, synthetic):
    assert session.state == STATE_EMPTY and not session.is_open
    session.open(synthetic["path"])
    assert session.is_open and session.state in ("queued", "running", "done")
    # The file is readable before the analysis is done; the statistics are not.
    assert session.records(0, 10)["total"] == 4 and session.children("")["node"]["kind"] == "file"
    assert session.words(0, 4)["count"] == 4
    wait_until(lambda: session.state == "done", what="analysis")
    progress = session.progress()
    assert progress["state"] == "done" and progress["fraction"] == 1.0 and progress["cached"] is False
    summary = session.summary()
    assert summary["totals"]["n_hits"] == synthetic["expected_hits"] and summary["stats"]["cached"] is False
    assert session.histogram("hits_per_frame")["counts"] and sum(session.histogram("hits_per_frame")["counts"]) == 12
    assert session.histogram("occupancy", roc=7)["counts"]
    with pytest.raises(ApiError):
        session.histogram("nonsense")


def test_second_open_comes_from_the_shared_cache(session, synthetic):
    session.open(synthetic["path"])
    wait_until(lambda: session.state == "done", what="analysis")
    assert list(synthetic["cache_dir"].glob("*.npz"))

    other = Session(cache_dir=synthetic["cache_dir"], workers=1)
    other.open(synthetic["path"])
    assert other.state == "done" and other.from_cache and other.progress()["cached"] is True
    assert other.summary()["totals"]["n_hits"] == session.summary()["totals"]["n_hits"]
    assert other.summary()["stats"]["cached"] is True
    other.close()
    assert other.state == STATE_EMPTY and not other.is_open


def test_unreadable_file_keeps_the_current_one(session, synthetic):
    session.open(synthetic["path"])
    with pytest.raises(ValueError):
        session.open(synthetic["garbage"])
    with pytest.raises((ValueError, OSError)):
        session.open(synthetic["folder"] / "missing.evio")
    assert session.is_open and session.path == synthetic["path"].resolve()


def test_switching_files_cancels_the_running_analysis(session, synthetic, monkeypatch):
    import threading
    from pyevio.web import jobs

    release = threading.Event()
    real_compute = stream_stats.compute_stream_stats

    def slow_compute(path, workers=None, progress=None, cancel=None, **_):
        release.wait(5)
        if cancel():
            raise stream_stats.StatsCancelled("cancelled")
        return real_compute(path, workers=1)

    monkeypatch.setattr(jobs, "compute_stream_stats", slow_compute)
    session.open(synthetic["path"])
    first_job = session.job
    assert session.state in ("queued", "running")
    with pytest.raises(ApiError, match="is (queued|running)"):
        session.summary()
    session.open(synthetic["path"])  # the same path: a new job, the old one cancelled
    release.set()
    wait_until(lambda: not first_job.is_active, what="first job to end")
    assert first_job.state == "cancelled"
    wait_until(lambda: session.state == "done", what="second analysis")
    assert session.summary()["totals"]["n_hits"] == synthetic["expected_hits"]
