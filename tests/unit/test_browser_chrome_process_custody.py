from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zyra_integrations.browser_use.chrome_process import (
    BrowserEndpoint,
    ChromeLaunchPlan,
    ChromeLaunchPolicy,
    ChromeProcess,
    ChromeProcessController,
)
from zyra_workers.browser_session.models import BrowserSessionCommand
from zyra_workers.browser_session.profile_store import BrowserProfileStore
from zyra_workers.browser_session.session_runtime import _endpoint_reattach_command


class _FakeProcess:
    def __init__(self, pid: int, executable: Path, command: tuple[str, ...], create_time: float = 100.0) -> None:
        self.pid = pid
        self._executable = executable
        self._command = command
        self._create_time = create_time
        self.running = True
        self.terminated = False

    def create_time(self) -> float:
        return self._create_time

    def exe(self) -> str:
        return str(self._executable)

    def cmdline(self) -> list[str]:
        return list(self._command)

    def is_running(self) -> bool:
        return self.running

    def status(self) -> str:
        return "running" if self.running else "stopped"

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float) -> int:
        self.running = False
        return 0

    def poll(self) -> int | None:
        return None if self.running else 0


class BrowserChromeProcessCustodyTests(unittest.TestCase):
    def _plan(self, root: Path, *, unsafe: bool = False) -> ChromeLaunchPlan:
        executable = root / "chrome.exe"
        executable.write_bytes(b"test")
        return ChromeLaunchPlan(
            executable=executable,
            user_data_dir=root / "profile",
            downloads_dir=root / "downloads",
            runtime_dir=root / "runtime",
            policy=ChromeLaunchPolicy(allow_unsafe_sandbox_bypass=unsafe),
            session_id="browser-session-1",
        )

    def test_windows_sandbox_bypass_is_explicit_and_headless_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = self._plan(root)
            unsafe = self._plan(root, unsafe=True)
            with patch("zyra_integrations.browser_use.chrome_process.os.name", "nt"):
                safe_command = safe.command(assigned_port=9222)
                unsafe_command = unsafe.command(assigned_port=9223)
            self.assertNotIn("--no-sandbox", safe_command)
            self.assertNotIn("--disable-gpu-sandbox", safe_command)
            self.assertIn("--no-sandbox", unsafe_command)
            self.assertIn("--disable-gpu-sandbox", unsafe_command)

    def test_signed_marker_adopts_and_stops_same_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._plan(root)
            plan.runtime_dir.mkdir(parents=True)
            plan.user_data_dir.mkdir(parents=True)
            command = plan.command(assigned_port=9222)
            fake = _FakeProcess(4242, plan.executable, command)
            handle = ChromeProcess(
                process=fake,
                endpoint=BrowserEndpoint(http_url="http://127.0.0.1:9222"),
                policy=plan.policy,
                command=command,
                owned=True,
            )
            first = ChromeProcessController(custody_root=root / "state")
            with patch("zyra_integrations.browser_use.chrome_process.psutil.Process", return_value=fake):
                marker_path = first._write_custody_marker(plan, handle, debug_port=9222)
                first._owned.pop(fake.pid, None)
                second = ChromeProcessController(custody_root=root / "state")
                with patch.object(second, "_wait_ready", return_value=None):
                    adopted = second.adopt(
                        session_id=plan.session_id,
                        pid=fake.pid,
                        endpoint_url=handle.endpoint.base_url,
                        runtime_dir=plan.runtime_dir,
                        user_data_dir=plan.user_data_dir,
                        executable_path=plan.executable,
                    )
                self.assertTrue(adopted.owned)
                second.stop(fake.pid, force=True)
            self.assertTrue(fake.terminated)
            self.assertFalse(marker_path.exists())

    def test_marker_tamper_and_pid_reuse_fail_without_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._plan(root)
            plan.runtime_dir.mkdir(parents=True)
            plan.user_data_dir.mkdir(parents=True)
            command = plan.command(assigned_port=9333)
            fake = _FakeProcess(4343, plan.executable, command)
            handle = ChromeProcess(
                process=fake,
                endpoint=BrowserEndpoint(http_url="http://127.0.0.1:9333"),
                policy=plan.policy,
                command=command,
                owned=True,
            )
            controller = ChromeProcessController(custody_root=root / "state")
            with patch("zyra_integrations.browser_use.chrome_process.psutil.Process", return_value=fake):
                marker_path = controller._write_custody_marker(plan, handle, debug_port=9333)
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker["debug_port"] = 9444
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "signature mismatch"):
                    controller.adopt(
                        session_id=plan.session_id,
                        pid=fake.pid,
                        endpoint_url=handle.endpoint.base_url,
                        runtime_dir=plan.runtime_dir,
                        user_data_dir=plan.user_data_dir,
                    )
                self.assertFalse(fake.terminated)
                controller._write_custody_marker(plan, handle, debug_port=9333)
                reused = _FakeProcess(fake.pid, plan.executable, command, create_time=101.0)
                with patch("zyra_integrations.browser_use.chrome_process.psutil.Process", return_value=reused):
                    with self.assertRaisesRegex(RuntimeError, "creation time mismatch"):
                        controller.adopt(
                            session_id=plan.session_id,
                            pid=fake.pid,
                            endpoint_url=handle.endpoint.base_url,
                            runtime_dir=plan.runtime_dir,
                            user_data_dir=plan.user_data_dir,
                        )
                self.assertFalse(reused.terminated)

    def test_verified_active_owned_profile_accepts_only_transient_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = BrowserSessionCommand(
                run_id="run-profile",
                task_id="task-profile",
                worker_request_id="request-profile",
                canonical_session_id="canonical-profile",
                workspace_root=root,
                artifact_root=root / "artifacts",
            )
            store = BrowserProfileStore(root / "runtime", root / "state")
            first = store.prepare(command, "session-profile")
            lock = first.profile.root / "user-data" / "LOCK"
            lock.write_text("active", encoding="utf-8")
            active = store.prepare(command, "session-profile", active_owned_profile=True)
            self.assertEqual(active.profile.root, first.profile.root)
            self.assertFalse(active.created)
            self.assertIn("active-owned", active.warnings[0])
            self.assertEqual(tuple(store.quarantine_root.iterdir()), ())
            (first.profile.root / "profile.json").write_text("{broken", encoding="utf-8")
            recovered = store.prepare(command, "session-profile", active_owned_profile=True)
            self.assertTrue(recovered.recovered)
            self.assertEqual(len(tuple(store.quarantine_root.iterdir())), 1)

    def test_adopted_local_process_rebuilds_endpoint_only_attach_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "chrome.exe"
            executable.write_bytes(b"test")
            local = BrowserSessionCommand(
                run_id="run-adopt",
                task_id="task-adopt",
                worker_request_id="request-adopt",
                canonical_session_id="canonical-adopt",
                workspace_root=root,
                artifact_root=root / "artifacts",
                executable_path=executable,
            )
            attached = _endpoint_reattach_command(local, "http://127.0.0.1:9222")
            self.assertEqual(attached.endpoint_url, "http://127.0.0.1:9222")
            self.assertIsNone(attached.executable_path)
            self.assertEqual(attached.canonical_session_id, local.canonical_session_id)


if __name__ == "__main__":
    unittest.main()
