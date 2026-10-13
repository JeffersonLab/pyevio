"""FastAPI application factory and ``serve()``.

``create_app`` takes everything a test needs to inject: the served root, the
token, the cache directory and the worker count for the statistics pool. The
uvicorn process count is always 1 because the job registry lives in memory.
"""
import logging
import webbrowser
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from pyevio.web import INDEX_HTML, WEB_DIR, ApiError
from pyevio.web.auth import TOKEN_HEADER, TOKEN_QUERY_PARAM, check_request, new_token, resolve_under_root
from pyevio.web.cache import StatsCache
from pyevio.web.jobs import FileRegistry
from pyevio.web.routes import router

log = logging.getLogger("pyevio.web")

STATIC_SUFFIXES = (".html", ".js", ".css", ".svg", ".png", ".ico")
START_FILE_QUERY_PARAM = "open"  # the page opens this file (relative to the served root) on load; app.js reads it


def create_app(root: Path, token: str, cache_dir: Optional[Path] = None, stats_workers: Optional[int] = None) -> FastAPI:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"served root is not a folder: {root}")
    app = FastAPI(title="pyevio web", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = root
    app.state.token = token
    app.state.registry = FileRegistry(root, StatsCache(cache_dir), stats_workers)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        token_given = request.query_params.get(TOKEN_QUERY_PARAM) or request.headers.get(TOKEN_HEADER)
        try:
            check_request(request.headers.get("host"), request.url.path, token_given, token)
        except ApiError as error:
            log.warning("%d %s %s: %s", error.status, request.method, request.url.path, error.message)
            return JSONResponse({"error": error.message}, status_code=error.status)
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        log.warning("%d %s %s: %s", error.status, request.method, request.url.path, error.message)
        return JSONResponse({"error": error.message}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def bad_parameters(request: Request, error: RequestValidationError):
        # One error shape for every failure: {"error": "..."}; FastAPI's default is {"detail": [...]}.
        message = "; ".join(f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors())
        log.warning("422 %s %s: %s", request.method, request.url.path, message)
        return JSONResponse({"error": message}, status_code=422)

    @app.exception_handler(ValueError)
    async def not_readable(request: Request, error: ValueError):
        # EvioFile raises ValueError for files it does not read (wrong version, no magic word).
        log.warning("400 %s %s: %s", request.method, request.url.path, error)
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.exception_handler(OSError)
    async def not_accessible(request: Request, error: OSError):
        log.warning("404 %s %s: %s", request.method, request.url.path, error)
        return JSONResponse({"error": str(error)}, status_code=404)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index():
        return FileResponse(INDEX_HTML)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Browsers ask for /favicon.ico before the page names its icon; serve the SVG there as well."""
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/{asset_name}", include_in_schema=False)
    def asset(asset_name: str):
        """Files beside index.html (scripts, styles); nothing outside the package folder."""
        path = resolve_under_root(WEB_DIR, asset_name)
        if path.suffix not in STATIC_SUFFIXES or not path.is_file():
            raise ApiError(404, f"no such page: {asset_name}")
        return FileResponse(path)

    app.include_router(router)
    return app


def served_root_and_start_file(path: Path) -> Tuple[Path, Optional[str]]:
    """Split the command-line PATH: a folder is served as is; a file's folder is served and the file opens at start.

    Returns the folder to serve and the file name relative to it (``None`` for a folder).
    """
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        return path, None
    return path.parent, path.name


def serve(path: str = ".", port: int = 8765, open_browser: bool = True, cache_dir: Optional[str] = None,
          workers: Optional[int] = None, verbose: bool = False) -> None:
    """Serve the viewer on 127.0.0.1 until Ctrl+C; prints the URL that carries the token.

    ``path`` is the folder to serve, or a file: then its folder is served and the page opens the file.
    """
    import uvicorn

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = new_token()
    root, start_file = served_root_and_start_file(Path(path))
    app = create_app(root, token, Path(cache_dir) if cache_dir else None, workers)
    url = f"http://127.0.0.1:{port}/?{TOKEN_QUERY_PARAM}={token}"
    if start_file:
        url += f"&{START_FILE_QUERY_PARAM}={quote(start_file)}"
    # flush: with stdout redirected to a file (ssh session logs), the URL must appear before uvicorn blocks.
    print(f"pyevio web: {url}\nFiles under: {app.state.root}\nCache: {app.state.registry.cache.directory}\n(Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, access_log=verbose, log_level="info" if verbose else "warning")
