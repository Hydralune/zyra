from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zyra_runtime import WorkerRequest

from .integration_models import (
    BrowserActionIntegrationError,
    BrowserActionPlan,
    PlanAdmission,
    PlanAdmissionIssue,
    PlanAdmissionIssueKind,
    PlanPhase,
    PlanStep,
    create_plan_id,
    parse_timestamp,
)
from .models import ActionIdentity, ActionRequest, ActionTargetKind, digest_value, stable_id
from .registry import BrowserActionRegistry, BrowserActionRegistryError
from .schema import BrowserActionSchemaProjector
from .selector_guard import SelectorExpectation


_BYPASS_KEY_PATTERNS = (
    re.compile(r"(^|[_-])(yolo|bypass|unrestricted|unsafe)([_-]|$)", re.IGNORECASE),
    re.compile(r"(^|[_-])(skip|disable|ignore)([_-])?(permission|approval|preflight|gateway)", re.IGNORECASE),
    re.compile(r"(^|[_-])(auto)([_-])?(approve|allow)([_-]|$)", re.IGNORECASE),
    re.compile(r"(^|[_-])(permission|approval)([_-])?(override|token|grant|capability)([_-]|$)", re.IGNORECASE),
)

_BYPASS_VALUE_WORDS = frozenset(
    {
        "yolo",
        "bypass",
        "bypass_permissions",
        "skip_permission",
        "skip_permissions",
        "auto_approve",
        "allow_all",
        "unrestricted",
        "disable_gateway",
        "direct_cdp",
        "raw_cdp",
    }
)

# This platform-launch authority is enforced independently by BrowserSessionRuntime
# and is required by the mandatory isolated Windows Chrome lane.  Treating its
# literal name as a permission-gateway bypass makes the secure live lane
# unreachable; nested copies and plan-step injection remain rejected.
_TRUSTED_TOP_LEVEL_RUNTIME_FLAGS = frozenset({"browser_allow_unsafe_sandbox_bypass"})

_RESERVED_STEP_KEYS = frozenset(
    {
        "permission_decision",
        "permission_effect",
        "permission_grant",
        "permission_grant_id",
        "permission_token",
        "approval",
        "approved",
        "allowed",
        "execution_grant",
        "preflight_receipt",
        "preflight_receipt_id",
        "selector_binding",
        "network_receipt",
        "file_receipt",
        "secret_receipt",
        "clipboard_receipt",
        "form_receipt",
    }
)

_ALLOWED_STEP_KEYS = frozenset(
    {
        "action",
        "browser_action",
        "name",
        "arguments",
        "args",
        "deadline_at",
        "timeout_seconds",
        "selector_expectation",
        "metadata",
        "description",
    }
)


@dataclass(frozen=True, slots=True)
class PlanAdapterConfig:
    backend: str = "zyra-browser-productized"
    maximum_steps: int = 256
    maximum_plan_bytes: int = 2_000_000
    maximum_timeout_seconds: float = 300.0
    minimum_timeout_seconds: float = 0.05
    reject_unknown_step_keys: bool = True
    require_opaque_selector_ref: bool = True
    reject_bypass_metadata: bool = True

    def __post_init__(self) -> None:
        if not self.backend:
            raise ValueError("browser plan adapter backend is required")
        if self.maximum_steps < 1 or self.maximum_plan_bytes < 4096:
            raise ValueError("browser plan adapter limits are too small")
        if self.minimum_timeout_seconds <= 0 or self.maximum_timeout_seconds < self.minimum_timeout_seconds:
            raise ValueError("browser plan adapter timeout bounds are invalid")


class BrowserActionPlanAdapter:
    """Translate a BrowserWorker plan into exact 04C action identities.

    Static admission validates every action schema and every attempt to inject
    permission or preflight state before the session-bound gateway may consume
    a grant.  Selector identity is always loaded from the authoritative 04B
    store.  Caller-supplied identity fields are comparison assertions only.
    """

    def __init__(
        self,
        registry: BrowserActionRegistry,
        selector_store: Any,
        *,
        projector: BrowserActionSchemaProjector | None = None,
        config: PlanAdapterConfig | None = None,
        disabled: bool = False,
    ) -> None:
        self.registry = registry
        self.selector_store = selector_store
        self.projector = projector or BrowserActionSchemaProjector()
        self.config = config or PlanAdapterConfig()
        self.disabled = disabled
        self._admissions = 0
        self._rejections = 0

    def admit(
        self,
        request: WorkerRequest,
        session_start: Any,
        raw_plan: Sequence[Mapping[str, Any]],
        *,
        permission_session_id: str,
        target_runtime: Any,
    ) -> PlanAdmission:
        self._ensure_available()
        issues: list[PlanAdmissionIssue] = []
        if not isinstance(request, WorkerRequest):
            issues.append(self._issue(0, "", "invalid_worker_request", "browser plan requires WorkerRequest", PlanAdmissionIssueKind.IDENTITY))
            return self._rejected(issues, raw_plan)
        session = getattr(session_start, "session", None)
        if session is None or not bool(getattr(session_start, "ok", False)):
            issues.append(self._issue(0, "", "invalid_session_start", "browser plan requires a running 04A session", PlanAdmissionIssueKind.OWNER))
            return self._rejected(issues, raw_plan)
        if str(getattr(session, "status", "")) != "running":
            issues.append(self._issue(0, "", "browser_session_not_running", "browser session is not running", PlanAdmissionIssueKind.OWNER))
        if str(getattr(session, "run_id", "")) != request.run_id or str(getattr(session, "task_id", "")) != request.task_id:
            issues.append(self._issue(0, "", "browser_session_identity_mismatch", "browser session belongs to another run/task", PlanAdmissionIssueKind.IDENTITY))
        if not permission_session_id:
            issues.append(self._issue(0, "", "permission_session_missing", "03A permission session identity is required", PlanAdmissionIssueKind.OWNER))
        if isinstance(raw_plan, (str, bytes, bytearray)) or not isinstance(raw_plan, Sequence):
            issues.append(self._issue(0, "", "browser_plan_not_sequence", "browser plan must be a sequence", PlanAdmissionIssueKind.SHAPE))
            return self._rejected(issues, raw_plan)
        if not raw_plan:
            issues.append(self._issue(0, "", "browser_plan_empty", "browser plan requires at least one action", PlanAdmissionIssueKind.SHAPE))
        if len(raw_plan) > self.config.maximum_steps:
            issues.append(
                self._issue(
                    0,
                    "",
                    "browser_plan_too_many_steps",
                    "browser plan exceeds the step limit",
                    PlanAdmissionIssueKind.SHAPE,
                    details={"maximum": self.config.maximum_steps, "actual": len(raw_plan)},
                )
            )
        try:
            plan_bytes = len(str(raw_plan).encode("utf-8", errors="replace"))
        except Exception:
            plan_bytes = self.config.maximum_plan_bytes + 1
        if plan_bytes > self.config.maximum_plan_bytes:
            issues.append(
                self._issue(
                    0,
                    "",
                    "browser_plan_too_large",
                    "browser plan exceeds the admission byte limit",
                    PlanAdmissionIssueKind.SHAPE,
                    details={"maximum": self.config.maximum_plan_bytes, "actual": plan_bytes},
                )
            )
        issues.extend(self._scan_request_bypass(request))

        active_target = self._active_target(target_runtime, issues)
        current_url = str(getattr(active_target, "url", "") or "")
        steps: list[PlanStep] = []
        for index, raw_step in enumerate(raw_plan, start=1):
            if not isinstance(raw_step, Mapping):
                issues.append(
                    self._issue(
                        index,
                        "",
                        "browser_plan_step_not_object",
                        "browser plan step must be an object",
                        PlanAdmissionIssueKind.SHAPE,
                        path=f"$.browser_plan[{index - 1}]",
                        details={"actual": type(raw_step).__name__},
                    )
                )
                continue
            step_issues, step = self._admit_step(
                request,
                session,
                raw_step,
                index=index,
                permission_session_id=permission_session_id,
                current_url=current_url,
                active_target=active_target,
            )
            issues.extend(step_issues)
            if step is not None:
                steps.append(step)

        schema_projection_digest = self._schema_projection_digest()
        bypass_scan_digest = digest_value(
            {
                "request_constraints": self._bypass_material(request.constraints),
                "request_metadata": self._bypass_material(request.metadata),
                "steps": [self._bypass_material(step) for step in raw_plan if isinstance(step, Mapping)],
            }
        )
        if issues or len(steps) != len(raw_plan):
            self._rejections += 1
            self._admissions += 1
            return PlanAdmission(
                admission_id=stable_id("bradmission", request.run_id, request.task_id, request.request_id, bypass_scan_digest, "rejected"),
                plan=None,
                issues=tuple(issues),
                registry_digest=self.registry.digest,
                schema_projection_digest=schema_projection_digest,
                bypass_scan_digest=bypass_scan_digest,
            )

        step_digests = tuple(step.digest for step in steps)
        plan_id = create_plan_id(
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.request_id,
            browser_session_id=str(session.session_id),
            step_digests=step_digests,
        )
        plan = BrowserActionPlan(
            plan_id=plan_id,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.request_id,
            browser_session_id=str(session.session_id),
            permission_session_id=permission_session_id,
            canonical_session_id=str(session.canonical_session_id),
            steps=tuple(steps),
            backend=self.config.backend,
            continue_on_error=bool(request.constraints.get("continue_on_error", False)),
        )
        self._admissions += 1
        return PlanAdmission(
            admission_id=stable_id("bradmission", plan.plan_id, plan.digest, self.registry.digest),
            plan=plan,
            issues=(),
            registry_digest=self.registry.digest,
            schema_projection_digest=schema_projection_digest,
            bypass_scan_digest=bypass_scan_digest,
        )

    def _admit_step(
        self,
        worker_request: WorkerRequest,
        session: Any,
        raw_step: Mapping[str, Any],
        *,
        index: int,
        permission_session_id: str,
        current_url: str,
        active_target: Any,
    ) -> tuple[list[PlanAdmissionIssue], PlanStep | None]:
        issues: list[PlanAdmissionIssue] = []
        raw_action = str(raw_step.get("action") or raw_step.get("browser_action") or raw_step.get("name") or "").strip()
        path = f"$.browser_plan[{index - 1}]"
        if not raw_action:
            issues.append(self._issue(index, "", "browser_action_missing", "browser plan step has no action", PlanAdmissionIssueKind.SHAPE, path=f"{path}.action"))
            return issues, None
        if self.config.reject_unknown_step_keys:
            for key in raw_step:
                normalized = str(key)
                if normalized in _RESERVED_STEP_KEYS:
                    issues.append(
                        self._issue(
                            index,
                            raw_action,
                            "permission_state_injection",
                            f"browser plan may not supply reserved state {normalized!r}",
                            PlanAdmissionIssueKind.BYPASS,
                            path=f"{path}.{normalized}",
                        )
                    )
                elif normalized not in _ALLOWED_STEP_KEYS:
                    issues.append(
                        self._issue(
                            index,
                            raw_action,
                            "unknown_plan_step_key",
                            f"browser plan step contains unknown key {normalized!r}",
                            PlanAdmissionIssueKind.SHAPE,
                            path=f"{path}.{normalized}",
                        )
                    )
        issues.extend(self._scan_step_bypass(index, raw_action, raw_step, path=path))
        arguments = raw_step.get("arguments", raw_step.get("args", {}))
        if not isinstance(arguments, Mapping):
            issues.append(
                self._issue(
                    index,
                    raw_action,
                    "browser_action_arguments_not_object",
                    "browser action arguments must be an object",
                    PlanAdmissionIssueKind.SCHEMA,
                    path=f"{path}.arguments",
                    details={"actual": type(arguments).__name__},
                )
            )
            return issues, None
        try:
            resolved = self.registry.resolve(raw_action, arguments)
        except BrowserActionRegistryError as exc:
            issues.append(
                self._issue(
                    index,
                    raw_action,
                    "unknown_browser_action",
                    str(exc),
                    PlanAdmissionIssueKind.UNKNOWN_ACTION,
                    path=f"{path}.action",
                )
            )
            return issues, None
        for validation in resolved.arguments.issues:
            if not validation.blocking:
                continue
            issues.append(
                self._issue(
                    index,
                    resolved.canonical_name,
                    validation.code,
                    validation.message,
                    PlanAdmissionIssueKind.SCHEMA,
                    path=f"{path}.arguments{validation.path.removeprefix('$')}",
                    details={"expected": validation.expected, "actual": validation.actual},
                )
            )
        if any(issue.step_index == index for issue in issues):
            return issues, None

        expectation: SelectorExpectation | None = None
        definition = resolved.definition
        if definition.selector_required or definition.target_kind in {
            ActionTargetKind.ELEMENT,
            ActionTargetKind.FILE_INPUT,
            ActionTargetKind.SELECT,
            ActionTargetKind.FRAME,
        }:
            selector_ref = str(resolved.arguments.values.get("selector_ref") or "").strip()
            if not selector_ref:
                issues.append(
                    self._issue(
                        index,
                        resolved.canonical_name,
                        "opaque_selector_ref_required",
                        "element action requires an opaque 04B selector_ref",
                        PlanAdmissionIssueKind.SELECTOR,
                        path=f"{path}.arguments.selector_ref",
                    )
                )
            else:
                try:
                    expectation = self._selector_expectation(selector_ref, raw_step)
                except Exception as exc:
                    issues.append(
                        self._issue(
                            index,
                            resolved.canonical_name,
                            getattr(exc, "code", "selector_expectation_failed"),
                            f"browser selector admission failed closed: {type(exc).__name__}: {exc}",
                            PlanAdmissionIssueKind.SELECTOR,
                            path=f"{path}.arguments.selector_ref",
                        )
                    )
        if issues:
            return issues, None

        deadline_at = self._deadline(raw_step, definition.timeout_seconds, worker_request.constraints)
        if deadline_at and parse_timestamp(deadline_at) <= dt.datetime.now(dt.UTC):
            issues.append(
                self._issue(
                    index,
                    resolved.canonical_name,
                    "browser_action_deadline_elapsed",
                    "browser action deadline elapsed before admission",
                    PlanAdmissionIssueKind.DEADLINE,
                    path=f"{path}.deadline_at",
                )
            )
            return issues, None

        target_url = str(resolved.arguments.values.get("url") or current_url)
        identity = ActionIdentity(
            run_id=worker_request.run_id,
            task_id=worker_request.task_id,
            worker_request_id=worker_request.request_id,
            session_id=permission_session_id,
            browser_session_id=str(session.session_id),
            step_index=index,
            node_id=worker_request.node_id or "",
            turn_id=str(worker_request.metadata.get("turn_id") or worker_request.constraints.get("browser_turn_id") or ""),
        )
        action_request = ActionRequest(
            identity=identity,
            action=resolved.canonical_name,
            arguments=dict(resolved.arguments.values),
            backend=self.config.backend,
            current_url=current_url,
            target_url=target_url,
            deadline_at=deadline_at,
            metadata={
                "registry_digest": self.registry.digest,
                "schema_identity": definition.identity,
                "source_action": raw_action,
                # The permission runtime must address the same logical tool use
                # before and after an ASK continuation.  Deriving this identity
                # from preflight arguments is unsafe because security receipts
                # are deliberately refreshed on resume.
                "permission_tool_use_id": identity.action_id,
                "active_target_id": str(getattr(active_target, "target_id", "") or ""),
                "active_target_generation": int(getattr(active_target, "generation", 0) or 0),
                "worker_node_id": worker_request.node_id or "",
            },
        )
        return issues, PlanStep(
            index=index,
            request=action_request,
            selector_expectation=expectation,
            original_action=raw_action,
            original_arguments_digest=digest_value(arguments),
            source=raw_step,
        )

    def _selector_expectation(self, selector_ref: str, raw_step: Mapping[str, Any]) -> SelectorExpectation:
        if self.config.require_opaque_selector_ref and not selector_ref.startswith("zyra-selector:"):
            raise BrowserActionIntegrationError(
                "opaque_selector_ref_required",
                "selector_ref is not a Zyra opaque 04B selector reference",
                phase=PlanPhase.STATIC_ADMISSION,
            )
        resolution = self.selector_store.resolve(selector_ref, require_current=True)
        entry = resolution.entry
        revision = resolution.revision
        supplied = raw_step.get("selector_expectation")
        if supplied is not None and not isinstance(supplied, Mapping):
            raise BrowserActionIntegrationError(
                "selector_expectation_not_object",
                "selector expectation assertion must be an object",
                phase=PlanPhase.STATIC_ADMISSION,
            )
        supplied_map = dict(supplied or {})
        comparisons = {
            "revision_id": revision.revision_id,
            "selector_index": entry.ref.selector_index,
            "backend_node_id": entry.backend_node_id,
            "frame_id": entry.frame_id,
            "stable_hash": entry.stable_hash,
            "attributes_digest": entry.attributes_digest,
            "target_id": revision.identity.target_id,
            "target_generation": revision.identity.target_generation,
            "cdp_session_id": entry.cdp_session_id,
            "cdp_generation": revision.identity.cdp_generation,
            "document_loader_id": revision.identity.document_loader_id,
        }
        mismatches: dict[str, Any] = {}
        for key, actual in comparisons.items():
            if key in supplied_map and supplied_map[key] not in (None, ""):
                expected = supplied_map[key]
                if isinstance(actual, int):
                    try:
                        expected = int(expected)
                    except (TypeError, ValueError):
                        pass
                if expected != actual:
                    mismatches[key] = {"expected": expected, "actual": actual}
        if mismatches:
            raise BrowserActionIntegrationError(
                "selector_assertion_mismatch",
                "caller selector assertion differs from authoritative 04B state",
                phase=PlanPhase.STATIC_ADMISSION,
                details={"mismatches": mismatches},
            )
        return SelectorExpectation(
            selector_ref=selector_ref,
            identity=revision.identity,
            revision_id=revision.revision_id,
            selector_index=entry.ref.selector_index,
            backend_node_id=entry.backend_node_id,
            frame_id=entry.frame_id,
            stable_hash=entry.stable_hash,
            attributes_digest=entry.attributes_digest,
            require_visible=bool(supplied_map.get("require_visible", True)),
            require_interactive=bool(supplied_map.get("require_interactive", True)),
            allow_disabled=bool(supplied_map.get("allow_disabled", False)),
            expected_tag=str(supplied_map.get("expected_tag") or entry.tag_name),
            expected_role=str(supplied_map.get("expected_role") or entry.role),
        )

    def _deadline(
        self,
        raw_step: Mapping[str, Any],
        definition_timeout: float,
        constraints: Mapping[str, Any],
    ) -> str:
        candidates: list[dt.datetime] = []
        for value in (raw_step.get("deadline_at"), constraints.get("browser_deadline_at"), constraints.get("deadline_at")):
            if value in (None, ""):
                continue
            try:
                candidates.append(parse_timestamp(str(value)))
            except (TypeError, ValueError) as exc:
                raise BrowserActionIntegrationError(
                    "invalid_browser_action_deadline",
                    f"browser action deadline is invalid: {exc}",
                    phase=PlanPhase.STATIC_ADMISSION,
                ) from exc
        timeout_value = raw_step.get("timeout_seconds", definition_timeout)
        try:
            timeout_seconds = float(timeout_value)
        except (TypeError, ValueError) as exc:
            raise BrowserActionIntegrationError(
                "invalid_browser_action_timeout",
                "browser action timeout must be numeric",
                phase=PlanPhase.STATIC_ADMISSION,
            ) from exc
        timeout_seconds = min(max(timeout_seconds, self.config.minimum_timeout_seconds), self.config.maximum_timeout_seconds)
        candidates.append(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=timeout_seconds))
        selected = min(candidates)
        return selected.isoformat().replace("+00:00", "Z")

    def _scan_request_bypass(self, request: WorkerRequest) -> list[PlanAdmissionIssue]:
        if not self.config.reject_bypass_metadata:
            return []
        issues: list[PlanAdmissionIssue] = []
        for root, value in (("constraints", request.constraints), ("metadata", request.metadata)):
            for path, key, item in walk_mapping(value, root=root):
                if (
                    root == "constraints"
                    and key in _TRUSTED_TOP_LEVEL_RUNTIME_FLAGS
                    and path == f"constraints.{key}"
                ):
                    continue
                if bypass_key(key) or bypass_value(item):
                    issues.append(
                        self._issue(
                            0,
                            "",
                            "browser_permission_bypass_attempt",
                            "browser request attempted to inject a permission bypass",
                            PlanAdmissionIssueKind.BYPASS,
                            path=path,
                            details={"key": key, "source": root},
                        )
                    )
        return issues

    def _scan_step_bypass(
        self,
        index: int,
        action: str,
        raw_step: Mapping[str, Any],
        *,
        path: str,
    ) -> list[PlanAdmissionIssue]:
        if not self.config.reject_bypass_metadata:
            return []
        issues: list[PlanAdmissionIssue] = []
        for item_path, key, value in walk_mapping(raw_step, root=path):
            if bypass_key(key) or bypass_value(value):
                issues.append(
                    self._issue(
                        index,
                        action,
                        "browser_permission_bypass_attempt",
                        "browser plan step attempted to inject a permission bypass",
                        PlanAdmissionIssueKind.BYPASS,
                        path=item_path,
                        details={"key": key},
                    )
                )
        return issues

    @staticmethod
    def _active_target(target_runtime: Any, issues: list[PlanAdmissionIssue]) -> Any:
        try:
            return target_runtime.ensure_valid_focus()
        except Exception as exc:
            issues.append(
                PlanAdmissionIssue(
                    0,
                    "",
                    "browser_active_target_unavailable",
                    f"04A active target is unavailable: {type(exc).__name__}: {exc}",
                    PlanAdmissionIssueKind.OWNER,
                )
            )
            return None

    def _schema_projection_digest(self) -> str:
        specs = self.registry.tool_specs()
        return digest_value(
            [
                {
                    "name": spec.name,
                    "input_schema": spec.input_schema,
                    "output_schema": spec.output_schema,
                    "metadata": spec.metadata,
                }
                for spec in specs
            ]
        )

    @staticmethod
    def _bypass_material(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, Mapping):
            return []
        return [
            {"path": path, "key": key, "flagged": bypass_key(key) or bypass_value(item)}
            for path, key, item in walk_mapping(value)
        ]

    def _rejected(self, issues: Sequence[PlanAdmissionIssue], raw_plan: Any) -> PlanAdmission:
        self._admissions += 1
        self._rejections += 1
        return PlanAdmission(
            admission_id=stable_id("bradmission", "rejected", digest_value(str(raw_plan)), self._admissions),
            plan=None,
            issues=tuple(issues),
            registry_digest=self.registry.digest,
            schema_projection_digest=self._schema_projection_digest(),
            bypass_scan_digest=digest_value(self._bypass_material(raw_plan) if isinstance(raw_plan, Mapping) else str(raw_plan)),
        )

    @staticmethod
    def _issue(
        step_index: int,
        action: str,
        code: str,
        message: str,
        kind: PlanAdmissionIssueKind,
        *,
        path: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> PlanAdmissionIssue:
        return PlanAdmissionIssue(step_index, action, code, message, kind, path, dict(details or {}))

    def _ensure_available(self) -> None:
        if self.disabled or self.registry is None or self.selector_store is None:
            raise BrowserActionIntegrationError(
                "browser_plan_adapter_disabled",
                "browser action plan adapter is disabled or missing an authoritative owner",
                phase=PlanPhase.STATIC_ADMISSION,
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-action-plan-adapter",
            "owner_unit": "M1-S04C-02",
            "disabled": self.disabled,
            "admissions": self._admissions,
            "rejections": self._rejections,
            "registry_digest": self.registry.digest if self.registry else "",
            "selector_owner": type(self.selector_store).__name__ if self.selector_store else "",
            "config": {
                "backend": self.config.backend,
                "maximum_steps": self.config.maximum_steps,
                "maximum_plan_bytes": self.config.maximum_plan_bytes,
                "maximum_timeout_seconds": self.config.maximum_timeout_seconds,
                "reject_unknown_step_keys": self.config.reject_unknown_step_keys,
                "require_opaque_selector_ref": self.config.require_opaque_selector_ref,
                "reject_bypass_metadata": self.config.reject_bypass_metadata,
            },
        }


def walk_mapping(value: Any, *, root: str = "$") -> tuple[tuple[str, str, Any], ...]:
    output: list[tuple[str, str, Any]] = []
    active: set[int] = set()

    def visit(item: Any, path: str, depth: int) -> None:
        if depth > 16:
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                return
            active.add(identity)
            for key, child in item.items():
                name = str(key)
                child_path = f"{path}.{name}" if path else name
                output.append((child_path, name, child))
                visit(child, child_path, depth + 1)
            active.remove(identity)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in active:
                return
            active.add(identity)
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]", depth + 1)
            active.remove(identity)

    visit(value, root, 0)
    return tuple(output)


def bypass_key(key: str) -> bool:
    normalized = str(key).strip().replace(" ", "_")
    return any(pattern.search(normalized) for pattern in _BYPASS_KEY_PATTERNS)


def bypass_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in _BYPASS_VALUE_WORDS
