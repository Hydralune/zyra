from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers import CodeWorkerSidecarClient


def main() -> None:
    client = CodeWorkerSidecarClient(ROOT)
    health = client.health()
    assert health["ok"] is True
    assert health["worker"] == "CodeWorkerRuntime"
    assert health["vendor"]["complete"] is True
    inventory = client.runtime_inventory()
    assert inventory["source"] == "claude-code-best"
    assert inventory["moduleEntrypoints"]["queryEngine"] is True
    assert inventory["toolRuntime"]["baseToolCount"] > 5
    print("CodeWorker sidecar verification passed")


if __name__ == "__main__":
    main()
