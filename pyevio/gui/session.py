"""The one open file of the console viewer: its ``EvioFile``, its analysis job and its statistics.

No Textual here. The app calls :meth:`Session.open` from its main thread, polls
:meth:`Session.progress` on a timer while the analysis runs in the job's own
thread, and reads everything else (summary, tree nodes, record rows, raw
words, histograms) through the same functions the web server uses, so the two
viewers show the same numbers for the same file.

The job thread never touches the ``EvioFile`` of the session: ``compute_stream_stats``
opens the file itself in its workers. Every pane holds only the dicts the model
functions return, never a ``Record`` or ``Bank``; a live numpy view would make
``EvioFile.close()`` raise ``BufferError`` on the next file switch.
"""
import time
from pathlib import Path
from typing import Optional

from pyevio.evio_file import EvioFile
from pyevio.stream_stats import StreamStats
from pyevio.web import ApiError
from pyevio.web.cache import StatsCache, cache_key
from pyevio.web.jobs import STATE_DONE, STATE_QUEUED, AnalysisJob
from pyevio.web.report import build_summary, histogram
from pyevio.web.structure import children_of, record_rows, words_at

STATE_EMPTY = "empty"  # no file open


class Session:
    """Owns the open file; see the module docstring for the threading rules."""

    def __init__(self, cache_dir: Optional[Path] = None, workers: Optional[int] = None):
        self.cache = StatsCache(cache_dir)
        self.workers = workers
        self.path: Optional[Path] = None
        self.evio_file: Optional[EvioFile] = None
        self.key: Optional[str] = None
        self.job: Optional[AnalysisJob] = None
        self.stats: Optional[StreamStats] = None
        self.from_cache = False
        self.opened_at: Optional[float] = None

    # ---------------------------------------------------------------- open / close
    def open(self, path) -> None:
        """Open ``path``; the previous file closes and its running analysis is cancelled.

        A file pyevio cannot read raises ``ValueError`` (or ``OSError``) before the
        previous file is touched, so the viewer keeps showing it. The cached statistics
        are loaded when present; otherwise the analysis starts in its thread.
        """
        path = Path(path).expanduser().resolve()
        new_file = EvioFile(str(path))
        self.close()
        self.path, self.evio_file, self.opened_at = path, new_file, time.monotonic()
        self.key = cache_key(path)
        self.stats = self.cache.load(self.key)
        self.from_cache = self.stats is not None
        if self.stats is None:
            self.job = AnalysisJob(self.key[:16], path, self.key, self.cache, self.workers)
            self.job.start()

    def close(self) -> None:
        """Cancel the running analysis and close the file. Raises ``BufferError`` while a view of the mmap is alive."""
        if self.job is not None and self.job.is_active:
            self.job.cancel()
        self.job = None
        self.stats = None
        self.from_cache = False
        if self.evio_file is not None:
            evio_file, self.evio_file = self.evio_file, None
            evio_file.close()
        self.path = self.key = None

    @property
    def is_open(self) -> bool:
        return self.evio_file is not None

    # ---------------------------------------------------------------- analysis state
    @property
    def state(self) -> str:
        """``empty``, or the job state: ``queued``, ``running``, ``done``, ``error``, ``cancelled``."""
        if self.evio_file is None:
            return STATE_EMPTY
        if self.stats is not None:
            return STATE_DONE
        if self.job is None:
            return STATE_QUEUED
        if self.job.state == STATE_DONE and self.job.stats is not None:
            self.stats = self.job.stats  # pick the finished result up on the first poll after the thread set it
            return STATE_DONE
        return self.job.state

    def progress(self) -> dict:
        """Progress as the web ``/progress`` endpoint reports it, plus ``cached`` and ``state``."""
        state = self.state
        if state == STATE_EMPTY:
            return {"state": state, "fraction": 0.0, "records_done": 0, "records_total": 0, "elapsed_s": 0.0,
                    "eta_s": None, "error": None, "cached": False}
        if self.job is None or (state == STATE_DONE and self.from_cache):
            n_records = self.stats.n_records if self.stats is not None else 0
            return {"state": STATE_DONE, "fraction": 1.0, "records_done": n_records, "records_total": n_records,
                    "elapsed_s": 0.0, "eta_s": None, "error": None, "cached": True}
        snapshot = self.job.snapshot()
        snapshot["cached"] = False
        return snapshot

    @property
    def error(self) -> Optional[str]:
        return self.job.error if self.job is not None else None

    def require_stats(self) -> StreamStats:
        if self.state != STATE_DONE or self.stats is None:
            raise ApiError(409, f"the analysis of {self.path.name if self.path else 'the file'} is {self.state}")
        return self.stats

    def require_file(self) -> EvioFile:
        if self.evio_file is None:
            raise ApiError(409, "no file is open")
        return self.evio_file

    # ---------------------------------------------------------------- model, as the web API serves it
    def summary(self) -> dict:
        """The Overview data (``pyevio.web.report.build_summary``); needs the finished analysis."""
        return build_summary(self.require_file(), self.require_stats(), str(self.path), self.from_cache)

    def histogram(self, name: str, roc: Optional[int] = None, port: Optional[int] = None, channel: Optional[int] = None) -> dict:
        return histogram(self.require_stats(), name, roc, port, channel)

    def records(self, start: int = 0, count: int = 500) -> dict:
        """A page of record rows; works before the analysis is done."""
        return record_rows(self.require_file(), start, count)

    def children(self, node: str = "", start: int = 0, count: int = 500) -> dict:
        """A structure node and a page of its children (``pyevio.web.structure.children_of``)."""
        return children_of(self.require_file(), node, start, count)

    def words(self, offset: int, count: int = 64, record_index: Optional[int] = None) -> dict:
        return words_at(self.require_file(), offset, count, record_index)
