from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zyra_evaluation.policy_benchmark.preflight import compute_preflight_id
from zyra_orchestration.topology_policy import (
    MechanismRegistry,
    ResolutionPurpose,
    canonical_digest,
)
from zyra_productization.release.worktree import require_worktree_boundary


ROOT = Path(__file__).resolve().parents[2]


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _repo_path(value: Any) -> Path:
    selected = (ROOT / str(value or "")).resolve()
    try:
        selected.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(f"manifest member escapes repository: {value}") from error
    if not selected.is_file():
        raise ValueError(f"manifest member is missing: {value}")
    return selected


def prepare(
    *,
    template: Path,
    output: Path,
    target_commit: str,
) -> dict[str, Any]:
    if len(target_commit) != 40:
        raise ValueError("target commit must be a full Git commit")
    require_worktree_boundary(ROOT, expected_head=target_commit)
    payload = _load(template)
    frozen = payload.get("frozen_inputs")
    if not isinstance(frozen, dict):
        raise ValueError("template frozen_inputs are missing")
    frozen["implementation_target_commit"] = target_commit
    registry = MechanismRegistry.load(ROOT)
    resolved = registry.resolve("topology_policy", purpose=ResolutionPurpose.NORMAL)
    if resolved.profile_id != "phase2_strongest_v1":
        raise ValueError("phase2_strongest_v1 is not the active default profile")
    profile = frozen.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("template profile is missing")
    profile["config_digest"] = resolved.config_digest
    identity_fields = {
        "policy_registry": ("registry_digest", "registry_digest"),
        "activation_gates": ("frozen_gate_digest", "gate_digest"),
        "phase1_baseline_manifest": ("manifest_digest", "manifest_digest"),
    }
    for label, (document_field, binding_field) in identity_fields.items():
        binding = frozen.get(label)
        if not isinstance(binding, dict):
            raise ValueError(f"template binding is missing: {label}")
        path = _repo_path(binding.get("path"))
        document = _load(path)
        identity = str(document.get(document_field) or "")
        if len(identity) != 64:
            raise ValueError(f"bound identity is invalid: {label}.{document_field}")
        binding["file_sha256"] = _file_digest(path)
        binding[binding_field] = identity
    for binding in payload.get("evidence_bindings") or ():
        if not isinstance(binding, dict):
            raise ValueError("evidence binding must be an object")
        path = _repo_path(binding.get("report_ref"))
        report = _load(path)
        binding["file_sha256"] = _file_digest(path)
        binding["report_digest"] = str(report.get("report_digest") or "")
    for binding in payload.get("supporting_evidence") or ():
        if not isinstance(binding, dict):
            raise ValueError("supporting evidence binding must be an object")
        binding["file_sha256"] = _file_digest(_repo_path(binding.get("path")))
    payload["frozen_at"] = datetime.now(UTC).isoformat()
    payload["preflight_id"] = compute_preflight_id(payload)
    payload.pop("manifest_digest", None)
    payload["manifest_digest"] = canonical_digest(payload)
    selected_output = output.resolve()
    relative = selected_output.relative_to(ROOT).as_posix()
    if not relative.startswith((".tmp/", "docs/evidence/", "docs/reviews/evidence/")):
        raise ValueError("final preflight manifest output must be generated evidence")
    if selected_output.exists():
        raise ValueError("final preflight manifest output already exists")
    selected_output.parent.mkdir(parents=True, exist_ok=True)
    selected_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema": "zyra.phase2-final-preflight-manifest-preparation/v1",
        "ready": True,
        "target_commit": target_commit,
        "manifest": relative,
        "preflight_id": payload["preflight_id"],
        "manifest_digest": payload["manifest_digest"],
        "file_sha256": _file_digest(selected_output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-commit", required=True)
    parser.add_argument(
        "--template",
        default="config/phase2/strongest-preflight.json",
    )
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    result = prepare(
        template=(ROOT / arguments.template).resolve(),
        output=(ROOT / arguments.output).resolve(),
        target_commit=arguments.target_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
