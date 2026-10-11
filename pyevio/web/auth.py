"""Request guards: Host check, token check and path confinement to the served root.

The server binds 127.0.0.1, which keeps other machines out but not other
local users or web pages. Three checks close those holes:

- A random token, printed once in the start URL and required on every
  ``/api`` request, so another local user cannot read the owner's files by
  guessing the port.
- A ``Host`` header limited to ``localhost``/``127.0.0.1``: a web page can
  reach a local server through DNS rebinding (its hostname resolves to
  127.0.0.1 after the page loaded), and such requests carry the page's host
  name. The check rejects them.
- Every path parameter is resolved (symlinks included) and must stay inside
  the resolved root, so ``../`` and a symlink to ``~/.ssh`` are rejected.
"""
import secrets
from pathlib import Path
from typing import Optional

from pyevio.web import ApiError

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"})
TOKEN_QUERY_PARAM = "token"
TOKEN_HEADER = "x-token"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def host_allowed(host_header: Optional[str]) -> bool:
    """True for ``localhost`` or ``127.0.0.1`` with an optional ``:port``."""
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
    return host.lower() in ALLOWED_HOSTS


def check_request(host_header: Optional[str], path: str, token_given: Optional[str], token_expected: str) -> None:
    """Raise :class:`ApiError` for a bad Host (403) or, on ``/api`` paths, a missing or wrong token (401).

    The token travels in the URL (``?token=``) because the viewer page is opened
    from a printed link; a header (``X-Token``) serves scripted clients.
    """
    if not host_allowed(host_header):
        raise ApiError(403, f"Host {host_header!r} is not localhost; the viewer serves local pages only")
    if path.startswith("/api/"):
        if not token_given or not secrets.compare_digest(token_given, token_expected):
            raise ApiError(401, "missing or wrong token; use the URL printed by `pyevio web`")


def resolve_under_root(root: Path, relative: str) -> Path:
    """Resolve ``relative`` against ``root`` and return it; raise :class:`ApiError` (403) when it leaves the root.

    ``root`` must already be resolved. Resolution follows symlinks, so a link that
    points outside the root is rejected the same way as ``../``. An absolute
    ``relative`` replaces the root in the join and is rejected unless it lies inside.
    """
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ApiError(403, f"path {relative!r} is outside the served root")
    return candidate


def inside_root(root: Path, path: Path) -> bool:
    """True when ``path`` (resolved) is the root or below it."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents
