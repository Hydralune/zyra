"""One-command launcher for the sealed long-run demo.

Wraps ``scripts/run_phase2_sealed_scenarios.py`` with the environment a clean
sealed run needs but that is easy to get wrong by hand:

* ``ZYRA_BUN_EXECUTABLE``  — the locked Bun binary used by the TypeScript
  CodeWorker / E02 port (otherwise the run fails with "Bun 1.2.15 is required").
* ``ZYRA_DEPLOYMENT_PROFILE_BASE_PORT`` — a dedicated port block so the sealed
  deployment nodes do not collide with a running dev API (which uses 8310+).
* ``ZYRA_DEPLOYMENT_STATE_ROOT`` — an isolated deployment state root.
* ``PYTHONPATH`` — prepends this checkout so the run uses *its own* source
  rather than an editable install pointing at another working copy.

Editable installs of the ``zyra_*`` packages may resolve to a different
checkout.  Prepending this repository root keeps the sealed run bound to the
frozen candidate commit.

Usage (run from the repository root of a clean worktree):
    python scripts/run_sealed_demo.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_phase2_sealed_scenarios.py"
MANIFEST = ROOT / "docs" / "evidence" / "phase2" / "sealed" / "webui-demo-manifest.json"
EVIDENCE_ROOT = ROOT / "docs" / "evidence" / "phase2" / "sealed" / "webui-demo-evidence"
DEPLOYMENT_STATE_ROOT = ROOT / "tmp" / "sealed-deploy"
BUN = ROOT / "node_modules" / ".bin" / ("bun.exe" if os.name == "nt" else "bun")

DEMO_BASE_PORT = os.environ.get("ZYRA_SEALED_DEMO_BASE_PORT", "8400")


def _prepare() -> None:
    if not RUNNER.is_file():
        raise SystemExit(f"sealed runner is missing: {RUNNER}")
    if not MANIFEST.is_file():
        raise SystemExit(
            "sealed manifest is missing; run `python "
            "scripts/make_sealed_demo_manifest.py` first"
        )
    if EVIDENCE_ROOT.exists() and any(EVIDENCE_ROOT.iterdir()):
        raise SystemExit(
            f"evidence root is not clean: {EVIDENCE_ROOT}\n"
            "Delete it before re-running (the sealed run refuses a dirty root)."
        )
    if not BUN.is_file():
        raise SystemExit(
            f"locked Bun binary is missing: {BUN}\nRun `bun install --frozen-lockfile`."
        )


def main() -> int:
    _prepare()
    environment = dict(os.environ)
    environment["ZYRA_BUN_EXECUTABLE"] = str(BUN)
    environment["ZYRA_DEPLOYMENT_PROFILE_BASE_PORT"] = DEMO_BASE_PORT
    environment["ZYRA_DEPLOYMENT_STATE_ROOT"] = str(DEPLOYMENT_STATE_ROOT)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    print(f"[sealed-demo] checkout     : {ROOT}")
    print(f"[sealed-demo] manifest     : {MANIFEST}")
    print(f"[sealed-demo] evidence root: {EVIDENCE_ROOT}")
    print(f"[sealed-demo] bun          : {BUN}")
    print(f"[sealed-demo] base port    : {DEMO_BASE_PORT}")
    print()

    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--manifest", str(MANIFEST)],
        cwd=str(ROOT),
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
