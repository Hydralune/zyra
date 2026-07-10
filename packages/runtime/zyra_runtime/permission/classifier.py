from __future__ import annotations

"""Advisory classifier boundary for permission decisions.

Classifiers can help explain or rank an ``ask`` decision, but they never own
the final guard.  The deterministic evaluator decides whether a proposal is
eligible to tighten or (in explicitly configured auto mode) relax an ask.
The projection deliberately excludes assistant-authored natural language so a
model cannot persuade its own safety classifier.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from threading import RLock
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from zyra_core import new_id, now_iso, to_jsonable

from .canonical import arguments_digest, canonical_arguments_json


class PermissionClassifierEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    UNAVAILABLE = "unavailable"


class PermissionClassifierStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    SKIPPED = "skipped"


class PermissionClassifierFailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    KEEP_ASK = "keep_ask"


@dataclass(frozen=True, slots=True)
class PermissionClassifierInput:
    session_id: str
    run_id: str
    task_id: str
    worker_id: str
    tool_call_id: str
    tool_name: str
    server_name: str
    arguments: dict[str, Any]
    arguments_digest: str
    transcript: tuple[dict[str, Any], ...]
    transcript_digest: str
    policy_labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_id: str,
        tool_call_id: str,
        tool_name: str,
        server_name: str,
        arguments: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]] = (),
        queued_commands: Sequence[Mapping[str, Any] | str] = (),
        policy_labels: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
        tool_projection: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str | None] | None = None,
    ) -> "PermissionClassifierInput":
        copied = json.loads(canonical_arguments_json(arguments))
        transcript = tuple(
            project_classifier_transcript(
                messages,
                queued_commands=queued_commands,
                current_tool_name=tool_name,
                current_arguments=copied,
                tool_projection=tool_projection,
            )
        )
        return cls(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            server_name=server_name,
            arguments=copied,
            arguments_digest=arguments_digest(copied),
            transcript=transcript,
            transcript_digest=_stable_digest(transcript),
            policy_labels=tuple(sorted({str(item) for item in policy_labels if str(item)})),
            metadata=dict(metadata or {}),
        )

    def to_dict(self, *, include_arguments: bool = False, include_transcript: bool = False) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "server_name": self.server_name,
            "arguments_digest": self.arguments_digest,
            "transcript_digest": self.transcript_digest,
            "transcript_items": len(self.transcript),
            "policy_labels": list(self.policy_labels),
            "metadata": _safe_metadata(self.metadata),
        }
        if include_arguments:
            payload["arguments"] = to_jsonable(self.arguments)
        if include_transcript:
            payload["transcript"] = [to_jsonable(item) for item in self.transcript]
        return payload


@dataclass(frozen=True, slots=True)
class PermissionClassifierProposal:
    effect: PermissionClassifierEffect
    reason: str
    confidence: str = "unknown"
    classifier: str = ""
    model: str = ""
    classifier_version: str = ""
    stage: str = ""
    eligible_for_auto_allow: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        reason: str,
        *,
        confidence: str = "medium",
        eligible_for_auto_allow: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionClassifierProposal":
        return cls(
            effect=PermissionClassifierEffect.ALLOW,
            reason=reason,
            confidence=confidence,
            eligible_for_auto_allow=eligible_for_auto_allow,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        confidence: str = "medium",
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionClassifierProposal":
        return cls(
            effect=PermissionClassifierEffect.DENY,
            reason=reason,
            confidence=confidence,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def ask(cls, reason: str, *, metadata: Mapping[str, Any] | None = None) -> "PermissionClassifierProposal":
        return cls(effect=PermissionClassifierEffect.ASK, reason=reason, metadata=dict(metadata or {}))

    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionClassifierProposal":
        return cls(
            effect=PermissionClassifierEffect.UNAVAILABLE,
            reason=reason,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": str(self.effect),
            "reason": self.reason,
            "confidence": self.confidence,
            "classifier": self.classifier,
            "model": self.model,
            "classifier_version": self.classifier_version,
            "stage": self.stage,
            "eligible_for_auto_allow": self.eligible_for_auto_allow,
            "metadata": _safe_metadata(self.metadata),
        }


class PermissionClassifier(Protocol):
    def __call__(
        self,
        classifier_input: PermissionClassifierInput,
    ) -> PermissionClassifierProposal | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RegisteredPermissionClassifier:
    name: str
    callback: PermissionClassifier
    source: str = "runtime"
    model: str = ""
    version: str = "1"
    priority: int = 100
    timeout_seconds: float = 8.0
    failure_policy: PermissionClassifierFailurePolicy = PermissionClassifierFailurePolicy.FAIL_CLOSED
    enabled: bool = True
    classifier_id: str = field(default_factory=lambda: new_id("permclassifier"))
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def config_digest(self) -> str:
        return _stable_digest(_safe_metadata(self.config))

    def descriptor(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "name": self.name,
            "source": self.source,
            "model": self.model,
            "version": self.version,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "failure_policy": str(self.failure_policy),
            "enabled": self.enabled,
            "config_digest": self.config_digest,
        }


@dataclass(frozen=True, slots=True)
class PermissionClassifierInvocation:
    invocation_id: str
    classifier_id: str
    classifier_name: str
    source: str
    status: PermissionClassifierStatus
    input_digest: str
    transcript_digest: str
    config_digest: str
    proposal: PermissionClassifierProposal
    started_at: str
    completed_at: str
    duration_ms: int
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "classifier_id": self.classifier_id,
            "classifier_name": self.classifier_name,
            "source": self.source,
            "status": str(self.status),
            "input_digest": self.input_digest,
            "transcript_digest": self.transcript_digest,
            "config_digest": self.config_digest,
            "proposal": self.proposal.to_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PermissionClassifierAggregate:
    effect: PermissionClassifierEffect
    reason: str
    invocations: tuple[PermissionClassifierInvocation, ...]
    advisory_only: bool = True
    created_at: str = field(default_factory=now_iso)

    @property
    def has_failure(self) -> bool:
        return any(
            item.status in {PermissionClassifierStatus.FAILED, PermissionClassifierStatus.TIMED_OUT}
            for item in self.invocations
        )

    @property
    def can_auto_allow(self) -> bool:
        allows = [item for item in self.invocations if item.proposal.effect == PermissionClassifierEffect.ALLOW]
        return bool(allows) and all(item.proposal.eligible_for_auto_allow for item in allows) and not self.has_failure

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": str(self.effect),
            "reason": self.reason,
            "advisory_only": self.advisory_only,
            "has_failure": self.has_failure,
            "can_auto_allow": self.can_auto_allow,
            "invocations": [item.to_dict() for item in self.invocations],
            "created_at": self.created_at,
        }


class PermissionClassifierAdapter:
    """Executes classifiers in a bounded, auditable advisory chain."""

    def __init__(self, classifiers: Iterable[RegisteredPermissionClassifier] = ()) -> None:
        self._lock = RLock()
        self._classifiers: dict[str, RegisteredPermissionClassifier] = {}
        for classifier in classifiers:
            self.register(classifier)

    def register(self, classifier: RegisteredPermissionClassifier) -> RegisteredPermissionClassifier:
        if not classifier.name.strip():
            raise ValueError("permission classifier name must not be empty")
        if classifier.timeout_seconds <= 0:
            raise ValueError("permission classifier timeout_seconds must be positive")
        with self._lock:
            if classifier.classifier_id in self._classifiers:
                raise ValueError(f"permission classifier id already registered: {classifier.classifier_id}")
            self._classifiers[classifier.classifier_id] = classifier
        return classifier

    def unregister(self, classifier_id: str) -> bool:
        with self._lock:
            return self._classifiers.pop(classifier_id, None) is not None

    def list_classifiers(self) -> tuple[RegisteredPermissionClassifier, ...]:
        with self._lock:
            values = list(self._classifiers.values())
        return tuple(sorted(values, key=lambda item: (item.priority, item.source, item.name, item.classifier_id)))

    def descriptors(self) -> list[dict[str, Any]]:
        return [item.descriptor() for item in self.list_classifiers()]

    def classify(self, classifier_input: PermissionClassifierInput) -> PermissionClassifierAggregate:
        invocations = tuple(self._invoke(item, classifier_input) for item in self.list_classifiers())
        if not invocations:
            return PermissionClassifierAggregate(
                effect=PermissionClassifierEffect.UNAVAILABLE,
                reason="no permission classifier configured",
                invocations=(),
            )
        # Any deny or unavailable/failure is conservative.  Classifier allow
        # never outranks deterministic rules; the evaluator may use a unanimous
        # eligible allow only in auto mode.
        if any(item.proposal.effect == PermissionClassifierEffect.DENY for item in invocations):
            chosen = next(item for item in invocations if item.proposal.effect == PermissionClassifierEffect.DENY)
            return PermissionClassifierAggregate(
                effect=PermissionClassifierEffect.DENY,
                reason=chosen.proposal.reason,
                invocations=invocations,
            )
        if any(
            item.proposal.effect == PermissionClassifierEffect.UNAVAILABLE
            or item.status in {PermissionClassifierStatus.FAILED, PermissionClassifierStatus.TIMED_OUT}
            for item in invocations
        ):
            return PermissionClassifierAggregate(
                effect=PermissionClassifierEffect.UNAVAILABLE,
                reason="permission classifier unavailable; deterministic ask remains",
                invocations=invocations,
            )
        if all(item.proposal.effect == PermissionClassifierEffect.ALLOW for item in invocations):
            return PermissionClassifierAggregate(
                effect=PermissionClassifierEffect.ALLOW,
                reason="all configured permission classifiers proposed allow",
                invocations=invocations,
            )
        return PermissionClassifierAggregate(
            effect=PermissionClassifierEffect.ASK,
            reason="classifier did not unanimously approve the action",
            invocations=invocations,
        )

    def _invoke(
        self,
        classifier: RegisteredPermissionClassifier,
        classifier_input: PermissionClassifierInput,
    ) -> PermissionClassifierInvocation:
        started_at = now_iso()
        started = monotonic()
        if not classifier.enabled:
            proposal = PermissionClassifierProposal.unavailable("classifier disabled")
            timestamp = now_iso()
            return PermissionClassifierInvocation(
                invocation_id=new_id("permclassify"),
                classifier_id=classifier.classifier_id,
                classifier_name=classifier.name,
                source=classifier.source,
                status=PermissionClassifierStatus.SKIPPED,
                input_digest=classifier_input.arguments_digest,
                transcript_digest=classifier_input.transcript_digest,
                config_digest=classifier.config_digest,
                proposal=_stamp_proposal(proposal, classifier),
                started_at=started_at,
                completed_at=timestamp,
                duration_ms=0,
            )
        status = PermissionClassifierStatus.COMPLETED
        error = ""
        pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"permission-classifier-{classifier.name[:20]}",
        )
        try:
            future = pool.submit(classifier.callback, classifier_input)
            raw = future.result(timeout=classifier.timeout_seconds)
            proposal = _coerce_proposal(raw)
        except FutureTimeout:
            status = PermissionClassifierStatus.TIMED_OUT
            error = "permission_classifier_timeout"
            proposal = _classifier_failure_proposal(classifier, "permission classifier timed out")
        except Exception as exc:  # noqa: BLE001 - adapter failures become conservative proposals.
            status = PermissionClassifierStatus.FAILED
            error = type(exc).__name__
            proposal = _classifier_failure_proposal(
                classifier,
                f"permission classifier failed: {type(exc).__name__}",
            )
        finally:
            # Do not let ThreadPoolExecutor.__exit__ wait for a classifier that
            # already exceeded its authorization deadline.
            pool.shutdown(wait=False, cancel_futures=True)
        return PermissionClassifierInvocation(
            invocation_id=new_id("permclassify"),
            classifier_id=classifier.classifier_id,
            classifier_name=classifier.name,
            source=classifier.source,
            status=status,
            input_digest=classifier_input.arguments_digest,
            transcript_digest=classifier_input.transcript_digest,
            config_digest=classifier.config_digest,
            proposal=_stamp_proposal(proposal, classifier),
            started_at=started_at,
            completed_at=now_iso(),
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            error=error,
        )


def project_classifier_transcript(
    messages: Sequence[Mapping[str, Any]],
    *,
    queued_commands: Sequence[Mapping[str, Any] | str] = (),
    current_tool_name: str,
    current_arguments: Mapping[str, Any],
    tool_projection: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str | None] | None = None,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").lower()
        content = message.get("content")
        if role == "user":
            text = _user_text(content)
            if text:
                projected.append({"user": text})
            continue
        if role != "assistant":
            continue
        # Assistant natural-language text is excluded.  Only structured tool
        # calls are retained.
        for call in _tool_calls(content, message):
            name = str(call.get("name") or call.get("tool_name") or "")
            arguments = call.get("input") if isinstance(call.get("input"), Mapping) else call.get("arguments")
            args = dict(arguments) if isinstance(arguments, Mapping) else {}
            projected_value = _project_tool(name, args, tool_projection)
            projected.append({"tool_use": {"name": name, "input": projected_value}})
    for queued in queued_commands:
        if isinstance(queued, Mapping):
            value = str(queued.get("value") or queued.get("command") or "")
            origin = str(queued.get("origin") or "queued")
        else:
            value = str(queued)
            origin = "queued"
        if value:
            projected.append({"queued_user_command": value, "origin": origin})
    projected.append(
        {
            "current_tool_use": {
                "name": current_tool_name,
                "input": _project_tool(current_tool_name, current_arguments, tool_projection),
                "arguments_digest": arguments_digest(current_arguments),
            }
        }
    )
    return projected


def _project_tool(
    tool_name: str,
    arguments: Mapping[str, Any],
    projection: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str | None] | None,
) -> Any:
    if projection is None:
        return _redact_arguments(arguments)
    value = projection(tool_name, arguments)
    # An empty projection is not interpreted as safe.  Preserve a digest so
    # unknown tools cannot exploit the upstream empty-string fast allow.
    if value is None or value == "":
        return {"projection": "unavailable", "arguments_digest": arguments_digest(arguments)}
    if isinstance(value, Mapping):
        return _redact_arguments(value)
    return str(value)


def _tool_calls(content: Any, message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    if isinstance(content, list):
        calls.extend(
            item
            for item in content
            if isinstance(item, Mapping) and str(item.get("type") or "") in {"tool_use", "function_call"}
        )
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        calls.extend(item for item in raw_calls if isinstance(item, Mapping))
    return calls


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content[:12000]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("type") or "") in {"text", "input_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(parts)[:12000]
    return ""


def _coerce_proposal(raw: PermissionClassifierProposal | Mapping[str, Any]) -> PermissionClassifierProposal:
    if isinstance(raw, PermissionClassifierProposal):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("permission classifier result must be a proposal or mapping")
    effect_value = str(raw.get("effect") or raw.get("behavior") or PermissionClassifierEffect.UNAVAILABLE)
    try:
        effect = PermissionClassifierEffect(effect_value)
    except ValueError as exc:
        raise ValueError(f"invalid classifier effect: {effect_value}") from exc
    metadata = raw.get("metadata")
    return PermissionClassifierProposal(
        effect=effect,
        reason=str(raw.get("reason") or raw.get("message") or ""),
        confidence=str(raw.get("confidence") or "unknown"),
        classifier=str(raw.get("classifier") or ""),
        model=str(raw.get("model") or ""),
        classifier_version=str(raw.get("classifier_version") or raw.get("version") or ""),
        stage=str(raw.get("stage") or ""),
        eligible_for_auto_allow=bool(raw.get("eligible_for_auto_allow", False)),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _classifier_failure_proposal(
    classifier: RegisteredPermissionClassifier,
    reason: str,
) -> PermissionClassifierProposal:
    if classifier.failure_policy == PermissionClassifierFailurePolicy.FAIL_CLOSED:
        return PermissionClassifierProposal.deny(
            reason,
            confidence="high",
            metadata={"classifier_failure": "true", "fail_closed": "true"},
        )
    return PermissionClassifierProposal.unavailable(
        reason,
        metadata={"classifier_failure": "true", "keep_ask": "true"},
    )


def _stamp_proposal(
    proposal: PermissionClassifierProposal,
    classifier: RegisteredPermissionClassifier,
) -> PermissionClassifierProposal:
    return PermissionClassifierProposal(
        effect=proposal.effect,
        reason=proposal.reason,
        confidence=proposal.confidence,
        classifier=proposal.classifier or classifier.name,
        model=proposal.model or classifier.model,
        classifier_version=proposal.classifier_version or classifier.version,
        stage=proposal.stage,
        eligible_for_auto_allow=proposal.eligible_for_auto_allow,
        metadata={**proposal.metadata, "source": classifier.source},
    )


_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "private_key",
)


def _redact_arguments(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS):
            output[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            output[str(key)] = _redact_arguments(item)
        elif isinstance(item, list):
            output[str(key)] = [
                _redact_arguments(child) if isinstance(child, Mapping) else to_jsonable(child)
                for child in item
            ]
        else:
            output[str(key)] = to_jsonable(item)
    return output


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    return _redact_arguments(value)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "PermissionClassifier",
    "PermissionClassifierAdapter",
    "PermissionClassifierAggregate",
    "PermissionClassifierEffect",
    "PermissionClassifierFailurePolicy",
    "PermissionClassifierInput",
    "PermissionClassifierInvocation",
    "PermissionClassifierProposal",
    "PermissionClassifierStatus",
    "RegisteredPermissionClassifier",
    "project_classifier_transcript",
]
