from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from zyra_core import create_task_state
from zyra_runtime import ToolCall, ToolExecutionContext, WorkerRequest
from zyra_skills import (
    McpSkillProjectionRuntime,
    PluginCapabilityIntegrationRuntime,
    SkillInvocationRequest,
    SkillRuntime,
    SkillRuntimeConfig,
    SkillTaskIntegrationRuntime,
    SkillToolProjectionRuntime,
    SkillUpdateControlRuntime,
    compose_skill_runtime,
)
from zyra_workers import CodeWorkerRuntime


ROOT = Path(__file__).resolve().parents[2]


class _McpPort:
    def __init__(self, resources: dict[str, str]) -> None:
        self.resources = resources
        self.reads: list[str] = []

    def server_snapshot(self, server_id: str):
        return {
            "server_id": server_id,
            "state": "connected",
            "enabled": True,
            "authenticated": True,
            "capability_revision": "cap-1",
        }

    def read_resource(self, server_id: str, uri: str, **context):
        self.reads.append(uri)
        return {"ok": True, "content": self.resources[uri], "sampling_requested": False}


class _AllowPermissionRuntime:
    def guard(self, evaluation):
        return SimpleNamespace(
            decision=SimpleNamespace(
                effect="allow",
                request_id=evaluation.tool_use_id,
                reason="test exact local update",
            ),
            execution_grant=None,
            events=(),
        )


class _AskThenAllowPermissionRuntime:
    def __init__(self, *, allow: bool = False) -> None:
        self.allow = allow

    def guard(self, evaluation):
        effect = "allow" if self.allow else "ask"
        return SimpleNamespace(
            decision=SimpleNamespace(
                effect=effect,
                request_id=evaluation.tool_use_id,
                reason=f"test {effect} exact local update",
            ),
            execution_grant=None,
            events=(),
        )


def _runtime(snapshot=None) -> SkillRuntime:
    runtime = SkillRuntime(
        SkillRuntimeConfig.for_project(ROOT, workspace_root=ROOT),
        state_snapshot=snapshot,
    )
    runtime.bootstrap()
    return runtime


def _request(skill: str, *, invocation_id: str, session_id: str = "skill-session") -> SkillInvocationRequest:
    return SkillInvocationRequest(
        run_id="run-skill-integration",
        task_id="task-skill-integration",
        session_id=session_id,
        agent_id="CodeWorkerRuntime",
        skill_name=skill,
        arguments={},
        parent_tool_use_id=f"tool-{invocation_id}",
        worker_request_id="worker-skill-integration",
        idempotency_key=f"idem-{invocation_id}",
        invocation_id=invocation_id,
    )


class SkillToolMainPathTests(unittest.TestCase):
    def test_codeworker_projects_skill_tools_before_query_permission_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = create_task_state("skill tool main path")
            run = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmp) / "workspace",
                artifact_root=Path(tmp) / "artifacts",
            ).run(
                WorkerRequest(
                    run_id=task.run_id,
                    task_id=task.task_id,
                    node_id=task.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "raw_input": "List the skills through the query loop.",
                        "query_turns": [[{
                            "tool_name": "list_skills",
                            "tool_call_id": "skill-list-call",
                            "arguments": {},
                        }]],
                    },
                )
            )
            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_suspended")
            self.assertEqual(run.worker_result.metadata["skill_tool_projection"], "active")
            tool_results = [
                event.payload["tool_result"]
                for event in run.event_records
                if isinstance(event.payload, dict) and isinstance(event.payload.get("tool_result"), dict)
            ]
            self.assertTrue(any(item["tool_call_id"] == "skill-list-call" for item in tool_results))
            self.assertTrue(any(item.get("error") == "permission_required" for item in tool_results))

    def test_projected_handler_discloses_inline_body_and_not_fork_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolExecutionContext.for_workspace(Path(tmp) / "workspace", Path(tmp) / "artifacts")
            request = SimpleNamespace(
                run_id="run-1",
                task_id="task-1",
                node_id="node-1",
                request_id="worker-1",
                worker_name="CodeWorkerRuntime",
                constraints={},
            )
            opened = SkillToolProjectionRuntime.open_for_worker(
                context,
                project_root=ROOT,
                request=request,
                session_id="session-1",
            )
            inline = opened.context.dynamic_handlers["skill"](
                ToolCall(
                    run_id="run-1",
                    task_id="task-1",
                    tool_name="skill",
                    tool_call_id="inline-call",
                    arguments={"skill": "code-change", "invocation_id": "inline-inv"},
                )
            )
            forked = opened.context.dynamic_handlers["skill"](
                ToolCall(
                    run_id="run-1",
                    task_id="task-1",
                    tool_name="skill",
                    tool_call_id="fork-call",
                    arguments={"skill": "web-research", "invocation_id": "fork-inv"},
                )
            )
            self.assertTrue(inline.ok)
            self.assertTrue(inline.output["body_disclosed"])
            self.assertIn("authorized workspace", inline.output["body"])
            self.assertTrue(forked.ok)
            self.assertFalse(forked.output["body_disclosed"])
            self.assertEqual(forked.output["body"], "")
            self.assertTrue(forked.output["fork_request"])
            self.assertIs(
                opened.context.runtime_services["skill_runtime"],
                opened.projection.runtime,
            )
            completed = opened.projection.finalize_successful_query(
                evidence_refs=("event://query-complete",),
                artifact_refs=("artifact://trace",),
            )
            self.assertEqual(len(completed), 1)
            self.assertEqual(
                str(opened.projection.runtime.state_store.get("inline-inv").status),
                "completed",
            )
            self.assertEqual(
                str(opened.projection.runtime.state_store.get("fork-inv").status),
                "fork_pending",
            )
            snapshot = opened.projection.snapshot()
            self.assertEqual(set(snapshot.invocation_ids), {"inline-inv", "fork-inv"})
            self.assertFalse(any("body" in item for item in snapshot.compact_references))

    def test_declared_resource_is_model_reachable_and_scope_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolExecutionContext.for_workspace(Path(tmp) / "workspace", Path(tmp) / "artifacts")
            request = SimpleNamespace(run_id="run-r", task_id="task-r", node_id="n", request_id="w", worker_name="CodeWorkerRuntime", constraints={})
            opened = SkillToolProjectionRuntime.open_for_worker(context, project_root=ROOT, request=request, session_id="session-r")
            opened.context.dynamic_handlers["skill"](
                ToolCall(run_id="run-r", task_id="task-r", tool_name="skill", tool_call_id="invoke-r", arguments={"skill": "code-change", "invocation_id": "inv-r"})
            )
            resource = opened.context.dynamic_handlers["read_skill_resource"](
                ToolCall(run_id="run-r", task_id="task-r", tool_name="read_skill_resource", tool_call_id="resource-r", arguments={"invocation_id": "inv-r", "path": "references/change-safety.md"})
            )
            escaped = opened.context.dynamic_handlers["read_skill_resource"](
                ToolCall(run_id="run-r", task_id="task-r", tool_name="read_skill_resource", tool_call_id="resource-x", arguments={"invocation_id": "inv-r", "path": "../SKILL.md"})
            )
            self.assertTrue(resource.ok)
            self.assertIn("change", resource.output["content"].lower())
            self.assertFalse(escaped.ok)


class TaskAndCompactIntegrationTests(unittest.TestCase):
    def test_disclosure_lease_commits_once_per_invocation_and_abort_retries(self) -> None:
        runtime = _runtime()
        plan = runtime.invoke(_request("code-change", invocation_id="lease-inv"))
        checkpoint = runtime.session_bridge.checkpoint(
            session_id=plan.state.session_id,
            agent_id=plan.state.agent_id,
            runtime_state_snapshot=runtime.state_snapshot(),
        )
        metadata = {
            "skill_runtime_state": checkpoint.to_dict(),
            "skill_session_context": {
                "invoked_skill_refs": [item.to_dict() for item in checkpoint.compact_references]
            },
        }
        restored_runtime = _runtime(checkpoint.to_dict())
        bridge = SkillTaskIntegrationRuntime()
        first = bridge.prepare_disclosures(metadata=metadata, runtime=restored_runtime, run_id=plan.state.run_id, task_id=plan.state.task_id, worker_request_id="worker-a")
        self.assertEqual(len(first.disclosures), 1)
        bridge.abort_disclosures(metadata=metadata, batch=first, reason="worker failed before accepting request")
        retry = bridge.prepare_disclosures(metadata=metadata, runtime=restored_runtime, run_id=plan.state.run_id, task_id=plan.state.task_id, worker_request_id="worker-b")
        self.assertEqual(len(retry.disclosures), 1)
        bridge.commit_disclosures(metadata=metadata, batch=retry, worker_event_ids=("event-1",))
        final = bridge.prepare_disclosures(metadata=metadata, runtime=restored_runtime, run_id=plan.state.run_id, task_id=plan.state.task_id, worker_request_id="worker-c")
        self.assertTrue(final.empty)
        self.assertEqual(final.skipped[0]["reason"], "already_disclosed")
        projected = bridge.snapshot(metadata).to_dict()
        self.assertEqual(projected["pending_disclosures"], [])
        self.assertNotIn("authorized workspace", json.dumps(projected))
        self.assertFalse(projected["body_persisted"])

    def test_terminal_and_fork_references_never_restore_parent_body(self) -> None:
        runtime = _runtime()
        inline = runtime.invoke(_request("code-change", invocation_id="complete-inv"))
        forked = runtime.invoke(_request("web-research", invocation_id="fork-compact"))
        runtime.invocation_runtime.complete("complete-inv", outcome_refs=("event://outcome",))
        states = runtime.state_store.all_states()
        self.assertEqual(runtime.compact_bridge.references(states, session_id="skill-session", agent_id="CodeWorkerRuntime"), ())
        fork_state = runtime.state_store.get("fork-compact")
        self.assertEqual(str(fork_state.status), "fork_pending")
        self.assertIsNotNone(forked.fork_request)


class PluginAndMcpIntegrationTests(unittest.TestCase):
    def test_plugin_commands_hooks_disable_and_last_good_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo-plugin"
            (root / ".zyra-plugin").mkdir(parents=True)
            (root / "skills" / "helper").mkdir(parents=True)
            (root / ".zyra-plugin" / "plugin.json").write_text(json.dumps({
                "id": "demo",
                "version": "1.0.0",
                "skills": ["skills"],
                "commands": [{"name": "assist", "skill": "helper", "aliases": ["help-now"]}],
                "hooks": [{"event": "pre_invoke", "action": "emit_event", "matcher": "*", "payload": {"kind": "plugin_seen"}}],
            }), encoding="utf-8")
            (root / "skills" / "helper" / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: helper\ndescription: Plugin helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nPlugin helper body.\n",
                encoding="utf-8",
            )
            plugins = PluginCapabilityIntegrationRuntime((root,))
            receipt = plugins.refresh()
            self.assertTrue(receipt.applied)
            self.assertEqual(plugins.resolve_command("/demo:assist").target_skill, "plugin:demo:helper")
            self.assertEqual(len(plugins.plugin_hooks_for_skill("plugin:demo:helper")), 1)
            disabled = plugins.disable("demo", reason="operator disabled")
            self.assertTrue(disabled.applied)
            with self.assertRaises(Exception):
                plugins.resolve_command("/demo:assist")
            (root / ".zyra-plugin" / "plugin.json").write_text("{broken", encoding="utf-8")
            rejected = plugins.refresh()
            self.assertFalse(rejected.applied)
            self.assertEqual(rejected.snapshot_digest, disabled.snapshot_digest)

    def test_mcp_skill_projection_verifies_exact_bytes_and_scans_as_source(self) -> None:
        body = "---\nschema: zyra.skill/v1\nname: remote-helper\ndescription: Remote helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nRemote helper body.\n"
        body_digest = f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"
        index = json.dumps({
            "schema": "zyra.mcp-skills/v1",
            "server_id": "skill-peer",
            "capability_revision": "cap-1",
            "skills": [{"name": "remote-helper", "version": "1.0.0", "body_uri": "skill:/remote-helper/SKILL.md", "body_digest": body_digest, "resources": []}],
            "next_cursor": "",
        })
        port = _McpPort({"skill://index.json": index, "skill:/remote-helper/SKILL.md": body})
        with tempfile.TemporaryDirectory() as tmp:
            opened = McpSkillProjectionRuntime(port, cache_root=tmp).open("skill-peer")
            scan = opened.source.scan(generation=1)
            self.assertTrue(scan.ok, scan.errors)
            self.assertEqual(scan.revisions[0].metadata.name, "remote-helper")
            self.assertEqual(port.reads, ["skill://index.json", "skill:/remote-helper/SKILL.md"])
            port.resources["skill:/remote-helper/SKILL.md"] = body + "tampered"
            with self.assertRaises(Exception):
                McpSkillProjectionRuntime(port, cache_root=Path(tmp) / "second").open("skill-peer")

    def test_request_scoped_mcp_source_does_not_leak_into_later_composition(self) -> None:
        body = "---\nschema: zyra.skill/v1\nname: transient\ndescription: Transient helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nTransient body.\n"
        body_digest = f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"
        index = json.dumps({
            "schema": "zyra.mcp-skills/v1",
            "server_id": "ephemeral-peer",
            "capability_revision": "cap-1",
            "skills": [{"name": "transient", "version": "1.0.0", "body_uri": "skill:/transient/SKILL.md", "body_digest": body_digest, "resources": []}],
            "next_cursor": "",
        })
        port = _McpPort({"skill://index.json": index, "skill:/transient/SKILL.md": body})
        with tempfile.TemporaryDirectory() as tmp:
            source = McpSkillProjectionRuntime(port, cache_root=Path(tmp) / "mcp").open("ephemeral-peer").source
            (Path(tmp) / "first").mkdir()
            (Path(tmp) / "second").mkdir()
            first, first_composition = compose_skill_runtime(
                product_root=ROOT,
                workspace_root=Path(tmp) / "first",
                external_sources=(source,),
            )
            first.bootstrap()
            self.assertIn("mcp:ephemeral-peer:transient", {item.qualified_name for item in first.list()})
            self.assertEqual(len(first_composition.external_source_ids), 1)
            self.assertTrue(first_composition.external_source_ids[0].startswith("mcp:ephemeral-peer:"))
            second, second_composition = compose_skill_runtime(
                product_root=ROOT,
                workspace_root=Path(tmp) / "second",
            )
            second.bootstrap()
            self.assertNotIn("mcp:ephemeral-peer:transient", {item.qualified_name for item in second.list()})
            self.assertEqual(second_composition.external_source_ids, ())

    def test_composition_discovers_workspace_project_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            skill = workspace / ".zyra" / "skills" / "workspace-helper"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: workspace-helper\ndescription: Workspace helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nWorkspace-owned body.\n",
                encoding="utf-8",
            )
            runtime, composition = compose_skill_runtime(product_root=ROOT, workspace_root=workspace)
            runtime.bootstrap()
            names = {item.qualified_name for item in runtime.list()}
            self.assertIn("project:workspace-helper", names)
            self.assertTrue(composition.project_roots)

    def test_local_update_rebuilds_composition_and_changes_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            bundle = workspace / ".zyra" / "skill-imports" / "release-one" / "installed-helper"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: installed-helper\ndescription: Installed helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nInstalled helper body.\n",
                encoding="utf-8",
            )
            runtime = SkillRuntime(
                SkillRuntimeConfig.for_project(ROOT, workspace_root=workspace)
            )
            runtime.bootstrap()
            metadata = {}
            control = SkillUpdateControlRuntime(
                product_root=ROOT,
                workspace_root=workspace,
                permission_runtime=_AllowPermissionRuntime(),
                skill_runtime=runtime,
            )
            receipt = control.execute(
                {"action": "install", "channel": "project", "bundle_name": "release-one"},
                run_id="run-update",
                task_id="task-update",
                session_id="session-update",
                task_metadata=metadata,
            )
            self.assertEqual(str(receipt.state.status), "committed")
            self.assertIn(
                "project:installed-helper",
                {item.qualified_name for item in control.skill_runtime.list()},
            )
            self.assertEqual(
                metadata["skill_update_control"]["latest_update_id"],
                receipt.state.request.update_id,
            )

    def test_pending_update_restores_and_resumes_after_control_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            bundle = workspace / ".zyra" / "skill-imports" / "pending-release" / "pending-helper"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: pending-helper\ndescription: Pending helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nPending helper body.\n",
                encoding="utf-8",
            )
            metadata = {}
            permission = _AskThenAllowPermissionRuntime()
            initial_runtime = SkillRuntime(SkillRuntimeConfig.for_project(ROOT, workspace_root=workspace))
            initial_runtime.bootstrap()
            initial = SkillUpdateControlRuntime(
                product_root=ROOT,
                workspace_root=workspace,
                permission_runtime=permission,
                skill_runtime=initial_runtime,
            )
            request = {
                "action": "install",
                "channel": "project",
                "bundle_name": "pending-release",
                "update_id": "pending-update-1",
            }
            with self.assertRaises(Exception):
                initial.execute(
                    request,
                    run_id="run-pending",
                    task_id="task-pending",
                    session_id="session-pending",
                    task_metadata=metadata,
                )
            self.assertIn("pending-update-1", metadata["skill_update_runtime_state"]["pending_requests"])
            permission.allow = True
            restored_runtime = SkillRuntime(SkillRuntimeConfig.for_project(ROOT, workspace_root=workspace))
            restored_runtime.bootstrap()
            restored = SkillUpdateControlRuntime(
                product_root=ROOT,
                workspace_root=workspace,
                permission_runtime=permission,
                skill_runtime=restored_runtime,
                state_snapshot=metadata["skill_update_runtime_state"],
            )
            receipt = restored.execute(
                request,
                run_id="run-pending",
                task_id="task-pending",
                session_id="session-pending",
                task_metadata=metadata,
            )
            self.assertEqual(str(receipt.state.status), "committed")
            self.assertNotIn("pending-update-1", metadata["skill_update_runtime_state"]["pending_requests"])
            self.assertIn("project:pending-helper", {item.qualified_name for item in restored.skill_runtime.list()})

    def test_publication_failure_rolls_back_committed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            bundle = workspace / ".zyra" / "skill-imports" / "bad-publication" / "rollback-helper"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: rollback-helper\ndescription: Rollback helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nRollback body.\n",
                encoding="utf-8",
            )
            runtime = SkillRuntime(SkillRuntimeConfig.for_project(ROOT, workspace_root=workspace))
            runtime.bootstrap()
            control = SkillUpdateControlRuntime(
                product_root=ROOT,
                workspace_root=workspace,
                permission_runtime=_AllowPermissionRuntime(),
                skill_runtime=runtime,
            )

            def reject_publication(_request):
                raise RuntimeError("publication rejected by registry")

            control.runtime.runtime_rebuilder = reject_publication
            with self.assertRaisesRegex(RuntimeError, "publication rejected"):
                control.execute(
                    {"action": "install", "channel": "project", "bundle_name": "bad-publication", "update_id": "rollback-update-1"},
                    run_id="run-rollback",
                    task_id="task-rollback",
                    session_id="session-rollback",
                    task_metadata={},
                )
            self.assertFalse((workspace / ".zyra" / "skills" / "rollback-helper").exists())

    def test_plugin_disable_persists_and_excludes_future_compositions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            plugin = workspace / ".zyra" / "plugins" / "demo"
            (plugin / ".zyra-plugin").mkdir(parents=True)
            (plugin / "skills" / "helper").mkdir(parents=True)
            (plugin / ".zyra-plugin" / "plugin.json").write_text(json.dumps({
                "id": "demo",
                "version": "1.0.0",
                "skills": ["skills"],
            }), encoding="utf-8")
            (plugin / "skills" / "helper" / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: helper\ndescription: Plugin helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nPlugin helper.\n",
                encoding="utf-8",
            )
            runtime, _ = compose_skill_runtime(product_root=ROOT, workspace_root=workspace)
            runtime.bootstrap()
            self.assertIn("plugin:demo:helper", {item.qualified_name for item in runtime.list()})
            control = SkillUpdateControlRuntime(
                product_root=ROOT,
                workspace_root=workspace,
                permission_runtime=_AllowPermissionRuntime(),
                skill_runtime=runtime,
            )
            receipt = control.execute(
                {"action": "disable", "channel": "plugin", "plugin_id": "demo", "reason": "operator disabled", "update_id": "disable-demo-1"},
                run_id="run-disable",
                task_id="task-disable",
                session_id="session-disable",
                task_metadata={},
            )
            self.assertEqual(str(receipt.state.status), "committed")
            self.assertNotIn("plugin:demo:helper", {item.qualified_name for item in control.skill_runtime.list()})
            rebuilt, _ = compose_skill_runtime(product_root=ROOT, workspace_root=workspace)
            rebuilt.bootstrap()
            self.assertNotIn("plugin:demo:helper", {item.qualified_name for item in rebuilt.list()})


if __name__ == "__main__":
    unittest.main()
