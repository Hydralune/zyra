from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import DoctorBlocked
from .models import digest, now_iso


class DeploymentEvidenceGate:
    def __init__(
        self,
        project_root: Path | str,
        *,
        evidence_path: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.evidence_path = Path(
            evidence_path
            or self.project_root
            / "docs"
            / "reviews"
            / "evidence"
            / "M3-S02B-01"
            / "verification-summary.json"
        ).resolve()

    def current_commit(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    def verify(
        self,
        *,
        expected_commit: str | None = None,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        if not self.evidence_path.is_file():
            blockers.append("deployment_evidence_missing")
            evidence: dict[str, Any] = {}
        else:
            try:
                value = json.loads(self.evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                blockers.append("deployment_evidence_invalid_json")
                evidence = {"error": f"{type(error).__name__}: {error}"}
            else:
                evidence = dict(value) if isinstance(value, Mapping) else {}
                if not evidence:
                    blockers.append("deployment_evidence_invalid_shape")
        commit = self.current_commit()
        required_commit = expected_commit or commit
        if evidence:
            if str(evidence.get("target_commit") or "") != required_commit:
                blockers.append("deployment_evidence_revision_mismatch")
            if evidence.get("verdict") != "PASS":
                blockers.append("deployment_evidence_verdict_not_pass")
            if evidence.get("semantic_health", {}).get("ready") is not True:
                blockers.append("deployment_semantic_health_not_ready")
            profiles = evidence.get("deployment_profiles")
            if not isinstance(profiles, Mapping):
                blockers.append("deployment_profile_evidence_missing")
            else:
                expected_profiles = {"device", "edge", "cloud"}
                if set(profiles) != expected_profiles:
                    blockers.append("deployment_profile_set_invalid")
                for profile in expected_profiles:
                    item = profiles.get(profile)
                    if not isinstance(item, Mapping):
                        blockers.append(f"deployment_profile_missing:{profile}")
                        continue
                    if int(item.get("pid") or 0) <= 0:
                        blockers.append(f"deployment_profile_pid_missing:{profile}")
                    if not item.get("node_id") or not item.get("endpoint"):
                        blockers.append(f"deployment_profile_identity_missing:{profile}")
                    if item.get("in_process") is True:
                        blockers.append(f"deployment_profile_in_process:{profile}")
            if evidence.get("placement", {}).get("binding_verified") is not True:
                blockers.append("deployment_placement_binding_not_verified")
            failure = evidence.get("failure_migration")
            if not isinstance(failure, Mapping):
                blockers.append("deployment_failure_migration_missing")
            else:
                if failure.get("migrated") is not True:
                    blockers.append("deployment_failure_not_migrated")
                if failure.get("checkpoint_handoff_verified") is not True:
                    blockers.append("deployment_checkpoint_handoff_not_verified")
            short_task = evidence.get("short_task")
            if not isinstance(short_task, Mapping) or short_task.get("accepted") is not True:
                blockers.append("deployment_short_task_not_accepted")
            accounting = evidence.get("effective_code")
            if not isinstance(accounting, Mapping):
                blockers.append("deployment_effective_code_missing")
            elif int(accounting.get("conservative_production_lines") or 0) < 6000:
                blockers.append("deployment_effective_code_below_minimum")
            if evidence.get("source_boundary", {}).get("ready") is not True:
                blockers.append("deployment_source_boundary_not_ready")
            if evidence.get("secret_leak_count") != 0:
                blockers.append("deployment_secret_leak_detected")
            stored_digest = str(evidence.get("evidence_digest") or "")
            semantic = {
                key: value for key, value in evidence.items()
                if key != "evidence_digest"
            }
            if not stored_digest or digest(semantic) != stored_digest:
                blockers.append("deployment_evidence_digest_invalid")
        result = {
            "schema": "zyra.deployment-freeze-admission/v1",
            "ready": not blockers,
            "target_commit": required_commit,
            "current_commit": commit,
            "evidence_path": str(self.evidence_path),
            "blockers": blockers,
            "verified_at": now_iso(),
            "fallback": False,
        }
        result["verification_digest"] = digest(result)
        return result

    def require(
        self,
        *,
        expected_commit: str | None = None,
    ) -> dict[str, Any]:
        result = self.verify(expected_commit=expected_commit)
        if result.get("ready") is not True:
            raise DoctorBlocked(
                "deployment_evidence_gate_blocked",
                "M3 deployment evidence gate is blocked",
                operation="evidence_gate",
                details=result,
            )
        return result


__all__ = ["DeploymentEvidenceGate"]
