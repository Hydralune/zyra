from __future__ import annotations

import os
import http.client
import select
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps" / "web"
DIST_ROOT = WEB_ROOT / "dist"


class WorkbenchHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self) -> None:
        # Only the launcher's fixed upstream is reachable. A query parameter
        # cannot turn the product server into an arbitrary network proxy.
        target = getattr(self.server, "api_origin", None)
        if not target:
            self.send_error(503, "No API is configured for this Web server")
            return
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin and origin != f"http://{host}"):
            self.send_error(403, "Cross-origin API requests are not allowed")
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(400, "A content length is required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return
        if not 0 <= length <= 16 * 1024 * 1024:
            self.send_error(413, "Request body is too large")
            return
        path = self.path[len("/api"):]
        if not path.startswith("/") or path.startswith("//"):
            self.send_error(400, "Invalid API path")
            return
        websocket = self.headers.get("Upgrade", "").lower() == "websocket"
        excluded = {"host", "origin", "connection", "keep-alive", "transfer-encoding",
                    "proxy-authorization", "proxy-connection", "te", "trailer"}
        headers = {key: value for key, value in self.headers.items() if key.lower() not in excluded}
        headers["Host"] = target.netloc
        # Preserve the validated browser origin for ticket-bound PTY handshakes.
        # The browser only sees this same-origin proxy, so backend CORS headers
        # are not used to authorize the upstream request.
        if origin:
            headers["Origin"] = origin
        headers["Connection"] = "Upgrade" if websocket else "close"
        upstream = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=3600)
        sent = False
        try:
            if websocket:
                upstream.connect()
                remote = upstream.sock
                assert remote is not None
                request = f"{self.command} {path} HTTP/1.1\r\n"
                request += "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
                remote.sendall(request.encode("latin-1"))
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = remote.recv(4096)
                    if not chunk:
                        raise ConnectionError("API closed the WebSocket handshake")
                    response.extend(chunk)
                    if len(response) > 65536:
                        raise ConnectionError("API handshake is too large")
                self.connection.sendall(response)
                sent = True
                self.close_connection = True
                if response.split(b"\r\n", 1)[0].split()[1] != b"101":
                    return
                # Forward the ticket-bound connection without interpreting or
                # fabricating terminal / event messages, including close frames.
                remote.settimeout(None)
                while True:
                    readable, _, _ = select.select([self.connection, remote], [], [], 60)
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (remote if source is self.connection else self.connection).sendall(chunk)
            else:
                upstream.request(self.command, path, body=self.rfile.read(length) if length else None, headers=headers)
                response = upstream.getresponse()
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding", "keep-alive", "server", "date"}:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                sent = True
                self.close_connection = True
                if self.command != "HEAD":
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent:
                self.send_error(502, "Zyra API is unavailable")
        finally:
            upstream.close()

    def _mutation(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy()
        else:
            self.send_error(405, "Method not allowed")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _mutation

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy()
        else:
            super().do_HEAD()

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy()
            return
        request_path = self.path.split("?", 1)[0]
        # A real file always wins over the single-page-app fallback.  The built
        # index.html references its assets relatively ("./index-<hash>.js"), so
        # on a deep route such as /tasks/<id> the browser requests
        # /tasks/index-<hash>.js.  Serving the SPA shell for that (as the
        # route-prefix check below used to) hands back HTML where a script is
        # expected, and the page renders blank on every refresh or deep link.
        candidate = (DIST_ROOT / request_path.lstrip("/")).resolve()
        if (
            request_path != "/"
            and DIST_ROOT.resolve() in candidate.parents
            and candidate.is_file()
        ):
            return super().do_GET()
        if getattr(self.server, "api_origin", None) and (
            request_path in {"/", "/index.html", "/settings"}
            or request_path.startswith("/tasks")
        ):
            body = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
            body = body.replace("<html ", '<html data-api-proxy="true" ', 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if request_path.startswith("/tasks") or request_path == "/settings":
            self.path = "/index.html"
        return super().do_GET()


def run() -> None:
    host = os.environ.get("ZYRA_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("ZYRA_WEB_PORT", "5173"))
    if not (DIST_ROOT / "index.html").is_file():
        raise SystemExit(
            "Zyra Web production bundle is missing. Run `bun run build:web` first."
        )
    os.chdir(DIST_ROOT)
    server = ThreadingHTTPServer((host, port), WorkbenchHandler)
    api_origin = os.environ.get("ZYRA_WEB_API_ORIGIN", "").strip()
    if api_origin:
        target = urlsplit(api_origin)
        if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}
                or target.username or target.password or target.path not in {"", "/"}
                or target.query or target.fragment):
            raise SystemExit("ZYRA_WEB_API_ORIGIN must be a credential-free loopback HTTP origin")
        server.api_origin = target
    print(f"Zyra Workbench listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
