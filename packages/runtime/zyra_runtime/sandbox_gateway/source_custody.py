from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import digest
from .constants import SOURCE_CUSTODY_SCHEMA


@dataclass(frozen=True, slots=True)
class SourceCustodyEntry:
    source_repository: str
    source_modules: tuple[str, ...]
    role: str
    mechanisms: tuple[str, ...]
    target_modules: tuple[str, ...]
    target_language: str
    adaptation: str
    canonical_owner: str
    production_owner: bool
    notes: str = ""
    source_language: str = ""
    migration_mode: str = ""
    source_symbols: tuple[str, ...] = ()
    runtime_entries: tuple[str, ...] = ()
    behavior_tests: tuple[str, ...] = ()
    landing_status: str = "internalized"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repository": self.source_repository,
            "source_modules": list(self.source_modules),
            "role": self.role,
            "mechanisms": list(self.mechanisms),
            "target_modules": list(self.target_modules),
            "target_language": self.target_language,
            "adaptation": self.adaptation,
            "canonical_owner": self.canonical_owner,
            "production_owner": self.production_owner,
            "notes": self.notes,
            "source_language": self.source_language,
            "migration_mode": self.migration_mode or self.adaptation,
            "source_symbols": list(self.source_symbols),
            "runtime_entries": list(self.runtime_entries),
            "behavior_tests": list(self.behavior_tests),
            "landing_status": self.landing_status,
        }


def source_custody_entries() -> tuple[SourceCustodyEntry, ...]:
    return (
        SourceCustodyEntry(
            source_repository="OpenHands",
            source_modules=(
                "openhands/app_server/sandbox/sandbox_models.py",
                "openhands/app_server/sandbox/sandbox_service.py",
                "openhands/app_server/sandbox/process_sandbox_service.py",
                "openhands/app_server/sandbox/workspace_archive.py",
                "openhands/app_server/file_store/files.py",
                "openhands/app_server/file_store/local.py",
                "openhands/app_server/event/event_service_base.py",
            ),
            role="primary",
            mechanisms=(
                "explicit sandbox lifecycle",
                "ready wait",
                "process state",
                "cleanup and interrupted recovery",
                "streaming archive boundary",
                "atomic state replacement",
            ),
            target_modules=(
                "zyra_runtime.sandbox_gateway.lifecycle",
                "zyra_runtime.sandbox_gateway.backends",
                "zyra_runtime.sandbox_gateway.state_store",
                "zyra_runtime.sandbox_gateway.archive_policy",
                "zyra_runtime.sandbox_gateway.event_port",
            ),
            target_language="python",
            adaptation=(
                "preserved Python and decomposed into Zyra session, event, permission, "
                "workspace-epoch, and recovery contracts"
            ),
            canonical_owner="SandboxGatewayRuntime",
            production_owner=True,
            source_language="python",
            migration_mode="same-language productized adaptation",
            source_symbols=("SandboxService", "ProcessSandboxService", "WorkspaceArchive"),
            runtime_entries=("zyra_runtime.sandbox_gateway.SandboxGatewayRuntime.execute",),
            behavior_tests=("tests/integration/test_sandbox_gateway_runtime.py",),
        ),
        SourceCustodyEntry(
            source_repository="openclaw",
            source_modules=(
                "src/acp/policy.ts",
                "src/gateway/node-command-policy.ts",
                "src/gateway/exec-approval-manager.ts",
                "src/gateway/node-invoke-system-run-approval-match.ts",
                "src/gateway/input-allowlist.ts",
                "src/acp/control-plane/manager.turn-timeout.ts",
                "src/acp/session-actor-queue.ts",
            ),
            role="supplementary",
            mechanisms=(
                "explicit allow ask deny",
                "unknown fail closed",
                "declared command and allowlist match",
                "exact approval binding",
                "atomic allow-once consumption",
                "timeout cancellation cleanup",
                "per-session serialization",
            ),
            target_modules=(
                "packages/runtime/sandbox-gateway-control/src/command-policy.ts",
                "packages/runtime/sandbox-gateway-control/src/approval-ledger.ts",
                "packages/runtime/sandbox-gateway-control/src/session-queue.ts",
                "packages/runtime/sandbox-gateway-control/src/timeout.ts",
                "zyra_runtime.sandbox_gateway.permission_relay",
                "zyra_runtime.sandbox_gateway.session_queue",
            ),
            target_language="typescript",
            adaptation=(
                "kept TypeScript mechanisms in a typed control package; Python relay "
                "only binds them to typescript.PermissionCoordinator and durable Zyra state"
            ),
            canonical_owner="typescript.PermissionCoordinator",
            production_owner=False,
            notes="The supplement cannot create a second gateway or permission authority.",
            source_language="typescript",
            migration_mode="mechanism-level supplementary adaptation",
            source_symbols=("ExecApprovalManager", "SessionActorQueue", "NodeCommandPolicy"),
            runtime_entries=("packages/runtime/sandbox-gateway-control/src/rpc.ts",),
            behavior_tests=("packages/runtime/sandbox-gateway-control/test/approval.test.ts",),
        ),
        SourceCustodyEntry(
            source_repository="oh-my-pi",
            source_modules=(
                "packages/coding-agent/src/task/isolation-runner.ts",
                "packages/coding-agent/src/task/worktree.ts",
                "packages/coding-agent/src/edit/modes/apply-patch.ts",
                "packages/hashline/src/patcher.ts",
                "python/robomp/src/sandbox.py",
            ),
            role="supplementary",
            mechanisms=(
                "dirty baseline isolation",
                "preserve patch on merge failure",
                "preflight all before write",
                "Hashline content binding",
                "credential-free remote execution",
                "secret redaction",
                "per-slot process cleanup",
                "symlink-safe temporary roots",
            ),
            target_modules=(
                "zyra_runtime.sandbox_gateway.isolation",
                "zyra_runtime.sandbox_gateway.patch_port",
                "zyra_runtime.sandbox_gateway.credential_relay",
                "zyra_runtime.sandbox_gateway.redaction",
                "packages/runtime/sandbox-gateway-control/src/hashline.ts",
                "packages/runtime/sandbox-gateway-control/src/credential-envelope.ts",
            ),
            target_language="typescript+python",
            adaptation=(
                "retained the source language of each mechanism and replaced upstream "
                "worktree ownership with WorkspaceEditPort transactions"
            ),
            canonical_owner="WorkspaceManagerRuntime",
            production_owner=False,
            source_language="typescript+python",
            migration_mode="bounded supplementary adaptation",
            source_symbols=("IsolationRunner", "HashlinePatcher", "Sandbox"),
            runtime_entries=("zyra_runtime.sandbox_gateway.GatewayFileArtifactPort.commit",),
            behavior_tests=("tests/unit/test_sandbox_gateway_integration_policy.py",),
        ),
        SourceCustodyEntry(
            source_repository="AgentScope",
            source_modules=("workspace and sandbox source graph entries",),
            role="conformance_only",
            mechanisms=("workspace lifecycle comparison",),
            target_modules=("tests/integration/test_sandbox_gateway_runtime.py",),
            target_language="python",
            adaptation="behavioral conformance only; no production control flow migrated",
            canonical_owner="none",
            production_owner=False,
            source_language="python",
            migration_mode="behavioral conformance",
            behavior_tests=("tests/integration/test_sandbox_gateway_runtime.py",),
            landing_status="conformance_only",
        ),
        SourceCustodyEntry(
            source_repository="Hermes",
            source_modules=("sandbox and tool policy source graph entries",),
            role="reference_only",
            mechanisms=("negative examples and deployment comparison",),
            target_modules=("docs/reviews/M1-S05B-01-sandbox-gateway-foundation-review.md",),
            target_language="documentation",
            adaptation="review evidence only",
            canonical_owner="none",
            production_owner=False,
            source_language="mixed",
            migration_mode="reference only",
            landing_status="reference_only",
        ),
        SourceCustodyEntry(
            source_repository="opencode",
            source_modules=("tool and permission protocol source graph entries",),
            role="conformance_only",
            mechanisms=("typed protocol comparison",),
            target_modules=("packages/runtime/sandbox-gateway-control/test/contracts.test.ts",),
            target_language="typescript",
            adaptation="contract conformance only; no duplicate runtime",
            canonical_owner="none",
            production_owner=False,
            source_language="typescript",
            migration_mode="typed contract conformance",
            behavior_tests=("packages/runtime/sandbox-gateway-control/test/contracts.test.ts",),
            landing_status="conformance_only",
        ),
        SourceCustodyEntry(
            source_repository="claude-code-best",
            source_modules=("permission and tool execution source graph entries",),
            role="reference_only",
            mechanisms=("permission identity compatibility",),
            target_modules=("zyra_runtime.sandbox_gateway.permission_relay",),
            target_language="python",
            adaptation=(
                "uses the canonical typescript.PermissionCoordinator contract rather "
                "than migrating another command gateway"
            ),
            canonical_owner="typescript.PermissionCoordinator",
            production_owner=False,
            source_language="typescript",
            migration_mode="reference only",
            landing_status="reference_only",
        ),
    )


def source_custody_manifest() -> Mapping[str, Any]:
    entries = source_custody_entries()
    payload = {
        "schema": SOURCE_CUSTODY_SCHEMA,
        "owner_slice": "M1-S05B-01",
        "entries": [item.to_dict() for item in entries],
        "invariants": {
            "gateway_count": 1,
            "permission_owner": "typescript.PermissionCoordinator",
            "workspace_owner": "WorkspaceManagerRuntime",
            "artifact_write_owner": "WorkspaceEditPort",
            "vendor_runtime_required": False,
            "cross_language_bulk_rewrite": False,
            "typescript_supplement_is_second_gateway": False,
        },
    }
    return {**payload, "manifest_digest": digest(payload)}


def assert_source_custody() -> Mapping[str, Any]:
    manifest = source_custody_manifest()
    production = [
        item for item in manifest["entries"] if item["production_owner"]
    ]
    primary = [item for item in production if item["role"] == "primary"]
    if len(primary) != 1 or primary[0]["source_repository"] != "OpenHands":
        raise AssertionError("sandbox gateway must retain one OpenHands primary source")
    if any(
        item["role"] in {"reference_only", "conformance_only"}
        and item["production_owner"]
        for item in manifest["entries"]
    ):
        raise AssertionError("reference and conformance sources cannot own production flow")
    if manifest["invariants"]["gateway_count"] != 1:
        raise AssertionError("source custody introduced a parallel gateway")
    return manifest
