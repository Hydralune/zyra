from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactRef, to_jsonable
from zyra_runtime.artifacts import (
    ARTIFACT_CONTRACT,
    DEFAULT_READ_CHUNK_BYTES,
    MAX_READ_CHUNK_BYTES,
    SAFE_INLINE_MEDIA_TYPES,
    ArtifactByteRange,
    ArtifactIntegrityError,
    ArtifactObservedContent,
    ArtifactRangeError,
    ArtifactRevisionError,
    LocalArtifactStore,
    compare_artifact_integrity,
    expected_artifact_integrity,
    normalize_download_policy,
    normalize_security_label,
    normalize_trust_disposition,
)


ARTIFACT_CATALOG_CONTRACT = "zyra.artifact-catalog.v2"
ARTIFACT_READ_CONTRACT = "zyra.artifact-read.v2"
ARTIFACT_RECEIPT_CONTRACT = "zyra.artifact-read-receipt.v1"
MAX_CATALOG_LIMIT = 500
DEFAULT_CATALOG_LIMIT = 100
MAX_FILTER_VALUES = 128
MAX_CURSOR_BYTES = 4096

_TOKEN_PATTERN = re.compile(
    r"(?i)(?P<label>api[_-]?key|access[_-]?token|secret|password|passwd|authorization)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
    r"(?P<value>bearer\s+)?(?P<secret>[^\s,;\"']{6,})"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    r".*?"
    r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    re.DOTALL,
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(?P<name>[A-Z][A-Z0-9_]{2,}(?:TOKEN|SECRET|PASSWORD|KEY))"
    r"(?P<separator>\s*=\s*)"
    r"(?P<value>.+)$"
)
_PROMPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b.{0,48}\b(previous|prior|system|developer|instructions?)\b"
        ),
    ),
    (
        "authority_impersonation",
        re.compile(
            r"(?im)^\s*(system|developer|assistant|tool)\s*(message|instruction)?\s*:"
        ),
    ),
    (
        "tool_invocation_request",
        re.compile(
            r"(?i)\b(run|execute|call|invoke|use)\b.{0,32}\b(tool|command|shell|terminal|powershell|bash)\b"
        ),
    ),
    (
        "secret_exfiltration_request",
        re.compile(
            r"(?i)\b(reveal|print|send|upload|exfiltrate|show)\b.{0,48}\b(secret|token|credential|password|environment)\b"
        ),
    ),
    (
        "permission_bypass_request",
        re.compile(
            r"(?i)\b(bypass|disable|skip|ignore)\b.{0,40}\b(permission|approval|policy|sandbox|guard)\b"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ArtifactCatalogQuery:
    task_id: str
    node_ids: tuple[str, ...] = ()
    worker_ids: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    content_families: tuple[str, ...] = ()
    revisions: tuple[str, ...] = ()
    created_after: str | None = None
    created_before: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_CATALOG_LIMIT
    include_deleted: bool = False

    @classmethod
    def from_query(
        cls,
        *,
        task_id: str,
        query: Mapping[str, Sequence[str]],
    ) -> "ArtifactCatalogQuery":
        return cls(
            task_id=_identity(task_id, "task"),
            node_ids=_query_values(query, "node_id"),
            worker_ids=_query_values(query, "worker_id"),
            media_types=tuple(value.lower() for value in _query_values(query, "media_type")),
            content_families=tuple(value.lower() for value in _query_values(query, "content_family")),
            revisions=_query_values(query, "revision"),
            created_after=_query_single(query, "created_after"),
            created_before=_query_single(query, "created_before"),
            cursor=_query_single(query, "cursor"),
            limit=_bounded_int(
                _query_single(query, "limit"),
                default=DEFAULT_CATALOG_LIMIT,
                minimum=1,
                maximum=MAX_CATALOG_LIMIT,
            ),
            include_deleted=_query_bool(query, "include_deleted"),
        )

    def filter_fingerprint(self) -> str:
        payload = {
            "task_id": self.task_id,
            "node_ids": list(self.node_ids),
            "worker_ids": list(self.worker_ids),
            "media_types": list(self.media_types),
            "content_families": list(self.content_families),
            "revisions": list(self.revisions),
            "created_after": self.created_after,
            "created_before": self.created_before,
            "include_deleted": self.include_deleted,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactReadQuery:
    task_id: str
    artifact_id: str
    expected_revision: str | None = None
    offset: int = 0
    length: int = DEFAULT_READ_CHUNK_BYTES
    purpose: str = "preview"

    @classmethod
    def from_query(
        cls,
        *,
        task_id: str,
        artifact_id: str,
        query: Mapping[str, Sequence[str]],
    ) -> "ArtifactReadQuery":
        purpose = str(_query_single(query, "purpose") or "preview").strip().lower()
        if purpose not in {"preview", "search", "media", "download", "metadata"}:
            raise ArtifactApiError(400, "invalid_read_purpose", "Artifact read purpose is not supported.")
        return cls(
            task_id=_identity(task_id, "task"),
            artifact_id=_identity(artifact_id, "artifact"),
            expected_revision=_query_single(query, "revision"),
            offset=_bounded_int(
                _query_single(query, "offset"),
                default=0,
                minimum=0,
                maximum=2**63 - 1,
            ),
            length=_bounded_int(
                _query_single(query, "length"),
                default=DEFAULT_READ_CHUNK_BYTES,
                minimum=1,
                maximum=MAX_READ_CHUNK_BYTES,
            ),
            purpose=purpose,
        )


@dataclass(frozen=True, slots=True)
class RedactionFinding:
    kind: str
    start: int
    end: int
    digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class PromptFinding:
    kind: str
    start: int
    end: int
    digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class ArtifactContentPolicy:
    security_label: str
    trust_disposition: str
    download_policy: str
    allow_inline: bool
    allow_download: bool
    quarantine: bool
    refusal_code: str | None = None
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "security_label": self.security_label,
            "trust_disposition": self.trust_disposition,
            "download_policy": self.download_policy,
            "allow_inline": self.allow_inline,
            "allow_download": self.allow_download,
            "quarantine": self.quarantine,
            "refusal_code": self.refusal_code,
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class ArtifactReadAudit:
    maximum: int = 16_384
    _entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "schema": ARTIFACT_RECEIPT_CONTRACT,
            "sequence": len(self._entries) + 1,
            "occurred_at": datetime.now(UTC).isoformat(),
            **dict(entry),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["receipt_digest"] = hashlib.sha256(encoded).hexdigest()
        self._entries.append(payload)
        if len(self._entries) > max(1, self.maximum):
            del self._entries[: len(self._entries) - self.maximum]
        return dict(payload)

    def entries(
        self,
        *,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            dict(entry)
            for entry in self._entries
            if (task_id is None or entry.get("task_id") == task_id)
            and (artifact_id is None or entry.get("artifact_id") == artifact_id)
        ]


class ArtifactApiError(ValueError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def response(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class ArtifactCatalogService:
    def __init__(
        self,
        *,
        store: LocalArtifactStore,
        cursor_secret: bytes | None = None,
        audit: ArtifactReadAudit | None = None,
    ) -> None:
        self.store = store
        self.cursor_secret = cursor_secret or hashlib.sha256(str(store.root).encode("utf-8")).digest()
        self.audit = audit or ArtifactReadAudit()

    def catalog(
        self,
        *,
        artifacts: Iterable[ArtifactRef],
        query: ArtifactCatalogQuery,
    ) -> dict[str, Any]:
        entries = [
            self.contract(artifact, verify=False)
            for artifact in artifacts
            if self._matches(artifact, query)
        ]
        entries.sort(key=_catalog_sort_key, reverse=True)
        start = self._cursor_start(query, entries)
        page = entries[start : start + query.limit]
        next_cursor = None
        if start + len(page) < len(entries) and page:
            next_cursor = self._encode_cursor(
                {
                    "schema": "zyra.artifact-cursor.v1",
                    "fingerprint": query.filter_fingerprint(),
                    "position": start + len(page),
                    "last_key": list(_catalog_sort_key(page[-1])),
                }
            )
        return {
            "schema": ARTIFACT_CATALOG_CONTRACT,
            "task_id": query.task_id,
            "state_owner": "TaskStore.ArtifactRef + LocalArtifactStore",
            "artifact_root_disclosed": False,
            "total": len(entries),
            "returned": len(page),
            "cursor": next_cursor,
            "filters": {
                "node_ids": list(query.node_ids),
                "worker_ids": list(query.worker_ids),
                "media_types": list(query.media_types),
                "content_families": list(query.content_families),
                "revisions": list(query.revisions),
                "created_after": query.created_after,
                "created_before": query.created_before,
                "include_deleted": query.include_deleted,
            },
            "artifacts": page,
        }

    def contract(
        self,
        artifact: ArtifactRef,
        *,
        verify: bool,
    ) -> dict[str, Any]:
        try:
            description = self.store.describe(artifact, verify=verify)
        except ValueError as error:
            description = {
                "exists": False,
                "is_file": False,
                "size_bytes": 0,
                "content_type": "application/octet-stream",
                "content_family": "binary",
                "integrity": "path_refused",
                "error": str(error),
            }
        metadata = dict(artifact.metadata)
        expected = expected_artifact_integrity(artifact)
        if (
            bool(description.get("exists"))
            and bool(description.get("is_file"))
            and (
                not expected["revision"]
                or not expected["sha256"]
                or expected["size_bytes"] is None
            )
        ):
            description = self.store.describe(artifact, verify=True)
        security = normalize_security_label(metadata.get("security_label"))
        trust = normalize_trust_disposition(metadata.get("trust_disposition"))
        download = normalize_download_policy(metadata.get("download_policy"), security_label=security)
        observed_revision = description.get("observed_revision")
        revision = expected["revision"] or observed_revision
        content_type = str(metadata.get("media_type") or description.get("content_type") or "application/octet-stream")
        content_family = str(metadata.get("content_family") or description.get("content_family") or "binary")
        return {
            "schema": ARTIFACT_CONTRACT,
            "artifact_id": artifact.artifact_id,
            "kind": _enum_value(artifact.kind),
            "title": artifact.title,
            "created_at": artifact.created_at,
            "immutable": True,
            "revision": revision,
            "sha256": expected["sha256"] or description.get("observed_sha256"),
            "size_bytes": expected["size_bytes"] if expected["size_bytes"] is not None else description.get("size_bytes", 0),
            "media_type": content_type,
            "content_family": content_family,
            "encoding": metadata.get("encoding"),
            "byte_order_mark": metadata.get("byte_order_mark"),
            "line_endings": list(metadata.get("line_endings") or []),
            "producer": {
                "node_id": artifact.producer_node_id or metadata.get("producer_node_id"),
                "span_id": metadata.get("producer_span_id"),
                "tool_call_id": metadata.get("producer_tool_call_id"),
                "worker_id": metadata.get("producer_worker_id"),
            },
            "security": {
                "label": security,
                "trust": trust,
                "download_policy": download,
            },
            "retention": {
                "policy": str(metadata.get("retention_policy") or "task"),
                "expires_at": metadata.get("retention_expires_at"),
            },
            "status": {
                "exists": bool(description.get("exists")),
                "is_file": bool(description.get("is_file")),
                "integrity": str(description.get("integrity") or "unverified"),
                "inline_safe": bool(metadata.get("inline_safe", False)),
                "executable_risk": bool(metadata.get("executable_risk", False)),
                "legacy_metadata": str(metadata.get("contract") or "") != ARTIFACT_CONTRACT,
                "error": description.get("error"),
            },
            "links": {
                "metadata": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}",
                "content": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}/content",
                "download": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}/download",
            },
        }

    def metadata(
        self,
        *,
        task_id: str,
        artifact: ArtifactRef,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        contract = self.contract(artifact, verify=True)
        revision = str(contract.get("revision") or "")
        if expected_revision and expected_revision != revision:
            raise ArtifactApiError(
                409,
                "artifact_revision_mismatch",
                "Requested artifact revision does not match canonical content.",
                details={"requested_revision": expected_revision, "canonical_revision": revision},
            )
        policy = self._policy(artifact, contract=contract, purpose="metadata")
        receipt = self.audit.append(
            {
                "task_id": task_id,
                "artifact_id": artifact.artifact_id,
                "revision": revision,
                "purpose": "metadata",
                "decision": "allow",
                "range": None,
                "transformations": [],
            }
        )
        return {
            "schema": ARTIFACT_READ_CONTRACT,
            "task_id": task_id,
            "artifact": contract,
            "policy": policy.to_json(),
            "receipt": receipt,
        }

    def read(
        self,
        *,
        artifact: ArtifactRef,
        query: ArtifactReadQuery,
    ) -> dict[str, Any]:
        started = time.perf_counter_ns()
        contract = self.contract(artifact, verify=False)
        policy = self._policy(artifact, contract=contract, purpose=query.purpose)
        if policy.refusal_code:
            receipt = self.audit.append(
                {
                    "task_id": query.task_id,
                    "artifact_id": query.artifact_id,
                    "revision": query.expected_revision,
                    "purpose": query.purpose,
                    "decision": "deny",
                    "reason": policy.refusal_code,
                    "range": {"offset": query.offset, "length": query.length},
                    "transformations": [],
                }
            )
            raise ArtifactApiError(
                403,
                policy.refusal_code,
                "Artifact content policy denied this read.",
                details={"policy": policy.to_json(), "receipt": receipt},
            )
        try:
            observed, selected = self.store.read_range(
                artifact,
                offset=query.offset,
                length=query.length,
                expected_revision=query.expected_revision,
            )
        except FileNotFoundError as error:
            raise ArtifactApiError(404, "artifact_content_missing", str(error)) from error
        except ArtifactRevisionError as error:
            raise ArtifactApiError(409, "artifact_revision_mismatch", str(error)) from error
        except ArtifactIntegrityError as error:
            raise ArtifactApiError(409, "artifact_integrity_failed", str(error)) from error
        except ArtifactRangeError as error:
            raise ArtifactApiError(416, "artifact_range_invalid", str(error)) from error
        except ValueError as error:
            raise ArtifactApiError(403, "artifact_path_refused", str(error)) from error
        verified_contract = self.contract_from_observed(artifact, observed)
        policy = self._policy(artifact, contract=verified_contract, purpose=query.purpose)
        content = selected.content
        transformations: list[str] = []
        redactions: list[RedactionFinding] = []
        prompts: list[PromptFinding] = []
        text: str | None = None
        text_encoding: str | None = observed.encoding
        decode_status = "binary"
        if observed.text_candidate and observed.encoding:
            text, decode_status = decode_bounded_text(
                content,
                encoding=observed.encoding,
                starts_at_zero=selected.offset == 0,
                complete=selected.complete,
            )
            if text is not None:
                text, redactions = redact_server_secrets(text)
                if redactions:
                    transformations.append("server_secret_redaction")
                prompts = find_prompt_injection(text)
                if prompts:
                    transformations.append("prompt_quarantine")
        if policy.quarantine and "trust_quarantine" not in transformations:
            transformations.append("trust_quarantine")
        encoded = base64.b64encode(content).decode("ascii") if text is None else None
        receipt = self.audit.append(
            {
                "task_id": query.task_id,
                "artifact_id": query.artifact_id,
                "revision": observed.revision,
                "sha256": observed.sha256,
                "purpose": query.purpose,
                "decision": "allow",
                "range": {
                    "offset": selected.offset,
                    "length": len(selected.content),
                    "requested_length": selected.length,
                    "end_exclusive": selected.end_exclusive,
                    "total_bytes": selected.total_bytes,
                    "complete": selected.complete,
                },
                "transformations": transformations,
                "elapsed_us": max(0, (time.perf_counter_ns() - started) // 1000),
            }
        )
        return {
            "schema": ARTIFACT_READ_CONTRACT,
            "task_id": query.task_id,
            "artifact": verified_contract,
            "policy": policy.to_json(),
            "range": {
                "offset": selected.offset,
                "length": len(selected.content),
                "requested_length": selected.length,
                "end_exclusive": selected.end_exclusive,
                "total_bytes": selected.total_bytes,
                "complete": selected.complete,
            },
            "content": {
                "text": text,
                "base64": encoded,
                "encoding": text_encoding,
                "decode_status": decode_status,
                "server_redacted": bool(redactions),
                "redactions": [finding.to_json() for finding in redactions],
                "prompt_findings": [finding.to_json() for finding in prompts],
                "quarantined": policy.quarantine or bool(prompts),
            },
            "receipt": receipt,
        }

    def download(
        self,
        *,
        artifact: ArtifactRef,
        query: ArtifactReadQuery,
    ) -> tuple[ArtifactObservedContent, ArtifactByteRange, dict[str, Any]]:
        contract = self.contract(artifact, verify=False)
        policy = self._policy(artifact, contract=contract, purpose="download")
        if not policy.allow_download or policy.refusal_code:
            receipt = self.audit.append(
                {
                    "task_id": query.task_id,
                    "artifact_id": query.artifact_id,
                    "revision": query.expected_revision,
                    "purpose": "download",
                    "decision": "deny",
                    "reason": policy.refusal_code or "artifact_download_denied",
                    "range": {"offset": query.offset, "length": query.length},
                    "transformations": [],
                }
            )
            raise ArtifactApiError(
                403,
                policy.refusal_code or "artifact_download_denied",
                "Artifact download policy denied this request.",
                details={"policy": policy.to_json(), "receipt": receipt},
            )
        try:
            observed, selected = self.store.read_range(
                artifact,
                offset=query.offset,
                length=query.length,
                expected_revision=query.expected_revision,
            )
        except FileNotFoundError as error:
            raise ArtifactApiError(404, "artifact_content_missing", str(error)) from error
        except ArtifactRevisionError as error:
            raise ArtifactApiError(409, "artifact_revision_mismatch", str(error)) from error
        except ArtifactIntegrityError as error:
            raise ArtifactApiError(409, "artifact_integrity_failed", str(error)) from error
        except ArtifactRangeError as error:
            raise ArtifactApiError(416, "artifact_range_invalid", str(error)) from error
        receipt = self.audit.append(
            {
                "task_id": query.task_id,
                "artifact_id": query.artifact_id,
                "revision": observed.revision,
                "sha256": observed.sha256,
                "purpose": "download",
                "decision": "allow",
                "range": {
                    "offset": selected.offset,
                    "length": len(selected.content),
                    "end_exclusive": selected.end_exclusive,
                    "total_bytes": selected.total_bytes,
                    "complete": selected.complete,
                },
                "transformations": [],
            }
        )
        return observed, selected, receipt

    def contract_from_observed(
        self,
        artifact: ArtifactRef,
        observed: ArtifactObservedContent,
    ) -> dict[str, Any]:
        metadata = dict(artifact.metadata)
        integrity = compare_artifact_integrity(artifact, observed)
        security = normalize_security_label(metadata.get("security_label"))
        trust = normalize_trust_disposition(metadata.get("trust_disposition"))
        download = normalize_download_policy(metadata.get("download_policy"), security_label=security)
        return {
            "schema": ARTIFACT_CONTRACT,
            "artifact_id": artifact.artifact_id,
            "kind": _enum_value(artifact.kind),
            "title": artifact.title,
            "created_at": artifact.created_at,
            "immutable": True,
            "revision": observed.revision,
            "sha256": observed.sha256,
            "size_bytes": observed.size_bytes,
            "media_type": observed.content_type,
            "content_family": observed.content_family,
            "encoding": observed.encoding,
            "byte_order_mark": observed.byte_order_mark,
            "line_endings": list(observed.line_endings),
            "producer": {
                "node_id": artifact.producer_node_id or metadata.get("producer_node_id"),
                "span_id": metadata.get("producer_span_id"),
                "tool_call_id": metadata.get("producer_tool_call_id"),
                "worker_id": metadata.get("producer_worker_id"),
            },
            "security": {
                "label": security,
                "trust": trust,
                "download_policy": download,
            },
            "retention": {
                "policy": str(metadata.get("retention_policy") or "task"),
                "expires_at": metadata.get("retention_expires_at"),
            },
            "status": {
                "exists": True,
                "is_file": True,
                "integrity": integrity,
                "inline_safe": observed.inline_safe,
                "executable_risk": observed.executable_risk,
                "legacy_metadata": str(metadata.get("contract") or "") != ARTIFACT_CONTRACT,
                "error": None,
            },
            "links": {
                "metadata": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}",
                "content": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}/content",
                "download": f"/tasks/{{task_id}}/artifacts/{artifact.artifact_id}/download",
            },
        }

    def _policy(
        self,
        artifact: ArtifactRef,
        *,
        contract: Mapping[str, Any],
        purpose: str,
    ) -> ArtifactContentPolicy:
        metadata = dict(artifact.metadata)
        security = normalize_security_label(metadata.get("security_label"))
        trust = normalize_trust_disposition(metadata.get("trust_disposition"))
        download = normalize_download_policy(metadata.get("download_policy"), security_label=security)
        status = contract.get("status") if isinstance(contract.get("status"), Mapping) else {}
        family = str(contract.get("content_family") or "binary").lower()
        media = str(contract.get("media_type") or "application/octet-stream").lower()
        executable = bool(status.get("executable_risk")) or family in {
            "executable",
            "html",
            "svg",
            "archive",
        }
        reasons: list[str] = []
        refusal: str | None = None
        if security == "secret":
            refusal = "artifact_secret_refused"
            reasons.append("secret security label")
        if purpose == "download" and download == "deny":
            refusal = refusal or "artifact_download_denied"
            reasons.append("download policy denies access")
        if purpose == "media" and media not in SAFE_INLINE_MEDIA_TYPES:
            refusal = refusal or "artifact_media_not_inline_safe"
            reasons.append("media type is not inline allowlisted")
        if executable and purpose in {"preview", "search", "media", "download"}:
            refusal = refusal or "artifact_executable_content_refused"
            reasons.append("executable or active content")
        allow_inline = (
            refusal is None
            and (
                family in {"text", "markdown", "json"}
                or media in SAFE_INLINE_MEDIA_TYPES
                or family == "binary"
            )
        )
        allow_download = refusal is None and download in {"allow", "confirm"} and not executable
        quarantine = trust != "trusted" or family in {"html", "svg"}
        return ArtifactContentPolicy(
            security_label=security,
            trust_disposition=trust,
            download_policy=download,
            allow_inline=allow_inline,
            allow_download=allow_download,
            quarantine=quarantine,
            refusal_code=refusal,
            reasons=tuple(reasons),
        )

    def _matches(
        self,
        artifact: ArtifactRef,
        query: ArtifactCatalogQuery,
    ) -> bool:
        metadata = artifact.metadata
        if not query.include_deleted and bool(metadata.get("deleted", False)):
            return False
        producer_node = str(artifact.producer_node_id or metadata.get("producer_node_id") or "")
        producer_worker = str(metadata.get("producer_worker_id") or "")
        media_type = str(metadata.get("media_type") or "").lower()
        family = str(metadata.get("content_family") or "").lower()
        revision = str(metadata.get("revision") or "")
        if query.node_ids and producer_node not in query.node_ids:
            return False
        if query.worker_ids and producer_worker not in query.worker_ids:
            return False
        if query.media_types and media_type not in query.media_types:
            return False
        if query.content_families and family not in query.content_families:
            return False
        if query.revisions and revision not in query.revisions:
            return False
        created = _timestamp(artifact.created_at)
        if query.created_after and created < _timestamp(query.created_after):
            return False
        if query.created_before and created > _timestamp(query.created_before):
            return False
        return True

    def _cursor_start(
        self,
        query: ArtifactCatalogQuery,
        entries: Sequence[Mapping[str, Any]],
    ) -> int:
        if not query.cursor:
            return 0
        payload = self._decode_cursor(query.cursor)
        if payload.get("fingerprint") != query.filter_fingerprint():
            raise ArtifactApiError(
                400,
                "artifact_cursor_filter_mismatch",
                "Artifact cursor does not match the current filters.",
            )
        position = _bounded_int(
            payload.get("position"),
            default=-1,
            minimum=-1,
            maximum=max(0, len(entries)),
        )
        if position < 0:
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor position is invalid.")
        return position

    def _encode_cursor(self, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.cursor_secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, value: str) -> dict[str, Any]:
        rendered = str(value or "").strip()
        if not rendered or len(rendered.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor is invalid.")
        padding = "=" * (-len(rendered) % 4)
        try:
            packed = base64.urlsafe_b64decode(rendered + padding)
        except Exception as error:
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor cannot be decoded.") from error
        if len(packed) <= hashlib.sha256().digest_size:
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor is incomplete.")
        raw = packed[: -hashlib.sha256().digest_size]
        signature = packed[-hashlib.sha256().digest_size :]
        expected = hmac.new(self.cursor_secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor signature is invalid.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor payload is invalid.") from error
        if not isinstance(payload, dict) or payload.get("schema") != "zyra.artifact-cursor.v1":
            raise ArtifactApiError(400, "artifact_cursor_invalid", "Artifact cursor schema is invalid.")
        return payload


def find_task_artifact(
    artifacts: Iterable[ArtifactRef],
    artifact_id: str,
) -> ArtifactRef | None:
    selected = str(artifact_id or "").strip()
    for artifact in artifacts:
        if artifact.artifact_id == selected:
            return artifact
    return None


def decode_bounded_text(
    content: bytes,
    *,
    encoding: str,
    starts_at_zero: bool,
    complete: bool,
) -> tuple[str | None, str]:
    candidates = [content]
    if not starts_at_zero:
        maximum_prefix = min(4, len(content))
        candidates = [content[prefix:] for prefix in range(maximum_prefix + 1)]
    for index, candidate in enumerate(candidates):
        try:
            text = candidate.decode(encoding, errors="strict")
            if index:
                return text, "aligned_partial_prefix"
            return text, "complete" if complete else "partial"
        except UnicodeDecodeError as error:
            if not complete and error.end == len(candidate):
                tail = candidate[: error.start]
                try:
                    text = tail.decode(encoding, errors="strict")
                    return text, "partial_trailing_codepoint"
                except UnicodeDecodeError:
                    pass
    return None, "invalid_encoding"


def redact_server_secrets(text: str) -> tuple[str, list[RedactionFinding]]:
    findings: list[RedactionFinding] = []

    def token_replacement(match: re.Match[str]) -> str:
        secret = match.group("secret")
        findings.append(
            RedactionFinding(
                kind="credential",
                start=match.start("secret"),
                end=match.end("secret"),
                digest=_finding_digest(secret),
            )
        )
        prefix = f"{match.group('label')}{match.group('separator')}{match.group('value') or ''}"
        return f"{prefix}[REDACTED:{_finding_digest(secret)[:12]}]"

    def private_key_replacement(match: re.Match[str]) -> str:
        secret = match.group(0)
        findings.append(
            RedactionFinding(
                kind="private_key",
                start=match.start(),
                end=match.end(),
                digest=_finding_digest(secret),
            )
        )
        return f"[REDACTED_PRIVATE_KEY:{_finding_digest(secret)[:12]}]"

    def assignment_replacement(match: re.Match[str]) -> str:
        value = match.group("value")
        findings.append(
            RedactionFinding(
                kind="credential_assignment",
                start=match.start("value"),
                end=match.end("value"),
                digest=_finding_digest(value),
            )
        )
        return f"{match.group('name')}{match.group('separator')}[REDACTED:{_finding_digest(value)[:12]}]"

    result = _PRIVATE_KEY_PATTERN.sub(private_key_replacement, text)
    result = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(assignment_replacement, result)
    result = _TOKEN_PATTERN.sub(token_replacement, result)
    return result, findings


def find_prompt_injection(text: str) -> list[PromptFinding]:
    findings: list[PromptFinding] = []
    for kind, pattern in _PROMPT_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                PromptFinding(
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    digest=_finding_digest(match.group(0)),
                )
            )
            if len(findings) >= 256:
                return findings
    findings.sort(key=lambda item: (item.start, item.end, item.kind))
    return findings


def _finding_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _catalog_sort_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("created_at") or ""),
        str(entry.get("artifact_id") or ""),
        str(entry.get("revision") or ""),
    )


def _timestamp(value: Any) -> float:
    rendered = str(value or "").strip()
    if not rendered:
        return 0.0
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _query_values(
    query: Mapping[str, Sequence[str]],
    name: str,
) -> tuple[str, ...]:
    result: list[str] = []
    for raw in query.get(name, ()):
        for value in str(raw).split(","):
            selected = value.strip()
            if selected and selected not in result:
                result.append(selected)
            if len(result) > MAX_FILTER_VALUES:
                raise ArtifactApiError(
                    400,
                    "artifact_filter_too_large",
                    f"Artifact filter {name} exceeds {MAX_FILTER_VALUES} values.",
                )
    return tuple(result)


def _query_single(
    query: Mapping[str, Sequence[str]],
    name: str,
) -> str | None:
    values = query.get(name)
    if not values:
        return None
    selected = str(values[0] or "").strip()
    return selected or None


def _query_bool(
    query: Mapping[str, Sequence[str]],
    name: str,
) -> bool:
    selected = str(_query_single(query, name) or "").lower()
    return selected in {"1", "true", "yes", "on"}


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError):
        selected = default
    return max(minimum, min(maximum, selected))


def _identity(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected or len(selected.encode("utf-8")) > 512:
        raise ArtifactApiError(400, f"invalid_{label}_identity", f"{label.title()} identity is invalid.")
    if any(character in selected for character in "\r\n\x00"):
        raise ArtifactApiError(400, f"invalid_{label}_identity", f"{label.title()} identity is invalid.")
    return selected


def _enum_value(value: Any) -> str:
    selected = getattr(value, "value", value)
    return str(selected)
