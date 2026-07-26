from __future__ import annotations

from pathlib import Path

from zyra_api import main as api
from zyra_runtime.productization.contracts import RuntimeDomain


def _configure_clean_state(monkeypatch, root: Path) -> None:
    values = {
        "ZYRA_STATE_ROOT": root,
        "ZYRA_ARTIFACT_ROOT": root / "artifacts",
        "ZYRA_SQLITE_PATH": root / "zyra.sqlite3",
        "ZYRA_EVENT_LOG": root / "events.jsonl",
        "ZYRA_WORKER_POOL_STORE": root / "worker-pool.json",
        "ZYRA_GRAPH_STATE_STORE": root / "graph-state.json",
        "ZYRA_RECOVERY_RUNTIME_STORE": root / "recovery.json",
        "ZYRA_PERMISSION_STATE": root / "permissions.json",
        "ZYRA_PERMISSION_STORE": root / "permission-compat.json",
        "ZYRA_MEMORY_INDEX_PATH": root / "memory-index.sqlite3",
        "ZYRA_MCP_STATE": root / "mcp-state.json",
        "ZYRA_SANDBOX_GATEWAY_STATE": root / "gateway-state",
        "ZYRA_TERMINAL_STATE": root / "terminal-state.json",
        "ZYRA_TOOL_WORKSPACE": root / "workspace",
        "ZYRA_WORKSPACE_ROOT": root / "managed-workspaces",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED", raising=False)
    monkeypatch.delenv("ZYRA_MEMORY_CURATOR_DISABLED", raising=False)
    monkeypatch.delenv("ZYRA_TERMINAL_DISABLED", raising=False)
    monkeypatch.delenv("ZYRA_DISABLE_RECOVERY_RUNTIME", raising=False)


def _reset_api_composition() -> None:
    api.reset_api_product_bootstrap()
    api.reset_runtime_owner_composition()
    api.reset_memory_curator_runtime()
    api.reset_worker_pool_api()
    api.reset_recovery_runtime_api()
    api.reset_terminal_api()
    api.reset_mcp_runtime()
    api.reset_provider_control_client()
    api.reset_runtime_event_spine_bridge()


def test_api_readiness_is_gated_by_product_bootstrap_and_migrations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_clean_state(monkeypatch, tmp_path)
    _reset_api_composition()
    try:
        typed_receipts = api.TypedReceiptStore(api.sqlite_path())
        owners, details = api.runtime_readiness_probes(
            typed_receipts=typed_receipts,
        )

        assert all(owners.values())
        productization = details["productization"]
        assert productization["ready"] is True
        assert productization["demo_fallback"] is False
        assert productization["source_store_fallback"] is False
        assert productization["migration"]["ready"] is True
        receipt = productization["receipt"]
        assert receipt["migration"]["transaction"]["state"] == "committed"
        assert receipt["owner_readiness"]["ready"] is True
        assert Path(
            productization["configuration"]["state"]["migration_journal"]
        ).is_file()
    finally:
        _reset_api_composition()


def test_api_readiness_uses_all_canonical_owners_and_rejects_owner_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_clean_state(monkeypatch, tmp_path)
    _reset_api_composition()
    try:
        composition = api.get_runtime_owner_composition()
        snapshot = composition.probe_all()

        assert snapshot["ready"] is True
        assert snapshot["blockers"] == []
        assert len(snapshot["domains"]) == 11
        assert {
            item["domain"] for item in snapshot["domains"]
        } == {item.value for item in RuntimeDomain}
        assert all(item["owner"]["ready"] for item in snapshot["domains"])
        assert all(
            entry["ready"]
            for item in snapshot["domains"]
            for entry in item["default_entries"]
        )

        proofs = {
            domain: composition.prove_owner_loss(domain)
            for domain in RuntimeDomain
        }
        assert all(proof["valid"] for proof in proofs.values())
        assert all(not proof["fallback_success"] for proof in proofs.values())
        assert all(
            proof["error_code"] == "canonical_owner_unavailable"
            for proof in proofs.values()
        )
    finally:
        _reset_api_composition()


def test_runtime_owner_enforcement_disable_fails_readiness_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_clean_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED", "1")
    _reset_api_composition()
    try:
        snapshot = api.get_runtime_owner_composition().probe_all()

        assert snapshot["ready"] is False
        assert set(snapshot["blockers"]) == {
            item.value for item in RuntimeDomain
        }
        assert all(not item["ready"] for item in snapshot["domains"])
    finally:
        _reset_api_composition()
