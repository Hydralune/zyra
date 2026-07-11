from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "skills",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_skills import (
    AtomicSkillPackageUpdater,
    ConditionalSkillActivator,
    DurableForkRequestQueue,
    FilesystemSkillSource,
    InvokedSkillState,
    PreauthorizedBuiltinSkillPermission,
    SafeFrontmatterParser,
    SkillAllowedToolsPolicy,
    SkillBodyLoaderDisabled,
    SkillBodyResourceLoader,
    SkillBudgetExceeded,
    SkillBudgetStage,
    SkillChangeDetector,
    SkillCommandSafety,
    SkillCommandSafetyClassifier,
    SkillCompactBridge,
    SkillContextBudget,
    SkillContextBudgetRuntime,
    SkillFrontmatterError,
    SkillHookEvent,
    SkillHookLifetime,
    SkillHookRuntime,
    SkillHookSpec,
    SkillInvocationMode,
    SkillInvocationRequest,
    SkillInvocationRuntime,
    SkillInvocationSpec,
    SkillInvocationStateStore,
    SkillInvocationStatus,
    SkillPolicyDenied,
    SkillRegistry,
    SkillRegistryDisabled,
    SkillResourceLoader,
    SkillRevisionStore,
    SkillRuntime,
    SkillRuntimeAuditor,
    SkillRuntimeConfig,
    SkillRuntimeDisabled,
    SkillRuntimeHealthProbe,
    SkillSearchIndex,
    SkillSourceKind,
    SkillSymlinkError,
    ToolSelector,
    ToolUseIdentity,
    default_skill_runtime,
    parse_skill_document,
)


def _skill_text(
    name: str,
    *,
    description: str = "A test skill with real runtime semantics.",
    mode: str = "inline",
    agent: str = "",
    allowed_tools: str = '["builtin/file_read","builtin/trace"]',
    resources: str = "[]",
    hooks: str = "[]",
    paths: str = "[]",
    body: str = "Perform the scoped test workflow.",
) -> str:
    invocation = f'{{"mode":"{mode}","max-skill-depth":0'
    if agent:
        invocation += f',"agent":"{agent}"'
    invocation += "}"
    return (
        "---\n"
        "schema: zyra.skill/v1\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "version: 1.0.0\n"
        "user-invocable: true\n"
        "model-invocable: true\n"
        f"invocation: {invocation}\n"
        f"allowed-tools: {allowed_tools}\n"
        "context-budget: {\"listing-tokens\":64,\"body-tokens\":2000,\"resource-read-tokens\":1000,\"invocation-total-tokens\":4000,\"restore-tokens\":1000}\n"
        f"resources: {resources}\n"
        f"hooks: {hooks}\n"
        f"paths: {paths}\n"
        "---\n"
        f"{body}\n"
    )


def _write_skill(root: Path, name: str, **kwargs: object) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_skill_text(name, **kwargs), encoding="utf-8")
    return directory


def _registry_for(root: Path, *, kind: SkillSourceKind = SkillSourceKind.PROJECT, namespace: str = "project") -> SkillRegistry:
    store = SkillRevisionStore()
    registry = SkillRegistry(
        revision_store=store,
        sources=(
            FilesystemSkillSource(
                root=root,
                source_kind=kind,
                source_id=f"{kind}:test",
                namespace=namespace,
            ),
        ),
    )
    result = registry.reload(expected_generation=0)
    if not result.applied:
        raise AssertionError(result.to_dict())
    return registry


def _invocation_runtime(
    registry: SkillRegistry,
    *,
    allowed_policy: SkillAllowedToolsPolicy | None = None,
    fork_port: object | None = None,
    body_disabled: bool = False,
) -> SkillInvocationRuntime:
    body = SkillBodyResourceLoader(registry.revision_store, disabled=body_disabled)
    policy = allowed_policy or SkillAllowedToolsPolicy()
    return SkillInvocationRuntime(
        registry=registry,
        body_loader=body,
        resource_loader=SkillResourceLoader(registry.revision_store),
        allowed_tools_policy=policy,
        state_store=SkillInvocationStateStore(),
        hook_runtime=SkillHookRuntime(),
        attachment_runtime=__import__("zyra_skills", fromlist=["SkillAttachmentRuntime"]).SkillAttachmentRuntime(),
        permission_port=PreauthorizedBuiltinSkillPermission(),
        fork_port=fork_port,
    )


class FrontmatterAndPathTests(unittest.TestCase):
    def test_valid_document_normalizes_runtime_contract(self) -> None:
        parsed = parse_skill_document(_skill_text("sample-skill"), expected_name="sample-skill")

        self.assertEqual(parsed.metadata.name, "sample-skill")
        self.assertEqual(parsed.metadata.invocation.mode, SkillInvocationMode.INLINE)
        self.assertEqual(parsed.metadata.allowed_tools[0].canonical_name, "builtin/file_read")
        self.assertIn("Perform the scoped", parsed.body)

    def test_duplicate_frontmatter_key_is_rejected(self) -> None:
        text = _skill_text("sample-skill").replace(
            "description: A test skill with real runtime semantics.\n",
            "description: first\ndescription: second\n",
        )
        with self.assertRaises(SkillFrontmatterError):
            parse_skill_document(text, expected_name="sample-skill")

    def test_yaml_alias_tag_and_merge_are_rejected(self) -> None:
        bad_values = (
            "description: &anchor unsafe\n",
            "description: !python/object unsafe\n",
            "description: safe\n<<: *defaults\n",
        )
        for replacement in bad_values:
            with self.subTest(replacement=replacement):
                text = _skill_text("sample-skill").replace(
                    "description: A test skill with real runtime semantics.\n",
                    replacement,
                )
                with self.assertRaises(SkillFrontmatterError):
                    parse_skill_document(text, expected_name="sample-skill")

    def test_unknown_security_field_and_name_mismatch_are_rejected(self) -> None:
        unknown = _skill_text("sample-skill").replace(
            "version: 1.0.0\n",
            "version: 1.0.0\nrun-command: rm -rf workspace\n",
        )
        with self.assertRaises(SkillFrontmatterError):
            parse_skill_document(unknown, expected_name="sample-skill")
        with self.assertRaises(SkillFrontmatterError):
            parse_skill_document(_skill_text("other-skill"), expected_name="sample-skill")

    def test_explicit_empty_allowed_tools_differs_from_missing_field(self) -> None:
        empty = parse_skill_document(
            _skill_text("empty-tools", allowed_tools="[]"),
            expected_name="empty-tools",
        )
        missing_text = _skill_text("missing-tools").replace(
            'allowed-tools: ["builtin/file_read","builtin/trace"]\n',
            "",
        )
        missing = parse_skill_document(missing_text, expected_name="missing-tools")

        self.assertEqual(empty.metadata.allowed_tools, ())
        self.assertIsNone(missing.metadata.allowed_tools)

    def test_resource_traversal_and_absolute_path_are_rejected(self) -> None:
        for resource in ('["../secret.txt"]', '["C:/secret.txt"]', '["/etc/passwd"]'):
            with self.subTest(resource=resource):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    _write_skill(root, "unsafe-resource", resources=resource)
                    scan = FilesystemSkillSource(
                        root=root,
                        source_kind=SkillSourceKind.PROJECT,
                        source_id="project:unsafe",
                        namespace="project",
                    ).scan(generation=1)
                    self.assertFalse(scan.ok)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_real_resource_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            directory = _write_skill(root, "linked-resource", resources='["references/outside.md"]')
            (directory / "references").mkdir()
            outside = root / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            try:
                os.symlink(outside, directory / "references" / "outside.md")
            except OSError:
                self.skipTest("symlink creation not permitted")
            scan = FilesystemSkillSource(
                root=root,
                source_kind=SkillSourceKind.PROJECT,
                source_id="project:linked",
                namespace="project",
            ).scan(generation=1)
            self.assertFalse(scan.ok)
            self.assertTrue(any(error["code"] == "skill_symlink_rejected" for error in scan.errors))

    @unittest.skipUnless(os.name == "nt", "NTFS junction test is Windows-specific")
    def test_real_resource_junction_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            source = base / "source"
            directory = _write_skill(source, "linked-resource", resources='["references/outside.md"]')
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside.md").write_text("secret", encoding="utf-8")
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(directory / "references"), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            scan = FilesystemSkillSource(
                root=source,
                source_kind=SkillSourceKind.PROJECT,
                source_id="project:junction",
                namespace="project",
            ).scan(generation=1)

            self.assertFalse(scan.ok)
            self.assertTrue(
                any(
                    error["code"] in {"skill_symlink_rejected", "skill_path_outside_root"}
                    for error in scan.errors
                ),
                scan.errors,
            )


class RegistryAndLoaderTests(unittest.TestCase):
    def test_listing_does_not_load_body_or_resources(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        before_body = runtime.body_loader.body_load_count
        before_resource = runtime.resource_loader.load_count

        listing = runtime.list()
        projection = runtime.listing_projection(session_id="s1", agent_id="a1")

        self.assertEqual(len(listing), 10)
        self.assertGreater(len(projection.entries), 0)
        self.assertEqual(runtime.body_loader.body_load_count, before_body)
        self.assertEqual(runtime.resource_loader.load_count, before_resource)
        self.assertFalse(any("text" in item for item in projection.entries))

    def test_body_and_resource_are_loaded_lazily_with_digest_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = _write_skill(root, "lazy-skill", resources='["references/info.md"]')
            (skill / "references").mkdir()
            (skill / "references" / "info.md").write_text("reference content", encoding="utf-8")
            registry = _registry_for(root)
            revision = registry.resolve("lazy-skill")
            body_loader = SkillBodyResourceLoader(registry.revision_store)
            resource_loader = SkillResourceLoader(registry.revision_store)

            self.assertEqual(body_loader.body_load_count, 0)
            self.assertEqual(resource_loader.load_count, 0)
            body = body_loader.load_body(revision)
            resource = resource_loader.load(revision, "references/info.md")

            self.assertIn("scoped test workflow", body.text)
            self.assertEqual(resource.content, "reference content")
            self.assertEqual(body_loader.body_load_count, 1)
            self.assertEqual(resource_loader.load_count, 1)

    def test_tampered_body_fails_exact_revision_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = _write_skill(root, "tamper-skill")
            registry = _registry_for(root)
            revision = registry.resolve("tamper-skill")
            (skill / "SKILL.md").write_text(_skill_text("tamper-skill", body="tampered"), encoding="utf-8")

            with self.assertRaises(Exception) as captured:
                SkillBodyResourceLoader(registry.revision_store).load_body(revision)
            self.assertEqual(getattr(captured.exception, "code", ""), "skill_revision_mismatch")

    def test_invalid_atomic_reload_preserves_last_good_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(root, "stable-skill")
            registry = _registry_for(root)
            generation = registry.generation
            (root / "stable-skill" / "SKILL.md").write_text("not-frontmatter", encoding="utf-8")

            result = registry.reload(expected_generation=generation)

            self.assertFalse(result.applied)
            self.assertEqual(registry.generation, generation)
            self.assertEqual(registry.resolve("stable-skill").metadata.name, "stable-skill")

    def test_concurrent_readers_observe_complete_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(root, "concurrent-skill")
            registry = _registry_for(root)
            observed: list[tuple[int, int]] = []
            stop = threading.Event()

            def reader() -> None:
                while not stop.is_set():
                    snapshot = registry.snapshot()
                    observed.append((snapshot.generation, len(snapshot.active_by_qualified_name)))

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                _write_skill(root, "second-skill")
                result = registry.reload(expected_generation=1)
                self.assertTrue(result.applied)
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertTrue(observed)
            self.assertTrue(all(item in {(1, 1), (2, 2)} for item in observed))

    def test_precedence_tombstone_does_not_expose_lower_source(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        revision = runtime.registry.resolve("code-change")
        runtime.registry.disable("builtin:code-change", reason="managed policy revoked skill")

        with self.assertRaises(Exception):
            runtime.registry.resolve("code-change")
        with self.assertRaises(Exception):
            runtime.registry.resolve("builtin:code-change")
        with self.assertRaises(Exception):
            runtime.registry.resolve("code-change", requested_ref=revision.version_ref)

    def test_revision_generation_failure_restores_exact_lifecycle_state(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        revision = runtime.registry.resolve("code-change")
        store = SkillRevisionStore()
        store.commit_generation((revision,), active_refs=(revision.version_ref.immutable_ref,))
        before = store.snapshot()

        with self.assertRaises(Exception):
            store.commit_generation((revision,), active_refs=("skill://missing/revision",))

        self.assertEqual(store.snapshot(), before)


class AllowedToolsPolicyTests(unittest.TestCase):
    def test_explicit_empty_policy_denies_every_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(root, "deny-tools", allowed_tools="[]")
            registry = _registry_for(root, kind=SkillSourceKind.BUILTIN, namespace="builtin")
            revision = registry.resolve("deny-tools")
            policy = SkillAllowedToolsPolicy()
            policy.bind(invocation_id="i1", session_id="s1", revision=revision)

            decision = policy.decide(
                session_id="s1",
                tool=ToolUseIdentity(namespace="builtin", name="file_read"),
            )

            self.assertFalse(decision.allowed_by_ceiling)
            self.assertTrue(decision.metadata["bypass_immune"])

    def test_admitted_tool_is_passthrough_not_a_grant(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        revision = runtime.registry.resolve("codebase-analysis")
        policy = SkillAllowedToolsPolicy()
        policy.bind(invocation_id="i1", session_id="s1", revision=revision)

        decision = policy.decide(
            session_id="s1",
            tool=ToolUseIdentity(namespace="builtin", name="file_read"),
        )

        self.assertTrue(decision.allowed_by_ceiling)
        self.assertTrue(decision.metadata["advisory_allow_only"])
        self.assertFalse(decision.metadata["grant_issued"])

    def test_nested_policy_is_intersection_not_union(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        broad = runtime.registry.resolve("code-change")
        narrow = runtime.registry.resolve("codebase-analysis")
        policy = SkillAllowedToolsPolicy()
        parent = policy.bind(invocation_id="parent", session_id="s1", revision=narrow)
        child = policy.bind(
            invocation_id="child",
            session_id="s1",
            revision=broad,
            parent_snapshot_ids=(parent.snapshot_id,),
        )

        names = {selector.name for selector in child.effective_tools}
        self.assertIn("file_read", names)
        self.assertNotIn("file_write", names)
        with self.assertRaises(SkillPolicyDenied):
            policy.require_allowed(
                session_id="s1",
                invocation_id="child",
                tool=ToolUseIdentity(namespace="builtin", name="file_write"),
            )

    def test_real_03a_hook_denies_unlisted_tool(self) -> None:
        from zyra_runtime.permission.hooks import PermissionHookAdapter, PermissionHookInput

        runtime = default_skill_runtime(refresh=True)
        revision = runtime.registry.resolve("codebase-analysis")
        policy = SkillAllowedToolsPolicy()
        policy.bind(invocation_id="i1", session_id="s1", revision=revision)
        adapter = PermissionHookAdapter()
        hook_id = policy.register_permission_hook(adapter)
        aggregate = adapter.run_pre_tool_use(
            PermissionHookInput.build(
                session_id="s1",
                run_id="r1",
                task_id="t1",
                worker_id="w1",
                tool_call_id="c1",
                tool_name="file_write",
                server_name="",
                arguments={"path": "x.txt"},
                mode="bypass",
                workspace_root=str(ROOT),
                interactive=True,
                sealed=False,
                metadata={"tool_namespace": "builtin", "skill_invocation_id": "i1"},
            )
        )

        self.assertEqual(str(aggregate.effect), "deny")
        self.assertTrue(policy.unregister_permission_hook(adapter))
        self.assertNotIn(hook_id, {item.hook_id for item in adapter.list_hooks()})


class InvocationLifecycleTests(unittest.TestCase):
    def _request(self, skill: str, **changes: object) -> SkillInvocationRequest:
        values = {
            "run_id": "r1",
            "task_id": "t1",
            "session_id": "s1",
            "agent_id": "CodeWorkerRuntime",
            "skill_name": skill,
            "arguments": {"goal": "test"},
            "worker_request_id": "w1",
            "parent_tool_use_id": "tool1",
        }
        values.update(changes)
        return SkillInvocationRequest(**values)

    def test_inline_invocation_changes_messages_policy_and_state(self) -> None:
        runtime = default_skill_runtime(refresh=True)

        plan = runtime.invoke(self._request("codebase-analysis"))

        self.assertEqual(plan.state.status, SkillInvocationStatus.INLINE_ACTIVE)
        self.assertIn("Base directory for this skill", plan.messages[0].content)
        self.assertTrue(plan.attachments)
        self.assertFalse(plan.to_dict(include_body=False)["body"].get("text"))
        self.assertFalse(plan.policy_snapshot.to_dict().get("grant", False))
        self.assertTrue(runtime.state_snapshot()["states"])

    def test_complete_cleans_policy_and_hook_leases(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        plan = runtime.invoke(self._request("codebase-analysis"))

        completed = runtime.invocation_runtime.complete(
            plan.state.invocation_id,
            outcome_refs=("outcome://1",),
            evidence_refs=("event://1",),
        )

        self.assertEqual(completed.status, SkillInvocationStatus.COMPLETED)
        self.assertFalse(runtime.allowed_tools_policy.active_snapshots("s1"))
        self.assertFalse(runtime.hook_runtime.active_for_session("s1"))
        projection = runtime.outcome_projection(completed.invocation_id)
        self.assertNotIn("body", projection)
        self.assertNotIn("effective_tools", projection)

    def test_forked_invocation_produces_03d_reference_only_handoff(self) -> None:
        runtime = default_skill_runtime(refresh=True)

        plan = runtime.invoke(self._request("verification"))

        self.assertEqual(plan.state.status, SkillInvocationStatus.FORK_PENDING)
        self.assertIsNotNone(plan.fork_request)
        fork = plan.fork_request.to_dict()
        self.assertIn("version_ref", fork)
        self.assertNotIn("body", fork)
        self.assertNotIn("grant", fork)
        self.assertEqual(runtime.fork_queue.snapshot()["owner"], "M1-03D SubagentRuntime (handoff only in 03C foundation)")

    def test_disabled_components_fail_real_invocation(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        runtime.invocation_runtime.disabled = True
        with self.assertRaises(SkillRuntimeDisabled):
            runtime.invoke(self._request("codebase-analysis"))

        runtime = default_skill_runtime(refresh=True)
        runtime.registry.disabled = True
        with self.assertRaises(SkillRegistryDisabled):
            runtime.invoke(self._request("codebase-analysis"))

        runtime = default_skill_runtime(refresh=True)
        runtime.body_loader.disabled = True
        with self.assertRaises(SkillBodyLoaderDisabled):
            runtime.invoke(self._request("codebase-analysis"))

        runtime = default_skill_runtime(refresh=True)
        runtime.allowed_tools_policy.disabled = True
        with self.assertRaises(Exception) as captured:
            runtime.invoke(self._request("codebase-analysis"))
        self.assertEqual(getattr(captured.exception, "code", ""), "skill_allowed_tools_policy_disabled")

    def test_state_snapshot_restores_without_body_or_callbacks(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        plan = runtime.invoke(self._request("codebase-analysis"))
        snapshot = runtime.state_snapshot()
        rendered = str(snapshot)

        self.assertNotIn("Base directory for this skill", rendered)
        self.assertFalse(snapshot["callbacks_in_checkpoint"])
        self.assertNotIn("callback", str(snapshot["states"]))
        restored = SkillInvocationStateStore()
        restored.restore(snapshot, session_id="s1")
        self.assertEqual(restored.get(plan.state.invocation_id).version_ref.content_digest, plan.state.version_ref.content_digest)

        checkpoint = runtime.session_bridge.checkpoint(
            session_id="s1",
            agent_id="CodeWorkerRuntime",
            runtime_state_snapshot=snapshot,
        ).to_dict()
        fresh = default_skill_runtime(refresh=True)
        fresh.session_bridge.restore(checkpoint, session_id="s1")
        self.assertEqual(
            fresh.state_store.get(plan.state.invocation_id).version_ref.content_digest,
            plan.state.version_ref.content_digest,
        )

    def test_restored_policy_reenters_real_03a_gate_with_namespace_intact(self) -> None:
        from zyra_runtime.permission.evaluator import PermissionPolicyEvaluator
        from zyra_runtime.permission.hooks import PermissionHookAdapter, PermissionHookEffect
        from zyra_runtime.permission.modes import ModeName, PermissionModeRuntime
        from zyra_runtime.permission.models import PermissionEvaluationRequest, ToolIdentity as PermissionToolIdentity

        original = default_skill_runtime(refresh=True)
        plan = original.invoke(self._request("codebase-analysis"))
        restored = SkillRuntime(original.config, state_snapshot=original.state_snapshot())
        restored.bootstrap()
        self.assertTrue(restored.allowed_tools_policy.active_snapshots("s1"))
        adapter = PermissionHookAdapter()
        restored.install_permission_hook(adapter)
        evaluator = PermissionPolicyEvaluator(
            mode_runtime=PermissionModeRuntime(ModeName.DEFAULT),
            hook_adapter=adapter,
        )

        trace = evaluator.evaluate(
            PermissionEvaluationRequest(
                run_id="r1",
                task_id="t1",
                session_id="s1",
                tool_use_id="mcp-call-1",
                tool_identity=PermissionToolIdentity(
                    namespace="mcp",
                    name="file_read",
                    server_id="untrusted-server",
                ),
                arguments={"path": "secret.txt"},
                metadata={"skill_invocation_id": plan.state.invocation_id},
            )
        )

        self.assertEqual(trace.pre_tool_hook.effect, PermissionHookEffect.DENY)
        self.assertEqual(str(trace.effect), "deny")

    def test_active_declarative_hook_leases_are_rebuilt_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            builtin = project / "skills" / "builtin"
            _write_skill(
                builtin,
                "hooked-context",
                hooks='[{"id":"pre-audit","event":"pre_tool","lifetime":"invocation","action":"emit_event"}]',
            )
            runtime = SkillRuntime(
                SkillRuntimeConfig(
                    project_root=str(project),
                    workspace_root=str(project),
                    builtin_root=str(builtin),
                )
            )
            runtime.bootstrap()
            plan = runtime.invoke(self._request("hooked-context"))
            original_ids = plan.state.hook_lease_ids
            self.assertTrue(original_ids)

            restored = SkillRuntime(runtime.config, state_snapshot=runtime.state_snapshot())
            restored.bootstrap()
            restored_state = restored.state_store.get(plan.state.invocation_id)

            self.assertTrue(restored.hook_runtime.active_for_session("s1"))
            self.assertTrue(restored_state.hook_lease_ids)
            self.assertNotEqual(restored_state.hook_lease_ids, original_ids)
            restored.invocation_runtime.complete(restored_state.invocation_id)
            self.assertFalse(restored.hook_runtime.active_for_session("s1"))

    def test_oversized_arguments_fail_closed_and_release_budget(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        request = self._request(
            "codebase-analysis",
            arguments={"payload": "x" * 40_000},
        )

        with self.assertRaises(SkillBudgetExceeded):
            runtime.invoke(request)

        failed = runtime.state_store.get(request.invocation_id)
        budget = runtime.invocation_runtime.budget_runtime.state(request.invocation_id)
        self.assertTrue(failed.status.terminal)
        self.assertTrue(budget.closed)
        self.assertEqual(budget.consumed_tokens, 0)
        self.assertFalse(runtime.allowed_tools_policy.active_snapshots("s1"))

    def test_compact_restore_uses_exact_digest_and_revocation_blocks(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        plan = runtime.invoke(self._request("codebase-analysis"))
        references = runtime.compact_bridge.references(
            (plan.state,),
            session_id="s1",
            agent_id="CodeWorkerRuntime",
        )
        restored = runtime.compact_bridge.restore(
            references,
            session_id="s1",
            agent_id="CodeWorkerRuntime",
        )
        self.assertEqual(restored[0].reference.version_ref.content_digest, plan.state.version_ref.content_digest)
        self.assertNotEqual(restored[0].restored_policy.snapshot_id, plan.policy_snapshot.snapshot_id)
        denied_after_restore = runtime.allowed_tools_policy.decide(
            session_id="s1",
            invocation_id=plan.state.invocation_id,
            tool=ToolUseIdentity(namespace="builtin", name="file_write"),
        )
        self.assertFalse(denied_after_restore.allowed_by_ceiling)

        runtime.registry.revision_store.revoke(plan.revision.version_ref, reason="test revocation")
        with self.assertRaises(Exception) as captured:
            runtime.compact_bridge.restore(
                references,
                session_id="s1",
                agent_id="CodeWorkerRuntime",
            )
        self.assertEqual(getattr(captured.exception, "code", ""), "skill_revoked")

    def test_02d_compact_runtime_consumes_structured_03c_reference(self) -> None:
        from zyra_runtime.compact_restore_runtime import CompactRestoreRuntime

        runtime = default_skill_runtime(refresh=True)
        plan = runtime.invoke(self._request("codebase-analysis"))
        references = runtime.compact_bridge.references(
            (plan.state,),
            session_id="s1",
            agent_id="CodeWorkerRuntime",
        )

        segments = CompactRestoreRuntime(
            skill_restore_resolver=runtime.restore_compact_references,
        )._structured_skill_restore_segments(
            [references[0].to_dict()]
        )

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].metadata["immutable_revision_verified"], "true")
        self.assertEqual(segments[0].metadata["permission_authority"], "M1-03A.ToolPermissionRuntime")
        self.assertEqual(segments[0].metadata["allowed_tools_restore_grant"], "false")
        self.assertIn("evidence-backed codebase map", segments[0].content)
        with self.assertRaises(RuntimeError):
            CompactRestoreRuntime()._structured_skill_restore_segments(
                [references[0].to_dict()]
            )

    def test_project_skill_compact_restore_remains_untrusted(self) -> None:
        from zyra_runtime.compact_restore_runtime import CompactRestoreRuntime

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            root = project / ".zyra" / "skills"
            _write_skill(root, "project-context", allowed_tools='["builtin/file_read"]')
            runtime = SkillRuntime(
                SkillRuntimeConfig(
                    project_root=str(project),
                    workspace_root=str(project),
                    builtin_root=str(project / "missing-builtins"),
                    project_roots=(str(root),),
                )
            )
            runtime.bootstrap()
            revision = runtime.registry.resolve("project-context")
            policy = runtime.allowed_tools_policy.bind(
                invocation_id="project-invocation",
                session_id="s1",
                revision=revision,
            )
            state = InvokedSkillState(
                invocation_id="project-invocation",
                run_id="r1",
                task_id="t1",
                session_id="s1",
                agent_id="CodeWorkerRuntime",
                version_ref=revision.version_ref,
                status=SkillInvocationStatus.INLINE_ACTIVE,
                policy_snapshot=policy,
            )
            reference = runtime.compact_bridge.references(
                (state,),
                session_id="s1",
                agent_id="CodeWorkerRuntime",
            )[0]

            segment = CompactRestoreRuntime(
                skill_restore_resolver=runtime.restore_compact_references,
            )._structured_skill_restore_segments([reference.to_dict()])[0]

            self.assertEqual(segment.metadata["skill_source_kind"], "project")
            self.assertEqual(segment.metadata["trust_level"], "untrusted")
            self.assertEqual(segment.metadata["prompt_injection_guard"], "required")


class SearchUpdateAuditTests(unittest.TestCase):
    def test_safe_property_classifier_never_grants_downstream_tools(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        revision = runtime.registry.resolve("verification")
        request = SkillInvocationRequest(
            run_id="run-safe",
            task_id="task-safe",
            session_id="session-safe",
            agent_id="CodeWorkerRuntime",
            skill_name="verification",
            arguments={"scope": ["runtime", "permission"]},
            interactive=True,
            headless=False,
        )

        verdict = SkillCommandSafetyClassifier().classify(
            request,
            revision,
            active_ref=revision.version_ref.immutable_ref,
        )

        self.assertEqual(verdict.safety, SkillCommandSafety.SAFE_CONTEXT_EXPANSION)
        self.assertTrue(verdict.bootstrap_allow)
        self.assertFalse(verdict.downstream_tools_authorized)
        invalid = SkillCommandSafetyClassifier().classify(
            request,
            revision,
            requested_resources=("references/not-declared.md",),
            active_ref=revision.version_ref.immutable_ref,
        )
        self.assertEqual(invalid.safety, SkillCommandSafety.INVALID)
        self.assertFalse(invalid.bootstrap_allow)

    def test_change_detector_drives_validated_atomic_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            builtin = project / "skills" / "builtin"
            skill_file = _write_skill(builtin, "reloadable", body="first stable body") / "SKILL.md"
            runtime = SkillRuntime(
                SkillRuntimeConfig(
                    project_root=str(project),
                    workspace_root=str(project),
                    builtin_root=str(builtin),
                )
            )
            runtime.bootstrap()
            first_ref = runtime.registry.resolve("reloadable").version_ref.immutable_ref
            skill_file.write_text(
                _skill_text("reloadable", body="second stable body with changed digest"),
                encoding="utf-8",
            )

            unsettled = runtime.reload_if_changed(now=10.0)
            reloaded = runtime.reload_if_changed(now=11.0)

            self.assertTrue(unsettled.changes.changed)
            self.assertFalse(unsettled.changes.stable)
            self.assertIsNone(unsettled.reload)
            self.assertTrue(reloaded.changes.reload_required)
            self.assertTrue(reloaded.reload.registry.applied)
            self.assertNotEqual(
                runtime.registry.resolve("reloadable").version_ref.immutable_ref,
                first_ref,
            )

    def test_change_detector_hashes_content_when_stat_identity_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_file = _write_skill(root, "digest-change", body="first-body") / "SKILL.md"
            detector = SkillChangeDetector((root,), stable_window_seconds=0)
            detector.capture()
            before = skill_file.stat()
            original = skill_file.read_text(encoding="utf-8")
            changed = original.replace("first-body", "other-body")
            self.assertEqual(len(changed), len(original))
            skill_file.write_text(changed, encoding="utf-8")
            os.utime(skill_file, ns=(before.st_atime_ns, before.st_mtime_ns))

            change = detector.poll(now=1.0)

            self.assertTrue(change.changed)
            self.assertTrue(change.stable)
            self.assertEqual(change.changes[0].kind.value, "modified")

    def test_context_budget_enforces_stage_total_release_and_restore(self) -> None:
        runtime = SkillContextBudgetRuntime()
        budget = SkillContextBudget(
            body_tokens=200,
            resource_read_tokens=100,
            restore_tokens=64,
            invocation_total_tokens=300,
        )
        runtime.open(invocation_id="inv-budget", session_id="s1", budget=budget)
        body = runtime.reserve(
            invocation_id="inv-budget",
            stage=SkillBudgetStage.BODY,
            tokens=190,
            source_ref="builtin:test/SKILL.md",
            reason="body disclosure",
        )
        resource = runtime.reserve(
            invocation_id="inv-budget",
            stage=SkillBudgetStage.RESOURCE,
            tokens=100,
            source_ref="builtin:test/references/guide.md",
            reason="resource disclosure",
        )
        with self.assertRaises(SkillBudgetExceeded):
            runtime.reserve(
                invocation_id="inv-budget",
                stage=SkillBudgetStage.RESTORE,
                tokens=11,
                source_ref="compact:test",
                reason="total ceiling",
            )

        runtime.release(resource.allocation_id, reason="resource evicted")
        runtime.reserve(
            invocation_id="inv-budget",
            stage=SkillBudgetStage.RESTORE,
            tokens=64,
            source_ref="compact:test",
            reason="restore",
        )
        snapshot = runtime.snapshot()
        restored = SkillContextBudgetRuntime()
        restored.restore(snapshot)

        self.assertEqual(restored.state("inv-budget").consumed_tokens, 254)
        self.assertEqual(restored.state("inv-budget").peak_tokens, 290)
        self.assertTrue(any(item.allocation_id == body.allocation_id for item in restored.allocations("inv-budget")))
        closed = restored.close("inv-budget", reason="invocation terminal")
        self.assertTrue(closed.closed)
        self.assertEqual(closed.consumed_tokens, 0)

    def test_local_search_uses_metadata_only(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        before = runtime.body_loader.body_load_count
        index = SkillSearchIndex(runtime.registry)

        result = index.search("verify implementation evidence")

        self.assertTrue(result.local_only)
        self.assertEqual(result.remote_search_status, "deferred_upstream_stub")
        self.assertTrue(any(hit.name == "verification" for hit in result.hits))
        self.assertEqual(runtime.body_loader.body_load_count, before)

    def test_conditional_activation_uses_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_skill(root, "python-check", paths='["src/**/*.py"]')
            registry = _registry_for(root)
            activator = ConditionalSkillActivator(registry)

            none = activator.activate(session_id="s1", agent_id="a1", changed_paths=("README.md",))
            matched = activator.activate(session_id="s1", agent_id="a1", changed_paths=("src/pkg/main.py",))

            self.assertEqual(none, ())
            self.assertIn("project:python-check", matched)

    def test_atomic_local_update_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_v1 = root / "source-v1"
            source_v2 = root / "source-v2"
            target = root / "target"
            _write_skill(source_v1, "updated-skill", body="version one")
            _write_skill(source_v2, "updated-skill", body="version two")
            updater = AtomicSkillPackageUpdater(
                staging_root=root / "staging",
                backup_root=root / "backups",
            )
            first = updater.install(
                source_root=source_v1,
                target_root=target,
                source_kind=SkillSourceKind.PROJECT,
                source_id="project:update",
                namespace="project",
            )
            second = updater.install(
                source_root=source_v2,
                target_root=target,
                source_kind=SkillSourceKind.PROJECT,
                source_id="project:update",
                namespace="project",
            )

            self.assertNotEqual(first.installed_refs, second.installed_refs)
            updater.rollback(second.update_id)
            self.assertIn("version one", (target / "updated-skill" / "SKILL.md").read_text(encoding="utf-8"))

    def test_runtime_health_and_source_audit_pass(self) -> None:
        runtime = default_skill_runtime(refresh=True)
        health = SkillRuntimeHealthProbe().probe(runtime)
        audit = SkillRuntimeAuditor(ROOT).audit(runtime)

        self.assertTrue(health.ok, health.to_dict())
        self.assertTrue(audit.ok, audit.to_dict())
        self.assertGreaterEqual(len(audit.source_decisions), 10)
        self.assertEqual(audit.state_custody["permission decisions/grants"], "M1-03A ToolPermissionRuntime")


if __name__ == "__main__":
    unittest.main()
