from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts" / "audit_m2_02_source_to_target.py"
LEDGER_PATH = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)


def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "m2_02_source_to_target_audit",
        AUDIT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ledger() -> dict:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def _entry(document: dict, unit: str, source_repo: str) -> dict:
    return next(
        item
        for item in document["entries"]
        if item.get("owner_unit") == unit
        and item.get("source_repo") == source_repo
    )


def test_current_m2_02_role_and_language_policy_passes() -> None:
    audit_module = _audit_module()
    target = audit_module.resolve_commit("HEAD")
    result = audit_module.audit(_ledger(), target)
    assert result["ok"] is True, result["errors"]
    assert result["warnings"] == []


def test_third_supplementary_source_fails_closed() -> None:
    audit_module = _audit_module()
    document = copy.deepcopy(_ledger())
    omp = _entry(document, "M2-S02B-01", "oh-my-pi")
    omp["metadata"]["source_role"] = "supplementary_implementation"
    omp["metadata"]["target_language"] = "typescript"
    omp["metadata"]["migration_mode"] = "cropped_migration"

    result = audit_module.audit(
        document,
        audit_module.resolve_commit("HEAD"),
    )

    assert result["ok"] is False
    assert any(
        error.startswith("M2-S02B-01:supplementary-count:3:maximum:2")
        for error in result["errors"]
    )
    assert not result["warnings"]


def test_combined_production_language_label_fails_closed() -> None:
    audit_module = _audit_module()
    document = copy.deepcopy(_ledger())
    primary = _entry(document, "M2-S02A-02", "zyra")
    primary["metadata"]["source_language"] = "typescript/python"

    result = audit_module.audit(
        document,
        audit_module.resolve_commit("HEAD"),
    )

    assert result["ok"] is False
    assert any(
        ":source-language-not-exact:typescript/python" in error
        for error in result["errors"]
    )


def test_slice_ledger_synchronizers_are_order_independent() -> None:
    modules = [
        _script_module(
            ROOT / "scripts" / name,
            f"ledger_sync_{index}",
        )
        for index, name in enumerate(
            (
                "sync_m2_topology_projection_source_ledger.py",
                "sync_m2_topology_interaction_source_ledger.py",
                "sync_m2_worker_causal_timeline_source_ledger.py",
                "sync_m2_recovery_control_timeline_source_ledger.py",
            )
        )
    ]
    forward = _ledger()
    for module in modules:
        forward = module.rewrite(forward)
    reverse = copy.deepcopy(forward)
    for module in reversed(modules):
        reverse = module.rewrite(reverse)

    assert reverse == forward
    for module in modules:
        assert module.rewrite(copy.deepcopy(forward)) == forward
