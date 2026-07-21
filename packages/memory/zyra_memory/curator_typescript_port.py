from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .curator_models import MemoryCandidate, stable_id


CURATOR_STATE_PROTOCOL = "zyra.memory-curator-state.v1"


class TypeScriptCuratorStateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TypeScriptConsolidationReceipt:
    request_id: str
    active_candidate_ids: tuple[str, ...]
    suppressed_candidate_ids: tuple[str, ...]
    relations: tuple[Mapping[str, Any], ...]
    diagnostics: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "active_candidate_ids": list(self.active_candidate_ids),
            "suppressed_candidate_ids": list(self.suppressed_candidate_ids),
            "relations": [dict(item) for item in self.relations],
            "diagnostics": dict(self.diagnostics),
        }


class TypeScriptCuratorStatePort:
    """Language-neutral request/response boundary to the retained OMP mechanism.

    The TypeScript runtime decides only candidate collision/job-state projections.
    It receives no SQLite path and has no canonical write capability.  Python
    validators and MemoryCommitRuntime re-check and persist any returned relation.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        bun_executable: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.entrypoint = (
            self.project_root
            / "packages"
            / "memory"
            / "curator-state-machine"
            / "src"
            / "main.ts"
        ).resolve()
        workspace_bun = self.project_root / "node_modules" / ".bin" / (
            "bun.exe" if __import__("os").name == "nt" else "bun"
        )
        self.bun_executable = (
            bun_executable
            or shutil.which("bun")
            or (str(workspace_bun) if workspace_bun.is_file() else "")
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    @property
    def available(self) -> bool:
        return bool(self.bun_executable and self.entrypoint.is_file())

    def health(self) -> Mapping[str, Any]:
        return {
            "protocol": CURATOR_STATE_PROTOCOL,
            "available": self.available,
            "bun_configured": bool(self.bun_executable),
            "entrypoint_exists": self.entrypoint.is_file(),
            "entrypoint": self._relative_entrypoint(),
            "canonical_write_capability": False,
            "database_path_shared": False,
            "source_mechanism": "oh-my-pi ownership-token/watermark/consolidation",
        }

    def consolidate(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        prior_candidates: Sequence[MemoryCandidate],
    ) -> TypeScriptConsolidationReceipt:
        request_id = stable_id(
            "ts_curator_request",
            "consolidate_candidates",
            *[item.candidate_id for item in candidates],
            *[item.candidate_id for item in prior_candidates],
        )
        response = self._invoke(
            {
                "protocol": CURATOR_STATE_PROTOCOL,
                "requestId": request_id,
                "operation": "consolidate_candidates",
                "payload": {
                    "candidates": [self._candidate_envelope(item) for item in candidates],
                    "priorCandidates": [
                        self._candidate_envelope(item) for item in prior_candidates
                    ],
                },
            }
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_invalid",
                "TypeScript curator response result must be an object",
            )
        active = self._string_tuple(result.get("activeCandidateIds"))
        suppressed = self._string_tuple(result.get("suppressedCandidateIds"))
        relation_values = result.get("relations")
        if not isinstance(relation_values, list):
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_invalid",
                "TypeScript curator relations must be an array",
            )
        relations = tuple(
            dict(item) for item in relation_values if isinstance(item, Mapping)
        )
        diagnostics = (
            dict(result.get("diagnostics"))
            if isinstance(result.get("diagnostics"), Mapping)
            else {}
        )
        expected_ids = {item.candidate_id for item in candidates}
        returned_ids = set(active).union(suppressed)
        if returned_ids != expected_ids:
            raise TypeScriptCuratorStateError(
                "typescript_curator_candidate_partition_invalid",
                "TypeScript curator did not return an exact candidate partition",
            )
        if set(active).intersection(suppressed):
            raise TypeScriptCuratorStateError(
                "typescript_curator_candidate_partition_invalid",
                "TypeScript curator candidate partition overlaps",
            )
        for relation in relations:
            source = str(relation.get("sourceCandidateId") or "")
            target = str(relation.get("targetCandidateId") or "")
            if source not in expected_ids:
                raise TypeScriptCuratorStateError(
                    "typescript_curator_relation_invalid",
                    "TypeScript relation source is not in the current batch",
                )
            if not target:
                raise TypeScriptCuratorStateError(
                    "typescript_curator_relation_invalid",
                    "TypeScript relation target is missing",
                )
        return TypeScriptConsolidationReceipt(
            request_id=request_id,
            active_candidate_ids=active,
            suppressed_candidate_ids=suppressed,
            relations=relations,
            diagnostics=diagnostics,
        )

    def validate_candidate(self, candidate: MemoryCandidate) -> Mapping[str, Any]:
        request_id = stable_id("ts_curator_request", "validate_candidate", candidate.candidate_id)
        response = self._invoke(
            {
                "protocol": CURATOR_STATE_PROTOCOL,
                "requestId": request_id,
                "operation": "validate_candidate",
                "payload": {"candidate": self._candidate_envelope(candidate)},
            }
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_invalid",
                "TypeScript candidate validation result must be an object",
            )
        return dict(result)

    def _invoke(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.bun_executable:
            raise TypeScriptCuratorStateError(
                "typescript_curator_bun_unavailable",
                "Bun executable is required for the TypeScript curator state runtime",
            )
        if not self.entrypoint.is_file():
            raise TypeScriptCuratorStateError(
                "typescript_curator_entrypoint_missing",
                f"TypeScript curator entrypoint is missing: {self._relative_entrypoint()}",
            )
        command = [self.bun_executable, str(self.entrypoint)]
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False, sort_keys=True),
                text=True,
                capture_output=True,
                cwd=self.project_root,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TypeScriptCuratorStateError(
                "typescript_curator_timeout",
                "TypeScript curator did not respond before the deadline",
            ) from error
        except OSError as error:
            raise TypeScriptCuratorStateError(
                "typescript_curator_spawn_failed",
                str(error),
            ) from error
        stdout = completed.stdout.strip()
        if not stdout:
            raise TypeScriptCuratorStateError(
                "typescript_curator_empty_response",
                completed.stderr.strip()[:1000] or "TypeScript curator returned no response",
            )
        try:
            response = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError as error:
            raise TypeScriptCuratorStateError(
                "typescript_curator_invalid_json",
                stdout[-1000:],
            ) from error
        if not isinstance(response, Mapping):
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_invalid",
                "TypeScript curator response must be an object",
            )
        if response.get("protocol") != CURATOR_STATE_PROTOCOL:
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_mismatch",
                "TypeScript curator response protocol mismatch",
            )
        if response.get("requestId") != request.get("requestId"):
            raise TypeScriptCuratorStateError(
                "typescript_curator_request_mismatch",
                "TypeScript curator response request id mismatch",
            )
        if response.get("ok") is not True:
            raise TypeScriptCuratorStateError(
                "typescript_curator_rejected",
                str(response.get("error") or completed.stderr or "TypeScript curator rejected request")[:1000],
            )
        return dict(response)

    @staticmethod
    def _candidate_envelope(candidate: MemoryCandidate) -> Mapping[str, Any]:
        return {
            "candidateId": candidate.candidate_id,
            "runId": candidate.run_id,
            "taskId": candidate.task_id,
            "kind": candidate.kind.value,
            "layer": candidate.proposed_layer,
            "scope": candidate.scope.value,
            "subject": candidate.subject,
            "summary": candidate.summary,
            "content": dict(candidate.content),
            "evidenceDigest": candidate.evidence_digest,
            "evidenceIds": list(candidate.evidence_ids),
            "confidence": candidate.confidence,
            "idempotencyKey": candidate.idempotency_key,
            "semanticDigest": candidate.semantic_digest,
            "state": candidate.state.value,
            "expectedMemoryId": candidate.expected_memory_id,
            "expectedRevision": candidate.expected_revision,
            "createdAt": candidate.created_at,
        }

    @staticmethod
    def _string_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise TypeScriptCuratorStateError(
                "typescript_curator_protocol_invalid",
                "TypeScript candidate partition must be an array",
            )
        return tuple(str(item) for item in value if str(item))

    def _relative_entrypoint(self) -> str:
        try:
            return self.entrypoint.relative_to(self.project_root).as_posix()
        except ValueError:
            return str(self.entrypoint)


__all__ = [
    "CURATOR_STATE_PROTOCOL",
    "TypeScriptConsolidationReceipt",
    "TypeScriptCuratorStateError",
    "TypeScriptCuratorStatePort",
]
