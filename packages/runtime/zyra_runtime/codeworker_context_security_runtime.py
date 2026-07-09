from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_INTEGRATION_OWNER_UNIT = "M1-02D"
CODEWORKER_CONTEXT_SECURITY_RUNTIME_ID = "codeworker_context_security_runtime"


class ContextTrustLevel(StrEnum):
    TRUSTED_SYSTEM = "trusted_system"
    WORKSPACE = "workspace"
    TOOL_OUTPUT = "tool_output"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    SECRET_REDACTED = "secret_redacted"
    UNKNOWN = "unknown"


class SecretRedactionState(StrEnum):
    CLEAN = "clean"
    REDACTED = "redacted"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ContextProvenanceKind(StrEnum):
    SYSTEM = "system"
    USER = "user"
    WORKSPACE_FILE = "workspace_file"
    TOOL_RESULT = "tool_result"
    COMPACT_SUMMARY = "compact_summary"
    MCP_INSTRUCTION = "mcp_instruction"
    DEFERRED_TOOL = "deferred_tool"
    ACTIVE_PLAN = "active_plan"
    INVOKED_SKILL = "invoked_skill"
    RUNTIME_BUDGET = "runtime_budget"
    MODEL_API = "model_api"
    UNKNOWN = "unknown"


class ContextSecurityStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class ContextSecuritySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ContextSecuritySurface(StrEnum):
    PROVENANCE = "provenance"
    TRUST = "trust"
    SECRET = "secret"
    PROMPT_INJECTION = "prompt_injection"
    RESTORE = "restore"
    MESSAGE = "message"


@dataclass(frozen=True, slots=True)
class ContextSecurityFinding:
    code: str
    severity: ContextSecuritySeverity
    surface: ContextSecuritySurface
    message: str
    source_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ContextSecuritySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "source_id": self.source_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextProvenance:
    source_id: str
    kind: ContextProvenanceKind
    source_path: str = ""
    source_repo: str = ""
    upstream_source_path: str = ""
    runtime_owner: str = "zyra"
    source_ref: str = ""
    query: str = ""
    scope: str = ""
    budget: str = ""
    code_index: bool = False
    external: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": str(self.kind),
            "source_path": self.source_path,
            "source_repo": self.source_repo,
            "upstream_source_path": self.upstream_source_path,
            "runtime_owner": self.runtime_owner,
            "source_ref": self.source_ref,
            "query": self.query,
            "scope": self.scope,
            "budget": self.budget,
            "code_index": self.code_index,
            "external": self.external,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextSecurityPolicy:
    block_secret_restore: bool = False
    mark_external_untrusted: bool = True
    preserve_untrusted_markers: bool = True
    max_redaction_preview_chars: int = 160
    prompt_injection_phrases: tuple[str, ...] = (
        "ignore previous instructions",
        "ignore all previous instructions",
        "system prompt",
        "developer message",
        "bypass permission",
        "disable safety",
        "reveal secret",
        "exfiltrate",
    )
    secret_patterns: tuple[str, ...] = (
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{12,})",
        r"(?i)(bearer)\s+([A-Za-z0-9_\-\.]{16,})",
        r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_secret_restore": self.block_secret_restore,
            "mark_external_untrusted": self.mark_external_untrusted,
            "preserve_untrusted_markers": self.preserve_untrusted_markers,
            "max_redaction_preview_chars": self.max_redaction_preview_chars,
            "prompt_injection_phrase_count": len(self.prompt_injection_phrases),
            "secret_pattern_count": len(self.secret_patterns),
        }


@dataclass(frozen=True, slots=True)
class SecretMatch:
    pattern_index: int
    start: int
    end: int
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_index": self.pattern_index,
            "start": self.start,
            "end": self.end,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class PromptInjectionSignal:
    phrase: str
    start: int
    end: int
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "start": self.start,
            "end": self.end,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class ContextSecurityVerdict:
    verdict_id: str
    source_id: str
    provenance: ContextProvenance
    trust_level: ContextTrustLevel
    secret_state: SecretRedactionState
    original_chars: int
    sanitized_text: str
    secret_matches: tuple[SecretMatch, ...]
    prompt_injection_signals: tuple[PromptInjectionSignal, ...]
    findings: tuple[ContextSecurityFinding, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def redacted(self) -> bool:
        return self.secret_state == SecretRedactionState.REDACTED

    @property
    def untrusted(self) -> bool:
        return self.trust_level == ContextTrustLevel.EXTERNAL_UNTRUSTED

    def metadata(self) -> dict[str, str]:
        return {
            "context_security_verdict_id": self.verdict_id,
            "context_security_source_id": self.source_id,
            "context_security_trust_level": str(self.trust_level),
            "context_security_secret_state": str(self.secret_state),
            "context_security_redacted": str(self.redacted).lower(),
            "context_security_untrusted": str(self.untrusted).lower(),
            "context_security_secret_match_count": str(len(self.secret_matches)),
            "context_security_prompt_injection_count": str(len(self.prompt_injection_signals)),
            "context_security_blocking_count": str(sum(1 for finding in self.findings if finding.blocking)),
            "source_provenance": str(self.provenance.kind),
            "source_ref": self.provenance.source_ref,
            "trust_level": str(self.trust_level),
            "secret_redaction_state": str(self.secret_state),
            "retrieval_query": self.provenance.query,
            "retrieval_scope": self.provenance.scope,
            "retrieval_budget": self.provenance.budget,
            "code_index_source": str(self.provenance.code_index).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.context_security_verdict.v1",
            "verdict_id": self.verdict_id,
            "source_id": self.source_id,
            "ok": self.ok,
            "trust_level": str(self.trust_level),
            "secret_state": str(self.secret_state),
            "original_chars": self.original_chars,
            "sanitized_chars": len(self.sanitized_text),
            "sanitized_text": self.sanitized_text,
            "redacted": self.redacted,
            "untrusted": self.untrusted,
            "secret_matches": [match.to_dict() for match in self.secret_matches],
            "prompt_injection_signals": [signal.to_dict() for signal in self.prompt_injection_signals],
            "findings": [finding.to_dict() for finding in self.findings],
            "provenance": self.provenance.to_dict(),
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ContextSecuritySnapshot:
    snapshot_id: str
    owner_unit: str
    runtime_id: str
    verdicts: tuple[ContextSecurityVerdict, ...]
    policy: ContextSecurityPolicy
    disabled: bool = False
    findings: tuple[ContextSecurityFinding, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self.findings) and all(
            verdict.ok for verdict in self.verdicts
        )

    @property
    def status(self) -> ContextSecurityStatus:
        if self.disabled:
            return ContextSecurityStatus.DISABLED
        if not self.ok:
            return ContextSecurityStatus.BLOCKED
        if self.findings or any(verdict.findings for verdict in self.verdicts):
            return ContextSecurityStatus.DEGRADED
        return ContextSecurityStatus.READY

    @property
    def secret_redaction_count(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.redacted)

    @property
    def untrusted_count(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.untrusted)

    @property
    def prompt_injection_count(self) -> int:
        return sum(len(verdict.prompt_injection_signals) for verdict in self.verdicts)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking) + sum(
            1 for verdict in self.verdicts for finding in verdict.findings if finding.blocking
        )

    def metadata(self) -> dict[str, str]:
        return {
            "context_security_snapshot_id": self.snapshot_id,
            "context_security_owner_unit": self.owner_unit,
            "context_security_runtime_id": self.runtime_id,
            "context_security_ok": str(self.ok).lower(),
            "context_security_status": str(self.status),
            "context_security_disabled": str(self.disabled).lower(),
            "context_security_verdicts": str(len(self.verdicts)),
            "context_security_redactions": str(self.secret_redaction_count),
            "context_security_untrusted": str(self.untrusted_count),
            "context_security_prompt_injection_signals": str(self.prompt_injection_count),
            "context_security_blocking_count": str(self.blocking_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.context_security_snapshot.v1",
            "snapshot_id": self.snapshot_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "policy": self.policy.to_dict(),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
            "findings": [finding.to_dict() for finding in self.findings],
            "secret_redaction_count": self.secret_redaction_count,
            "untrusted_count": self.untrusted_count,
            "prompt_injection_count": self.prompt_injection_count,
            "blocking_count": self.blocking_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerContextSecurityRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_INTEGRATION_OWNER_UNIT,
        runtime_id: str = CODEWORKER_CONTEXT_SECURITY_RUNTIME_ID,
        policy: ContextSecurityPolicy | None = None,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.policy = policy or ContextSecurityPolicy()
        self.disabled = disabled
        self._compiled_secret_patterns = tuple(re.compile(pattern) for pattern in self.policy.secret_patterns)

    def classify_text(
        self,
        text: str,
        *,
        source_id: str,
        provenance: ContextProvenance,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextSecurityVerdict:
        metadata = dict(metadata or {})
        findings: list[ContextSecurityFinding] = []
        if self.disabled:
            findings.append(
                ContextSecurityFinding(
                    code="CONTEXT_SECURITY_RUNTIME_DISABLED",
                    severity=ContextSecuritySeverity.BLOCKER,
                    surface=ContextSecuritySurface.RESTORE,
                    message="CodeWorker context security runtime is disabled.",
                    source_id=source_id,
                )
            )
            return ContextSecurityVerdict(
                verdict_id=new_id("ctxsec"),
                source_id=source_id,
                provenance=provenance,
                trust_level=ContextTrustLevel.UNKNOWN,
                secret_state=SecretRedactionState.UNKNOWN,
                original_chars=len(text),
                sanitized_text=text,
                secret_matches=(),
                prompt_injection_signals=(),
                findings=tuple(findings),
            )

        secret_matches = self._secret_matches(text)
        prompt_signals = self._prompt_injection_signals(text)
        trust = self._trust_level(provenance=provenance, metadata=metadata)
        secret_state = SecretRedactionState.CLEAN
        sanitized = text
        if secret_matches:
            sanitized = self._redact(text, secret_matches)
            secret_state = SecretRedactionState.REDACTED
            findings.append(
                ContextSecurityFinding(
                    code="SECRET_REDACTED_FROM_CONTEXT",
                    severity=ContextSecuritySeverity.WARNING
                    if not self.policy.block_secret_restore
                    else ContextSecuritySeverity.BLOCKER,
                    surface=ContextSecuritySurface.SECRET,
                    message="Secret-like content was redacted before context restore.",
                    source_id=source_id,
                    metadata={"secret_match_count": str(len(secret_matches))},
                )
            )
            if self.policy.block_secret_restore:
                secret_state = SecretRedactionState.BLOCKED
        if prompt_signals:
            findings.append(
                ContextSecurityFinding(
                    code="UNTRUSTED_PROMPT_INJECTION_MARKER",
                    severity=ContextSecuritySeverity.WARNING,
                    surface=ContextSecuritySurface.PROMPT_INJECTION,
                    message="Potential prompt-injection language was preserved as untrusted content.",
                    source_id=source_id,
                    metadata={"signal_count": str(len(prompt_signals))},
                )
            )
            if trust == ContextTrustLevel.UNKNOWN:
                trust = ContextTrustLevel.EXTERNAL_UNTRUSTED
        if provenance.external and self.policy.mark_external_untrusted:
            trust = ContextTrustLevel.EXTERNAL_UNTRUSTED
        if trust == ContextTrustLevel.UNKNOWN:
            findings.append(
                ContextSecurityFinding(
                    code="CONTEXT_PROVENANCE_UNKNOWN",
                    severity=ContextSecuritySeverity.WARNING,
                    surface=ContextSecuritySurface.PROVENANCE,
                    message="Context item has incomplete provenance.",
                    source_id=source_id,
                )
            )
        return ContextSecurityVerdict(
            verdict_id=new_id("ctxsec"),
            source_id=source_id,
            provenance=provenance,
            trust_level=trust,
            secret_state=secret_state,
            original_chars=len(text),
            sanitized_text=sanitized,
            secret_matches=tuple(secret_matches),
            prompt_injection_signals=tuple(prompt_signals),
            findings=tuple(findings),
        )

    def classify_mapping(
        self,
        item: Mapping[str, Any],
        *,
        default_source_id: str = "",
        default_kind: ContextProvenanceKind = ContextProvenanceKind.UNKNOWN,
    ) -> ContextSecurityVerdict:
        metadata = _as_mapping(item.get("metadata"))
        provenance = provenance_from_mapping(
            item,
            default_source_id=default_source_id,
            default_kind=default_kind,
        )
        content = str(item.get("content") or item.get("text") or metadata.get("content") or "")
        return self.classify_text(
            content,
            source_id=provenance.source_id or default_source_id or new_id("ctxsrc"),
            provenance=provenance,
            metadata=metadata,
        )

    def build_snapshot(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        default_kind: ContextProvenanceKind = ContextProvenanceKind.UNKNOWN,
    ) -> ContextSecuritySnapshot:
        verdicts: list[ContextSecurityVerdict] = []
        findings: list[ContextSecurityFinding] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                findings.append(
                    ContextSecurityFinding(
                        code="CONTEXT_SECURITY_ITEM_NOT_MAPPING",
                        severity=ContextSecuritySeverity.WARNING,
                        surface=ContextSecuritySurface.MESSAGE,
                        message="Context security item was skipped because it is not a mapping.",
                        source_id=f"item:{index}",
                    )
                )
                continue
            verdicts.append(
                self.classify_mapping(
                    item,
                    default_source_id=str(item.get("source_id") or item.get("segment_id") or f"item:{index}"),
                    default_kind=default_kind,
                )
            )
        return ContextSecuritySnapshot(
            snapshot_id=new_id("ctxsec_snapshot"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            verdicts=tuple(verdicts),
            policy=self.policy,
            disabled=self.disabled,
            findings=tuple(findings),
        )

    def event_for_snapshot(
        self,
        snapshot: ContextSecuritySnapshot,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str = "",
        worker_request_id: str = "",
        phase: str = "context_security_snapshot",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "phase": phase,
                    "context_security": snapshot.to_dict(),
                }
            },
        )

    def metadata(self, snapshot: ContextSecuritySnapshot | None = None) -> dict[str, str]:
        if snapshot is None:
            return {
                "context_security_ok": str(not self.disabled).lower(),
                "context_security_status": str(
                    ContextSecurityStatus.DISABLED if self.disabled else ContextSecurityStatus.READY
                ),
                "context_security_runtime_id": self.runtime_id,
            }
        return snapshot.metadata()

    def _secret_matches(self, text: str) -> list[SecretMatch]:
        matches: list[SecretMatch] = []
        for pattern_index, pattern in enumerate(self._compiled_secret_patterns):
            for match in pattern.finditer(text):
                matches.append(
                    SecretMatch(
                        pattern_index=pattern_index,
                        start=match.start(),
                        end=match.end(),
                        preview=_preview(text, match.start(), match.end(), self.policy.max_redaction_preview_chars),
                    )
                )
        matches.sort(key=lambda item: (item.start, item.end, item.pattern_index))
        return _dedupe_secret_matches(matches)

    def _prompt_injection_signals(self, text: str) -> list[PromptInjectionSignal]:
        lowered = text.lower()
        signals: list[PromptInjectionSignal] = []
        for phrase in self.policy.prompt_injection_phrases:
            needle = phrase.lower()
            start = 0
            while True:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                end = index + len(needle)
                signals.append(
                    PromptInjectionSignal(
                        phrase=phrase,
                        start=index,
                        end=end,
                        preview=_preview(text, index, end, self.policy.max_redaction_preview_chars),
                    )
                )
                start = end
        return signals

    def _redact(self, text: str, matches: Sequence[SecretMatch]) -> str:
        if not matches:
            return text
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            if match.start < cursor:
                continue
            pieces.append(text[cursor : match.start])
            pieces.append("[REDACTED_SECRET]")
            cursor = match.end
        pieces.append(text[cursor:])
        return "".join(pieces)

    def _trust_level(self, *, provenance: ContextProvenance, metadata: Mapping[str, Any]) -> ContextTrustLevel:
        explicit = str(metadata.get("trust_level") or metadata.get("trust") or "").strip()
        if explicit:
            try:
                return ContextTrustLevel(explicit)
            except ValueError:
                pass
        if provenance.kind in {ContextProvenanceKind.SYSTEM, ContextProvenanceKind.RUNTIME_BUDGET}:
            return ContextTrustLevel.TRUSTED_SYSTEM
        if provenance.kind in {ContextProvenanceKind.WORKSPACE_FILE, ContextProvenanceKind.ACTIVE_PLAN}:
            return ContextTrustLevel.WORKSPACE
        if provenance.kind == ContextProvenanceKind.TOOL_RESULT:
            return ContextTrustLevel.TOOL_OUTPUT
        if provenance.kind in {ContextProvenanceKind.MCP_INSTRUCTION, ContextProvenanceKind.UNKNOWN}:
            return ContextTrustLevel.EXTERNAL_UNTRUSTED if provenance.external else ContextTrustLevel.UNKNOWN
        if provenance.kind in {ContextProvenanceKind.COMPACT_SUMMARY, ContextProvenanceKind.INVOKED_SKILL}:
            return ContextTrustLevel.WORKSPACE
        return ContextTrustLevel.UNKNOWN


def provenance_from_mapping(
    item: Mapping[str, Any],
    *,
    default_source_id: str = "",
    default_kind: ContextProvenanceKind = ContextProvenanceKind.UNKNOWN,
) -> ContextProvenance:
    metadata = _as_mapping(item.get("metadata"))
    kind = _kind_from_value(
        item.get("kind")
        or metadata.get("kind")
        or item.get("source_provenance")
        or metadata.get("source_provenance"),
        default=default_kind,
    )
    source_id = str(
        item.get("source_id")
        or item.get("segment_id")
        or item.get("artifact_id")
        or metadata.get("source_id")
        or default_source_id
        or new_id("ctxsrc")
    )
    source_ref = str(item.get("source_ref") or metadata.get("source_ref") or source_id)
    external = _truthy(item.get("external") or metadata.get("external"))
    if kind in {ContextProvenanceKind.MCP_INSTRUCTION, ContextProvenanceKind.UNKNOWN} and "mcp" in source_ref.lower():
        external = True
    return ContextProvenance(
        source_id=source_id,
        kind=kind,
        source_path=str(item.get("source_path") or metadata.get("source_path") or ""),
        source_repo=str(item.get("source_repo") or metadata.get("source_repo") or ""),
        upstream_source_path=str(item.get("upstream_source_path") or metadata.get("upstream_source_path") or ""),
        runtime_owner=str(item.get("runtime_owner") or metadata.get("runtime_owner") or "zyra"),
        source_ref=source_ref,
        query=str(item.get("retrieval_query") or metadata.get("retrieval_query") or ""),
        scope=str(item.get("retrieval_scope") or metadata.get("retrieval_scope") or ""),
        budget=str(item.get("retrieval_budget") or metadata.get("retrieval_budget") or ""),
        code_index=_truthy(item.get("code_index_source") or metadata.get("code_index_source")),
        external=external,
        metadata={str(k): str(v) for k, v in metadata.items() if isinstance(k, str)},
    )


def context_security_metadata(snapshot: ContextSecuritySnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {
            "context_security_ok": "false",
            "context_security_status": "missing",
            "context_security_snapshot_id": "",
        }
    return snapshot.metadata()


def default_context_security_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_security_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "post-compact restore keeps typed provenance and untrusted markers",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/utils/permissions/*",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_security_runtime.py",
            "decision": "adapter_encapsulated",
            "capability": "secret and prompt-injection boundaries feed later permission and patch engines",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_security_runtime.py",
            "decision": "adapter_encapsulated",
            "capability": "session event provenance is preserved on restored message parts",
        },
    )


def _kind_from_value(value: Any, *, default: ContextProvenanceKind) -> ContextProvenanceKind:
    raw = str(value or "").strip()
    if not raw:
        return default
    aliases = {
        "file_attachment": ContextProvenanceKind.WORKSPACE_FILE,
        "tool": ContextProvenanceKind.TOOL_RESULT,
        "compact-summary": ContextProvenanceKind.COMPACT_SUMMARY,
        "compact_summary": ContextProvenanceKind.COMPACT_SUMMARY,
        "mcp_instruction_delta": ContextProvenanceKind.MCP_INSTRUCTION,
        "deferred_tool": ContextProvenanceKind.DEFERRED_TOOL,
        "active_plan": ContextProvenanceKind.ACTIVE_PLAN,
        "invoked_skill": ContextProvenanceKind.INVOKED_SKILL,
        "budget_state": ContextProvenanceKind.RUNTIME_BUDGET,
    }
    if raw in aliases:
        return aliases[raw]
    try:
        return ContextProvenanceKind(raw)
    except ValueError:
        return default


def _dedupe_secret_matches(matches: Sequence[SecretMatch]) -> list[SecretMatch]:
    if not matches:
        return []
    result: list[SecretMatch] = []
    for match in matches:
        if result and match.start < result[-1].end:
            previous = result[-1]
            if (match.end - match.start) > (previous.end - previous.start):
                result[-1] = match
            continue
        result.append(match)
    return result


def _preview(text: str, start: int, end: int, max_chars: int) -> str:
    max_chars = max(16, max_chars)
    radius = max(4, max_chars // 2)
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return prefix + text[left:right] + suffix


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
