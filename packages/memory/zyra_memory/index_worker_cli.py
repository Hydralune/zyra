from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .memory_index import MemoryIndexRuntime
from .sqlite_store import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zyra_memory.index_worker_cli",
        description=(
            "Run the durable Zyra memory-index worker. The worker owns only "
            "derived state; --canonical-db remains the MemoryRecordStore."
        ),
    )
    parser.add_argument("--canonical-db", required=True, help="Path to canonical Zyra SQLiteStore")
    parser.add_argument("--index-db", required=True, help="Path to rebuildable retrieval index SQLite")
    parser.add_argument("--worker-id", default=f"index-worker:{os.getpid()}")
    parser.add_argument("--lease-ttl", type=float, default=30.0)
    parser.add_argument("--heartbeat-interval", type=float, default=0.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--scope", default="")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job")
    parser.add_argument("--drain", type=int, default=0, help="Drain at most N queued jobs")
    parser.add_argument("--sweep-once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--phase-file",
        default="",
        help="Optional test-observation JSON file updated when a worker phase is reached",
    )
    parser.add_argument(
        "--pause-phase",
        choices=("", "leased", "building", "staged_batch", "staged", "publishing", "ready"),
        default="",
        help="Test hook: pause at a phase until --release-file exists or the process is killed",
    )
    parser.add_argument("--release-file", default="", help="Test hook release sentinel for --pause-phase")
    parser.add_argument("--pause-timeout", type=float, default=300.0)
    return parser


class PhaseObserver:
    def __init__(
        self,
        *,
        phase_file: str,
        pause_phase: str,
        release_file: str,
        pause_timeout: float,
    ) -> None:
        self.phase_file = Path(phase_file).resolve() if phase_file else None
        self.pause_phase = pause_phase
        self.release_file = Path(release_file).resolve() if release_file else None
        self.pause_timeout = max(0.1, float(pause_timeout))

    def __call__(self, phase: str, lease: Any) -> None:
        if self.phase_file is not None:
            self._atomic_json(
                self.phase_file,
                {
                    "phase": phase,
                    "job_id": lease.job_id,
                    "scope_key": lease.scope_key,
                    "generation": lease.generation,
                    "worker_id": lease.worker_id,
                    "lease_epoch": lease.epoch,
                    "pid": os.getpid(),
                    "observed_at": time.time(),
                },
            )
        if phase != self.pause_phase:
            return
        if self.release_file is None:
            raise RuntimeError("--pause-phase requires --release-file")
        deadline = time.monotonic() + self.pause_timeout
        while not self.release_file.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"pause phase {phase} exceeded --pause-timeout")
            time.sleep(0.025)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def create_runtime(args: argparse.Namespace) -> MemoryIndexRuntime:
    canonical_path = Path(args.canonical_db).resolve()
    index_path = Path(args.index_db).resolve()
    if canonical_path == index_path:
        raise ValueError("canonical and derived index databases must use different paths")
    canonical = SQLiteStore(canonical_path)
    canonical.initialize()
    observer = PhaseObserver(
        phase_file=args.phase_file,
        pause_phase=args.pause_phase,
        release_file=args.release_file,
        pause_timeout=args.pause_timeout,
    )
    interval = args.heartbeat_interval if args.heartbeat_interval > 0 else None
    runtime = MemoryIndexRuntime(
        canonical_store=canonical,
        index_path=index_path,
        worker_id=args.worker_id,
        lease_ttl_seconds=args.lease_ttl,
        heartbeat_interval_seconds=interval,
    )
    runtime.worker.failpoint = observer
    return runtime


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = create_runtime(args)
    if args.status:
        print(json.dumps(runtime.status(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.sweep_once:
        result = runtime.sweeper.sweep_once()
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.once:
        outcome = runtime.worker.process_one(scope_key=args.scope)
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if outcome.status in {"ready", "idle", "fenced"} else 1
    if args.drain > 0:
        outcomes = runtime.worker.drain(maximum_jobs=args.drain, scope_key=args.scope)
        print(json.dumps([item.to_dict() for item in outcomes], ensure_ascii=False, sort_keys=True))
        return 0 if all(item.status in {"ready", "idle", "fenced"} for item in outcomes) else 1

    stop = threading.Event()

    def stop_worker(signum: int, frame: Any) -> None:
        del signum, frame
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, signal_name, None)
        if value is not None:
            signal.signal(value, stop_worker)
    runtime.worker.run_forever(
        stop_event=stop,
        poll_interval_seconds=max(0.01, args.poll_interval),
        sweep=runtime.sweeper,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
