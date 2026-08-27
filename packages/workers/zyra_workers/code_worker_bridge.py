from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from zyra_runtime.sandbox_gateway.command_policy import StructuredCommandPolicy
from zyra_runtime.sandbox_gateway.file_policy import FilePolicyConfig, GatewayFilePolicy
from zyra_runtime.sandbox_gateway.integration_host import GatewayHostProcessRuntime
from zyra_runtime.sandbox_gateway.integration_policy import (
    GatewayPolicyConfig,
    GatewayPolicyRuntime,
)


def code_worker_entrypoint(project_root: str | Path) -> Path:
    return Path(project_root) / "apps" / "code-worker" / "src" / "main.ts"


class CodeWorkerSidecarClient:
    def __init__(
        self,
        project_root: str | Path,
        node_executable: str | None = None,
        *,
        host_process_runtime: GatewayHostProcessRuntime | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.bun_executable = self._resolve_bun()
        self.node_executable = node_executable or shutil.which("node") or "node"
        self.entrypoint = code_worker_entrypoint(self.project_root)
        self.host_process_runtime = host_process_runtime or GatewayHostProcessRuntime(
            GatewayPolicyRuntime(
                GatewayPolicyConfig(workspace_root=self.project_root.resolve()),
                command_policy=StructuredCommandPolicy(),
                file_policy=GatewayFilePolicy(FilePolicyConfig(allow_executable=True)),
            ),
            allowed_roots=(self.project_root,),
        )

    def _resolve_bun(self) -> str | None:
        installed = shutil.which("bun")
        if installed:
            return installed
        candidates = (
            self.project_root / "node_modules" / ".bin" / "bun.exe",
            self.project_root / "node_modules" / ".bin" / "bun",
        )
        return next(
            (str(candidate.resolve()) for candidate in candidates if candidate.is_file()),
            None,
        )

    def health(self) -> dict[str, Any]:
        return self._run_one_shot("--health")

    def runtime_snapshot(self) -> dict[str, Any]:
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
        command = (
            [self.bun_executable, str(self.entrypoint), flag]
            if self.bun_executable
            else [
                self.node_executable,
                "--experimental-strip-types",
                str(self.entrypoint),
                flag,
            ]
        )
        completed = self.host_process_runtime.run(
            executable=command[0],
            argv=command[1:],
            cwd=self.project_root,
            timeout_seconds=30.0,
            operation_name=f"code-worker-{flag.lstrip('-')}",
        )
        if not completed.ok:
            raise RuntimeError(
                completed.stderr.decode("utf-8", errors="replace")
                or completed.failure_code
                or "code worker control process failed"
            )
        first_line = completed.stdout.decode("utf-8", errors="replace").splitlines()[0]
        payload = json.loads(first_line)
        if not payload.get("ok", True):
            raise RuntimeError(str(payload.get("error") or "code worker sidecar failed"))
        return payload
