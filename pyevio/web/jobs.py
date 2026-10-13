"""Opened files and their analysis jobs.

``FileRegistry.open`` validates a path (it must open as an EVIO file before any
status is sent), derives the ``file_id`` from the cache key and either returns
the cached statistics or starts one :class:`AnalysisJob`. One job runs at a
time: opening another file cancels the running job, since the viewer shows one
file and a 15 GB analysis would otherwise keep every core busy for nothing.

A job is a daemon thread around ``compute_stream_stats`` (which runs its own
worker pool). Progress comes from the callback the stats module calls after
every decoded task; cancellation is a flag the stats module polls between
tasks and chunks.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from pyevio.evio_file import EvioFile
from pyevio.stream_stats import StatsCancelled, StreamStats, compute_stream_stats
from pyevio.web import ApiError
from pyevio.web.auth import resolve_under_root
from pyevio.web.cache import StatsCache, cache_key

log = logging.getLogger("pyevio.web")

STATE_QUEUED, STATE_RUNNING, STATE_DONE, STATE_ERROR, STATE_CANCELLED = "queued", "running", "done", "error", "cancelled"


class AnalysisJob:
    """Runs ``compute_stream_stats`` for one file in a thread and tracks its progress."""

    def __init__(self, file_id: str, path: Path, key: str, cache: StatsCache, workers: Optional[int]):
        self.file_id, self.path, self.key, self.cache, self.workers = file_id, path, key, cache, workers
        self.state = STATE_QUEUED
        self.records_done = 0
        self.records_total = 0
        self.error: Optional[str] = None
        self.stats: Optional[StreamStats] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"pyevio-stats-{file_id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_active(self) -> bool:
        return self.state in (STATE_QUEUED, STATE_RUNNING)

    def _progress(self, records_done: int, records_total: int) -> None:
        self.records_done, self.records_total = records_done, records_total

    def _run(self) -> None:
        self.state = STATE_RUNNING
        self.started_at = time.monotonic()
        try:
            # The module-level name is looked up at run time so tests can substitute it.
            stats = compute_stream_stats(str(self.path), workers=self.workers, progress=self._progress,
                                         cancel=self._cancel.is_set)
            if self._cancel.is_set():
                raise StatsCancelled("cancelled after the last task")
            try:
                self.cache.save(self.key, stats)
            except OSError as error:  # an unwritable cache dir must not turn a finished analysis into an error
                log.warning("cannot cache the result of %s: %s", self.path, error)
            self.stats = stats
            self.state = STATE_DONE
            log.info("analyzed %s in %.1f s (%d records)", self.path, stats.wall_seconds, stats.n_records)
        except StatsCancelled:
            self.state = STATE_CANCELLED
            log.info("cancelled analysis of %s", self.path)
        except Exception as error:  # the job must record every failure; the thread has no other outlet
            self.state = STATE_ERROR
            self.error = f"{type(error).__name__}: {error}"
            log.exception("analysis of %s failed", self.path)
        finally:
            self.finished_at = time.monotonic()

    def snapshot(self) -> dict:
        """Progress as the ``/progress`` endpoint reports it."""
        now = self.finished_at or time.monotonic()
        elapsed = (now - self.started_at) if self.started_at else 0.0
        fraction = self.records_done / self.records_total if self.records_total else (1.0 if self.state == STATE_DONE else 0.0)
        eta = (elapsed / fraction - elapsed) if (self.state == STATE_RUNNING and 0 < fraction < 1) else None
        return {"state": self.state, "fraction": fraction, "records_done": self.records_done,
                "records_total": self.records_total, "elapsed_s": elapsed, "eta_s": eta, "error": self.error}


@dataclass
class OpenFile:
    """One file the viewer opened: identity, its job (if any) and its statistics once known."""
    file_id: str
    path: Path  # resolved, inside the root
    relative: str
    key: str
    job: Optional[AnalysisJob] = None
    stats: Optional[StreamStats] = None
    from_cache: bool = False
    opened_at: float = field(default_factory=time.monotonic)

    def progress(self) -> dict:
        """Progress as ``/progress`` reports it; a file without a job came from the cache."""
        if self.job is None:
            n_records = self.stats.n_records if self.stats is not None else 0
            return {"state": STATE_DONE, "fraction": 1.0, "records_done": n_records, "records_total": n_records,
                    "elapsed_s": 0.0, "eta_s": None, "error": None, "cached": True}
        snapshot = self.job.snapshot()
        snapshot["cached"] = False
        return snapshot


class FileRegistry:
    """Files opened through the API, by ``file_id``; starts and cancels their jobs."""

    def __init__(self, root: Path, cache: StatsCache, workers: Optional[int] = None):
        self.root = root
        self.cache = cache
        self.workers = workers
        self.files: Dict[str, OpenFile] = {}
        self.active_job: Optional[AnalysisJob] = None
        self._lock = threading.Lock()

    def open(self, relative: str) -> OpenFile:
        """Open ``relative`` (under the root), return its entry and start its job unless the result is cached."""
        path = resolve_under_root(self.root, relative)
        if not path.is_file():
            raise ApiError(404, f"no such file under the served root: {relative!r}")
        # Open before answering: an unreadable or non-EVIO file fails here with its reason.
        with EvioFile(str(path)):
            pass
        key = cache_key(path)
        file_id = key[:16]
        with self._lock:
            entry = self.files.get(file_id)
            if entry is None:
                entry = OpenFile(file_id=file_id, path=path, relative=relative, key=key)
                self.files[file_id] = entry
            if entry.stats is None:
                entry.stats = self.cache.load(key)
                entry.from_cache = entry.stats is not None
            if entry.stats is None and (entry.job is None or not entry.job.is_active):
                self._start_job(entry)
        return entry

    def _start_job(self, entry: OpenFile) -> None:
        if self.active_job is not None and self.active_job.is_active and self.active_job.file_id != entry.file_id:
            log.info("cancelling analysis of %s: %s opened", self.active_job.path, entry.path)
            self.active_job.cancel()
        entry.job = AnalysisJob(entry.file_id, entry.path, entry.key, self.cache, self.workers)
        self.active_job = entry.job
        entry.job.start()

    def get(self, file_id: str) -> OpenFile:
        entry = self.files.get(file_id)
        if entry is None:
            raise ApiError(404, f"unknown file id {file_id!r}; open the file first")
        return entry

    def stats_of(self, file_id: str) -> StreamStats:
        """The statistics of a finished job; 409 while the analysis is not done."""
        entry = self.get(file_id)
        if entry.stats is None and entry.job is not None and entry.job.state == STATE_DONE:
            entry.stats = entry.job.stats
        if entry.stats is None:
            state = entry.job.state if entry.job else STATE_QUEUED
            raise ApiError(409, f"analysis of {entry.relative!r} is {state}; poll /progress until done")
        return entry.stats
