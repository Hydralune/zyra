from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
from urllib.parse import urlsplit

import pytest

from scripts import dev_web


@contextmanager
def serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ws":
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.write(b"HELLO")
            self.wfile.flush()
            self.wfile.write(self.rfile.read(5))
            self.wfile.flush()
            self.close_connection = True
            return
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@pytest.fixture
def web(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text('<html lang="zh-CN"><script src="/index.js"></script></html>', encoding="utf8")
    monkeypatch.setattr(dev_web, "DIST_ROOT", tmp_path)
    with serving(Upstream) as api, serving(dev_web.WorkbenchHandler) as server:
        server.api_origin = urlsplit(f"http://127.0.0.1:{api.server_port}")
        yield server


def request(web, method, path, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", web.server_port, timeout=3)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_fixed_upstream_and_deep_page_refresh(web):
    assert request(web, "GET", "/api/tasks?limit=2") == (200, b"/tasks?limit=2")
    origin = f"http://127.0.0.1:{web.server_port}"
    assert request(web, "POST", "/api/tasks", b'{"goal":"test"}', {"Origin": origin}) == (201, b'{"goal":"test"}')
    status, body = request(web, "GET", "/tasks/task_test_001?view=artifacts")
    assert status == 200
    assert b'data-api-proxy="true"' in body and b'src="/index.js"' in body


def test_proxy_rejects_foreign_origins_hosts_and_unbounded_bodies(web):
    assert request(web, "POST", "/api/tasks", b"test", {"Origin": "https://example.com"})[0] == 403
    assert request(web, "GET", "/api/health", headers={"Host": "example.com"})[0] == 403
    assert request(web, "POST", "/api/tasks", headers={"Content-Length": "999999999"})[0] == 413
    assert request(web, "GET", "/api//example.com/health")[0] == 400


def test_websocket_upgrade_preserves_bidirectional_bytes(web):
    with socket.create_connection(("127.0.0.1", web.server_port), timeout=3) as client:
        client.sendall((f"GET /api/ws HTTP/1.1\r\nHost: 127.0.0.1:{web.server_port}\r\n"
                        "Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n").encode())
        stream = client.makefile("rb")
        assert b"101" in stream.readline()
        while stream.readline() != b"\r\n":
            pass
        assert stream.read(5) == b"HELLO"
        client.sendall(b"WORLD")
        assert stream.read(5) == b"WORLD"
        stream.close()
