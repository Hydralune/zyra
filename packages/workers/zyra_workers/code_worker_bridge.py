from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def code_worker_entrypoint(project_root: str | Path) -> Path:
    return Path(project_root) / "apps" / "code-worker" / "src" / "main.mjs"


class CodeWorkerSidecarClient:
    def __init__(self, project_root: str | Path, node_executable: str | None = None) -> None:
        self.project_root = Path(project_root)
        self.node_executable = node_executable or shutil.which("node") or "node"
        self.entrypoint = code_worker_entrypoint(self.project_root)

    def health(self) -> dict[str, Any]:
        return self._run_one_shot("--health")

    def vendor_snapshot(self) -> dict[str, Any]:
        return self._run_one_shot("--snapshot")

    def runtime_inventory(self) -> dict[str, Any]:
        return self._run_one_shot("--inventory")

    def query_contract(self) -> dict[str, Any]:
        return self._run_one_shot("--query-contract")

    def session_contract(self) -> dict[str, Any]:
        return self._run_one_shot("--session-contract")

    def tool_loop_contract(self) -> dict[str, Any]:
        return self._run_one_shot("--tool-loop-contract")

    def _run_one_shot(self, flag: str) -> dict[str, Any]:
        completed = subprocess.run(
            [self.node_executable, str(self.entrypoint), flag],
            cwd=self.project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        first_line = completed.stdout.splitlines()[0]
        payload = json.loads(first_line)
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("error") or "code worker sidecar failed"))
        return payload
