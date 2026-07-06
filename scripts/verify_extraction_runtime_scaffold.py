from __future__ import annotations

import json
import subprocess
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

from zyra_runtime.scaffold import default_m1_01b_runtime_scaffold
from zyra_workers.runtime_scaffold import build_m1_01b_worker_scaffolds


def main() -> None:
    runtime = default_m1_01b_runtime_scaffold()
    runtime_health = runtime.health()
    assert runtime_health["ok"] is True
    workers = build_m1_01b_worker_scaffolds(runtime)
    assert len(workers) == 5
    assert all(worker.health().ok for worker in workers)

    completed = subprocess.run(
        [sys.executable, "scripts/zyra_source_extract.py", "smoke", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    print("Extraction runtime scaffold verification passed")


if __name__ == "__main__":
    main()
