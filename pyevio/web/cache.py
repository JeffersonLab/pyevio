"""Result cache: one ``.npz`` per analyzed file under the cache directory.

The key is sha256 over the resolved path, the file size, its mtime (ns) and
``STATS_VERSION``, so a rewritten file or a changed statistics layout misses
the cache instead of serving stale numbers. The key doubles as the viewer's
``file_id``: opening the same unchanged file twice yields the same id and the
cached result.
"""
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from pyevio.stream_stats import STATS_VERSION, StreamStats

log = logging.getLogger("pyevio.web")

DEFAULT_CACHE_DIR = Path("~/.cache/pyevio/stats")


def cache_key(path: Path) -> str:
    """sha256 of (resolved path, size, mtime_ns, STATS_VERSION) as hex."""
    stat = path.stat()
    digest = hashlib.sha256()
    for part in (str(path), stat.st_size, stat.st_mtime_ns, STATS_VERSION):
        digest.update(str(part).encode())
        digest.update(b"\0")
    return digest.hexdigest()


class StatsCache:
    """Load and save :class:`StreamStats` by key; every failure to load counts as a miss."""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = (directory or DEFAULT_CACHE_DIR).expanduser().resolve()

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.npz"

    def load(self, key: str) -> Optional[StreamStats]:
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            return StreamStats.load(str(path))
        except (OSError, ValueError, KeyError) as error:
            log.warning("cache entry %s unreadable (%s); recomputing", path, error)
            return None

    def save(self, key: str, stats: StreamStats) -> Path:
        """Write atomically: numpy appends ``.npz`` to any other suffix, so the temporary name ends in ``.npz`` too."""
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.path_for(key)
        partial = self.directory / f"{key}.partial.npz"
        stats.save(str(partial))
        os.replace(partial, final)
        return final
