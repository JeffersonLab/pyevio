"""Token, Host and path confinement: the bad cases are rejected, the good ones pass."""
from fastapi.testclient import TestClient

from conftest import TOKEN


def test_missing_or_wrong_token_is_401(app):
    anonymous = TestClient(app, base_url="http://localhost")
    assert anonymous.get("/api/ls").status_code == 401
    assert anonymous.get("/api/ls", headers={"X-Token": "wrong"}).status_code == 401
    assert anonymous.get(f"/api/ls?token={TOKEN}").status_code == 200
    assert anonymous.get("/api/ls", headers={"X-Token": TOKEN}).status_code == 200


def test_index_needs_no_token_but_a_local_host(app):
    anonymous = TestClient(app, base_url="http://localhost")
    assert anonymous.get("/").status_code == 200
    assert anonymous.get("/index.html").status_code == 200
    assert anonymous.get("/", headers={"Host": "viewer.example.com"}).status_code == 403


def test_bad_host_is_403(client):
    for host in ("evil.example", "evil.example:8765", "localhost.evil.example", ""):
        response = client.get("/api/ls", headers={"Host": host})
        assert response.status_code == 403, (host, response.text)
    for host in ("localhost", "localhost:8765", "127.0.0.1", "127.0.0.1:9000"):
        assert client.get("/api/ls", headers={"Host": host}).status_code == 200, host


def test_paths_outside_root_are_403(client):
    for bad in ("../outside/secret.evio", "runs/../../outside/secret.evio", "/etc/passwd", "link_outside.evio",
                "dir_outside/secret.evio"):
        response = client.post("/api/open", json={"path": bad})
        assert response.status_code == 403, (bad, response.text)
    assert client.get("/api/ls", params={"dir": "../outside"}).status_code == 403
    assert client.get("/api/ls", params={"dir": "dir_outside"}).status_code == 403
    assert client.get("/api/ls", params={"dir": "/"}).status_code == 403


def test_no_api_file_endpoint(client):
    assert client.get("/api/file", params={"path": "runs/synthetic.evio"}).status_code == 404


def test_asset_route_stays_inside_the_package(client):
    assert client.get("/index.html").status_code == 200
    assert client.get("/__init__.py").status_code == 404
    assert client.get("/..%2F..%2Fpyproject.toml").status_code in (403, 404)


def test_favicon_is_served_at_both_names(client):
    """Browsers ask for /favicon.ico before the page names its icon; both answers are the same SVG."""
    for name in ("/favicon.ico", "/favicon.svg"):
        response = client.get(name)
        assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml"), name
        assert response.content.lstrip().startswith(b"<svg")


def test_ls_lists_only_under_root(client, served_root):
    listing = client.get("/api/ls").json()
    assert listing["dir"] == "" and listing["parent"] is None
    assert "runs" in listing["dirs"] and "dir_outside" not in listing["dirs"]
    names = [entry["name"] for entry in listing["files"]]
    assert "link_outside.evio" not in names
    if served_root["golden"]:
        assert "basic_le.evio" in names
    runs = client.get("/api/ls", params={"dir": "runs"}).json()
    assert runs["dir"] == "runs" and runs["parent"] == ""
    assert [entry["name"] for entry in runs["files"]] == ["synthetic.evio"]
    assert client.get("/api/ls", params={"dir": "missing"}).status_code == 404
