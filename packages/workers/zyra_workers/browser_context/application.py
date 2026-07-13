from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..browser_state.contracts import BrowserDisclosureBudget
from ..browser_state.errors import BrowserSelectorMapMissing
from ..browser_state.runtime import BrowserDomStateRuntime
from ..browser_state.selector_store import BrowserSelectorMapStore
from ..browser_state.state_delta import compare_selector_revisions
from ..browser_state.watchdog import BrowserDomWatchdog
from .externalizer import BrowserStateArtifactExternalizer
from .message_manager import BrowserMessageManagerRuntime
from .models import BrowserMessageTurn
from .turn_store import BrowserTurnProjectionStore


class BrowserMessageStateApplication:
    """Registry-scoped 04A -> 04B application service."""

    def __new__(cls, browser_runtime: Any, *args: Any, **kwargs: Any):
        if browser_runtime is None or bool(kwargs.get("disabled")):
            return super().__new__(cls)
        existing = getattr(browser_runtime, "_browser_message_state_application", None)
        if isinstance(existing, cls):
            return existing
        instance = super().__new__(cls)
        setattr(browser_runtime, "_browser_message_state_application", instance)
        return instance

    def __init__(
        self,
        browser_runtime: Any,
        *,
        artifact_store: Any,
        selector_store: BrowserSelectorMapStore | None = None,
        dom_runtime: BrowserDomStateRuntime | None = None,
        message_manager: BrowserMessageManagerRuntime | None = None,
        watchdog: BrowserDomWatchdog | None = None,
        turn_store: BrowserTurnProjectionStore | None = None,
        disabled: bool = False,
    ) -> None:
        if getattr(self, "_initialized", False):
            return
        if browser_runtime is None:
            raise ValueError("BrowserMessageStateApplication requires the existing 04A BrowserRuntime")
        self.browser_runtime = browser_runtime
        self.artifact_store = artifact_store
        self.disabled = disabled
        state_root = Path(browser_runtime.config.state_root) / "dom-state"
        self.selector_store = selector_store or BrowserSelectorMapStore(state_root, disabled=disabled)
        self.turn_store = turn_store or BrowserTurnProjectionStore(state_root, disabled=disabled)
        self.dom_runtime = dom_runtime or BrowserDomStateRuntime(
            request_timeout_seconds=browser_runtime.config.request_timeout_seconds,
            disabled=disabled,
        )
        self.watchdog = watchdog or BrowserDomWatchdog(disabled=disabled)
        self.message_manager = message_manager or BrowserMessageManagerRuntime(
            externalizer=BrowserStateArtifactExternalizer(artifact_store, disabled=disabled),
            disabled=disabled,
        )
        self._captures = 0
        self._failures = 0
        self._initialized = True

    def read_state(
        self,
        *,
        request: Any,
        session_start: Any,
        action_receipts: Sequence[Any] = (),
        budget: BrowserDisclosureBudget | None = None,
        context_window: Any | None = None,
        source_event_id: str = "",
    ) -> BrowserMessageTurn:
        if self.disabled:
            raise RuntimeError("browser message/state application is disabled")
        session = session_start.session
        constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
        effective_budget = budget or _budget_from_constraints(constraints)
        previous_facts = tuple(dict.fromkeys((
            *self.turn_store.previous_facts(
                session.canonical_session_id,
                browser_session_id=session.session_id,
            ),
            *(str(item) for item in constraints.get("browser_previous_facts", ()) if str(item)),
        )))
        try:
            capture = self.dom_runtime.capture_from_browser_runtime(
                self.browser_runtime,
                run_id=request.run_id,
                task_id=request.task_id,
                canonical_session_id=session.canonical_session_id,
                browser_session_id=session.session_id,
                session_revision=session.revision,
                worker_request_id=request.request_id,
                node_id=request.node_id or "",
                step_index=len(action_receipts),
                constraints=constraints,
            )
            try:
                previous = self.selector_store.latest(
                    session.session_id,
                    capture.request.target_id,
                    require_current=False,
                )
                expected = previous.revision_id
            except BrowserSelectorMapMissing:
                previous = None
                expected = ""
            revision = self.selector_store.commit(
                capture,
                expected_previous_revision_id=expected,
            )
            dom_delta = compare_selector_revisions(previous, revision)
            self.watchdog.observe(capture, revision, delta=dom_delta)
            turn = self.message_manager.build_turn(
                capture,
                revision,
                action_receipts,
                budget=effective_budget,
                goal=str(constraints.get("goal") or constraints.get("browser_goal") or ""),
                constraints=_constraint_texts(constraints),
                previous_facts=previous_facts,
                context_window=context_window,
                source_event_id=source_event_id,
                dom_delta=dom_delta,
            )
            previous_turn = self.turn_store.latest(
                session.canonical_session_id,
                session.session_id,
                capture.request.target_id,
            )
            self.turn_store.record(
                turn,
                canonical_session_id=session.canonical_session_id,
                target_id=capture.request.target_id,
                target_generation=capture.request.target_generation,
                cdp_session_id=capture.request.cdp_session_id,
                cdp_generation=capture.request.cdp_generation,
                document_loader_id=capture.request.document_loader_id,
                capture_digest=capture.state_digest,
                expected_previous_turn_id=previous_turn.turn_id if previous_turn else "",
            )
        except Exception:
            self._failures += 1
            raise
        self._captures += 1
        return turn

    def selector_preflight(
        self,
        selector_ref: str,
        *,
        browser_session_id: str,
        target_id: str,
    ) -> Any:
        revision = self.selector_store.latest(browser_session_id, target_id)
        self.watchdog.assert_selector_revision(browser_session_id, revision)
        return self.selector_store.resolve(
            selector_ref,
            expected_identity=revision.identity,
            require_current=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserMessageStateApplication",
            "owner_unit": "M1-S04B-01",
            "disabled": self.disabled,
            "captures": self._captures,
            "failures": self._failures,
            "dom_runtime": self.dom_runtime.snapshot().to_dict(),
            "capture_policy": self.dom_runtime.capture_policy.snapshot(),
            "selector_store": self.selector_store.snapshot(),
            "message_manager": self.message_manager.snapshot(),
            "watchdog": self.watchdog.snapshot(),
            "turn_store": self.turn_store.snapshot().to_dict(),
        }


def _budget_from_constraints(constraints: Mapping[str, Any]) -> BrowserDisclosureBudget:
    raw = constraints.get("browser_context_budget")
    if not isinstance(raw, Mapping):
        return BrowserDisclosureBudget()
    allowed = {
        "max_context_tokens", "max_context_bytes", "max_inline_selectors", "max_inline_facts",
        "max_fact_chars", "max_action_result_tokens", "raw_externalize_bytes", "preserve_recent_actions",
    }
    values = {key: int(value) for key, value in raw.items() if key in allowed and value not in (None, "")}
    return BrowserDisclosureBudget(**values)


def _constraint_texts(constraints: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("constraints", "requirements", "allowed_domains", "denied_domains", "privacy", "deadline"):
        value = constraints.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values.extend(str(item) for item in value if str(item))
        elif value not in (None, ""):
            values.append(f"{key}: {value}")
    return tuple(values)
