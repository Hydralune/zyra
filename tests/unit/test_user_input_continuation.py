from __future__ import annotations

import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state, now_iso
from zyra_memory import SQLiteStore, canonical_user_input_digest
from zyra_runtime import ToolCall, ToolExecutionContext, ToolExecutor, default_tool_registry
from zyra_workers import CanonicalUserInputBridge, user_input_tool_spec


class UserInputContinuationTests(unittest.TestCase):
    def test_provider_tool_blocks_then_resumes_with_the_canonical_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = root / "canonical.sqlite3"
            store = SQLiteStore(database)
            state = create_task_state("Ask which implementation to use, then continue.")
            store.save_checkpoint(state)
            questions = [
                {
                    "header": "实现",
                    "id": "implementation",
                    "question": "请选择这次采用的实现方式。",
                    "options": [
                        {"label": "现有结构", "description": "在当前模块内完成。"},
                        {"label": "独立模块", "description": "拆分为新的边界。"},
                    ],
                }
            ]
            bridge = CanonicalUserInputBridge(
                database,
                poll_seconds=0.01,
                maximum_wait_seconds=5,
            )
            registry = default_tool_registry().merged([user_input_tool_spec()])
            executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    root / "workspace",
                    root / "artifacts",
                    registry=registry,
                    runtime_services={"user_input_bridge": bridge},
                )
            )
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="request_user_input",
                tool_call_id="toolcall_structured_question",
                arguments={"questions": questions},
            )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(executor.execute, call)
                request = None
                deadline = time.monotonic() + 2
                while request is None and time.monotonic() < deadline:
                    pending = store.user_input_requests(
                        state.task_id,
                        include_terminal=False,
                    )
                    request = pending[0] if pending else None
                    if request is None:
                        time.sleep(0.01)
                self.assertIsNotNone(request)
                assert request is not None
                self.assertFalse(future.done())

                answers = {"implementation": {"answers": ["独立模块"]}}
                store.answer_user_input_request(
                    task_id=state.task_id,
                    request_id=request["request_id"],
                    expected_revision=request["revision"],
                    answer_id="answer_structured_question",
                    answer_digest=canonical_user_input_digest(answers),
                    answers=answers,
                    responder="test",
                    answered_at=now_iso(),
                )
                result = future.result(timeout=2)

            self.assertTrue(result.ok)
            self.assertEqual(result.tool_call_id, call.tool_call_id)
            self.assertEqual(result.output["request_id"], request["request_id"])
            self.assertEqual(result.output["answers"], answers)
            events = store.task_events(state.task_id)
            self.assertEqual(events[-1]["event_type"], "user_input")
            self.assertEqual(
                [event["payload"]["phase"] for event in events],
                ["requested", "answered"],
            )

    def test_provider_tool_closes_request_when_task_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = root / "canonical.sqlite3"
            bridge = CanonicalUserInputBridge(database, poll_seconds=0.01)
            call = ToolCall(
                run_id="run_missing",
                task_id="task_missing",
                node_id="node_missing",
                tool_name="request_user_input",
                tool_call_id="toolcall_missing_task",
                arguments={
                    "questions": [{
                        "header": "范围",
                        "id": "scope",
                        "question": "采用哪个范围？",
                        "options": [
                            {"label": "最小", "description": "只改当前范围。"},
                            {"label": "完整", "description": "同步整理相关模块。"},
                        ],
                    }],
                },
            )

            result = bridge(call)

            store = SQLiteStore(database)
            requests = store.user_input_requests(call.task_id)
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "user_input_task_inactive")
            self.assertEqual(requests[0]["status"], "cancelled")
            self.assertEqual(
                store.user_input_requests(call.task_id, include_terminal=False),
                [],
            )
            self.assertEqual(
                [event["payload"]["phase"] for event in store.task_events(call.task_id)],
                ["requested", "cancelled"],
            )

    def test_recovery_rebinds_a_replayed_tool_call_to_the_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = root / "canonical.sqlite3"
            store = SQLiteStore(database)
            state = create_task_state("Ask for a database, survive restart, then continue.")
            store.save_checkpoint(state)
            original_questions = [{
                "header": "数据库选择",
                "id": "db_choice",
                "question": "请选择要使用的数据库类型：",
                "options": [
                    {"label": "SQLite", "description": "使用嵌入式数据库。"},
                    {"label": "PostgreSQL", "description": "使用服务端数据库。"},
                ],
            }]
            original = store.create_user_input_request(
                request_id="request_before_daemon_restart",
                run_id=state.run_id,
                task_id=state.task_id,
                node_id="node_before_restart",
                tool_call_id="toolcall_before_restart",
                request_digest=canonical_user_input_digest(original_questions),
                questions=original_questions,
                created_at=now_iso(),
            )
            replayed_questions = [{
                "header": "数据库选择",
                "id": "database_choice",
                "question": "请选择使用 SQLite 还是 PostgreSQL？",
                "options": [
                    {"label": "SQLite", "description": "选择 SQLite。"},
                    {"label": "PostgreSQL", "description": "选择 PostgreSQL。"},
                ],
            }]
            bridge = CanonicalUserInputBridge(
                database,
                poll_seconds=0.01,
                maximum_wait_seconds=5,
                continuation_request_ids=[original["request_id"]],
            )
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id="node_after_restart",
                tool_name="request_user_input",
                tool_call_id="toolcall_after_restart",
                arguments={"questions": replayed_questions},
            )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(bridge, call)
                time.sleep(0.05)
                self.assertFalse(future.done())
                self.assertEqual(
                    [item["request_id"] for item in store.user_input_requests(state.task_id)],
                    [original["request_id"]],
                )
                answers = {"db_choice": {"answers": ["SQLite"]}}
                store.answer_user_input_request(
                    task_id=state.task_id,
                    request_id=original["request_id"],
                    expected_revision=0,
                    answer_id="answer_after_daemon_restart",
                    answer_digest=canonical_user_input_digest(answers),
                    answers=answers,
                    responder="test",
                    answered_at=now_iso(),
                )
                result = future.result(timeout=2)

            self.assertTrue(result.ok)
            self.assertEqual(result.output["request_id"], original["request_id"])
            self.assertEqual(
                result.output["answers"],
                {"database_choice": {"answers": ["SQLite"]}},
            )
            self.assertEqual(result.metadata["continuation_rebound"], "true")
            self.assertEqual(len(store.user_input_requests(state.task_id)), 1)


if __name__ == "__main__":
    unittest.main()
