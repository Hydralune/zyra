from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps" / "web"
DIST_ROOT = WEB_ROOT / "dist"


class WorkbenchHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        request_path = self.path.split("?", 1)[0]
        candidate = (DIST_ROOT / request_path.lstrip("/")).resolve()
        if (
            request_path != "/"
            and DIST_ROOT.resolve() in candidate.parents
            and candidate.is_file()
        ):
            return super().do_GET()
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
    print(f"Zyra Workbench listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
