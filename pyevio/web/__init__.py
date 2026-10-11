"""Local web server for the EVIO stream viewer (index.html), with a folder browser.

The viewer decodes files in the browser. The server only lists folders and
streams file bytes, and listens on 127.0.0.1. To view files on a remote machine,
run `pyevio web --no-browser` there and connect with
`ssh -L 8765:localhost:8765 user@host`.
"""
import functools
import http.server
import json
import os
import shutil
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
EVIO_SUFFIXES = {".evio", ".dat"}


class Handler(http.server.SimpleHTTPRequestHandler):
    start_dir = str(Path.cwd())

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/start":
            return self._json({"dir": self.start_dir})
        if url.path == "/api/ls":
            return self._ls(q.get("dir", self.start_dir), q.get("all") == "1")
        if url.path == "/api/file":
            return self._file(q.get("path", ""))
        if url.path not in ("/", "/index.html"):
            return self.send_error(404)
        return super().do_GET()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ls(self, d, show_all):
        p = Path(os.path.expanduser(d)).resolve()
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            return self._json({"error": f"Folder not found: {p}"}, 404)
        dirs, files = [], []
        try:
            for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        dirs.append(e.name)
                    elif e.is_file() and (show_all or Path(e.name).suffix.lower() in EVIO_SUFFIXES):
                        files.append({"name": e.name, "size": e.stat().st_size})
                except OSError:
                    continue
        except PermissionError:
            return self._json({"error": f"No permission to read {p}"}, 403)
        parent = str(p.parent) if p.parent != p else None
        return self._json({"dir": str(p), "parent": parent, "dirs": dirs, "files": files})

    def _file(self, path):
        p = Path(os.path.expanduser(path)).resolve()
        if not p.is_file():
            return self._json({"error": f"File not found: {p}"}, 404)
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def log_message(self, fmt, *args):  # keep the terminal quiet except for errors
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


def serve(start_dir=".", port=8765, open_browser=True):
    """Serve the viewer at http://localhost:<port> until Ctrl+C."""
    Handler.start_dir = str(Path(os.path.expanduser(start_dir)).resolve())
    handler = functools.partial(Handler, directory=str(HERE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://localhost:{port}/index.html"
        print(f"Viewer: {url}   (Ctrl+C to stop)\nFile browser starts in: {Handler.start_dir}")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
