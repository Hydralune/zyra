from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from zyra_workspace import WorkspaceManagerConfig, WorkspaceManagerRuntime

from .jobs import CodeIndexBuildQueue
from .runtime import CodeIndexRuntime
from .workspace_source import BoundWorkspaceSource
from .worker import CodeIndexWorkerRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zyra_code_index.worker_cli",
        description="Run the Zyra-owned durable code-index worker and sweeper.",
    )
    parser.add_argument("--index-db", required=True)
    parser.add_argument("--workspace-state-root", required=True)
    parser.add_argument("--workspace-data-root", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--backend-id", default="local-default")
    parser.add_argument("--worker-id", default=f"code-index-worker:{os.getpid()}")
    parser.add_argument("--lease-ttl", type=float, default=30.0)
    parser.add_argument("--heartbeat-interval", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--drain", type=int, default=0)
    parser.add_argument("--sweep-once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--phase-file", default="")
    parser.add_argument(
        "--pause-phase",
        choices=("", "leased", "building", "candidate_built", "publishing", "published", "ready"),
        default="",
    )
    parser.add_argument("--release-file", default="")
    parser.add_argument("--pause-timeout", type=float, default=300.0)
    return parser


class PhaseObserver:
    """Deterministic process-crash hook used by recovery verification."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.phase_file = Path(args.phase_file).resolve() if args.phase_file else None
        self.pause_phase = str(args.pause_phase)
        self.release_file = Path(args.release_file).resolve() if args.release_file else None
        self.pause_timeout = max(0.1, float(args.pause_timeout))

    def __call__(self, phase: str, lease: Any) -> None:
        if self.phase_file is not None:
            payload = {
                "phase": phase,
                "job_id": lease.job_id,
                "workspace_id": lease.workspace_id,
                "generation": lease.generation,
                "worker_id": lease.worker_id,
                "lease_epoch": lease.epoch,
                "pid": os.getpid(),
                "observed_at": time.time(),
            }
            temporary = self.phase_file.with_name(
                f".{self.phase_file.name}.{os.getpid()}.tmp"
            )
            self.phase_file.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.phase_file)
        if phase != self.pause_phase:
            return
        if self.release_file is None:
            raise RuntimeError("--pause-phase requires --release-file")
        deadline = time.monotonic() + self.pause_timeout
        while not self.release_file.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"pause phase {phase} exceeded --pause-timeout")
            time.sleep(0.025)


def create_worker(args: argparse.Namespace) -> tuple[CodeIndexWorkerRuntime, CodeIndexRuntime]:
    config = WorkspaceManagerConfig(
        state_root=Path(args.workspace_state_root).resolve(),
        data_root=Path(args.workspace_data_root).resolve(),
        local_enabled=True,
        default_backend_id=str(args.backend_id),
    )
    manager = WorkspaceManagerRuntime(config)

    def current_source() -> BoundWorkspaceSource:
        return BoundWorkspaceSource.from_manager_snapshot(
            manager,
            args.workspace_id,
            expected_revision=args.source_revision,
        )

    runtime = CodeIndexRuntime(current_source(), index_path=Path(args.index_db).resolve())
    queue = CodeIndexBuildQueue(runtime.store)

    def runtime_factory(job: Any) -> CodeIndexRuntime:
        return CodeIndexRuntime(
            BoundWorkspaceSource.from_manager_snapshot(
                manager,
                job.workspace_id,
                expected_revision=job.source_revision,
            ),
            index_path=Path(args.index_db).resolve(),
        )

    def source_guard(job: Any) -> None:
        BoundWorkspaceSource.from_manager_snapshot(
            manager,
            job.workspace_id,
            expected_revision=job.source_revision,
        )

    interval = args.heartbeat_interval if args.heartbeat_interval > 0 else None
    worker = CodeIndexWorkerRuntime(
        worker_id=args.worker_id,
        queue=queue,
        runtime_factory=runtime_factory,
        source_guard=source_guard,
        lease_ttl_seconds=args.lease_ttl,
        heartbeat_interval_seconds=interval,
        failpoint=PhaseObserver(args),
    )
    return worker, runtime


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    worker, runtime = create_worker(args)
    queue = worker.queue
    if args.status:
        print(json.dumps({"runtime": runtime.status(), "fence": queue.fence_state(args.workspace_id)}, sort_keys=True))
        return 0
    if args.sweep_once:
        print(json.dumps(queue.sweep_expired().to_dict(), sort_keys=True))
        return 0
    if args.once:
        outcome = worker.process_one(workspace_id=args.workspace_id)
        print(json.dumps(outcome.to_dict(), sort_keys=True))
        return 0 if outcome.status in {"ready", "idle", "fenced"} else 1
    if args.drain > 0:
        outcomes = worker.drain(maximum_jobs=args.drain, workspace_id=args.workspace_id)
        print(json.dumps([item.to_dict() for item in outcomes], sort_keys=True))
        return 0 if all(item.status in {"ready", "idle", "fenced"} for item in outcomes) else 1

    stop = threading.Event()

    def stop_worker(signum: int, frame: Any) -> None:
        del signum, frame
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, signal_name, None)
        if value is not None:
            signal.signal(value, stop_worker)
    worker.run_forever(stop_event=stop)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
