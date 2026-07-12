from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if not package_path.is_dir():
        continue
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers import (  # noqa: E402
    browser_use_health_summary,
    configure_browser_use_environment,
    find_browser_executable,
    inspect_browser_use_runtime,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check vendored browser-use runtime availability.")
    parser.add_argument("--live", action="store_true", help="Start a real BrowserSession against a local fixture.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Live smoke timeout in seconds.")
    parser.add_argument("--executable", type=str, default="", help="Chrome/Edge executable path.")
    args = parser.parse_args()

    health = inspect_browser_use_runtime(ROOT)
    result: dict[str, Any] = {"health": browser_use_health_summary(health)}
    exit_code = 0
    if not health.importable:
        exit_code = 1
    elif args.live:
        executable = find_browser_executable([args.executable] if args.executable else [])
        if executable is None:
            result["live"] = {
                "ok": False,
                "error": "browser_executable_not_found",
                "checked_env": "BROWSER_USE_CHROME_PATH",
            }
            exit_code = 2
        else:
            try:
                result["live"] = asyncio.run(
                    asyncio.wait_for(_run_live_smoke(executable=executable), timeout=args.timeout)
                )
            except Exception as error:  # noqa: BLE001 - smoke diagnostics must preserve runtime failures.
                result["live"] = {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "executable": str(executable),
                }
                exit_code = 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


async def _run_live_smoke(*, executable: Path) -> dict[str, Any]:
    paths = configure_browser_use_environment(ROOT)
    smoke_dir = paths.root / "smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    page = smoke_dir / "index.html"
    page.write_text(
        "<html><head><title>Zyra BrowserUse Live Smoke</title></head>"
        "<body><h1>Zyra browser-use live smoke</h1><p>runtime fixture loaded</p></body></html>",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietStaticHandler, directory=str(smoke_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"

    from browser_use.browser.session import BrowserSession

    session = BrowserSession(
        executable_path=executable,
        headless=True,
        keep_alive=False,
        user_data_dir=paths.temp_dir / "browser-use-user-data-dir-smoke",
        downloads_path=paths.root / "downloads",
        traces_dir=paths.root / "traces",
        enable_default_extensions=False,
        captcha_solver=False,
        chromium_sandbox=False,
        args=[
            "--disable-background-networking",
            "--disable-component-extensions-with-background-pages",
            "--disable-extensions",
        ],
    )
    try:
        await session.start()
        await session.navigate_to(url)
        state = await session.get_browser_state_summary(include_screenshot=False)
        state_text = await session.get_state_as_text()
        return {
            "ok": True,
            "executable": str(executable),
            "served_url": url,
            "url": state.url,
            "title": state.title,
            "tabs": len(state.tabs),
            "state_text_preview": state_text[:500],
        }
    finally:
        await session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


if __name__ == "__main__":
    main()
