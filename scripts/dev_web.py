from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps" / "web"


def run() -> None:
    host = os.environ.get("ZYRA_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("ZYRA_WEB_PORT", "5173"))
    os.chdir(WEB_ROOT)
    server = ThreadingHTTPServer((host, port), SimpleHTTPRequestHandler)
    print(f"Zyra Web console listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
