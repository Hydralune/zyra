from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "apps" / "api",
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_api.mcp_api import (  # noqa: E402
    McpApiFacade,
    McpMutationAuthorization,
    McpMutationPolicy,
    McpMutationRequest,
    handle_get,
    handle_post,
)
from zyra_integrations.mcp.config import McpConfigNotFound  # noqa: E402
from zyra_integrations.mcp.runtime import McpClientRuntimeDisabled  # noqa: E402
from zyra_integrations.mcp.store import McpStateConflict  # noqa: E402


@dataclass
class SafeValue:
    value: dict[str, Any]

    def safe_dict(self) -> dict[str, Any]:
        return dict(self.value)


@dataclass
class FakeServer:
    name: str
    server_id: str
    disabled: bool = False
    approval: str = "not_required"
    scope: str = "dynamic"
    source_id: str = "manual:dynamic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server_id": self.server_id,
            "disabled": self.disabled,
            "approval": self.approval,
            "config": {
                "transport": "in_process",
                "env": {"TOKEN": "<redacted>"},
                "headers": {"Authorization": "<redacted>"},
            },
        }


class FakeConfigStore:
    def __init__(self) -> None:
        self.revision = 7
        self.records = {
            "alpha": FakeServer("alpha", "srv-alpha"),
            "beta": FakeServer("beta", "srv-beta", disabled=True),
        }

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "sources": {"manual:dynamic": {"server_names": sorted(self.records)}},
            "secret_values_included": False,
        }

    def list_servers(self, *, include_inactive: bool = False) -> dict[str, FakeServer]:
        if include_inactive:
            return dict(self.records)
        return {name: record for name, record in self.records.items() if not record.disabled}


@dataclass
class FakeConnectionSnapshot:
    server_id: str
    state: str = "connected"

    def to_dict(self) -> dict[str, Any]:
        return {"server_id": self.server_id, "state": self.state, "healthy": self.state == "connected"}


class FakeConnectionRuntime:
    def snapshots(self) -> tuple[FakeConnectionSnapshot, ...]:
        return (FakeConnectionSnapshot("srv-alpha"),)


@dataclass
class FakeCatalogSnapshot:
    server_id: str
    generation: int
    tools: tuple[SafeValue, ...]
    resources: tuple[SafeValue, ...]
    resource_templates: tuple[dict[str, Any], ...]
    prompts: tuple[SafeValue, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "generation": self.generation,
            "tools": [item.safe_dict() for item in self.tools],
            "resources": [item.safe_dict() for item in self.resources],
            "resource_templates": list(self.resource_templates),
            "prompts": [item.safe_dict() for item in self.prompts],
            "instructions": "<present>",
        }


class FakeCatalog:
    def __init__(self) -> None:
        self.snapshots = (
            FakeCatalogSnapshot(
                "srv-alpha",
                3,
                (SafeValue({"server_id": "srv-alpha", "local_name": "mcp__alpha__echo"}),),
                (SafeValue({"server_id": "srv-alpha", "uri": "memo://one"}),),
                ({"uriTemplate": "memo://{id}"},),
                (SafeValue({"server_id": "srv-alpha", "remote_name": "welcome"}),),
            ),
            FakeCatalogSnapshot(
                "srv-beta",
                1,
                (SafeValue({"server_id": "srv-beta", "local_name": "mcp__beta__sum"}),),
                (),
                (),
                (),
            ),
        )

    def list(self) -> tuple[FakeCatalogSnapshot, ...]:
        return self.snapshots

    def safe_dict(self) -> dict[str, Any]:
        return {
            "servers": [snapshot.safe_dict() for snapshot in self.snapshots],
            "server_count": len(self.snapshots),
            "tool_count": sum(len(snapshot.tools) for snapshot in self.snapshots),
        }


class FakeElicitationQueue:
    def __init__(self) -> None:
        self.last_list: dict[str, Any] = {}
        self.records = (
            SafeValue(
                {
                    "request": {
                        "server_id": "srv-alpha",
                        "session_id": "session-1",
                        "request_id": "ask-1",
                    },
                    "status": "pending",
                    "sensitive_fields": ["password"],
                }
            ),
        )

    def list(self, **kwargs: Any) -> tuple[SafeValue, ...]:
        self.last_list = dict(kwargs)
        return self.records


class FakeMcpRuntime:
    def __init__(self) -> None:
        self.config_store = FakeConfigStore()
        self.connection_runtime = FakeConnectionRuntime()
        self.catalog = FakeCatalog()
        self.elicitation_queue = FakeElicitationQueue()
        self.healthy = True
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.failures: dict[str, Exception] = {}

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        error = self.failures.get(name)
        if error is not None:
            raise error
        self.calls.append((name, args, kwargs))

    def diagnostics(self) -> dict[str, Any]:
        self._record("diagnostics")
        return {
            "schema": "zyra.mcp-client-runtime.v1",
            "ok": self.healthy,
            "enabled": True,
            "health": {"connected_count": 1 if self.healthy else 0},
        }

    def add_server(self, name: str, config: dict[str, Any], **kwargs: Any) -> FakeServer:
        self._record("add_server", name, config, **kwargs)
        record = FakeServer(name, f"srv-{name}")
        self.config_store.records[name] = record
        self.config_store.revision += 1
        return record

    def connect_server(self, name: str, **context: Any) -> SafeValue:
        self._record("connect_server", name, **context)
        return SafeValue({"connected": True, "snapshot": {"server_id": "srv-alpha"}})

    def disconnect_server(self, server_id: str, **context: Any) -> FakeConnectionSnapshot:
        self._record("disconnect_server", server_id, **context)
        return FakeConnectionSnapshot(server_id, "disconnected")

    def reconnect_server(self, server_id: str, **context: Any) -> SafeValue:
        self._record("reconnect_server", server_id, **context)
        return SafeValue({"connected": True, "already_connected": False})

    def refresh_server(self, server_id: str, **kwargs: Any) -> SafeValue:
        self._record("refresh_server", server_id, **kwargs)
        return SafeValue({"server_id": server_id, "reason": "manual"})

    def set_server_disabled(self, name: str, disabled: bool, **context: Any) -> dict[str, Any]:
        self._record("set_server_disabled", name, disabled, **context)
        self.config_store.records[name].disabled = disabled
        return {"internal_state": {"access_token": "must-never-leak"}}

    def approve_server(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self._record("approve_server", name, **kwargs)
        self.config_store.records[name].approval = "approved"
        return {"raw_state": {"client_secret": "must-never-leak"}}

    def reject_server(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self._record("reject_server", name, **kwargs)
        self.config_store.records[name].approval = "rejected"
        return {"raw_state": {"client_secret": "must-never-leak"}}

    def read_resource(self, server_id: str, uri: str, **context: Any) -> dict[str, Any]:
        self._record("read_resource", server_id, uri, **context)
        return {"content": [{"type": "text", "text": "safe result"}], "artifact_refs": []}

    def get_prompt(self, server_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._record("get_prompt", server_id, name, arguments)
        return {"description": "Greeting", "messages": [{"role": "user", "content": "Hello"}]}

    def resolve_elicitation(self, **kwargs: Any) -> SafeValue:
        self._record("resolve_elicitation", **kwargs)
        return SafeValue({"record": {"status": "resolved", "request_id": kwargs["request_id"]}})

    def install_auth_tokens(self, server_id: str, tokens: dict[str, Any]) -> dict[str, Any]:
        self._record("install_auth_tokens", server_id, tokens)
        return {"authorized": True, "snapshot": {"status": "authorized", "has_refresh_token": True}}

    def revoke_auth(self, server_id: str) -> dict[str, Any]:
        self._record("revoke_auth", server_id)
        return {"fully_revoked": True, "local_deleted": True}


class McpApiFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeMcpRuntime()
        self.authorized: list[McpMutationRequest] = []

        def authorize(request: McpMutationRequest) -> McpMutationAuthorization:
            self.authorized.append(request)
            index = len(self.authorized)
            return McpMutationAuthorization(
                allowed=True,
                decision_id=f"decision-{index}",
                request_id=f"request-{index}",
                grant_id=f"grant-{index}",
                custody_fingerprint="sha256:test-custody",
                reason_code="test.exact",
                metadata={"grant_consumed_before_mutation": True},
            )

        self.facade = McpApiFacade(  # type: ignore[arg-type]
            self.runtime,
            mutation_authorizer=authorize,
        )

    def test_ignores_non_mcp_paths_and_functional_entry_points_match(self) -> None:
        self.assertIsNone(self.facade.handle_get(["health"], {}))
        self.assertIsNone(self.facade.handle_post(["tasks"], {}, "actor"))
        status, body, _ = handle_get(self.runtime, ["mcp", "config"], {})  # type: ignore[arg-type,misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["config"]["revision"], 7)
        result = handle_post(self.runtime, ["mcp", "servers"], None, "actor")  # type: ignore[arg-type]
        self.assertEqual(result[0], HTTPStatus.BAD_REQUEST)  # type: ignore[index]

    def test_mutation_facade_is_fail_closed_without_permission_authority(self) -> None:
        facade = McpApiFacade(self.runtime)  # type: ignore[arg-type]
        status, body, headers = facade.handle_post(
            ["mcp", "servers"],
            {"name": "blocked", "config": {"transport": "in_process"}},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["error"], "mcp_permission_required")
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertFalse(any(call[0] == "add_server" for call in self.runtime.calls))

        functional = handle_post(
            self.runtime,  # type: ignore[arg-type]
            ["mcp", "servers"],
            {"name": "also-blocked", "config": {"transport": "in_process"}},
            "operator",
        )
        self.assertEqual(functional[0], HTTPStatus.FORBIDDEN)  # type: ignore[index]

    def test_health_is_derived_from_runtime_and_returns_unavailable_when_unhealthy(self) -> None:
        status, body, _ = self.facade.handle_get(["mcp", "health"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(body["ok"])
        self.runtime.healthy = False
        status, body, headers = self.facade.handle_get(["mcp"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertFalse(body["ok"])
        self.assertEqual(body["runtime"]["health"]["connected_count"], 0)
        self.assertEqual(headers["X-Zyra-MCP-State-Owner"], "McpClientRuntime")

    def test_get_config_servers_and_server_detail_are_safe(self) -> None:
        status, config, _ = self.facade.handle_get(["mcp", "config"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(config["config"]["secret_values_included"])

        status, body, _ = self.facade.handle_get(
            ["mcp", "servers"],
            {"include_inactive": ["false"]},
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["servers"][0]["connection"]["state"], "connected")
        serialized = json.dumps(body)
        self.assertNotIn("must-never-leak", serialized)

        status, detail, _ = self.facade.handle_get(["mcp", "servers", "srv-alpha"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(detail["server"]["name"], "alpha")

    def test_get_catalog_tools_resources_prompts_and_elicitations(self) -> None:
        status, catalog, _ = self.facade.handle_get(["mcp", "catalog"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(catalog["catalog"]["tool_count"], 2)

        status, tools, _ = self.facade.handle_get(
            ["mcp", "tools"], {"server_id": "srv-alpha"}
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(tools["count"], 1)
        self.assertEqual(tools["catalog_generations"], {"srv-alpha": 3})

        status, resources, _ = self.facade.handle_get(["mcp", "resources"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(resources["resource_template_count"], 1)

        status, prompts, _ = self.facade.handle_get(["mcp", "prompts"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(prompts["prompts"][0]["remote_name"], "welcome")

        status, elicitations, headers = self.facade.handle_get(
            ["mcp", "elicitations"],
            {"server_id": ["srv-alpha"], "session_id": "session-1", "include_terminal": "true"},
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(elicitations["count"], 1)
        self.assertTrue(self.runtime.elicitation_queue.last_list["include_terminal"])
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")

    def test_add_and_server_actions_use_canonical_identity_and_actor(self) -> None:
        status, created, headers = self.facade.handle_post(
            ["mcp", "servers"],
            {"name": "gamma", "config": {"transport": "in_process"}},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(created["server"]["server_id"], "srv-gamma")
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")

        status, connected, _ = self.facade.handle_post(
            ["mcp", "servers", "srv-alpha", "connect"],
            {"run_id": "run-1", "task_id": "task-1"},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(connected["result"]["connected"])
        connect_call = next(item for item in self.runtime.calls if item[0] == "connect_server")
        self.assertEqual(connect_call[1], ("alpha",))

        status, refreshed, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "refresh"],
            {"refresh_kinds": "tools,prompts"},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        refresh_call = next(item for item in self.runtime.calls if item[0] == "refresh_server")
        self.assertEqual(refresh_call[1], ("srv-alpha",))
        self.assertEqual(refresh_call[2]["refresh_kinds"], ("tools", "prompts"))
        self.assertEqual(refreshed["server_id"], "srv-alpha")

        status, approved, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "approve"],
            {"reason": "reviewed", "actor": "spoofed"},
            "real-actor",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        approve_call = next(item for item in self.runtime.calls if item[0] == "approve_server")
        self.assertEqual(approve_call[2]["actor"], "real-actor")
        self.assertEqual(approved["result"]["server"]["approval"], "approved")
        self.assertEqual(self.authorized[0].action, "mcp.server.add")
        self.assertEqual(self.authorized[0].server_id, "gamma")
        self.assertEqual(self.authorized[1].action, "mcp.server.connect")
        self.assertEqual(self.authorized[1].server_id, "srv-alpha")
        self.assertNotEqual(
            self.authorized[1].arguments,
            self.authorized[2].arguments,
            "different actions must not share an exact permission binding",
        )

    def test_disable_discards_internal_state_and_reject_requires_actor(self) -> None:
        status, body, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "disable"], {"disabled": True}, "actor"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(body["result"]["disabled"])
        self.assertNotIn("must-never-leak", json.dumps(body))

        status, error, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "reject"], {}, ""
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(error["error"], "invalid_request")

    def test_resource_prompt_and_elicitation_operations_forward_validated_values(self) -> None:
        status, resource, _ = self.facade.handle_post(
            ["mcp", "resources", "read"],
            {"server_id": "srv-alpha", "uri": "memo://one", "task_id": "task-1"},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(resource["resource"]["content"][0]["text"], "safe result")

        status, prompt, _ = self.facade.handle_post(
            ["mcp", "prompts", "get"],
            {"server_id": "srv-alpha", "name": "welcome", "arguments": {"name": "Ada"}},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(prompt["prompt"]["description"], "Greeting")

        status, elicitation, _ = self.facade.handle_post(
            ["mcp", "elicitations", "resolve"],
            {
                "server_id": "srv-alpha",
                "session_id": "session-1",
                "request_id": "ask-1",
                "expected_revision": 2,
                "action": "accept",
                "content": {"choice": "yes"},
                "idempotency_key": "resolve-1",
                "run_id": "run-1",
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(elicitation["elicitation"]["record"]["status"], "resolved")
        resolve_call = next(item for item in self.runtime.calls if item[0] == "resolve_elicitation")
        self.assertEqual(resolve_call[2]["actor_id"], "operator")
        self.assertEqual(resolve_call[2]["expected_revision"], 2)

    def test_auth_install_and_revoke_never_reflect_secrets_and_are_no_store(self) -> None:
        secret = "access-token-super-secret"
        refresh = "refresh-token-super-secret"
        status, installed, headers = self.facade.handle_post(
            ["mcp", "auth", "srv-alpha", "install"],
            {"tokens": {"access_token": secret, "refresh_token": refresh, "token_type": "Bearer"}},
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.CREATED)
        serialized = json.dumps(installed)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(refresh, serialized)
        self.assertFalse(installed["secret_values_included"])
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(headers["Pragma"], "no-cache")
        install_call = next(item for item in self.runtime.calls if item[0] == "install_auth_tokens")
        self.assertEqual(install_call[1][0], "alpha")

        status, revoked, headers = self.facade.handle_post(
            ["mcp", "servers", "alpha", "auth", "revoke"], {}, "operator"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(revoked["auth"]["fully_revoked"])
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        revoke_call = next(item for item in self.runtime.calls if item[0] == "revoke_auth")
        self.assertEqual(revoke_call[1][0], "alpha")
        install_permission = next(
            item for item in self.authorized if item.action == "mcp.auth.install"
        )
        self.assertNotIn(secret, json.dumps(install_permission.arguments))
        self.assertNotIn(refresh, json.dumps(install_permission.arguments))
        self.assertTrue(install_permission.arguments["tokens_digest"].startswith("sha256:"))

    def test_deployment_policy_denies_enterprise_stdio_and_unsafe_http(self) -> None:
        self.runtime.config_store.records["enterprise"] = FakeServer(
            "enterprise",
            "srv-enterprise",
            scope="enterprise",
            source_id="enterprise:managed",
        )
        status, body, _ = self.facade.handle_post(
            ["mcp", "servers", "enterprise", "connect"], {}, "operator"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "enterprise_source_read_only")

        status, body, _ = self.facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "shell",
                "config": {"transport": "stdio", "command": "powershell.exe"},
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "stdio_command_not_allowlisted")

        policy = McpMutationPolicy(http_endpoints=("https://8.8.8.8/mcp",))
        facade = McpApiFacade(  # type: ignore[arg-type]
            self.runtime,
            mutation_authorizer=lambda _: McpMutationAuthorization(
                allowed=True, decision_id="d", grant_id="g"
            ),
            mutation_policy=policy,
        )
        status, body, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "remote",
                "config": {
                    "transport": "streamable_http",
                    "url": "https://api.example.com/mcp",
                },
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "http_dns_names_denied")

    def test_stdio_policy_requires_absolute_exact_executable_and_empty_invocation(self) -> None:
        executable = Path(sys.executable).resolve()
        policy = McpMutationPolicy(stdio_commands=(str(executable),))
        facade = McpApiFacade(  # type: ignore[arg-type]
            self.runtime,
            mutation_authorizer=lambda _: McpMutationAuthorization(
                allowed=True, decision_id="stdio-d", grant_id="stdio-g"
            ),
            mutation_policy=policy,
        )

        status, body, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "path-lookup",
                "config": {"transport": "stdio", "command": executable.name},
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "stdio_command_not_allowlisted")

        status, body, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "python-c",
                "config": {
                    "transport": "stdio",
                    "command": str(executable),
                    "args": ["-c", "print('unsafe template expansion')"],
                },
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "stdio_arguments_require_exact_template")

        status, body, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "path-env-attack",
                "config": {
                    "transport": "stdio",
                    "command": str(executable),
                    "env": {"PATH": str(ROOT / "attacker-controlled-bin")},
                },
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["reason_code"], "stdio_environment_require_exact_template")

        status, created, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "absolute-empty-invocation",
                "config": {
                    "transport": "stdio",
                    "command": str(executable),
                    "args": [],
                    "env": {},
                },
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.CREATED, created)

        diagnostics = policy.diagnostics()["stdio"]
        self.assertEqual(diagnostics["mode"], "absolute_canonical_executable_exact")
        self.assertFalse(diagnostics["path_lookup"])
        self.assertEqual(diagnostics["effective_entries"], 1)

    def test_http_policy_uses_exact_public_ip_literal_without_dns(self) -> None:
        def resolver_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("MCP HTTP mutation policy must never resolve DNS")

        endpoint = "https://8.8.8.8/mcp"
        policy = McpMutationPolicy(
            http_endpoints=(endpoint,),
            resolver=resolver_must_not_run,
        )
        facade = McpApiFacade(  # type: ignore[arg-type]
            self.runtime,
            mutation_authorizer=lambda _: McpMutationAuthorization(
                allowed=True, decision_id="http-d", grant_id="http-g"
            ),
            mutation_policy=policy,
        )

        for name, url, reason in (
            ("dns-name", "https://example.com/mcp", "http_dns_names_denied"),
            ("loopback", "https://127.0.0.1/mcp", "http_loopback_or_private_denied"),
            ("private", "https://10.0.0.8/mcp", "http_loopback_or_private_denied"),
            ("wrong-path", "https://8.8.8.8/other", "http_target_not_allowlisted"),
        ):
            status, body, _ = facade.handle_post(
                ["mcp", "servers"],
                {
                    "name": name,
                    "config": {"transport": "streamable_http", "url": url},
                },
                "operator",
            )  # type: ignore[misc]
            self.assertEqual(status, HTTPStatus.FORBIDDEN, body)
            self.assertEqual(body["reason_code"], reason)

        status, created, _ = facade.handle_post(
            ["mcp", "servers"],
            {
                "name": "ip-literal",
                "config": {"transport": "streamable_http", "url": endpoint},
            },
            "operator",
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.CREATED, created)

        diagnostics = policy.diagnostics()["http"]
        self.assertEqual(diagnostics["mode"], "public_ip_literal_url_exact")
        self.assertFalse(diagnostics["dns_resolution"])
        self.assertFalse(diagnostics["domain_names_allowed"])
        self.assertEqual(diagnostics["effective_entries"], 1)

    def test_domain_and_unexpected_errors_map_without_leaking_exception_text(self) -> None:
        self.runtime.failures["connect_server"] = McpConfigNotFound("secret-server-name")
        status, body, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "connect"], {}, "operator"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(body["error"], "mcp_not_found")
        self.assertNotIn("secret-server-name", json.dumps(body))

        self.runtime.failures["connect_server"] = McpStateConflict("secret conflict detail")
        status, body, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "connect"], {}, "operator"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertTrue(body["retryable"])

        self.runtime.failures["connect_server"] = McpClientRuntimeDisabled("secret disabled detail")
        status, body, _ = self.facade.handle_post(
            ["mcp", "servers", "alpha", "connect"], {}, "operator"
        )  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(body["error"], "mcp_runtime_disabled")

        self.runtime.failures["diagnostics"] = RuntimeError("credential=do-not-return")
        status, body, headers = self.facade.handle_get(["mcp", "health"], {})  # type: ignore[misc]
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(body["error"], "mcp_internal_error")
        self.assertNotIn("do-not-return", json.dumps(body))
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")


if __name__ == "__main__":
    unittest.main()
