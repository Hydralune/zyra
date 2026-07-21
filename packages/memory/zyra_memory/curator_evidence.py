from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef, now_iso

from .curator_models import (
    EXTRACTOR_VERSION,
    CuratorEvidenceRef,
    EvidenceBundle,
    EvidenceDocument,
    EvidenceKind,
    TrustTier,
    canonical_json,
    stable_digest,
    stable_id,
    unique_strings,
)
from .curator_store import CuratorCandidateStore


MAX_EVENT_CHARS = 40_000
MAX_ARTIFACT_CHARS = 80_000
MAX_BUNDLE_DOCUMENTS = 10_000
MAX_NESTING_DEPTH = 12
MAX_COLLECTION_ITEMS = 500


TOOL_EVENT_TYPES = {
    "mcp_tool_result",
    "command_succeeded",
    "command_failed",
    "agent_message",
}


BROWSER_EVENT_PREFIXES = (
    "browser_",
    "runtime.browser.",
)


CODE_EVENT_TYPES = {
    "command_succeeded",
    "command_failed",
    "artifact_written",
    "evaluation",
}


VERIFIED_RUNTIME_EVENT_TYPES = {
    "task_created",
    "node_created",
    "node_updated",
    "artifact_written",
    "constraint_check",
    "topology_route",
    "resource_decision",
    "recovery_planned",
    "worker_health",
    "evaluation",
    "budget_updated",
    "command_validated",
    "command_started",
    "command_succeeded",
    "command_failed",
    "subagent_completed",
    "subagent_failed",
    "browser_session_lifecycle",
    "browser_runtime_diagnostic",
}


USER_EVENT_TYPES = {
    "requirement_change",
    "side_question",
    "prompt_queue_updated",
}


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "provider_token",
        re.compile(
            r"\b(?:(?:sk|pk|rk)[-_][A-Za-z0-9]{12,}"
            r"|(?:tok|key|secret|token|password)[-_][A-Za-z0-9]{12,})\b"
        ),
    ),
    (
        "jwt",
        re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "github_pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "npm_token",
        re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]{0,20000}?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    (
        "authorization_header",
        re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    ),
    (
        "password_assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[=:]\s*['\"]?[^\s'\"]{8,}"),
    ),
)


SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|credential)"
)


CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ArtifactReader(Protocol):
    def read_preview(self, artifact: ArtifactRef, *, max_chars: int = 20_000) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SecretMatch:
    kind: str
    fingerprint: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    fingerprints: tuple[str, ...]
    match_count: int
    redacted_paths: tuple[str, ...]

    @property
    def redacted(self) -> bool:
        return self.match_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "fingerprints": list(self.fingerprints),
            "match_count": self.match_count,
            "redacted_paths": list(self.redacted_paths),
            "redacted": self.redacted,
        }


class SecretRedactor:
    """Deterministic pre-persistence redaction with non-reversible fingerprints."""

    def __init__(self, *, replacement: str = "[REDACTED]") -> None:
        self.replacement = replacement

    def redact_text(self, text: str) -> tuple[str, tuple[SecretMatch, ...]]:
        output = CONTROL_CHAR_PATTERN.sub(" ", str(text))
        matches: list[SecretMatch] = []
        for kind, pattern in SECRET_PATTERNS:
            def replacement(match: re.Match[str]) -> str:
                raw = match.group(0)
                fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
                matches.append(
                    SecretMatch(
                        kind=kind,
                        fingerprint=f"{kind}:{fingerprint}",
                        start=match.start(),
                        end=match.end(),
                    )
                )
                return self.replacement

            output = pattern.sub(replacement, output)
        return output, tuple(matches)

    def redact_value(self, value: Any, *, path: str = "$", depth: int = 0) -> RedactionResult:
        if depth > MAX_NESTING_DEPTH:
            return RedactionResult(
                value="[TRUNCATED_NESTING]",
                fingerprints=(),
                match_count=0,
                redacted_paths=(),
            )
        if isinstance(value, str):
            redacted, matches = self.redact_text(value)
            return RedactionResult(
                value=redacted,
                fingerprints=unique_strings([match.fingerprint for match in matches]),
                match_count=len(matches),
                redacted_paths=(path,) if matches else (),
            )
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            fingerprints: list[str] = []
            redacted_paths: list[str] = []
            match_count = 0
            items = sorted(value.items(), key=lambda item: str(item[0]))[:MAX_COLLECTION_ITEMS]
            for raw_key, raw_item in items:
                key = str(raw_key)
                child_path = f"{path}.{key}"
                if SENSITIVE_KEY_PATTERN.search(key):
                    raw_text = canonical_json(raw_item)
                    fingerprint = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:20]
                    output[key] = self.replacement
                    fingerprints.append(f"sensitive_key:{fingerprint}")
                    redacted_paths.append(child_path)
                    match_count += 1
                    continue
                child = self.redact_value(raw_item, path=child_path, depth=depth + 1)
                output[key] = child.value
                fingerprints.extend(child.fingerprints)
                redacted_paths.extend(child.redacted_paths)
                match_count += child.match_count
            if len(value) > MAX_COLLECTION_ITEMS:
                output["__truncated_items__"] = len(value) - MAX_COLLECTION_ITEMS
            return RedactionResult(
                value=output,
                fingerprints=unique_strings(fingerprints),
                match_count=match_count,
                redacted_paths=unique_strings(redacted_paths),
            )
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            output_items: list[Any] = []
            fingerprints: list[str] = []
            redacted_paths: list[str] = []
            match_count = 0
            for index, raw_item in enumerate(value[:MAX_COLLECTION_ITEMS]):
                child = self.redact_value(
                    raw_item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                )
                output_items.append(child.value)
                fingerprints.extend(child.fingerprints)
                redacted_paths.extend(child.redacted_paths)
                match_count += child.match_count
            if len(value) > MAX_COLLECTION_ITEMS:
                output_items.append({"__truncated_items__": len(value) - MAX_COLLECTION_ITEMS})
            return RedactionResult(
                value=output_items,
                fingerprints=unique_strings(fingerprints),
                match_count=match_count,
                redacted_paths=unique_strings(redacted_paths),
            )
        if isinstance(value, (bytes, bytearray)):
            fingerprint = hashlib.sha256(bytes(value)).hexdigest()[:20]
            return RedactionResult(
                value=f"[BINARY:{len(value)}]",
                fingerprints=(f"binary:{fingerprint}",),
                match_count=0,
                redacted_paths=(),
            )
        if value is None or isinstance(value, (bool, int, float)):
            return RedactionResult(value=value, fingerprints=(), match_count=0, redacted_paths=())
        return self.redact_value(str(value), path=path, depth=depth + 1)


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostics:
    event_count: int
    artifact_count: int
    tool_result_count: int
    browser_trace_count: int
    code_trace_count: int
    redacted_document_count: int
    secret_match_count: int
    missing_artifact_ids: tuple[str, ...]
    skipped_event_ids: tuple[str, ...]
    evidence_kind_counts: Mapping[str, int]
    extractor_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "artifact_count": self.artifact_count,
            "tool_result_count": self.tool_result_count,
            "browser_trace_count": self.browser_trace_count,
            "code_trace_count": self.code_trace_count,
            "redacted_document_count": self.redacted_document_count,
            "secret_match_count": self.secret_match_count,
            "missing_artifact_ids": list(self.missing_artifact_ids),
            "skipped_event_ids": list(self.skipped_event_ids),
            "evidence_kind_counts": dict(self.evidence_kind_counts),
            "extractor_version": self.extractor_version,
        }


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    bundle: EvidenceBundle
    documents: tuple[EvidenceDocument, ...]
    diagnostics: ExtractionDiagnostics

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        return {
            "bundle": self.bundle.to_dict(),
            "documents": [item.to_dict(include_text=include_text) for item in self.documents],
            "diagnostics": self.diagnostics.to_dict(),
        }


class EventArtifactTraceExtractor:
    """Create immutable task-bound evidence from live events and artifacts.

    The extractor follows Hermes' protected-trajectory principle but produces
    Zyra evidence documents rather than a prompt transcript.  Tool, browser and
    code traces receive additional typed projections while retaining the
    original event identity and content digest.
    """

    def __init__(
        self,
        *,
        store: CuratorCandidateStore,
        artifact_reader: ArtifactReader | None = None,
        redactor: SecretRedactor | None = None,
        max_event_chars: int = MAX_EVENT_CHARS,
        max_artifact_chars: int = MAX_ARTIFACT_CHARS,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        self.store = store
        self.artifact_reader = artifact_reader
        self.redactor = redactor or SecretRedactor()
        self.max_event_chars = max(1000, int(max_event_chars))
        self.max_artifact_chars = max(1000, int(max_artifact_chars))
        self.extractor_version = extractor_version

    def extract(
        self,
        *,
        run_id: str,
        task_id: str,
        events: Sequence[Mapping[str, Any]],
        artifacts: Sequence[ArtifactRef | Mapping[str, Any]] = (),
        start_sequence: int = 0,
        end_sequence: int | None = None,
    ) -> ExtractionResult:
        if not run_id.strip() or not task_id.strip():
            raise ValueError("extractor requires run/task identity")
        if start_sequence < 0:
            raise ValueError("start_sequence must be non-negative")
        if end_sequence is not None and end_sequence < start_sequence:
            raise ValueError("end_sequence precedes start_sequence")
        selected_events = self._select_events(
            events,
            run_id=run_id,
            task_id=task_id,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
        )
        documents: list[EvidenceDocument] = []
        skipped_event_ids: list[str] = []
        referenced_artifact_ids: list[str] = []
        for sequence, event in selected_events:
            try:
                event_documents = self._event_documents(
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    event=event,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                skipped_event_ids.append(str(event.get("event_id") or f"sequence:{sequence}"))
                continue
            documents.extend(event_documents)
            referenced_artifact_ids.extend(self._artifact_ids(event))
        artifact_map = self._artifact_map(artifacts)
        missing_artifact_ids: list[str] = []
        artifact_sequence = (
            selected_events[-1][0] + 1
            if selected_events
            else max(0, start_sequence)
        )
        for offset, artifact_id in enumerate(unique_strings(referenced_artifact_ids)):
            artifact = artifact_map.get(artifact_id)
            if artifact is None:
                missing_artifact_ids.append(artifact_id)
                continue
            document = self._artifact_document(
                run_id=run_id,
                task_id=task_id,
                sequence=artifact_sequence + offset,
                artifact=artifact,
            )
            if document is None:
                missing_artifact_ids.append(artifact_id)
                continue
            documents.append(document)
        for artifact_id, artifact in sorted(artifact_map.items()):
            if artifact_id in referenced_artifact_ids:
                continue
            if not self._artifact_should_be_included(artifact):
                continue
            document = self._artifact_document(
                run_id=run_id,
                task_id=task_id,
                sequence=artifact_sequence + len(documents),
                artifact=artifact,
            )
            if document is not None:
                documents.append(document)
        documents = self._deduplicate_documents(documents)
        if not documents:
            raise ValueError("no admissible evidence was extracted")
        if len(documents) > MAX_BUNDLE_DOCUMENTS:
            documents = documents[:MAX_BUNDLE_DOCUMENTS]
        self.store.save_evidence_documents(documents)
        bundle = EvidenceBundle.build(
            run_id=run_id,
            task_id=task_id,
            evidence_refs=[document.ref for document in documents],
            extractor_version=self.extractor_version,
            metadata={
                "source": "event_artifact_trace",
                "missing_artifact_ids": sorted(set(missing_artifact_ids)),
                "skipped_event_ids": sorted(set(skipped_event_ids)),
            },
        )
        self.store.save_bundle(bundle)
        kinds = Counter(document.ref.kind.value for document in documents)
        diagnostics = ExtractionDiagnostics(
            event_count=kinds[EvidenceKind.EVENT.value],
            artifact_count=kinds[EvidenceKind.ARTIFACT.value],
            tool_result_count=kinds[EvidenceKind.TOOL_RESULT.value],
            browser_trace_count=kinds[EvidenceKind.BROWSER_TRACE.value],
            code_trace_count=kinds[EvidenceKind.CODE_TRACE.value],
            redacted_document_count=sum(1 for item in documents if item.redacted),
            secret_match_count=sum(len(item.secret_fingerprints) for item in documents),
            missing_artifact_ids=unique_strings(missing_artifact_ids),
            skipped_event_ids=unique_strings(skipped_event_ids),
            evidence_kind_counts=dict(sorted(kinds.items())),
            extractor_version=self.extractor_version,
        )
        return ExtractionResult(bundle=bundle, documents=tuple(documents), diagnostics=diagnostics)

    def verify_document(self, document: EvidenceDocument) -> tuple[bool, str]:
        try:
            document.validated()
        except ValueError as error:
            return False, str(error)
        stored = self.store.evidence_document(document.ref.evidence_id)
        if stored is None:
            return False, "evidence_missing"
        if stored.ref.content_digest != document.ref.content_digest:
            return False, "evidence_digest_mismatch"
        if stored.ref.identity_projection() != document.ref.identity_projection():
            return False, "evidence_identity_mismatch"
        if stable_digest(stored.normalized) != stored.ref.content_digest:
            return False, "stored_evidence_corrupted"
        return True, "ok"

    def verify_bundle(self, bundle: EvidenceBundle) -> tuple[bool, tuple[str, ...]]:
        issues: list[str] = []
        try:
            bundle.validated()
        except ValueError as error:
            issues.append(str(error))
            return False, tuple(issues)
        stored = self.store.bundle(bundle.bundle_id)
        if stored is None:
            issues.append("bundle_missing")
            return False, tuple(issues)
        if stored.bundle_digest != bundle.bundle_digest:
            issues.append("bundle_digest_mismatch")
        if stored.projection() != bundle.projection():
            issues.append("bundle_projection_mismatch")
        for ref in bundle.evidence_refs:
            document = self.store.evidence_document(ref.evidence_id)
            if document is None:
                issues.append(f"evidence_missing:{ref.evidence_id}")
                continue
            if document.ref.content_digest != ref.content_digest:
                issues.append(f"evidence_digest_mismatch:{ref.evidence_id}")
        return not issues, tuple(issues)

    def _select_events(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        task_id: str,
        start_sequence: int,
        end_sequence: int | None,
    ) -> list[tuple[int, Mapping[str, Any]]]:
        output: list[tuple[int, Mapping[str, Any]]] = []
        for sequence, event in enumerate(events):
            if sequence < start_sequence:
                continue
            if end_sequence is not None and sequence > end_sequence:
                continue
            event_run = str(event.get("run_id") or run_id)
            event_task = str(event.get("task_id") or task_id)
            if event_run != run_id or event_task != task_id:
                continue
            output.append((sequence, event))
        return output

    def _event_documents(
        self,
        *,
        run_id: str,
        task_id: str,
        sequence: int,
        event: Mapping[str, Any],
    ) -> tuple[EvidenceDocument, ...]:
        event_id = str(event.get("event_id") or stable_id("event", run_id, task_id, sequence, event))
        event_type = self._event_type(event)
        created_at = str(event.get("created_at") or now_iso())
        node_id = str(event.get("node_id") or "")
        payload = self._payload(event)
        normalized = {
            "event_id": event_id,
            "event_type": event_type,
            "run_id": run_id,
            "task_id": task_id,
            "node_id": node_id,
            "created_at": created_at,
            "payload": self._bounded_value(payload),
        }
        documents: list[EvidenceDocument] = [
            self._build_document(
                kind=EvidenceKind.EVENT,
                run_id=run_id,
                task_id=task_id,
                sequence=sequence,
                source_id=event_id,
                source_revision=stable_digest(normalized),
                event_id=event_id,
                node_id=node_id,
                producer=self._producer(payload),
                trust=self._event_trust(event_type, payload),
                title=f"event:{event_type}:{event_id}",
                normalized=normalized,
                metadata={
                    "event_type": event_type,
                    "payload_keys": sorted(str(key) for key in payload),
                },
                max_chars=self.max_event_chars,
            )
        ]
        tool_projection = self._tool_projection(event_type, payload)
        if tool_projection is not None:
            documents.append(
                self._build_document(
                    kind=EvidenceKind.TOOL_RESULT,
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    source_id=f"{event_id}:tool",
                    source_revision=stable_digest(tool_projection),
                    event_id=event_id,
                    node_id=node_id,
                    producer=str(tool_projection.get("tool_name") or self._producer(payload)),
                    trust=TrustTier.TOOL,
                    title=f"tool-result:{tool_projection.get('tool_name') or 'unknown'}:{event_id}",
                    normalized=tool_projection,
                    metadata={"event_type": event_type, "projection": "tool_result"},
                    max_chars=self.max_event_chars,
                )
            )
        browser_projection = self._browser_projection(event_type, payload)
        if browser_projection is not None:
            documents.append(
                self._build_document(
                    kind=EvidenceKind.BROWSER_TRACE,
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    source_id=f"{event_id}:browser",
                    source_revision=stable_digest(browser_projection),
                    event_id=event_id,
                    node_id=node_id,
                    producer=self._producer(payload) or "browser-runtime",
                    trust=TrustTier.VERIFIED_RUNTIME,
                    title=f"browser-trace:{event_type}:{event_id}",
                    normalized=browser_projection,
                    metadata={"event_type": event_type, "projection": "browser_trace"},
                    max_chars=self.max_event_chars,
                )
            )
        code_projection = self._code_projection(event_type, payload)
        if code_projection is not None:
            documents.append(
                self._build_document(
                    kind=EvidenceKind.CODE_TRACE,
                    run_id=run_id,
                    task_id=task_id,
                    sequence=sequence,
                    source_id=f"{event_id}:code",
                    source_revision=stable_digest(code_projection),
                    event_id=event_id,
                    node_id=node_id,
                    producer=self._producer(payload) or "code-runtime",
                    trust=TrustTier.VERIFIED_RUNTIME,
                    title=f"code-trace:{event_type}:{event_id}",
                    normalized=code_projection,
                    metadata={"event_type": event_type, "projection": "code_trace"},
                    max_chars=self.max_event_chars,
                )
            )
        return tuple(documents)

    def _artifact_document(
        self,
        *,
        run_id: str,
        task_id: str,
        sequence: int,
        artifact: ArtifactRef,
    ) -> EvidenceDocument | None:
        if self.artifact_reader is None:
            return None
        preview = self.artifact_reader.read_preview(artifact, max_chars=self.max_artifact_chars)
        if str(preview.get("error") or ""):
            return None
        content = preview.get("content")
        if content is None and bool(preview.get("binary", False)):
            normalized = {
                "artifact_id": artifact.artifact_id,
                "kind": str(artifact.kind),
                "title": artifact.title,
                "producer_node_id": artifact.producer_node_id,
                "created_at": artifact.created_at,
                "binary": True,
                "size_bytes": int(preview.get("size_bytes", 0)),
                "content_type": str(preview.get("content_type") or "application/octet-stream"),
                "metadata": self._bounded_value(artifact.metadata),
            }
        else:
            normalized = {
                "artifact_id": artifact.artifact_id,
                "kind": str(artifact.kind),
                "title": artifact.title,
                "producer_node_id": artifact.producer_node_id,
                "created_at": artifact.created_at,
                "binary": False,
                "content": str(content or ""),
                "truncated": bool(preview.get("truncated", False)),
                "size_bytes": int(preview.get("size_bytes", 0)),
                "content_type": str(preview.get("content_type") or "text/plain"),
                "metadata": self._bounded_value(artifact.metadata),
            }
        return self._build_document(
            kind=EvidenceKind.ARTIFACT,
            run_id=run_id,
            task_id=task_id,
            sequence=sequence,
            source_id=artifact.artifact_id,
            source_revision=stable_digest(normalized),
            artifact_id=artifact.artifact_id,
            node_id=artifact.producer_node_id or "",
            producer="artifact-store",
            trust=TrustTier.VERIFIED_RUNTIME,
            title=f"artifact:{artifact.title or artifact.artifact_id}",
            normalized=normalized,
            media_type=str(preview.get("content_type") or "application/octet-stream"),
            metadata={
                "artifact_kind": str(artifact.kind),
                "binary": bool(normalized.get("binary")),
                "truncated": bool(normalized.get("truncated", False)),
            },
            max_chars=self.max_artifact_chars,
        )

    def _build_document(
        self,
        *,
        kind: EvidenceKind,
        run_id: str,
        task_id: str,
        sequence: int,
        source_id: str,
        source_revision: str,
        trust: TrustTier,
        title: str,
        normalized: Mapping[str, Any],
        artifact_id: str = "",
        event_id: str = "",
        node_id: str = "",
        producer: str = "",
        media_type: str = "application/json",
        metadata: Mapping[str, Any] | None = None,
        max_chars: int,
    ) -> EvidenceDocument:
        redacted = self.redactor.redact_value(normalized)
        safe_normalized = redacted.value if isinstance(redacted.value, Mapping) else {"value": redacted.value}
        content_digest = stable_digest(safe_normalized)
        evidence_id = stable_id(
            "evidence",
            run_id,
            task_id,
            kind.value,
            source_id,
            source_revision,
            content_digest,
        )
        text = self._document_text(safe_normalized, max_chars=max_chars)
        ref = CuratorEvidenceRef(
            evidence_id=evidence_id,
            kind=kind,
            run_id=run_id,
            task_id=task_id,
            source_id=source_id,
            source_revision=source_revision,
            content_digest=content_digest,
            trust=trust,
            sequence=sequence,
            created_at=now_iso(),
            artifact_id=artifact_id,
            event_id=event_id,
            node_id=node_id,
            producer=producer,
            media_type=media_type,
            char_count=len(text),
            metadata={
                **dict(metadata or {}),
                "redacted_paths": list(redacted.redacted_paths),
                "secret_match_count": redacted.match_count,
                "extractor_version": self.extractor_version,
            },
        ).validated()
        return EvidenceDocument(
            ref=ref,
            title=title,
            text=text,
            normalized=safe_normalized,
            redacted=redacted.redacted,
            secret_fingerprints=redacted.fingerprints,
        ).validated()

    @staticmethod
    def _event_type(event: Mapping[str, Any]) -> str:
        value = event.get("event_type")
        if hasattr(value, "value"):
            value = value.value
        return str(value or "unknown")

    @staticmethod
    def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
        value = event.get("payload")
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _producer(payload: Mapping[str, Any]) -> str:
        for key in ("worker", "worker_id", "runtime", "provider", "tool_name", "source"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = payload.get("metadata")
        if isinstance(nested, Mapping):
            return EventArtifactTraceExtractor._producer(nested)
        return ""

    @staticmethod
    def _event_trust(event_type: str, payload: Mapping[str, Any]) -> TrustTier:
        explicit = str(payload.get("trust") or payload.get("trust_tier") or "")
        if explicit in {item.value for item in TrustTier}:
            return TrustTier(explicit)
        if event_type in VERIFIED_RUNTIME_EVENT_TYPES:
            return TrustTier.VERIFIED_RUNTIME
        if event_type in USER_EVENT_TYPES:
            return TrustTier.USER
        if event_type in TOOL_EVENT_TYPES:
            return TrustTier.TOOL
        if str(payload.get("source") or "").casefold() in {"model", "llm", "assistant"}:
            return TrustTier.MODEL_PROPOSAL
        return TrustTier.UNKNOWN

    @staticmethod
    def _tool_projection(event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        tool_result = payload.get("tool_result")
        tool_call = payload.get("tool_call")
        if not isinstance(tool_result, Mapping) and not isinstance(tool_call, Mapping):
            if event_type not in TOOL_EVENT_TYPES:
                return None
            if not any(key in payload for key in ("tool_name", "command", "exit_code", "stdout", "stderr")):
                return None
        result = dict(tool_result) if isinstance(tool_result, Mapping) else {}
        call = dict(tool_call) if isinstance(tool_call, Mapping) else {}
        return {
            "event_type": event_type,
            "tool_name": str(
                result.get("tool_name")
                or call.get("tool_name")
                or payload.get("tool_name")
                or payload.get("command_name")
                or ""
            ),
            "call_id": str(
                result.get("tool_call_id")
                or call.get("tool_call_id")
                or payload.get("tool_call_id")
                or payload.get("request_id")
                or ""
            ),
            "arguments": call.get("arguments", payload.get("arguments", {})),
            "status": str(result.get("status") or payload.get("status") or event_type),
            "exit_code": result.get("exit_code", payload.get("exit_code")),
            "output": result.get("output", payload.get("output", payload.get("stdout", ""))),
            "error": result.get("error", payload.get("error", payload.get("stderr", ""))),
            "duration_ms": result.get("duration_ms", payload.get("duration_ms")),
            "artifact_ids": EventArtifactTraceExtractor._artifact_ids_from_payload(payload),
        }

    @staticmethod
    def _browser_projection(event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        browser_keys = {
            "browser_session_id",
            "target_id",
            "page_id",
            "url",
            "navigation",
            "screenshot",
            "selector",
            "dom_snapshot",
            "browser_action",
            "watchdog",
        }
        if not event_type.startswith(BROWSER_EVENT_PREFIXES) and not browser_keys.intersection(payload):
            return None
        action = payload.get("browser_action")
        action_map = dict(action) if isinstance(action, Mapping) else {}
        return {
            "event_type": event_type,
            "session_id": str(payload.get("browser_session_id") or payload.get("session_id") or ""),
            "target_id": str(payload.get("target_id") or payload.get("page_id") or ""),
            "url": str(payload.get("url") or action_map.get("url") or ""),
            "action": str(action_map.get("name") or payload.get("action") or ""),
            "selector": str(action_map.get("selector") or payload.get("selector") or ""),
            "status": str(payload.get("status") or payload.get("result") or ""),
            "diagnostic": payload.get("diagnostic", payload.get("watchdog", {})),
            "navigation": payload.get("navigation", {}),
            "artifact_ids": EventArtifactTraceExtractor._artifact_ids_from_payload(payload),
            "causal": {
                "request_id": str(payload.get("request_id") or ""),
                "action_id": str(payload.get("action_id") or ""),
                "continuation_id": str(payload.get("continuation_id") or ""),
            },
        }

    @staticmethod
    def _code_projection(event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        code_keys = {
            "patch",
            "diff",
            "file_path",
            "files_changed",
            "test_result",
            "verification",
            "workspace_transaction_id",
            "command",
        }
        if event_type not in CODE_EVENT_TYPES and not code_keys.intersection(payload):
            return None
        if not code_keys.intersection(payload):
            return None
        return {
            "event_type": event_type,
            "workspace_id": str(payload.get("workspace_id") or ""),
            "transaction_id": str(payload.get("workspace_transaction_id") or payload.get("transaction_id") or ""),
            "command": str(payload.get("command") or payload.get("command_name") or ""),
            "status": str(payload.get("status") or event_type),
            "file_path": str(payload.get("file_path") or payload.get("path") or ""),
            "files_changed": payload.get("files_changed", ()),
            "patch": payload.get("patch", payload.get("diff", "")),
            "test_result": payload.get("test_result", payload.get("verification", {})),
            "exit_code": payload.get("exit_code"),
            "artifact_ids": EventArtifactTraceExtractor._artifact_ids_from_payload(payload),
        }

    @staticmethod
    def _artifact_ids(event: Mapping[str, Any]) -> tuple[str, ...]:
        payload = EventArtifactTraceExtractor._payload(event)
        return EventArtifactTraceExtractor._artifact_ids_from_payload(payload)

    @staticmethod
    def _artifact_ids_from_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
        values: list[object] = []
        for key in ("artifact_id", "screenshot_artifact_id", "trace_artifact_id"):
            if payload.get(key):
                values.append(payload[key])
        for key in ("artifact_ids", "artifacts", "evidence_artifact_ids"):
            raw = payload.get(key)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                for item in raw:
                    if isinstance(item, Mapping):
                        values.append(item.get("artifact_id") or "")
                    else:
                        values.append(item)
        return unique_strings(values)

    @staticmethod
    def _artifact_map(
        artifacts: Sequence[ArtifactRef | Mapping[str, Any]],
    ) -> dict[str, ArtifactRef]:
        output: dict[str, ArtifactRef] = {}
        for value in artifacts:
            if isinstance(value, ArtifactRef):
                artifact = value
            elif isinstance(value, Mapping):
                kind_raw = str(value.get("kind") or ArtifactKind.FILE.value)
                try:
                    kind = ArtifactKind(kind_raw)
                except ValueError:
                    kind = ArtifactKind.FILE
                artifact = ArtifactRef(
                    artifact_id=str(value.get("artifact_id") or ""),
                    kind=kind,
                    uri=str(value.get("uri") or ""),
                    title=str(value.get("title") or ""),
                    producer_node_id=(
                        str(value["producer_node_id"])
                        if value.get("producer_node_id") is not None
                        else None
                    ),
                    created_at=str(value.get("created_at") or now_iso()),
                    metadata=dict(value.get("metadata") or {}),
                )
            else:
                continue
            if artifact.artifact_id:
                output[artifact.artifact_id] = artifact
        return output

    @staticmethod
    def _artifact_should_be_included(artifact: ArtifactRef) -> bool:
        return artifact.kind in {
            ArtifactKind.CODE,
            ArtifactKind.REPORT,
            ArtifactKind.TRACE,
            ArtifactKind.STRUCTURED_DATA,
            ArtifactKind.MARKDOWN,
        }

    @staticmethod
    def _bounded_value(value: Any, *, depth: int = 0) -> Any:
        if depth > MAX_NESTING_DEPTH:
            return "[TRUNCATED_NESTING]"
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            items = sorted(value.items(), key=lambda item: str(item[0]))[:MAX_COLLECTION_ITEMS]
            for key, item in items:
                output[str(key)] = EventArtifactTraceExtractor._bounded_value(item, depth=depth + 1)
            if len(value) > MAX_COLLECTION_ITEMS:
                output["__truncated_items__"] = len(value) - MAX_COLLECTION_ITEMS
            return output
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            output = [
                EventArtifactTraceExtractor._bounded_value(item, depth=depth + 1)
                for item in value[:MAX_COLLECTION_ITEMS]
            ]
            if len(value) > MAX_COLLECTION_ITEMS:
                output.append({"__truncated_items__": len(value) - MAX_COLLECTION_ITEMS})
            return output
        if isinstance(value, (bytes, bytearray)):
            return f"[BINARY:{len(value)}:{hashlib.sha256(bytes(value)).hexdigest()[:16]}]"
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        return str(value)

    @staticmethod
    def _document_text(value: Mapping[str, Any], *, max_chars: int) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        if len(raw) <= max_chars:
            return raw
        head = max(1, int(max_chars * 0.65))
        tail = max_chars - head
        return f"{raw[:head]}\n...[TRUNCATED:{len(raw) - max_chars}]...\n{raw[-tail:]}"

    @staticmethod
    def _deduplicate_documents(documents: Sequence[EvidenceDocument]) -> list[EvidenceDocument]:
        output: list[EvidenceDocument] = []
        seen_ids: set[str] = set()
        for document in sorted(
            documents,
            key=lambda item: (item.ref.sequence, item.ref.kind.value, item.ref.evidence_id),
        ):
            if document.ref.evidence_id in seen_ids:
                continue
            seen_ids.add(document.ref.evidence_id)
            output.append(document)
        return output


class EvidenceResolver:
    """Re-resolve stored evidence and verify task/run/range/hash at validation time."""

    def __init__(self, store: CuratorCandidateStore) -> None:
        self.store = store

    def resolve_candidate(
        self,
        *,
        candidate_id: str,
    ) -> tuple[EvidenceBundle | None, tuple[EvidenceDocument, ...], tuple[str, ...]]:
        candidate = self.store.candidate(candidate_id)
        if candidate is None:
            return None, (), ("candidate_missing",)
        try:
            bundle = self.store.bundle(candidate.evidence_bundle_id)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return None, (), (f"bundle_forged:{type(error).__name__}",)
        if bundle is None:
            return None, (), ("bundle_missing",)
        issues: list[str] = []
        if bundle.bundle_digest != candidate.evidence_digest:
            issues.append("candidate_bundle_digest_mismatch")
        if bundle.run_id != candidate.run_id or bundle.task_id != candidate.task_id:
            issues.append("candidate_bundle_scope_mismatch")
        if bundle.evidence_range.to_dict() != candidate.evidence_range.to_dict():
            issues.append("candidate_evidence_range_mismatch")
        bundle_ids = tuple(item.evidence_id for item in bundle.evidence_refs)
        if bundle_ids != candidate.evidence_ids:
            issues.append("candidate_evidence_ids_mismatch")
        try:
            documents = self.store.evidence_documents(bundle_ids)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return bundle, (), (f"evidence_forged:{type(error).__name__}",)
        by_id = {document.ref.evidence_id: document for document in documents}
        ordered: list[EvidenceDocument] = []
        for ref in bundle.evidence_refs:
            document = by_id.get(ref.evidence_id)
            if document is None:
                issues.append(f"evidence_missing:{ref.evidence_id}")
                continue
            if document.ref.identity_projection() != ref.identity_projection():
                issues.append(f"evidence_identity_mismatch:{ref.evidence_id}")
            if stable_digest(document.normalized) != ref.content_digest:
                issues.append(f"evidence_forged:{ref.evidence_id}")
            if document.ref.run_id != candidate.run_id or document.ref.task_id != candidate.task_id:
                issues.append(f"evidence_scope_mismatch:{ref.evidence_id}")
            if not candidate.evidence_range.contains(document.ref.sequence):
                issues.append(f"evidence_range_escape:{ref.evidence_id}")
            ordered.append(document)
        if len(ordered) != len(candidate.evidence_ids):
            issues.append("evidence_cardinality_mismatch")
        return bundle, tuple(ordered), unique_strings(issues)

    def provenance_summary(self, documents: Sequence[EvidenceDocument]) -> Mapping[str, Any]:
        trust_counts = Counter(document.ref.trust.value for document in documents)
        kind_counts = Counter(document.ref.kind.value for document in documents)
        producers = Counter(document.ref.producer or "unknown" for document in documents)
        return {
            "evidence_count": len(documents),
            "trust_counts": dict(sorted(trust_counts.items())),
            "kind_counts": dict(sorted(kind_counts.items())),
            "producer_counts": dict(sorted(producers.items())),
            "redacted_count": sum(1 for document in documents if document.redacted),
            "secret_fingerprint_count": sum(
                len(document.secret_fingerprints) for document in documents
            ),
            "sequence_start": min((item.ref.sequence for item in documents), default=0),
            "sequence_end": max((item.ref.sequence for item in documents), default=0),
        }


__all__ = [
    "ArtifactReader",
    "EventArtifactTraceExtractor",
    "EvidenceResolver",
    "ExtractionDiagnostics",
    "ExtractionResult",
    "RedactionResult",
    "SecretMatch",
    "SecretRedactor",
]
