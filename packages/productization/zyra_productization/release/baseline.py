from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import IntegrityViolation
from .integrity import sha256_file, stable_digest


BASELINE_SCHEMA = "zyra.phase2-baseline-manifest/v1"
VERIFICATION_SCHEMA = "zyra.phase2-baseline-verification/v1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class Phase2BaselineVerifier:
    """Fail-closed verifier for the immutable Phase 2 entry baseline."""

    def __init__(
        self,
        project_root: Path,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.workspace_root = (
            workspace_root.resolve()
            if workspace_root is not None
            else self.project_root.parent
        )

    def verify(
        self,
        manifest_path: Path,
        *,
        require_clean: bool = False,
    ) -> dict[str, Any]:
        manifest_path = manifest_path.resolve()
        findings: list[dict[str, Any]] = []
        manifest = self._load_mapping(manifest_path, findings)
        if manifest is None:
            return self._receipt(
                manifest_path,
                findings,
                reference_count=0,
                p2_base_commit="",
            )

        self._expect(
            manifest.get("schema") == BASELINE_SCHEMA,
            findings,
            "baseline_schema_invalid",
            expected=BASELINE_SCHEMA,
            actual=manifest.get("schema"),
        )
        unsigned = {
            key: value
            for key, value in manifest.items()
            if key != "manifest_digest"
        }
        expected_digest = str(manifest.get("manifest_digest") or "")
        actual_digest = stable_digest(unsigned)
        self._expect(
            _DIGEST.fullmatch(expected_digest) is not None
            and expected_digest == actual_digest,
            findings,
            "baseline_manifest_digest_mismatch",
            expected=expected_digest,
            actual=actual_digest,
        )

        identity = manifest.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        p2_base_commit = str(identity.get("p2_base_commit") or "")
        p2_base_tree = str(identity.get("p2_base_tree") or "")
        self._verify_git_identity(
            p2_base_commit,
            p2_base_tree,
            findings,
            require_clean=require_clean,
        )
        self._verify_lineage(
            manifest.get("lineage"),
            p2_base_commit,
            findings,
        )
        self._verify_policy(manifest, findings)

        references = manifest.get("references")
        reference_count = 0
        reference_ids: set[str] = set()
        if not isinstance(references, Sequence) or isinstance(
            references, (str, bytes)
        ):
            self._finding(
                findings,
                "baseline_references_invalid",
                actual=type(references).__name__,
            )
        else:
            for index, raw_reference in enumerate(references):
                reference_count += 1
                self._verify_reference(
                    raw_reference,
                    index=index,
                    p2_base_commit=p2_base_commit,
                    seen=reference_ids,
                    findings=findings,
                )
        self._verify_inventory(
            manifest.get("evidence_inventory"),
            reference_ids,
            findings,
        )
        return self._receipt(
            manifest_path,
            findings,
            reference_count=reference_count,
            p2_base_commit=p2_base_commit,
            manifest_digest=actual_digest,
        )

    def assert_valid(
        self,
        manifest_path: Path,
        *,
        require_clean: bool = False,
    ) -> dict[str, Any]:
        receipt = self.verify(
            manifest_path,
            require_clean=require_clean,
        )
        if not receipt["valid"]:
            raise IntegrityViolation(
                "Phase 2 baseline reconciliation failed.",
                code="phase2_baseline_reconciliation_failed",
                details=receipt,
            )
        return receipt

    def _verify_git_identity(
        self,
        commit: str,
        tree: str,
        findings: list[dict[str, Any]],
        *,
        require_clean: bool,
    ) -> None:
        if _COMMIT.fullmatch(commit) is None:
            self._finding(
                findings,
                "baseline_commit_invalid",
                commit=commit,
            )
            return
        resolved_tree = self._git(
            "rev-parse",
            f"{commit}^{{tree}}",
            findings=findings,
            code="baseline_commit_unresolvable",
        )
        if resolved_tree is None:
            return
        self._expect(
            _COMMIT.fullmatch(tree) is not None and tree == resolved_tree,
            findings,
            "baseline_tree_mismatch",
            expected=tree,
            actual=resolved_tree,
        )
        if require_clean:
            status = self._git(
                "status",
                "--porcelain",
                "--untracked-files=all",
                findings=findings,
                code="baseline_worktree_status_failed",
                strip=False,
            )
            if status is not None:
                self._expect(
                    status.strip() == "",
                    findings,
                    "baseline_worktree_dirty",
                    entries=status.splitlines(),
                )

    def _verify_lineage(
        self,
        raw_lineage: Any,
        p2_base_commit: str,
        findings: list[dict[str, Any]],
    ) -> None:
        if not isinstance(raw_lineage, Sequence) or isinstance(
            raw_lineage, (str, bytes)
        ):
            self._finding(findings, "baseline_lineage_invalid")
            return
        roles: set[str] = set()
        for index, raw in enumerate(raw_lineage):
            if not isinstance(raw, Mapping):
                self._finding(
                    findings,
                    "baseline_lineage_entry_invalid",
                    index=index,
                )
                continue
            role = str(raw.get("role") or "")
            commit = str(raw.get("commit") or "")
            self._expect(
                bool(role) and role not in roles,
                findings,
                "baseline_lineage_role_duplicate",
                role=role,
                index=index,
            )
            roles.add(role)
            if _COMMIT.fullmatch(commit) is None:
                self._finding(
                    findings,
                    "baseline_lineage_commit_invalid",
                    role=role,
                    commit=commit,
                )
                continue
            ancestor = self._git_returncode(
                "merge-base",
                "--is-ancestor",
                commit,
                p2_base_commit,
            )
            self._expect(
                ancestor == 0,
                findings,
                "baseline_lineage_not_ancestor",
                role=role,
                commit=commit,
                p2_base_commit=p2_base_commit,
                git_returncode=ancestor,
            )
        required = {
            "benchmark_implementation",
            "first_stage_report_evidence",
            "first_stage_final_freeze_evidence",
            "first_stage_report",
        }
        self._expect(
            required.issubset(roles),
            findings,
            "baseline_lineage_roles_missing",
            missing=sorted(required - roles),
        )

    def _verify_policy(
        self,
        manifest: Mapping[str, Any],
        findings: list[dict[str, Any]],
    ) -> None:
        profile = manifest.get("mechanism_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        self._expect(
            profile.get("profile_id") == "phase2_strongest_v1",
            findings,
            "baseline_mechanism_profile_invalid",
            actual=profile.get("profile_id"),
        )
        self._expect(
            profile.get("activation_state") == "baseline_frozen_not_activated",
            findings,
            "baseline_mechanism_activation_overclaim",
            actual=profile.get("activation_state"),
        )
        policy = manifest.get("training_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        self._expect(
            policy.get("training_allowed") is False
            and policy.get("evidence_reuse") == "read_only",
            findings,
            "baseline_training_policy_invalid",
            actual=dict(policy),
        )

    def _verify_reference(
        self,
        raw: Any,
        *,
        index: int,
        p2_base_commit: str,
        seen: set[str],
        findings: list[dict[str, Any]],
    ) -> None:
        if not isinstance(raw, Mapping):
            self._finding(
                findings,
                "baseline_reference_invalid",
                index=index,
            )
            return
        reference_id = str(raw.get("id") or "")
        self._expect(
            bool(reference_id) and reference_id not in seen,
            findings,
            "baseline_reference_id_duplicate",
            id=reference_id,
            index=index,
        )
        seen.add(reference_id)
        scope = str(raw.get("scope") or "")
        root = {
            "project": self.project_root,
            "workspace": self.workspace_root,
        }.get(scope)
        if root is None:
            self._finding(
                findings,
                "baseline_reference_scope_invalid",
                id=reference_id,
                scope=scope,
            )
            return
        relative = str(raw.get("path") or "").replace("\\", "/")
        path = self._safe_path(
            root,
            relative,
            reference_id=reference_id,
            findings=findings,
        )
        if path is None:
            return
        if not path.is_file() or path.is_symlink():
            self._finding(
                findings,
                "baseline_reference_file_missing",
                id=reference_id,
                path=relative,
            )
            return
        expected_sha = str(raw.get("sha256") or "")
        actual_sha = sha256_file(path)
        self._expect(
            _DIGEST.fullmatch(expected_sha) is not None
            and expected_sha == actual_sha,
            findings,
            "baseline_reference_digest_mismatch",
            id=reference_id,
            path=relative,
            expected=expected_sha,
            actual=actual_sha,
        )
        expected_size = raw.get("size_bytes")
        self._expect(
            isinstance(expected_size, int)
            and expected_size >= 0
            and expected_size == path.stat().st_size,
            findings,
            "baseline_reference_size_mismatch",
            id=reference_id,
            expected=expected_size,
            actual=path.stat().st_size,
        )
        source_commit = str(raw.get("source_commit") or "")
        if source_commit:
            self._expect(
                _COMMIT.fullmatch(source_commit) is not None
                and self._git_returncode(
                    "merge-base",
                    "--is-ancestor",
                    source_commit,
                    p2_base_commit,
                )
                == 0,
                findings,
                "baseline_reference_commit_mismatch",
                id=reference_id,
                source_commit=source_commit,
                p2_base_commit=p2_base_commit,
            )
        bindings = raw.get("json_bindings", [])
        if bindings:
            self._verify_json_bindings(
                path,
                bindings,
                reference_id=reference_id,
                findings=findings,
            )

    def _verify_json_bindings(
        self,
        path: Path,
        bindings: Any,
        *,
        reference_id: str,
        findings: list[dict[str, Any]],
    ) -> None:
        value = self._load_mapping(path, findings, reference_id=reference_id)
        if value is None:
            return
        if not isinstance(bindings, Sequence) or isinstance(
            bindings, (str, bytes)
        ):
            self._finding(
                findings,
                "baseline_reference_bindings_invalid",
                id=reference_id,
            )
            return
        for binding in bindings:
            if not isinstance(binding, Mapping):
                self._finding(
                    findings,
                    "baseline_reference_binding_invalid",
                    id=reference_id,
                )
                continue
            pointer = str(binding.get("pointer") or "")
            expected = binding.get("expected")
            try:
                actual = self._json_pointer(value, pointer)
            except (KeyError, IndexError, TypeError, ValueError) as error:
                self._finding(
                    findings,
                    "baseline_reference_binding_missing",
                    id=reference_id,
                    pointer=pointer,
                    error=str(error),
                )
                continue
            self._expect(
                actual == expected,
                findings,
                "baseline_reference_binding_mismatch",
                id=reference_id,
                pointer=pointer,
                expected=expected,
                actual=actual,
            )

    def _verify_inventory(
        self,
        raw_inventory: Any,
        reference_ids: set[str],
        findings: list[dict[str, Any]],
    ) -> None:
        inventory = raw_inventory if isinstance(raw_inventory, Mapping) else {}
        self._expect(
            inventory.get("classification") == "read_only_evidence_inventory"
            and inventory.get("training_eligible") is False,
            findings,
            "baseline_evidence_inventory_classification_invalid",
            actual=dict(inventory),
        )
        ids = inventory.get("reference_ids")
        ids = (
            {str(item) for item in ids}
            if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes))
            else set()
        )
        self._expect(
            ids == reference_ids,
            findings,
            "baseline_evidence_inventory_reference_mismatch",
            missing=sorted(reference_ids - ids),
            extra=sorted(ids - reference_ids),
        )
        volume = inventory.get("evidence_volume")
        volume = volume if isinstance(volume, Mapping) else {}
        self._expect(
            volume.get("semantic_label") == "evidence_volume_only"
            and volume.get("training_sample_count") == 0,
            findings,
            "baseline_evidence_volume_overclaim",
            actual=dict(volume),
        )

    def _safe_path(
        self,
        root: Path,
        relative: str,
        *,
        reference_id: str,
        findings: list[dict[str, Any]],
    ) -> Path | None:
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or ":" in relative
            or any(part == ".." for part in candidate.parts)
        ):
            self._finding(
                findings,
                "baseline_reference_path_invalid",
                id=reference_id,
                path=relative,
            )
            return None
        path = (root / candidate).resolve()
        if not path.is_relative_to(root):
            self._finding(
                findings,
                "baseline_reference_path_escape",
                id=reference_id,
                path=relative,
            )
            return None
        return path

    @staticmethod
    def _json_pointer(value: Any, pointer: str) -> Any:
        if pointer == "":
            return value
        if not pointer.startswith("/"):
            raise ValueError("JSON pointer must start with '/'.")
        current = value
        for raw_part in pointer[1:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping):
                current = current[part]
            elif isinstance(current, Sequence) and not isinstance(
                current, (str, bytes)
            ):
                current = current[int(part)]
            else:
                raise TypeError("JSON pointer traverses a scalar value.")
        return current

    def _load_mapping(
        self,
        path: Path,
        findings: list[dict[str, Any]],
        *,
        reference_id: str = "manifest",
    ) -> Mapping[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            self._finding(
                findings,
                "baseline_json_unreadable",
                id=reference_id,
                path=str(path),
                error=str(error),
            )
            return None
        if not isinstance(value, Mapping):
            self._finding(
                findings,
                "baseline_json_root_invalid",
                id=reference_id,
                path=str(path),
            )
            return None
        return value

    def _git(
        self,
        *arguments: str,
        findings: list[dict[str, Any]],
        code: str,
        strip: bool = True,
    ) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.project_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._finding(findings, code, error=str(error))
            return None
        if completed.returncode != 0:
            self._finding(
                findings,
                code,
                returncode=completed.returncode,
                stderr=completed.stderr.strip(),
            )
            return None
        return completed.stdout.strip() if strip else completed.stdout

    def _git_returncode(self, *arguments: str) -> int:
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=self.project_root,
                check=False,
                capture_output=True,
                timeout=20,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return -1

    @staticmethod
    def _expect(
        condition: bool,
        findings: list[dict[str, Any]],
        code: str,
        **details: Any,
    ) -> None:
        if not condition:
            Phase2BaselineVerifier._finding(findings, code, **details)

    @staticmethod
    def _finding(
        findings: list[dict[str, Any]],
        code: str,
        **details: Any,
    ) -> None:
        findings.append(
            {
                "severity": "blocker",
                "code": code,
                "details": details,
            }
        )

    @staticmethod
    def _receipt(
        manifest_path: Path,
        findings: list[dict[str, Any]],
        *,
        reference_count: int,
        p2_base_commit: str,
        manifest_digest: str = "",
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schema": VERIFICATION_SCHEMA,
            "valid": not findings,
            "manifest": manifest_path.name,
            "manifest_digest": manifest_digest,
            "p2_base_commit": p2_base_commit,
            "reference_count": reference_count,
            "finding_count": len(findings),
            "findings": findings,
        }
        receipt["verification_digest"] = stable_digest(receipt)
        return receipt


__all__ = [
    "BASELINE_SCHEMA",
    "Phase2BaselineVerifier",
    "VERIFICATION_SCHEMA",
]
