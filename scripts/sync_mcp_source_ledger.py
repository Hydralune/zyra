from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TargetBinding,
    TestEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402
from zyra_integrations.mcp.source_audit import (  # noqa: E402
    MCP_SOURCE_DECISIONS,
    McpSourceDecision,
    SourceDisposition,
)


DEFAULT_LEDGER_PATH = (
    PROJECT_ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M1-03B"
SLICE_ID = "M1-S03B-01"
STAMP = "2026-07-11T12:00:00.000Z"
DOWNSTREAM_UNITS = ["M1-03C", "M1-03D", "M1-05B", "M2-04A"]
MCP_EVENTS = [
    "mcp_config_changed",
    "mcp_connection_changed",
    "mcp_capabilities_changed",
    "mcp_auth_changed",
    "mcp_elicitation",
    "mcp_task_updated",
    "mcp_instructions_changed",
    "mcp_tool_result",
]
SOURCE_GRAPH_REFS = {
    "claude-code-best": "provenance/source-graphs/claude-code-best/batch-05-mcp-runtime-tools-auth.md",
    "agent-framework": "provenance/source-graphs/agent-framework/batch-02-tools-skills-mcp-middleware.md",
    "opencode": "provenance/source-graphs/opencode/batch-06-mcp-plugin-acp-control-plane.md",
    "agentscope": "provenance/source-graphs/agentscope/batch-04-mcp-workspace-gateway-security.md",
    "hermes-agent": "provenance/source-graphs/hermes-agent/batch-08-plugins-providers-mcp-acp-source-verdict.md",
}
SOURCE_AUDIT_TEST = "tests/unit/test_mcp_source_audit.py"


def strategy_for_decision(decision: McpSourceDecision) -> MigrationStrategy:
    if decision.disposition is SourceDisposition.ADAPTER:
        return MigrationStrategy.ADAPTER
    if decision.disposition is SourceDisposition.DEFERRED:
        return MigrationStrategy.CANDIDATE_REVIEW
    return MigrationStrategy.REIMPLEMENTED_PATTERN


def lifecycle_for_decision(decision: McpSourceDecision) -> LedgerLifecycle:
    if decision.disposition is SourceDisposition.DEFERRED:
        return LedgerLifecycle.DEFERRED
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        return LedgerLifecycle.CANDIDATE
    return LedgerLifecycle.PRODUCTIZED


def status_for_decision(decision: McpSourceDecision) -> MainPathStatus:
    if decision.disposition is SourceDisposition.DEFERRED:
        return MainPathStatus.PLANNED
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        return MainPathStatus.INVENTORIED
    return MainPathStatus.TESTED_MAIN_PATH


def runtime_entry_for_decision(decision: McpSourceDecision) -> RuntimeEntry:
    symbol = (
        decision.runtime_entry
        if decision.claims_runtime_ownership and decision.runtime_entry
        else "zyra_integrations.mcp.source_audit.source_decision"
    )
    # TypeScript entries are ``<package>/<module>#<Symbol>``; Python entries stay
    # dotted.  The MCP client runtime has been TypeScript since the e02 cutover.
    if "#" in symbol:
        module, _, function = symbol.partition("#")
    else:
        module, _, function = symbol.rpartition(".")
    if not module or not function:
        raise ValueError(f"Invalid MCP runtime entry symbol: {symbol!r}")
    return RuntimeEntry(
        module=module,
        function=function,
        protocol=(
            "zyra-mcp-runtime-v1"
            if decision.claims_runtime_ownership
            else "zyra-mcp-source-decision-v1"
        ),
        health_check=(
            "bun test packages/integrations/claude-mcp/test && "
            "python -m unittest tests.unit.test_mcp_source_audit"
        ),
        config_refs=[
            "packages/integrations/claude-mcp/src/config/config-store.ts",
            "packages/integrations/zyra_integrations/mcp/store.py",
            "packages/integrations/zyra_integrations/mcp/models.py",
        ],
    )


def test_entry_for_decision(decision: McpSourceDecision) -> TestEntry:
    path = decision.test_target if decision.claims_runtime_ownership else SOURCE_AUDIT_TEST
    if not path:
        raise ValueError(f"MCP runtime decision has no test target: {decision.key}")
    if path.endswith(".ts"):
        # The MCP behavior suite is TypeScript since the e02 cutover; a Python
        # unittest module path derived from it would not name a runnable test.
        command = f"bun test {path}"
        kind = "behavior"
    else:
        module = path.removesuffix(".py").replace("/", ".")
        command = f"python -m unittest {module}"
        kind = "integration" if "/integration/" in f"/{path}" else "unit"
    return TestEntry(
        path=path,
        command=command,
        kind=kind,
        expected_signal=(
            "real MCP connection/capability/auth/tool/restore behavior changes the main path"
            if decision.claims_runtime_ownership
            else "the source disposition is explicit and cannot claim runtime ownership"
        ),
        required=True,
    )


def _surfaces(decision: McpSourceDecision) -> tuple[list[str], list[str], list[str]]:
    joined = " ".join((*decision.target_paths, *decision.mechanisms)).casefold()
    surfaces = ["mcp_runtime"]
    routes = ["GET /mcp"]
    artifacts: list[str] = []
    if any(token in joined for token in ("config", "connection", "transport", "protocol")):
        surfaces.extend(["mcp_connection", "mcp_diagnostics_api"])
        routes.extend(["GET /mcp/servers", "POST /mcp/servers/{server_id}/connect"])
    if any(token in joined for token in ("capabil", "projection", "tool", "resource", "prompt")):
        surfaces.extend(["tool_registry", "code_worker"])
        routes.extend(["GET /mcp/tools", "GET /mcp/resources", "GET /mcp/prompts"])
    if any(token in joined for token in ("auth", "credential", "oauth", "xaa")):
        surfaces.extend(["mcp_auth", "mcp_control_api"])
        routes.append("POST /mcp/servers/{server_id}/auth/install")
    if "elicitation" in joined:
        surfaces.extend(["mcp_elicitation", "mcp_control_api"])
        routes.append("POST /mcp/elicitations/resolve")
    if any(token in joined for token in ("output", "artifact", "binary", "spill")):
        surfaces.append("artifact_store")
        artifacts.extend(["tool_output", "binary"])
    if any(token in joined for token in ("instruction", "compact", "restore")):
        surfaces.extend(["code_worker_context", "compact_restore"])
    return list(dict.fromkeys(surfaces)), list(dict.fromkeys(routes)), list(dict.fromkeys(artifacts))


def _event_types(decision: McpSourceDecision) -> list[str]:
    joined = " ".join((*decision.target_paths, *decision.mechanisms)).casefold()
    selected: list[str] = []
    rules = (
        (("config",), "mcp_config_changed"),
        (("connection", "transport", "protocol", "lifecycle"), "mcp_connection_changed"),
        (("capabil", "catalog", "projection", "resource", "prompt"), "mcp_capabilities_changed"),
        (("auth", "credential", "oauth", "xaa", "token"), "mcp_auth_changed"),
        (("elicitation",), "mcp_elicitation"),
        (("task", "poll", "cancel"), "mcp_task_updated"),
        (("instruction", "compact", "restore"), "mcp_instructions_changed"),
        (("tool", "output", "artifact", "binary", "spill"), "mcp_tool_result"),
    )
    for markers, event_type in rules:
        if any(marker in joined for marker in markers):
            selected.append(event_type)
    return selected or ["mcp_connection_changed"]


def main_path_for_decision(decision: McpSourceDecision) -> MainPathBinding:
    if not decision.claims_runtime_ownership:
        return MainPathBinding(
            surfaces=["mcp_source_decision"],
            worker_runtime=(
                f"Inventory-only {decision.disposition.value} decision; behavior is owned by "
                f"{decision.replacement or ', '.join(decision.target_paths)}."
            ),
        )
    surfaces, routes, artifacts = _surfaces(decision)
    return MainPathBinding(
        surfaces=surfaces,
        event_types=_event_types(decision),
        api_routes=routes,
        control_commands=["/mcp"],
        artifact_kinds=artifacts,
        worker_runtime=decision.main_path_evidence,
    )


def replacement_plan_for_decision(decision: McpSourceDecision) -> str:
    if decision.disposition is SourceDisposition.DEFERRED:
        return (
            f"Deferred to {decision.next_owner}: {decision.rationale} "
            f"Current boundary: {decision.replacement}."
        ).strip()
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        return (
            f"The source remains {decision.disposition.value}; {decision.replacement or decision.rationale} "
            "It is not a runtime dependency and cannot be counted as internalized production code."
        ).strip()
    return (
        "Selected mechanisms are decomposed into Zyra MCP config, connection, protocol, auth, capability, "
        "projection, output, elicitation, sampling, task, instruction and event modules. Runtime behavior "
        "uses Zyra state/permission/artifact/session boundaries and never imports the source repository."
    )


def build_entry(decision: McpSourceDecision) -> InternalizationLedgerEntry:
    runtime_owner = decision.claims_runtime_ownership
    bindings = [
        TargetBinding(
            target_path=path,
            role="primary" if index == 0 else "supporting",
            required_for_main_path=runtime_owner,
        )
        for index, path in enumerate(decision.target_paths)
    ]
    bindings.append(TargetBinding(
        target_path="packages/integrations/zyra_integrations/mcp/source_audit.py",
        role="source-audit",
        required_for_main_path=False,
    ))
    capability_name = f"mcp-runtime:{decision.source_path}"
    summary = ", ".join(decision.mechanisms)
    entry = InternalizationLedgerEntry(
        ledger_id=InternalizationLedgerEntry.new(
            source_repo=decision.repository,
            source_path=decision.source_path,
            capability_name=capability_name,
            capability_summary=summary,
            target_paths=[decision.target_paths[0]],
        ).ledger_id,
        source_repo=decision.repository,
        source_path=decision.source_path,
        capability_name=capability_name,
        capability_summary=summary,
        target_bindings=bindings,
        migration_strategy=strategy_for_decision(decision),
        main_path_status=status_for_decision(decision),
        lifecycle=lifecycle_for_decision(decision),
        runtime_entry=runtime_entry_for_decision(decision),
        test_entries=[test_entry_for_decision(decision)],
        main_path=main_path_for_decision(decision),
        line_count_policy=(
            LineCountPolicy.COUNTS_WHEN_PRODUCTIZED
            if runtime_owner
            else LineCountPolicy.EXCLUDED_INVENTORY_ONLY
        ),
        license_notice=LicenseNotice(
            source_repo=decision.repository,
            status=NoticeStatus.RECORDED,
            license_hint="Source mechanism recorded for Zyra-owned MCP runtime internalization.",
            notice_path="third_party/NOTICE.md",
            notes="No runtime dependency on the parent source repository.",
        ),
        owner_unit=OWNER_UNIT,
        milestone="M1",
        downstream_units=list(DOWNSTREAM_UNITS),
        dependencies=["M1-02C", "M1-02D", "M1-03A"] if runtime_owner else [],
        source_evidence=[SourceEvidence(
            source_repo=decision.repository,
            source_path=decision.source_path,
            exists_in_workspace=True,
            source_kind="file",
            reason="M1-S03B-01 MCP source-to-target decision",
            symbols=[],
            tags=["mcp", decision.authority.value, decision.disposition.value],
        )],
        tags=[
            "m1-03b",
            "slice-03b-01",
            "mcp-runtime",
            decision.authority.value,
            decision.disposition.value,
        ],
        blockers=[],
        risk_notes=list(decision.limitations),
        replacement_plan=replacement_plan_for_decision(decision),
        created_at=STAMP,
        updated_at=STAMP,
        metadata={
            "source_authority": decision.authority.value,
            "source_disposition": decision.disposition.value,
            "source_graph_ref": SOURCE_GRAPH_REFS[decision.repository],
            "main_path_evidence": decision.main_path_evidence,
            "next_owner": decision.next_owner or "",
            "slice_id": SLICE_ID,
        },
    )
    errors = entry.validate()
    if errors:
        raise ValueError(f"Invalid MCP ledger entry {entry.ledger_id}: {errors}")
    return entry


def expected_entries() -> list[InternalizationLedgerEntry]:
    entries = [build_entry(decision) for decision in MCP_SOURCE_DECISIONS]
    keys = {(entry.source_repo, entry.source_path) for entry in entries}
    if len(keys) != len(entries):
        raise ValueError("MCP source decisions contain duplicate repository/path identities")
    return entries


def rewrite_payload(payload: dict[str, object]) -> dict[str, object]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Ledger payload has no entries list")
    replacements = [entry.to_dict() for entry in expected_entries()]
    output: list[dict[str, object]] = []
    inserted = False
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("Ledger entry is not an object")
        if raw.get("owner_unit") == OWNER_UNIT:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(raw)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    result = dict(payload)
    result["entries"] = output
    result["summary"] = to_jsonable(InternalizationLedger(typed).summary())
    return result


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, int]:
    original = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite_payload(original)
    before = canonical_json(original)
    after = canonical_json(expected)
    aligned_before = before == after
    if write and not aligned_before:
        path.write_text(after, encoding="utf-8", newline="\n")
    return aligned_before if not write else True, len(expected_entries()), 0 if aligned_before else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize M1-S03B-01 MCP source decisions into the bundled ledger."
    )
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Write the deterministic M1-03B projection.")
    mode.add_argument("--check", action="store_true", help="Fail when M1-03B rows are not aligned.")
    mode.add_argument("--dry-run", action="store_true", help="Report drift without changing the ledger.")
    args = parser.parse_args(argv)
    path = args.ledger_path.resolve()
    aligned, count, changed = synchronize(path, write=args.write)
    print(f"mcp_source_ledger_aligned={str(aligned).lower()}")
    print(f"mcp_source_decision_count={count}")
    print(f"mcp_owner_groups_changed={changed}")
    print(f"ledger_path={path}")
    if args.check:
        return 0 if aligned else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
