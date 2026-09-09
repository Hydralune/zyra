#!/usr/bin/env python3
"""Freeze the current uncommitted container patch as one SWE-bench prediction."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--bridge", type=Path, default=ROOT / "scripts" / "docker_wsl_bridge.py"
    )
    args = parser.parse_args()
    completed = subprocess.run(
        [
            sys.executable,
            str(args.bridge.resolve()),
            "exec",
            args.container,
            "git",
            "-C",
            "/testbed",
            "diff",
            "--binary",
            "HEAD",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace")[:1000]
        )
    patch = completed.stdout.decode("utf-8")
    if not patch.strip():
        raise RuntimeError("refusing to freeze an empty SWE-bench prediction")
    record = {
        "instance_id": args.instance_id,
        "model_name_or_path": args.model_name,
        "model_patch": patch,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({
        "output": str(args.output),
        "patch_bytes": len(completed.stdout),
        "patch_sha256": hashlib.sha256(completed.stdout).hexdigest(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
