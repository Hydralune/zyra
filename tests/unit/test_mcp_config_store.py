from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.config import (  # noqa: E402
    McpConfigStore,
    McpConfigValidationError,
    McpEnterpriseExclusiveError,
    McpEnvironmentExpansionError,
    McpSourceGenerationConflict,
    McpSuppressionReason,
    expand_env_string,
    mcp_server_signature,
    unwrap_remote_url,
)
from zyra_integrations.mcp.events import (  # noqa: E402
    McpEventEnvelope,
    McpEventProjector,
    McpRuntimeEventKind,
    sanitize_mcp_payload,
)
from zyra_integrations.mcp.store import (  # noqa: E402
    McpRuntimeStateStore,
    McpStateConflict,
    McpStateCorrupt,
    McpStateSerializationError,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += timedelta(seconds=seconds)


class McpRuntimeStateStoreTests(unittest.TestCase):
    def test_atomic_revision_restart_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp" / "state.json"
            first = McpRuntimeStateStore(path)
            self.assertEqual(first.read_state()["revision"], 0)
            state = first.set("feature", {"enabled": True})
            self.assertEqual(state["revision"], 1)
            self.assertTrue(path.is_file())
            self.assertFalse(first.lock_path.exists())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

            second = McpRuntimeStateStore(path)
            self.assertEqual(second.get("feature"), {"enabled": True})
            integrity = second.verify_integrity()
            self.assertTrue(integrity["valid"])
            self.assertEqual(integrity["revision"], 1)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema"], "zyra.mcp-runtime-state")
            self.assertTrue(str(persisted["checksum"]).startswith("sha256:"))

    def test_cross_instance_compare_and_swap_rejects_stale_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = McpRuntimeStateStore(path)
            second = McpRuntimeStateStore(path)
            first.set("one", 1, expected_revision=0)
            with self.assertRaises(McpStateConflict):
                second.set("two", 2, expected_revision=0)
            second.set("two", 2, expected_revision=1)
            self.assertEqual(first.read_state()["runtime_values"], {"one": 1, "two": 2})
            self.assertEqual(first.revision, 2)

    def test_concurrent_store_instances_do_not_lose_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            workers = 8
            writes_per_worker = 15

            def increment(_: int) -> None:
                store = McpRuntimeStateStore(path)
                for _index in range(writes_per_worker):
                    def mutate(state: dict[str, object]) -> None:
                        values = state["runtime_values"]
                        assert isinstance(values, dict)
                        values["counter"] = int(values.get("counter", 0)) + 1

                    store.mutate(mutate, journal=False)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(increment, range(workers)))
            final = McpRuntimeStateStore(path).read_state()
            self.assertEqual(final["runtime_values"]["counter"], workers * writes_per_worker)
            self.assertEqual(final["revision"], workers * writes_per_worker)

    def test_live_connected_state_restores_as_reconnecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            owner = McpRuntimeStateStore(path)
            owner.set_connection(
                "server-a",
                {"server_id": "server-a", "state": "connected", "generation": 3, "healthy": True},
            )
            self.assertEqual(owner.get_connection("server-a")["state"], "connected")

            restarted = McpRuntimeStateStore(path)
            restored = restarted.get_connection("server-a")
            self.assertEqual(restored["state"], "reconnecting")
            self.assertFalse(restored["healthy"])
            self.assertEqual(restored["restore_reason"], "live_transport_not_restorable")
            # Reading a restored view does not corrupt the atomically persisted snapshot.
            self.assertEqual(owner.raw_persisted_state()["connections"]["server-a"]["state"], "connected")

    def test_raw_auth_material_is_rejected_but_opaque_reference_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = McpRuntimeStateStore(path)
            with self.assertRaises(McpStateSerializationError):
                store.put_auth_record(
                    "server-a",
                    {"state": "authenticated", "access_token": "raw-super-secret-token"},
                )
            self.assertFalse(path.exists())

            store.put_auth_record(
                "server-a",
                {
                    "state": "authenticated",
                    "credential_ref": "vault://mcp/server-a/current",
                    "access_token_present": True,
                },
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("raw-super-secret-token", text)
            self.assertIn("vault://mcp/server-a/current", text)

    def test_journal_and_event_payloads_are_recursively_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = McpRuntimeStateStore(path)
            store.append_journal(
                McpRuntimeEventKind.AUTH_REFRESHED,
                {
                    "access_token": "journal-secret",
                    "nested": {
                        "Authorization": "Bearer auth-secret",
                        "url": "https://example.test/mcp?token=query-secret&view=ok",
                    },
                    "env": {"SAFE": "also-secret"},
                },
            )
            text = path.read_text(encoding="utf-8")
            for secret in ("journal-secret", "auth-secret", "query-secret", "also-secret"):
                self.assertNotIn(secret, text)
            entry = store.read_state()["journal"][-1]
            self.assertEqual(entry["payload"]["access_token"], "[REDACTED]")
            self.assertEqual(entry["payload"]["nested"]["Authorization"], "[REDACTED]")
            self.assertIn("token=%5BREDACTED%5D", entry["payload"]["nested"]["url"])

            safe = sanitize_mcp_payload({"password": "pw", "credential_ref": "vault://safe"})
            self.assertEqual(safe["password"], "[REDACTED]")
            self.assertEqual(safe["credential_ref"], "vault://safe")

    def test_snapshot_checksum_detects_tampering_and_restore_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = McpRuntimeStateStore(path)
            store.set_connection("server-a", {"server_id": "server-a", "state": "connected"})
            snapshot = store.snapshot()
            snapshot["payload"]["connections"]["server-a"]["generation"] = 999
            with self.assertRaises(McpStateCorrupt):
                McpRuntimeStateStore(Path(directory) / "restored.json").restore_snapshot(snapshot)

    def test_projected_event_contains_causality_and_no_secrets(self) -> None:
        projector = McpEventProjector()
        event = projector.event(
            McpEventEnvelope(
                kind=McpRuntimeEventKind.TOOL_CALL_COMPLETED,
                server_id="server-a",
                session_id="session-a",
                worker_request_id="worker-request-a",
                tool_call_id="tool-call-a",
                cause_event_id="event-cause",
                connection_generation=4,
                payload={"result": "ok", "refresh_token": "do-not-log"},
            ),
            run_id="run-a",
            task_id="task-a",
        )
        body = event.payload["query_session"]["mcp_runtime"]
        self.assertEqual(body["tool_call_id"], "tool-call-a")
        self.assertEqual(body["cause_event_id"], "event-cause")
        self.assertEqual(body["connection_generation"], 4)
        self.assertEqual(body["payload"]["refresh_token"], "[REDACTED]")
        self.assertNotIn("do-not-log", json.dumps(event.payload))


class McpConfigStoreTests(unittest.TestCase):
    def _store(self, directory: str, *, environ: dict[str, str] | None = None, clock=None) -> McpConfigStore:
        state_store = McpRuntimeStateStore(Path(directory) / "state.json", clock=clock)
        return McpConfigStore(state_store, environ=environ or {}, clock=clock)

    def test_stdio_and_unwrapped_remote_signatures(self) -> None:
        stdio = {"type": "stdio", "command": "python", "args": ["-m", "server"]}
        self.assertEqual(mcp_server_signature(stdio), 'stdio:["python","-m","server"]')
        self.assertNotEqual(
            mcp_server_signature(stdio),
            mcp_server_signature({"type": "stdio", "command": "python", "args": ["server", "-m"]}),
        )
        original = "https://vendor.example/mcp"
        wrapped = (
            "https://proxy.example/v2/session_ingress/shttp/mcp/session?"
            f"mcp_url={quote(original, safe='')}"
        )
        self.assertEqual(unwrap_remote_url(wrapped), original)
        self.assertEqual(
            mcp_server_signature({"type": "http", "url": wrapped}),
            mcp_server_signature({"type": "http", "url": original}),
        )

    def test_manual_precedence_and_content_dedupe_preserve_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            shared = {"type": "stdio", "command": "python", "args": ["-m", "shared"]}
            config.replace_source(
                "claudeai:connectors",
                {"same-name": {"type": "http", "url": "https://cloud.example/mcp"}},
                scope="claudeai",
            )
            config.replace_source(
                "plugin:alpha",
                {"plugin-copy": shared},
                scope="plugin",
            )
            config.replace_source(
                "manual:user",
                {
                    "same-name": {"type": "http", "url": "https://manual.example/mcp"},
                    "manual-copy": shared,
                },
                scope="user",
            )
            result = config.resolve()
            self.assertEqual(result.servers["same-name"].provenance.source_id, "manual:user")
            self.assertIn("manual-copy", result.servers)
            self.assertNotIn("plugin-copy", result.servers)
            duplicate = result.all_servers["plugin-copy"]
            self.assertEqual(duplicate.suppression_reason, McpSuppressionReason.DUPLICATE_SIGNATURE)
            self.assertEqual(duplicate.duplicate_of, "manual-copy")
            self.assertEqual(
                result.all_servers["same-name"].shadowed_provenance[0].source_id,
                "claudeai:connectors",
            )

    def test_project_server_requires_approval_and_config_change_invalidates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "project:repo",
                {"project-server": {"type": "stdio", "command": "one", "args": []}},
                scope="project",
            )
            self.assertEqual(config.resolve().pending, ("project-server",))
            self.assertEqual(config.effective_servers(), {})
            config.approve_project_server("project-server", source_id="project:repo", actor="tester")
            self.assertIn("project-server", config.effective_servers())

            # A metadata-only refresh preserves signature-bound approval.
            config.replace_source(
                "project:repo",
                {
                    "project-server": {
                        "type": "stdio",
                        "command": "one",
                        "args": [],
                        "metadata": {"label": "refreshed"},
                    }
                },
                scope="project",
            )
            self.assertIn("project-server", config.effective_servers())

            # Changing command+args changes the approval signature.
            config.replace_source(
                "project:repo",
                {"project-server": {"type": "stdio", "command": "two", "args": []}},
                scope="project",
            )
            self.assertEqual(config.resolve().pending, ("project-server",))
            config.reject_project_server("project-server", source_id="project:repo", reason="untrusted")
            self.assertEqual(config.resolve().rejected, ("project-server",))

    def test_enterprise_source_is_exclusive_and_blocks_manual_add(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "manual:user",
                {"user-server": {"type": "stdio", "command": "user", "args": []}},
                scope="user",
            )
            config.replace_source(
                "enterprise:managed",
                {"managed-server": {"type": "stdio", "command": "managed", "args": []}},
                scope="enterprise",
            )
            result = config.resolve()
            self.assertTrue(result.enterprise_exclusive)
            self.assertEqual(set(result.servers), {"managed-server"})
            self.assertEqual(
                result.all_servers["user-server"].suppression_reason,
                McpSuppressionReason.ENTERPRISE_EXCLUSIVE,
            )
            with self.assertRaises(McpEnterpriseExclusiveError):
                config.add_server("another", {"type": "stdio", "command": "other", "args": []})
            config.remove_source("enterprise:managed")
            self.assertEqual(set(config.effective_servers()), {"user-server"})

    def test_denylist_wins_and_allowlist_preserves_transport_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "manual:user",
                {
                    "stdio-ok": {"type": "stdio", "command": "python", "args": ["-m", "ok"]},
                    "stdio-no": {"type": "stdio", "command": "python", "args": ["-m", "no"]},
                    "remote-ok": {"type": "http", "url": "https://allowed.example/mcp"},
                },
                scope="user",
            )
            config.set_policy(
                allowed_mcp_servers=[
                    {"serverCommand": ["python", "-m", "ok"]},
                    {"serverUrl": "https://allowed.example/*"},
                ],
                denied_mcp_servers=[{"serverName": "remote-ok"}],
                provenance=["enterprise-policy"],
            )
            result = config.resolve()
            self.assertEqual(set(result.servers), {"stdio-ok"})
            self.assertIn("stdio-no", result.policy_blocked)
            self.assertIn("remote-ok", result.policy_blocked)
            self.assertEqual(result.all_servers["remote-ok"].policy.reason, "denylist_match")

            config.set_policy(allowed_mcp_servers=[])
            self.assertEqual(config.effective_servers(), {})

    def test_disabled_manual_duplicate_does_not_suppress_enabled_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            shared = {"type": "stdio", "command": "python", "args": ["-m", "same"]}
            config.replace_source("plugin:a", {"plugin-server": shared}, scope="plugin")
            config.replace_source(
                "manual:user",
                {"manual-server": {**shared, "disabled": True}},
                scope="user",
            )
            result = config.resolve()
            self.assertIn("plugin-server", result.servers)
            self.assertNotIn("manual-server", result.servers)
            self.assertEqual(
                result.all_servers["manual-server"].suppression_reason,
                McpSuppressionReason.DISABLED,
            )

    def test_environment_templates_expand_only_in_memory_and_never_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_store = McpRuntimeStateStore(state_path)
            config = McpConfigStore(
                state_store,
                environ={"API_TOKEN": "runtime-secret-value", "HOST": "runtime.example"},
            )
            config.replace_source(
                "manual:user",
                {
                    "templated": {
                        "type": "stdio",
                        "command": "runner",
                        "args": ["--host=${HOST:-localhost}"],
                        "env": {"API_TOKEN": "${API_TOKEN}"},
                    }
                },
                scope="user",
            )
            materialized = config.materialize_server("templated")
            self.assertTrue(materialized.ready)
            self.assertEqual(materialized.config.args, ("--host=runtime.example",))
            self.assertEqual(materialized.config.env["API_TOKEN"], "runtime-secret-value")
            persisted = state_path.read_text(encoding="utf-8")
            self.assertIn("${API_TOKEN}", persisted)
            self.assertNotIn("runtime-secret-value", persisted)
            self.assertNotIn("runtime-secret-value", json.dumps(config.public_snapshot()))

            missing = McpConfigStore(state_store, environ={"HOST": "runtime.example"})
            with self.assertRaises(McpEnvironmentExpansionError):
                missing.materialize_server("templated")
            with self.assertRaises(McpConfigValidationError):
                config.replace_source(
                    "manual:bad",
                    {"bad": {"type": "stdio", "command": "x", "args": [], "env": {"API_TOKEN": "literal"}}},
                    scope="user",
                )
            with self.assertRaises(McpConfigValidationError):
                config.replace_source(
                    "manual:bad-default",
                    {"bad": {"type": "stdio", "command": "x", "args": [], "env": {"API_TOKEN": "${API_TOKEN:-fallback-secret}"}}},
                    scope="user",
                )

    def test_expand_env_default_escape_and_missing_contract(self) -> None:
        result = expand_env_string(r"${HOST:-localhost}:\${LITERAL}:${MISSING}", {})
        self.assertEqual(result.value, "localhost:${LITERAL}:${MISSING}")
        self.assertEqual(result.defaulted_variables, ("HOST",))
        self.assertEqual(result.missing_variables, ("MISSING",))
        self.assertEqual(result.referenced_variables, ("HOST", "MISSING"))

    def test_stale_cleanup_removes_orphan_runtime_records_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock(datetime(2026, 7, 11, tzinfo=timezone.utc))
            state_store = McpRuntimeStateStore(Path(directory) / "state.json", clock=clock)
            config = McpConfigStore(state_store, environ={}, clock=clock)
            plugin_source = config.replace_source(
                "plugin:stale",
                {"plugin-server": {"type": "stdio", "command": "plugin", "args": []}},
                scope="plugin",
                stale_after_seconds=5,
            )
            config.replace_source(
                "manual:user",
                {"user-server": {"type": "stdio", "command": "user", "args": []}},
                scope="user",
                stale_after_seconds=1,
            )
            plugin_id = plugin_source["servers"]["plugin-server"]["server_id"]
            state_store.set_connection(plugin_id, {"server_id": plugin_id, "state": "failed"})
            state_store.put_auth_record(plugin_id, {"state": "needs_auth", "credential_ref": "vault://plugin"})
            state_store.update_section("catalogs", {plugin_id: {"generation": 1, "tools": []}})
            clock.advance(6)
            result = config.cleanup_stale_sources()
            self.assertEqual(result.removed_source_ids, ("plugin:stale",))
            state = state_store.read_state()
            self.assertNotIn(plugin_id, state["connections"])
            self.assertNotIn(plugin_id, state["auth"])
            self.assertNotIn(plugin_id, state["catalogs"])
            self.assertIn("manual:user", state["config_sources"])

    def test_source_generation_rejects_stale_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "plugin:one",
                {"server": {"type": "stdio", "command": "one", "args": []}},
                scope="plugin",
                generation=4,
            )
            with self.assertRaises(McpSourceGenerationConflict):
                config.replace_source(
                    "plugin:one",
                    {"server": {"type": "stdio", "command": "stale", "args": []}},
                    scope="plugin",
                    generation=3,
                )

    def test_tool_allow_deny_filters_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "manual:user",
                {
                    "tools": {
                        "type": "stdio",
                        "command": "tools",
                        "args": [],
                        "allowed_tools": ["read_*", "status"],
                        "denied_tools": ["read_secret"],
                    }
                },
                scope="user",
            )
            self.assertTrue(config.tool_allowed("tools", "read_file").allowed)
            self.assertFalse(config.tool_allowed("tools", "write_file").allowed)
            self.assertFalse(config.tool_allowed("tools", "read_secret").allowed)
            config.set_policy(denied_tools={"tools": ["status"]})
            self.assertFalse(config.tool_allowed("tools", "status").allowed)

    def test_tool_policy_resolves_generated_and_explicit_canonical_server_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "manual:identity",
                {
                    "generated-tools": {
                        "type": "stdio",
                        "command": "generated-tools-command",
                        "args": [],
                        "allowed_tools": ["read_*", "status"],
                        "denied_tools": ["read_secret"],
                    },
                    "friendly-explicit-name": {
                        "server_id": "canonical-explicit-id",
                        "type": "stdio",
                        "command": "explicit-tools-command",
                        "args": [],
                        "allowed_tools": ["inspect"],
                    },
                },
                scope="user",
            )

            generated = config.get_server("generated-tools", include_inactive=True)
            explicit = config.get_server("friendly-explicit-name", include_inactive=True)
            self.assertIsNotNone(generated)
            self.assertIsNotNone(explicit)
            assert generated is not None and explicit is not None
            self.assertNotEqual(generated.server_id, generated.name)
            self.assertTrue(generated.server_id.startswith("mcp-generated-tools-"))
            self.assertEqual(explicit.server_id, "canonical-explicit-id")

            # worker_projection asks by canonical runtime identity.  Both the
            # generated and explicitly different IDs must reach the config's
            # name-keyed policy instead of being silently omitted.
            self.assertTrue(config.tool_allowed(generated.server_id, "read_file").allowed)
            self.assertFalse(config.tool_allowed(generated.server_id, "write_file").allowed)
            self.assertFalse(config.tool_allowed(generated.server_id, "read_secret").allowed)
            self.assertTrue(config.tool_allowed("canonical-explicit-id", "inspect").allowed)
            self.assertFalse(config.tool_allowed("canonical-explicit-id", "mutate").allowed)
            self.assertEqual(
                config.get_server("canonical-explicit-id", include_inactive=True),
                explicit,
            )
            self.assertEqual(
                config.materialize_server("canonical-explicit-id").config.server_id,
                "canonical-explicit-id",
            )

            config.set_policy(denied_tools={"generated-tools": ["status"]})
            denied = config.tool_allowed(generated.server_id, "status")
            self.assertFalse(denied.allowed)
            self.assertEqual(denied.reason, "tool_denylist_match")
            self.assertEqual(config.tool_allowed("missing-server-id", "read_file").reason, "unknown_server")

    def test_duplicate_canonical_server_identity_is_ambiguous_and_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._store(directory)
            config.replace_source(
                "manual:ambiguous-identity",
                {
                    "first": {
                        "server_id": "shared-runtime-id",
                        "type": "stdio",
                        "command": "first-command",
                        "args": [],
                    },
                    "second": {
                        "server_id": "shared-runtime-id",
                        "type": "stdio",
                        "command": "second-command",
                        "args": [],
                    },
                },
                scope="user",
            )

            decision = config.tool_allowed("shared-runtime-id", "anything")
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "ambiguous_server_identity")
            self.assertIsNone(config.get_server("shared-runtime-id", include_inactive=True))

    def test_concurrent_source_writers_merge_without_lost_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            count = 12

            def write_source(index: int) -> None:
                config = McpConfigStore(McpRuntimeStateStore(path), environ={})
                config.replace_source(
                    f"plugin:{index}",
                    {f"server-{index}": {"type": "stdio", "command": f"command-{index}", "args": []}},
                    scope="plugin",
                )

            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(write_source, range(count)))
            state = McpRuntimeStateStore(path).read_state()
            self.assertEqual(len(state["config_sources"]), count)
            self.assertEqual(
                set(McpConfigStore(McpRuntimeStateStore(path), environ={}).effective_servers()),
                {f"server-{index}" for index in range(count)},
            )


if __name__ == "__main__":
    unittest.main()
