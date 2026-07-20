from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from zyra_memory import (
    IndexLease,
    MemoryIndexRuntime,
    MemoryLayer,
    MemoryRecord,
    PublicationFencedError,
    SQLiteStore,
)


ROOT = Path(__file__).resolve().parents[2]


class IndexWorkerProcessRecoveryTests(unittest.TestCase):
    def test_real_worker_kill_sweeper_requeue_new_generation_and_old_publish_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical_path = root / "canonical.sqlite3"
            index_path = root / "derived.sqlite3"
            phase_path = root / "phase.json"
            release_path = root / "release"
            canonical = SQLiteStore(canonical_path)
            canonical.save_memory_records([_record()])
            coordinator = MemoryIndexRuntime(
                canonical_store=canonical,
                index_path=index_path,
                worker_id="coordinator",
                lease_ttl_seconds=0.5,
                heartbeat_interval_seconds=0.1,
            )
            queued = coordinator.synchronize_task("task-worker", process=False)
            self.assertTrue(queued.job_id)

            environment = dict(os.environ)
            python_paths = [
                str(ROOT / "packages" / "memory"),
                str(ROOT / "packages" / "core"),
                environment.get("PYTHONPATH", ""),
            ]
            environment["PYTHONPATH"] = os.pathsep.join(item for item in python_paths if item)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "zyra_memory.index_worker_cli",
                    "--canonical-db",
                    str(canonical_path),
                    "--index-db",
                    str(index_path),
                    "--worker-id",
                    "worker-a",
                    "--lease-ttl",
                    "0.5",
                    "--heartbeat-interval",
                    "0.1",
                    "--once",
                    "--scope",
                    "memory:task-worker",
                    "--phase-file",
                    str(phase_path),
                    "--pause-phase",
                    "building",
                    "--release-file",
                    str(release_path),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_until(lambda: _phase_is(phase_path, "building"), timeout=10.0)
                phase = phase_path.read_text(encoding="utf-8")
                self.assertIn('"phase": "building"', phase)
                old_job = coordinator.queue.require(queued.job_id)
                old_lease = IndexLease(
                    job_id=old_job.job_id,
                    scope_key=old_job.scope_key,
                    worker_id=old_job.lease_owner,
                    token=old_job.lease_token,
                    epoch=old_job.lease_epoch,
                    generation=old_job.generation,
                    expires_at=old_job.lease_expires_at,
                )
                self.assertEqual(old_lease.worker_id, "worker-a")
                self.assertTrue(old_lease.token)

                process.kill()
                process.wait(timeout=10.0)
                time.sleep(0.7)

                sweep = coordinator.sweeper.sweep_once()
                self.assertEqual(sweep.stale_job_ids, (old_job.job_id,))
                self.assertEqual(sweep.generations, (old_job.generation + 1,))
                self.assertEqual(coordinator.sweeper.sweep_once().count, 0)

                worker_b = MemoryIndexRuntime(
                    canonical_store=canonical,
                    index_path=index_path,
                    worker_id="worker-b",
                    lease_ttl_seconds=2.0,
                    heartbeat_interval_seconds=0.2,
                )
                outcome = worker_b.worker.process_one(scope_key="memory:task-worker")
                self.assertEqual(outcome.status, "ready")
                self.assertEqual(outcome.generation, old_job.generation + 1)
                with self.assertRaises(PublicationFencedError):
                    worker_b.index.publish(old_lease)

                restarted = MemoryIndexRuntime(
                    canonical_store=SQLiteStore(canonical_path),
                    index_path=index_path,
                    worker_id="worker-restarted",
                )
                recalled = restarted.retrieve("task-worker", "recovery fence")
                self.assertEqual([item.memory_id for item in recalled.records], ["memory-worker"])
                events = restarted.index.audit_events(scope_key="memory:task-worker")
                event_types = {item["event_type"] for item in events}
                self.assertTrue(
                    {
                        "index.queued",
                        "index.leased",
                        "index.building",
                        "index.stale",
                        "index.requeued",
                        "index.publishing",
                        "index.ready",
                    }.issubset(event_types)
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10.0)


def _wait_until(predicate, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise TimeoutError("condition was not reached before timeout")


def _phase_is(path: Path, expected: str) -> bool:
    try:
        return f'"phase": "{expected}"' in path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return False


def _record() -> MemoryRecord:
    return MemoryRecord(
        memory_id="memory-worker",
        run_id="run-worker",
        task_id="task-worker",
        layer=MemoryLayer.EPISODIC,
        source_type="failure_event",
        source_id="worker-killed",
        summary="Worker recovery fence",
        content={"text": "recovery fence lease heartbeat generation"},
        keywords=["recovery", "fence", "lease", "generation"],
        score=0.9,
        metadata={"failure_kind": "worker_killed"},
    )
