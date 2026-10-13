"""Web server for the EVIO viewer: the browser renders what pyevio computes.

``pyevio web ROOT`` serves the viewer page and a JSON API on 127.0.0.1. The API
lists files under ROOT, runs :func:`pyevio.stream_stats.compute_stream_stats`
as a background job per opened file, caches the result as ``.npz`` and serves
the file structure, record headers, raw words and histograms on demand. The
browser never receives the file.

Modules, one job each:

- ``app``: the FastAPI factory, logging and ``serve()``.
- ``auth``: Host check, token check and path confinement to ROOT.
- ``jobs``: opened files, the analysis job thread and its progress.
- ``cache``: ``.npz`` result cache keyed by path, size and mtime; an entry of another stats version is recomputed in place.
- ``structure``: tree nodes, record pages and raw words from pyevio core.
- ``report``: summary and histogram JSON from ``StreamStats``.
- ``routes``: the ``/api`` endpoints.

FastAPI and uvicorn are the optional extra ``pyevio[web]``; this package
imports them only inside ``app`` and ``routes`` so the core library stays free
of web dependencies.
"""
from pathlib import Path


class ApiError(Exception):
    """A request the API rejects; ``status`` is the HTTP status, ``message`` the JSON ``error`` text."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


EVIO_NAME_MARKERS = (".evio", ".dat", ".ev")


def is_evio_name(name: str) -> bool:
    """True for file names the folder browsers list by default (a marker anywhere in the name: ``run.evio.00000.v6`` counts)."""
    lower = name.lower()
    return any(marker in lower for marker in EVIO_NAME_MARKERS)


WEB_DIR = Path(__file__).resolve().parent
INDEX_HTML = WEB_DIR / "index.html"
INSTALL_HINT = "The web viewer needs FastAPI and uvicorn: pip install 'pyevio[web]'"
