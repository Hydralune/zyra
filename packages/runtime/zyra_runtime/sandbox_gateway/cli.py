from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .command_policy import StructuredCommandPolicy
from .models import GatewayCommandEnvelope
from .source_custody import assert_source_custody, source_custody_manifest
from .state_store import GatewayStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zyra-sandbox-gateway")
    subcommands = parser.add_subparsers(dest="command", required=True)
    describe = subcommands.add_parser("describe")
    describe.add_argument("--state-root", type=Path)
    subcommands.add_parser("source-custody")
    policy = subcommands.add_parser("policy")
    policy.add_argument("--session-id", required=True)
    policy.add_argument("--run-id", required=True)
    policy.add_argument("--task-id", required=True)
    policy.add_argument("--worker-id", required=True)
    policy.add_argument("--tool-use-id", required=True)
    policy.add_argument("--workspace-id", default="")
    policy.add_argument("--owner-epoch", type=int, default=0)
    policy.add_argument("--fence-digest", default="")
    policy.add_argument("--network-profile", default="offline")
    policy.add_argument("--sealed", action="store_true")
    policy.add_argument("executable")
    policy.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "source-custody":
        print(json.dumps(assert_source_custody(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "describe":
        value = {
            "runtime_id": "SandboxGatewayRuntime",
            "source_custody": source_custody_manifest(),
            "policy": StructuredCommandPolicy().descriptor(),
        }
        if args.state_root is not None:
            value["state_custody"] = GatewayStateStore(
                args.state_root
            ).custody_descriptor()
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    envelope = GatewayCommandEnvelope.build(
        session_id=args.session_id,
        run_id=args.run_id,
        task_id=args.task_id,
        worker_id=args.worker_id,
        executable=args.executable,
        argv=args.argv,
        tool_use_id=args.tool_use_id,
        workspace_id=args.workspace_id,
        owner_epoch=args.owner_epoch,
        fence_digest=args.fence_digest,
        network_profile=args.network_profile,
    )
    decision = StructuredCommandPolicy().evaluate(
        envelope,
        sealed=args.sealed,
    )
    print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
    return 0 if not decision.denied else 2
