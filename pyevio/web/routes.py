"""The ``/api`` endpoints. Every handler is thin: resolve, open the file, call core or stats, return JSON.

File endpoints open the ``EvioFile`` first, so a file that vanished or cannot be
read fails with a status instead of a broken response. Opening is cheap (a
header scan, ~50 ms on a 15 GB file) and a fresh handle per request keeps the
record cache from growing across requests.
"""
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from pyevio.evio_file import EvioFile
from pyevio.web import ApiError
from pyevio.web.auth import inside_root, resolve_under_root
from pyevio.web.jobs import FileRegistry
from pyevio.web.report import build_summary, histogram
from pyevio.web.structure import DEFAULT_CHILDREN, MAX_CHILDREN, MAX_WORDS, children_of, record_rows, words_at

router = APIRouter(prefix="/api")

# Files the folder browser lists by default; the corpus names end in ".evio.00000.v6", so match the stem too.
EVIO_NAME_MARKERS = (".evio", ".dat", ".ev")


def is_evio_name(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in EVIO_NAME_MARKERS)


def registry_of(request: Request) -> FileRegistry:
    return request.app.state.registry


class OpenRequest(BaseModel):
    path: str


@router.get("/ls")
def list_directory(request: Request, dir: str = "", all: bool = False) -> dict:
    """Folders and EVIO files of one directory under the root; paths are relative to the root."""
    root: Path = request.app.state.root
    directory = resolve_under_root(root, dir)
    if not directory.is_dir():
        raise ApiError(404, f"no such folder under the served root: {dir!r}")
    dirs, files = [], []
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
    except PermissionError:
        raise ApiError(403, f"no permission to read {dir!r}")
    for entry in entries:
        if entry.name.startswith(".") or not inside_root(root, Path(entry.path)):
            continue  # hidden, or a symlink that leaves the root: it could not be opened anyway
        try:
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file() and (all or is_evio_name(entry.name)):
                stat = entry.stat()
                files.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
        except OSError:
            continue
    relative = directory.relative_to(root).as_posix() if directory != root else ""
    parent = None if directory == root else (directory.parent.relative_to(root).as_posix() if directory.parent != root else "")
    return {"dir": relative, "parent": parent, "dirs": dirs, "files": files}


@router.post("/open")
def open_file(request: Request, body: OpenRequest) -> dict:
    """Open a file under the root; starts its analysis unless cached. Returns the file id and the job state."""
    entry = registry_of(request).open(body.path)
    return {"file_id": entry.file_id, "path": entry.relative, "size": entry.path.stat().st_size, **entry.progress()}


@router.get("/files/{file_id}/progress")
def progress(request: Request, file_id: str) -> dict:
    return {"file_id": file_id, **registry_of(request).get(file_id).progress()}


@router.get("/files/{file_id}/summary")
def summary(request: Request, file_id: str) -> dict:
    registry = registry_of(request)
    entry = registry.get(file_id)
    stats = registry.stats_of(file_id)
    with EvioFile(str(entry.path)) as evio_file:
        return {"file_id": file_id, **build_summary(evio_file, stats, entry.relative, entry.from_cache)}


@router.get("/files/{file_id}/records")
def records(request: Request, file_id: str, start: int = Query(0, alias="from", ge=0),
            count: int = Query(DEFAULT_CHILDREN, ge=0, le=MAX_CHILDREN)) -> dict:
    entry = registry_of(request).get(file_id)
    with EvioFile(str(entry.path)) as evio_file:
        return {"file_id": file_id, **record_rows(evio_file, start, count)}


@router.get("/files/{file_id}/structure")
def structure(request: Request, file_id: str, node: str = "", start: int = Query(0, alias="from", ge=0),
              count: int = Query(DEFAULT_CHILDREN, ge=0, le=MAX_CHILDREN)) -> dict:
    entry = registry_of(request).get(file_id)
    with EvioFile(str(entry.path)) as evio_file:
        return {"file_id": file_id, **children_of(evio_file, node, start, count)}


@router.get("/files/{file_id}/words")
def words(request: Request, file_id: str, offset: int = Query(..., ge=0), count: int = Query(64, ge=0),
          record: Optional[int] = Query(None, ge=0)) -> dict:
    """Raw 32-bit words as hex strings; ``count`` is capped at MAX_WORDS.

    ``offset`` is a file offset, or with ``record=N`` an offset into that record's
    buffer (the decompressed bytes of an LZ4/gzip record), as the structure tree reports them.
    """
    entry = registry_of(request).get(file_id)
    with EvioFile(str(entry.path)) as evio_file:
        return {"file_id": file_id, **words_at(evio_file, offset, min(count, MAX_WORDS), record)}


@router.get("/files/{file_id}/hist/{name}")
def hist(request: Request, file_id: str, name: str, roc: Optional[int] = None, port: Optional[int] = None,
         ch: Optional[int] = None) -> dict:
    stats = registry_of(request).stats_of(file_id)
    return {"file_id": file_id, **histogram(stats, name, roc, port, ch)}
