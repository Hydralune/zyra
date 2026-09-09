#!/usr/bin/env python3
"""Exercise targeted benchmark file pull/push over stdio on a real container."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for package in ROOT.joinpath("packages").iterdir():
    if package.is_dir() and str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_orchestration.deployment import code_worker_adapter  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("container")
    parser.add_argument("--workdir", default="/testbed")
    parser.add_argument("--source", default="sklearn/pipeline.py")
    parser.add_argument("--bridge", type=Path, default=ROOT / "scripts" / "docker_wsl_bridge.py")
    args = parser.parse_args()

    temp_parent = ROOT / ".tmp"
    temp_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="path-stream-selftest-", dir=temp_parent) as temporary:
        data_root = Path(temporary) / "workspace-data"
        workspace = data_root / "task"
        sync_root = Path(temporary) / "sync"
        workspace.mkdir(parents=True)
        sync_root.mkdir()
        binding = {
            "container": args.container,
            "container_ref_digest": "selftest",
            "workdir": args.workdir,
            "docker_command_prefix": (sys.executable, str(args.bridge.resolve())),
            "workspace_data_root": data_root,
            "sync_root": sync_root,
        }

        code_worker_adapter._pull_benchmark_workspace_paths(
            binding, workspace, (args.source,)
        )
        pulled = workspace.joinpath(*args.source.split("/")).read_bytes()
        container_hash = code_worker_adapter._run_benchmark_docker(
            binding,
            ("exec", args.container, "sha256sum", f"{args.workdir}/{args.source}"),
            operation="path_stream_selftest_hash",
            timeout_seconds=30.0,
        ).stdout.decode("ascii").split()[0]
        if digest(pulled) != container_hash:
            raise RuntimeError("targeted pull bytes differ from the container source")

        before = code_worker_adapter._workspace_manifest(workspace)
        binary_name = ".zyra-path-stream-selftest.bin"
        binary_payload = b"ZYRA\x00path-stream\xffselftest\n"
        (workspace / binary_name).write_bytes(binary_payload)
        after = code_worker_adapter._workspace_manifest(workspace)
        report = code_worker_adapter._push_benchmark_workspace_delta(
            binding, workspace, before=before, after=after
        )
        pushed = code_worker_adapter._run_benchmark_docker(
            binding,
            ("exec", args.container, "sha256sum", f"{args.workdir}/{binary_name}"),
            operation="path_stream_selftest_pushed_hash",
            timeout_seconds=30.0,
        ).stdout.decode("ascii").split()[0]
        if pushed != digest(binary_payload):
            raise RuntimeError("targeted push bytes differ from the host source")

        print(json.dumps({
            "schema": "zyra.benchmark-path-stream-selftest/v1",
            "status": "passed",
            "container": args.container,
            "source": args.source,
            "source_sha256": container_hash,
            "binary_roundtrip_sha256": pushed,
            "push_report": report,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
