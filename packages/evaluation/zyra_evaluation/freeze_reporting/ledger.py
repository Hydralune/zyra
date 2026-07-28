from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    digest,
    now,
    require_mapping,
    require_sequence,
    safe_relative_path,
)
from .contracts import ACTIVE_ROLES, ALL_SOURCE_ROLES
from .errors import blocker, fail, require_no_blockers
from .inputs import FreezeInputSet
from .sources import (
    LANGGRAPH_REQUIRED_CAPABILITIES,
    OMP_REQUIRED_CAPABILITIES,
    OPENCODE_REQUIRED_CAPABILITIES,
    behavior_tests,
    existing_targets,
    expand_source_capabilities,
)


class InternalizationLedgerBuilder:
    """Projects protected custody facts into a role/language/owner freeze ledger."""

    def __init__(self, inputs: FreezeInputSet) -> None:
        self.inputs = inputs
        self.repository_root = inputs.repository_root

    def build(self) -> dict[str, Any]:
        custody = self._source_custody()
        source_rows = [
            require_mapping(item, "source repository row")
            for item in require_sequence(
                custody.get("source_repository_rows"),
                "source repository rows",
            )
        ]
        rows: list[dict[str, Any]] = []
        for source_row in source_rows:
            rows.extend(self._project_source(source_row))
        rows.extend(self._specialized_rows(rows))
        rows.extend(self._openclaw_row(rows))
        rows.extend(self._claude_reference_rows(rows))
        ledger = {
            "schema": "zyra.first-stage-internalization-ledger/v1",
            "source_receipt_path": self._custody_path(),
            "source_receipt_sha256": self.inputs.digests[self._custody_id()],
            "rows": sorted(
                rows,
                key=lambda item: (
                    item["source_id"],
                    item["capability"],
                    item["role"],
                ),
            ),
            "generated_at": now(),
        }
        verification = InternalizationLedgerVerifier(
            self.repository_root
        ).verify(ledger)
        ledger["summary"] = verification["summary"]
        ledger["ledger_digest"] = digest(ledger)
        return ledger

    def _source_custody(self) -> dict[str, Any]:
        return self.inputs.document(self._custody_id())

    def _custody_id(self) -> str:
        for input_id, document in self.inputs.documents.items():
            if "source-custody" in str(document.get("schema") or ""):
                return input_id
            if document.get("source_repository_rows"):
                return input_id
        raise fail(
            "source-custody-input-missing",
            "M3 source-custody receipt is required for the freeze ledger.",
            phase="ledger",
        )

    def _custody_path(self) -> str:
        return self.inputs.relative_path(self._custody_id())

    def _project_source(
        self,
        source_row: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        selected = require_mapping(source_row, "source custody row")
        source_id = str(selected.get("source_repo") or "").strip()
        languages = sorted(
            {
                str(item).strip()
                for item in require_sequence(
                    selected.get("source_languages") or [],
                    "source languages",
                )
                if str(item).strip()
            }
        )
        owners = [
            str(item).strip()
            for item in require_sequence(
                selected.get("owners") or [],
                "source owners",
            )
            if str(item).strip()
        ]
        projected = []
        for capability_row in expand_source_capabilities(selected):
            custody_capability = capability_row["capability"]
            capability = self._precise_capability(source_id, custody_capability)
            role = capability_row["role"]
            active = capability_row["active"]
            lookup_capability = self._lookup_capability(capability)
            targets = (
                existing_targets(self.repository_root, lookup_capability)
                if active
                else []
            )
            tests = (
                behavior_tests(self.repository_root, lookup_capability)
                if active
                else []
            )
            owner = "; ".join(owners)
            status = "internalized" if active else "inactive"
            if source_id.lower() == "openclaw":
                continue
            projected.append(
                {
                    "source_id": source_id,
                    "capability": capability,
                    "role": role,
                    "status": status,
                    "source_languages": languages or ["unresolved"],
                    "target_language": self._target_language(targets),
                    "owner": owner if active else "",
                    "target_paths": targets,
                    "test_paths": tests,
                    "source_commits": [
                        str(item)
                        for item in require_sequence(
                            selected.get("source_commits") or [],
                            "source commits",
                        )
                    ],
                    "licenses": [
                        str(item)
                        for item in require_sequence(
                            selected.get("license_ids") or [],
                            "source licenses",
                        )
                    ],
                    "decision": (
                        "Protected M3 custody receipt marks this capability active "
                        "inside the listed Zyra owner."
                        if active
                        else
                        "Protected custody receipt retains this capability as an "
                        "inactive conformance/reference/deferred role."
                    ),
                }
            )
        return projected

    @staticmethod
    def _precise_capability(source_id: str, capability: str) -> str:
        """Disambiguate coarse repository rows into independently owned domains.

        The protected source-custody receipt groups several distinct M2 UI and
        workspace domains under broad labels.  Reusing those broad labels would
        manufacture duplicate primary owners in the freeze ledger.  This mapping
        preserves the protected source and role while naming the concrete state
        boundary each source actually contributes to.
        """

        key = (source_id.lower(), capability)
        return {
            ("claude-code-best", "control_console"): "interactive_runtime_console",
            ("opencode", "control_console"): "session_control_console",
            ("openhands", "control_console"): "artifact_runtime_console",
            (
                "opencode",
                "session_event_projection",
            ): "durable_session_event_projection",
            (
                "openhands",
                "session_event_projection",
            ): "conversation_event_projection",
            (
                "agentscope",
                "workspace_sandbox",
            ): "worker_workspace_sandbox",
            (
                "openhands",
                "workspace_sandbox",
            ): "repository_workspace_sandbox",
        }.get(key, capability)

    @staticmethod
    def _lookup_capability(capability: str) -> str:
        if capability.endswith("_console"):
            return "control_console"
        if capability.endswith("_event_projection"):
            return "event_projection"
        if capability.endswith("_workspace_sandbox"):
            return "workspace_sandbox"
        return {
            "provider_control_plane": "provider_control",
            "checkpoint_exact_resume": "exact_resume",
            "immutable_graph_state": "dynamic_topology",
        }.get(capability, capability)

    def _specialized_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        additions: list[dict[str, Any]] = []
        by_source = defaultdict(set)
        source_languages = defaultdict(set)
        for row in rows:
            by_source[str(row["source_id"]).lower()].add(str(row["capability"]))
            source_languages[str(row["source_id"]).lower()].update(
                row["source_languages"]
            )
        additions.extend(
            self._missing_specialized(
                "opencode",
                OPENCODE_REQUIRED_CAPABILITIES,
                by_source["opencode"],
                source_languages["opencode"] or {"typescript", "tsx"},
                active_roles={
                    "v2-durable-session-event": "primary_implementation",
                    "v1-provider-product-loop": "primary_implementation",
                    "typed-protocol": "primary_implementation",
                    "app-session-ui": "primary_implementation",
                    "tui-control": "supplementary_implementation",
                    "permission-question": "supplementary_implementation",
                    "terminal-review-diff": "primary_implementation",
                },
            )
        )
        additions.extend(
            self._missing_specialized(
                "oh-my-pi",
                OMP_REQUIRED_CAPABILITIES,
                by_source["oh-my-pi"],
                source_languages["oh-my-pi"]
                or {"typescript", "python", "rust"},
                active_roles={
                    "agentloop-session": "supplementary_implementation",
                    "tasktool-pal": "supplementary_implementation",
                    "mnemopi-compact": "supplementary_implementation",
                    "provider-rpc-acp": "supplementary_implementation",
                    "hashline": "supplementary_implementation",
                },
                experimental={"snapcompact"},
            )
        )
        additions.extend(
            self._missing_specialized(
                "langgraph",
                LANGGRAPH_REQUIRED_CAPABILITIES,
                by_source["langgraph"],
                source_languages["langgraph"] or {"python"},
                active_roles={
                    "checkpoint-identity-lineage": "primary_implementation",
                    "pending-committed-writes": "primary_implementation",
                    "side-effect-fence": "primary_implementation",
                    "exact-resume": "primary_implementation",
                },
                force_reference={
                    "open-world-topology",
                    "immutable-branch-delta",
                    "deterministic-commit",
                    "codeworker-reasoning-cohesion",
                    "stategraph-pregel-inactive",
                    "generic-channel-reducer-inactive",
                    "toolnode-stream-store-inactive",
                    "sdk-server-inactive",
                },
            )
        )
        return additions

    def _missing_specialized(
        self,
        source_id: str,
        expected: Sequence[str],
        present: set[str],
        languages: set[str],
        *,
        active_roles: Mapping[str, str],
        experimental: set[str] | None = None,
        force_reference: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        additions = []
        experimental = experimental or set()
        force_reference = force_reference or set()
        for capability in expected:
            if capability in present:
                continue
            role = active_roles.get(capability)
            if capability in experimental:
                role = "experimental"
            if capability in force_reference:
                role = "reference_only"
            if role is None:
                role = "reference_only"
            active = role in ACTIVE_ROLES
            normalized_capability = self._capability_hint(capability)
            targets = (
                existing_targets(self.repository_root, normalized_capability)
                if active
                else []
            )
            tests = (
                behavior_tests(self.repository_root, normalized_capability)
                if active
                else []
            )
            additions.append(
                {
                    "source_id": source_id,
                    "capability": capability,
                    "role": role,
                    "status": "internalized" if active else (
                        "experimental" if role == "experimental" else "inactive"
                    ),
                    "source_languages": sorted(languages),
                    "target_language": self._target_language(targets),
                    "owner": self._owner_hint(capability) if active else "",
                    "target_paths": targets,
                    "test_paths": tests,
                    "source_commits": [],
                    "licenses": [],
                    "decision": self._specialized_decision(source_id, capability, role),
                }
            )
        return additions

    @staticmethod
    def _capability_hint(capability: str) -> str:
        lowered = capability.lower()
        if "session" in lowered or "event" in lowered:
            return "event_projection"
        if "provider" in lowered or "model" in lowered:
            return "provider_control"
        if "permission" in lowered:
            return "permission_runtime"
        if "mcp" in lowered:
            return "mcp_runtime"
        if "memory" in lowered or "mnemopi" in lowered or "compact" in lowered:
            return "memory_retrieval"
        if "task" in lowered or "pal" in lowered:
            return "workspace_sandbox"
        if (
            "checkpoint" in lowered
            or "resume" in lowered
            or "write" in lowered
            or "fence" in lowered
        ):
            return "exact_resume"
        if "topology" in lowered or "delta" in lowered or "commit" in lowered:
            return "dynamic_topology"
        if "ui" in lowered or "tui" in lowered or "terminal" in lowered:
            return "control_console"
        if "protocol" in lowered:
            return "event_projection"
        if "hashline" in lowered:
            return "hashline"
        return capability.replace("-", "_")

    @staticmethod
    def _owner_hint(capability: str) -> str:
        lowered = capability.lower()
        if any(item in lowered for item in ("checkpoint", "resume", "write", "fence")):
            return "GraphCommitRuntime / CheckpointRecoveryRuntime"
        if any(item in lowered for item in ("provider", "model")):
            return "ProviderControlPlane"
        if any(item in lowered for item in ("memory", "compact", "mnemopi")):
            return "MemoryFabric and compact/restore owners"
        if any(item in lowered for item in ("session", "event", "protocol")):
            return "Zyra EventStore/Projector and single frontend store"
        if any(item in lowered for item in ("task", "pal", "workspace")):
            return "SubagentTaskStore / WorkspaceManager / WorkerPool"
        return "Existing Zyra capability owner"

    @staticmethod
    def _specialized_decision(source_id: str, capability: str, role: str) -> str:
        if source_id == "langgraph" and role == "reference_only":
            return (
                "Inactive by the 2026-07-13 correction: Zyra owns open-world "
                "topology and immutable commit; LangGraph does not own StateGraph, "
                "Pregel, channels, ToolNode, stream, Store, SDK, or server."
            )
        return (
            f"Freeze-specific expansion of the protected {source_id} custody "
            f"summary for {capability}; role={role}. Listing does not create a "
            "new runtime or migration quota."
        )

    def _openclaw_row(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "source_id": "openclaw",
                "capability": "historical-provenance-license-no-runtime-dependency",
                "role": "excluded_forward_only",
                "status": "historical-only",
                "source_languages": ["typescript"],
                "target_language": "none",
                "owner": "",
                "target_paths": [],
                "test_paths": [],
                "source_commits": [],
                "licenses": [],
                "decision": (
                    "Protected through M1-S05D-02 only. No M3 source role, source "
                    "reading, migration, adapter, conformance work, process, package, "
                    "database, cache, or root-path runtime dependency is permitted."
                ),
            }
        ]

    @staticmethod
    def _claude_reference_rows(
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        additions = []
        present = {str(item["source_id"]).lower() for item in rows}
        for source_id, decision in (
            (
                "claudecode-related/claude-reviews-claude",
                "Reference-only engineering decomposition; no runtime dependency "
                "and no effective production credit.",
            ),
            (
                "claudecode-related/Dive-into-Claude-Code",
                "Reference-only design-space and competition narrative input; no "
                "runtime dependency and no effective production credit.",
            ),
        ):
            if source_id.lower() in present:
                continue
            additions.append(
                {
                    "source_id": source_id,
                    "capability": "claude-runtime-analysis-reference",
                    "role": "reference_only",
                    "status": "inactive",
                    "source_languages": ["markdown"],
                    "target_language": "none",
                    "owner": "",
                    "target_paths": [],
                    "test_paths": [],
                    "source_commits": [],
                    "licenses": [],
                    "decision": decision,
                }
            )
        return additions

    @staticmethod
    def _target_language(paths: Sequence[str]) -> str:
        suffixes = {Path(path).suffix.lower() for path in paths if Path(path).suffix}
        if any(path.endswith("/src") or "typescript" in path for path in paths):
            return "typescript"
        if suffixes & {".ts", ".tsx"}:
            return "typescript"
        if suffixes & {".rs"}:
            return "rust"
        if suffixes & {".py"} or paths:
            return "python"
        return "none"


class InternalizationLedgerVerifier:
    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)

    def verify(self, ledger: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(ledger, "internalization ledger")
        rows = [
            require_mapping(item, "internalization ledger row")
            for item in require_sequence(selected.get("rows"), "ledger rows")
        ]
        findings: list[dict[str, Any]] = []
        identities: set[tuple[str, str, str]] = set()
        primary_by_capability = defaultdict(set)
        supplements_by_capability = defaultdict(set)
        role_counts: Counter[str] = Counter()
        language_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        specialized = defaultdict(set)
        for row in rows:
            source_id = str(row.get("source_id") or "")
            capability = str(row.get("capability") or "")
            role = str(row.get("role") or "")
            identity = (source_id.lower(), capability, role)
            if identity in identities:
                findings.append(
                    blocker(
                        "ledger-row-duplicate",
                        "Internalization ledger row is duplicated.",
                        source_id=source_id,
                        capability=capability,
                        role=role,
                    )
                )
            identities.add(identity)
            source_counts[source_id] += 1
            role_counts[role] += 1
            specialized[source_id.lower()].add(capability)
            for language in row.get("source_languages") or []:
                selected_language = str(language).lower()
                language_counts[selected_language] += 1
                if selected_language in {"mixed", "unknown", ""}:
                    findings.append(
                        blocker(
                            "ledger-language-not-concrete",
                            "Ledger source language must be concrete.",
                            source_id=source_id,
                            capability=capability,
                            language=selected_language,
                        )
                    )
            if role == "primary_implementation":
                primary_by_capability[capability].add(source_id)
            if role == "supplementary_implementation":
                supplements_by_capability[capability].add(source_id)
            if role in ACTIVE_ROLES:
                findings.extend(self._verify_active_row(row))
            if source_id.lower() == "openclaw":
                findings.extend(self._verify_openclaw(row))
            if source_id.lower().startswith("claudecode-related/"):
                if role != "reference_only":
                    findings.append(
                        blocker(
                            "claude-auxiliary-role-invalid",
                            "Claude auxiliary repositories must remain reference_only.",
                            source_id=source_id,
                            role=role,
                        )
                    )
                if row.get("target_paths") or row.get("owner"):
                    findings.append(
                        blocker(
                            "claude-auxiliary-runtime-forbidden",
                            "Claude auxiliary repositories cannot acquire runtime custody.",
                            source_id=source_id,
                        )
                    )
        for capability, sources in primary_by_capability.items():
            if len(sources) > 1:
                findings.append(
                    blocker(
                        "ledger-primary-owner-duplicated",
                        "Capability has more than one primary implementation source.",
                        capability=capability,
                        sources=sorted(sources),
                    )
                )
        for capability, sources in supplements_by_capability.items():
            if len(sources) > 2:
                findings.append(
                    blocker(
                        "ledger-supplement-limit-exceeded",
                        "Capability has more than two supplementary sources.",
                        capability=capability,
                        sources=sorted(sources),
                    )
                )
        for source_id, expected in (
            ("opencode", OPENCODE_REQUIRED_CAPABILITIES),
            ("oh-my-pi", OMP_REQUIRED_CAPABILITIES),
            ("langgraph", LANGGRAPH_REQUIRED_CAPABILITIES),
        ):
            missing = sorted(set(expected) - specialized[source_id])
            if missing:
                findings.append(
                    blocker(
                        "ledger-specialized-source-incomplete",
                        "Freeze ledger omits required source subdomains.",
                        source_id=source_id,
                        missing=missing,
                    )
                )
        require_no_blockers(
            findings,
            code="internalization-ledger-invalid",
            message="Role-aware internalization ledger contains blocking findings.",
            phase="ledger",
        )
        summary = {
            "source_count": len(source_counts),
            "row_count": len(rows),
            "active_row_count": sum(
                count for role, count in role_counts.items() if role in ACTIVE_ROLES
            ),
            "inactive_row_count": sum(
                count for role, count in role_counts.items() if role not in ACTIVE_ROLES
            ),
            "role_counts": dict(sorted(role_counts.items())),
            "source_row_counts": dict(sorted(source_counts.items())),
            "language_counts": dict(sorted(language_counts.items())),
            "primary_capability_count": len(primary_by_capability),
            "supplementary_capability_count": len(supplements_by_capability),
        }
        receipt = {
            "schema": "zyra.first-stage-internalization-ledger-verification/v1",
            "valid": True,
            "summary": summary,
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _verify_active_row(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        source_id = str(row.get("source_id") or "")
        capability = str(row.get("capability") or "")
        if not str(row.get("owner") or "").strip():
            findings.append(
                blocker(
                    "ledger-active-owner-missing",
                    "Active source row has no canonical owner.",
                    source_id=source_id,
                    capability=capability,
                )
            )
        targets = [str(item) for item in row.get("target_paths") or []]
        tests = [str(item) for item in row.get("test_paths") or []]
        if not targets:
            findings.append(
                blocker(
                    "ledger-active-target-missing",
                    "Active source row has no product target.",
                    source_id=source_id,
                    capability=capability,
                )
            )
        if not tests:
            findings.append(
                blocker(
                    "ledger-active-test-missing",
                    "Active source row has no behavior test.",
                    source_id=source_id,
                    capability=capability,
                )
            )
        for path in targets + tests:
            relative = safe_relative_path(path, "ledger target/test path")
            if not (self.repository_root / relative).exists():
                findings.append(
                    blocker(
                        "ledger-active-path-missing",
                        "Active source target or test path does not exist.",
                        source_id=source_id,
                        capability=capability,
                        path=relative,
                    )
                )
        return findings

    @staticmethod
    def _verify_openclaw(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        findings = []
        if row.get("role") != "excluded_forward_only":
            findings.append(
                blocker(
                    "ledger-openclaw-role-forbidden",
                    "OpenClaw must remain excluded_forward_only.",
                    role=row.get("role"),
                )
            )
        if row.get("target_paths") or row.get("test_paths") or row.get("owner"):
            findings.append(
                blocker(
                    "ledger-openclaw-forward-obligation-forbidden",
                    "OpenClaw cannot acquire M3 target, test, or owner obligations.",
                )
            )
        return findings
