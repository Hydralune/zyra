from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin

from zyra_workers.browser_state.contracts import (
    BrowserSelectorEntry,
    BrowserSelectorResolution,
    SelectorMapIdentity,
)

from .models import SelectorBinding, digest_value, stable_id
from .network_policy import CanonicalUrl, canonicalize_url


class SelectorGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class ElementIntent(StrEnum):
    PASSIVE = "passive"
    ACTIVATE = "activate"
    TEXT_INPUT = "text_input"
    SECRET_INPUT = "secret_input"
    FORM_SUBMIT = "form_submit"
    FILE_UPLOAD = "file_upload"
    DROPDOWN = "dropdown"
    CHECK_CONTROL = "check_control"
    DRAG_SOURCE = "drag_source"
    DRAG_TARGET = "drag_target"
    HOVER = "hover"


class ElementSensitivity(StrEnum):
    ORDINARY = "ordinary"
    AUTHENTICATION = "authentication"
    PAYMENT = "payment"
    DESTRUCTIVE = "destructive"
    PUBLISH = "publish"
    ACCOUNT = "account"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SelectorExpectation:
    selector_ref: str
    identity: SelectorMapIdentity
    revision_id: str = ""
    selector_index: int | None = None
    backend_node_id: int | None = None
    frame_id: str = ""
    stable_hash: str = ""
    attributes_digest: str = ""
    require_visible: bool = True
    require_interactive: bool = True
    allow_disabled: bool = False
    expected_tag: str = ""
    expected_role: str = ""

    def __post_init__(self) -> None:
        if not self.selector_ref:
            raise ValueError("selector expectation requires opaque selector_ref")
        if min(
            self.identity.target_generation,
            self.identity.cdp_generation,
            self.selector_index if self.selector_index is not None else 1,
            self.backend_node_id if self.backend_node_id is not None else 1,
        ) <= 0:
            raise ValueError("selector expectation generations and node ids must be positive")

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector_ref": self.selector_ref,
            "identity": self.identity.to_dict(),
            "revision_id": self.revision_id,
            "selector_index": self.selector_index,
            "backend_node_id": self.backend_node_id,
            "frame_id": self.frame_id,
            "stable_hash": self.stable_hash,
            "attributes_digest": self.attributes_digest,
            "require_visible": self.require_visible,
            "require_interactive": self.require_interactive,
            "allow_disabled": self.allow_disabled,
            "expected_tag": self.expected_tag,
            "expected_role": self.expected_role,
        }


@dataclass(frozen=True, slots=True)
class ElementSemantics:
    tag_name: str
    role: str
    accessible_name: str
    text_preview: str
    input_type: str
    form_action: str
    form_method: str
    intent: ElementIntent
    sensitivity: ElementSensitivity
    inferred_effects: tuple[str, ...]
    stable_hash: str
    attributes_digest: str
    live_probe_digest: str = ""
    form_owner_backend_node_id: int = 0
    form_enctype: str = ""
    form_target: str = ""

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag_name": self.tag_name,
            "role": self.role,
            "accessible_name": self.accessible_name,
            "text_preview": self.text_preview,
            "input_type": self.input_type,
            "form_action": self.form_action,
            "form_method": self.form_method,
            "intent": str(self.intent),
            "sensitivity": str(self.sensitivity),
            "inferred_effects": list(self.inferred_effects),
            "stable_hash": self.stable_hash,
            "attributes_digest": self.attributes_digest,
            "live_probe_digest": self.live_probe_digest,
            "form_owner_backend_node_id": self.form_owner_backend_node_id,
            "form_enctype": self.form_enctype,
            "form_target": self.form_target,
        }


@dataclass(frozen=True, slots=True)
class SelectorReceipt:
    receipt_id: str
    action_id: str
    expectation_digest: str
    binding: SelectorBinding
    semantics: ElementSemantics
    selector_ref: str
    current: bool
    probe_required: bool

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "expectation_digest": self.expectation_digest,
            "binding": self.binding.to_dict(),
            "semantics": self.semantics.to_dict(),
            "selector_ref": self.selector_ref,
            "current": self.current,
            "probe_required": self.probe_required,
            "binding_digest": self.binding.identity_digest,
        }


class SelectorStore(Protocol):
    def resolve(
        self,
        selector_ref: str,
        *,
        expected_identity: SelectorMapIdentity | None = None,
        require_current: bool = True,
    ) -> BrowserSelectorResolution: ...


class ElementSemanticProbe(Protocol):
    def enrich(
        self,
        entry: BrowserSelectorEntry,
        semantics: ElementSemantics,
        *,
        action: str,
        current_url: str,
    ) -> ElementSemantics: ...


class BrowserSelectorGuard:
    """Adapter to the authoritative 04B selector-map owner.

    This class does not persist selectors.  It resolves one opaque reference
    against the current 04B CAS revision and converts that existing identity to
    a permission/executor binding.  An index or backend id never authorizes an
    action by itself.
    """

    def __init__(
        self,
        store: SelectorStore,
        *,
        semantic_probe: ElementSemanticProbe | None = None,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.semantic_probe = semantic_probe
        self.disabled = disabled

    def resolve(
        self,
        *,
        action_id: str,
        expectation: SelectorExpectation,
        action: str,
        current_url: str,
        secret_input: bool = False,
    ) -> SelectorReceipt:
        if self.disabled:
            raise SelectorGuardError("selector_guard_disabled", "browser selector guard is disabled")
        try:
            resolution = self.store.resolve(
                expectation.selector_ref,
                expected_identity=expectation.identity,
                require_current=True,
            )
        except Exception as exc:
            raise SelectorGuardError(
                "selector_resolution_failed",
                f"browser selector could not be resolved as current: {exc}",
                details={"selector_ref": expectation.selector_ref},
            ) from exc
        self._match_expectation(expectation, resolution)
        entry = resolution.entry
        revision = resolution.revision
        semantics = derive_semantics(entry, action=action, current_url=current_url, secret_input=secret_input)
        if self.semantic_probe is not None:
            try:
                semantics = self.semantic_probe.enrich(
                    entry,
                    semantics,
                    action=action,
                    current_url=current_url,
                )
            except SelectorGuardError:
                raise
            except Exception as exc:
                raise SelectorGuardError(
                    "selector_semantic_probe_failed",
                    f"browser element semantics could not be read from the live DOM: {type(exc).__name__}: {exc}",
                    details={"selector_ref": expectation.selector_ref},
                ) from exc
        self._enforce_action_compatibility(action, entry, semantics)
        binding = SelectorBinding(
            browser_session_id=revision.identity.browser_session_id,
            target_id=revision.identity.target_id,
            cdp_session_id=entry.cdp_session_id,
            selector_revision_id=revision.revision_id,
            selector_generation=revision.revision,
            selector_ref=entry.ref.opaque_ref,
            backend_node_id=entry.backend_node_id,
            frame_id=entry.frame_id,
            document_loader_id=revision.identity.document_loader_id,
            target_generation=revision.identity.target_generation,
            cdp_generation=revision.identity.cdp_generation,
            capture_digest=revision.capture_digest,
        )
        receipt_id = stable_id(
            "brselector",
            action_id,
            expectation.digest,
            binding.identity_digest,
            semantics.digest,
        )
        return SelectorReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            expectation_digest=expectation.digest,
            binding=binding,
            semantics=semantics,
            selector_ref=entry.ref.opaque_ref,
            current=resolution.current,
            probe_required=action not in {"extract_text", "search_page", "snapshot_state"},
        )

    def revalidate(
        self,
        receipt: SelectorReceipt,
        *,
        expectation: SelectorExpectation,
        action: str,
        current_url: str,
        secret_input: bool = False,
    ) -> SelectorReceipt:
        current = self.resolve(
            action_id=receipt.action_id,
            expectation=expectation,
            action=action,
            current_url=current_url,
            secret_input=secret_input,
        )
        if current.binding.identity_digest != receipt.binding.identity_digest:
            raise SelectorGuardError(
                "selector_binding_changed",
                "browser selector identity changed after action approval",
                details={
                    "approved_binding": receipt.binding.identity_digest,
                    "current_binding": current.binding.identity_digest,
                },
            )
        if current.semantics.digest != receipt.semantics.digest:
            raise SelectorGuardError(
                "selector_semantics_changed",
                "browser selector semantics changed after action approval",
                details={
                    "approved_semantics": receipt.semantics.digest,
                    "current_semantics": current.semantics.digest,
                },
            )
        return current

    @staticmethod
    def _match_expectation(expectation: SelectorExpectation, resolution: BrowserSelectorResolution) -> None:
        entry = resolution.entry
        revision = resolution.revision
        mismatches: dict[str, Any] = {}
        if expectation.revision_id and revision.revision_id != expectation.revision_id:
            mismatches["revision_id"] = (expectation.revision_id, revision.revision_id)
        if expectation.selector_index is not None and entry.ref.selector_index != expectation.selector_index:
            mismatches["selector_index"] = (expectation.selector_index, entry.ref.selector_index)
        if expectation.backend_node_id is not None and entry.backend_node_id != expectation.backend_node_id:
            mismatches["backend_node_id"] = (expectation.backend_node_id, entry.backend_node_id)
        if expectation.frame_id and entry.frame_id != expectation.frame_id:
            mismatches["frame_id"] = (expectation.frame_id, entry.frame_id)
        if expectation.stable_hash and entry.stable_hash != expectation.stable_hash:
            mismatches["stable_hash"] = (expectation.stable_hash, entry.stable_hash)
        if expectation.attributes_digest and entry.attributes_digest != expectation.attributes_digest:
            mismatches["attributes_digest"] = (expectation.attributes_digest, entry.attributes_digest)
        if expectation.expected_tag and entry.tag_name.casefold() != expectation.expected_tag.casefold():
            mismatches["tag_name"] = (expectation.expected_tag, entry.tag_name)
        if expectation.expected_role and entry.role.casefold() != expectation.expected_role.casefold():
            mismatches["role"] = (expectation.expected_role, entry.role)
        if expectation.require_visible and not entry.visible:
            mismatches["visible"] = (True, entry.visible)
        if expectation.require_interactive and not entry.interactive:
            mismatches["interactive"] = (True, entry.interactive)
        if not expectation.allow_disabled and entry.disabled:
            mismatches["disabled"] = (False, entry.disabled)
        if entry.cdp_session_id != expectation.identity.cdp_session_id and not entry.frame_id:
            mismatches["cdp_session_id"] = (expectation.identity.cdp_session_id, entry.cdp_session_id)
        if mismatches:
            raise SelectorGuardError(
                "selector_identity_mismatch",
                "browser selector does not match its approved composite identity",
                details={"mismatches": mismatches, "selector_ref": expectation.selector_ref},
            )

    @staticmethod
    def _enforce_action_compatibility(action: str, entry: BrowserSelectorEntry, semantics: ElementSemantics) -> None:
        tag = entry.tag_name.casefold()
        role = entry.role.casefold()
        if action == "upload_file" and not (tag == "input" and semantics.input_type == "file"):
            raise SelectorGuardError("file_input_required", "upload requires the exact approved file input element")
        if action in {"select_dropdown", "get_dropdown_options"} and not (tag == "select" or role in {"combobox", "listbox"}):
            raise SelectorGuardError("select_control_required", "dropdown action requires an approved select control")
        if action == "input_text" and semantics.input_type in {"file", "button", "submit", "reset", "checkbox", "radio"}:
            raise SelectorGuardError("text_input_required", "input_text cannot target this control type")
        if action == "check_element" and not (semantics.input_type in {"checkbox", "radio"} or role in {"checkbox", "radio", "switch"}):
            raise SelectorGuardError("check_control_required", "check action requires a checkbox, radio or switch")
        if action in {"click_element", "submit_form", "drag_element", "hover_element"} and not entry.interactive:
            raise SelectorGuardError("interactive_element_required", "browser action requires an interactive element")


SENSITIVE_TERMS: dict[ElementSensitivity, tuple[str, ...]] = {
    ElementSensitivity.AUTHENTICATION: ("login", "sign in", "password", "otp", "verify", "authorize"),
    ElementSensitivity.PAYMENT: ("pay", "purchase", "checkout", "card", "billing", "transfer"),
    ElementSensitivity.DESTRUCTIVE: ("delete", "remove", "erase", "destroy", "terminate", "cancel subscription"),
    ElementSensitivity.PUBLISH: ("publish", "post", "send", "submit", "share", "upload"),
    ElementSensitivity.ACCOUNT: ("account", "profile", "permission", "role", "security", "email change"),
}


def derive_semantics(
    entry: BrowserSelectorEntry,
    *,
    action: str,
    current_url: str,
    secret_input: bool,
) -> ElementSemantics:
    text = " ".join((entry.accessible_name, entry.text_preview, entry.role, entry.tag_name)).casefold()
    tag = entry.tag_name.casefold()
    role = entry.role.casefold()
    input_type = infer_input_type(entry)
    form_action = ""
    form_method = ""
    attributes = parse_attribute_hints(entry)
    if attributes.get("formaction"):
        form_action = urljoin(current_url, attributes["formaction"])
    elif attributes.get("action") and tag == "form":
        form_action = urljoin(current_url, attributes["action"])
    form_method = attributes.get("formmethod", attributes.get("method", "get")).lower()
    intent = action_intent(action, tag=tag, role=role, input_type=input_type, secret_input=secret_input)
    sensitivity = ElementSensitivity.ORDINARY
    for candidate, terms in SENSITIVE_TERMS.items():
        if any(term in text for term in terms):
            sensitivity = candidate
            break
    effects: list[str] = []
    if intent == ElementIntent.FORM_SUBMIT or input_type == "submit" or role == "button" and "submit" in text:
        effects.extend(("form_submit", "network_request", "navigation_possible"))
    if form_action:
        effects.append("cross_origin_possible")
    if intent == ElementIntent.FILE_UPLOAD:
        effects.extend(("file_read", "data_egress", "network_request_possible"))
    if intent == ElementIntent.SECRET_INPUT:
        effects.extend(("secret_materialization", "credential_egress_possible"))
    if action == "click_element":
        effects.extend(("navigation_possible", "popup_possible", "download_possible"))
    if action == "select_dropdown":
        effects.append("framework_change_event")
    if action == "check_element":
        effects.append("framework_change_event")
    return ElementSemantics(
        tag_name=tag,
        role=role,
        accessible_name=entry.accessible_name,
        text_preview=entry.text_preview,
        input_type=input_type,
        form_action=form_action,
        form_method=form_method,
        intent=intent,
        sensitivity=sensitivity,
        inferred_effects=tuple(dict.fromkeys(effects)),
        stable_hash=entry.stable_hash,
        attributes_digest=entry.attributes_digest,
    )


def action_intent(action: str, *, tag: str, role: str, input_type: str, secret_input: bool) -> ElementIntent:
    if secret_input:
        return ElementIntent.SECRET_INPUT
    if action == "upload_file":
        return ElementIntent.FILE_UPLOAD
    if action in {"select_dropdown", "get_dropdown_options"}:
        return ElementIntent.DROPDOWN
    if action == "check_element":
        return ElementIntent.CHECK_CONTROL
    if action == "input_text":
        return ElementIntent.TEXT_INPUT
    if action == "submit_form" or input_type == "submit":
        return ElementIntent.FORM_SUBMIT
    if action == "drag_element":
        return ElementIntent.DRAG_SOURCE
    if action == "hover_element":
        return ElementIntent.HOVER
    if action == "click_element":
        return ElementIntent.ACTIVATE
    return ElementIntent.PASSIVE


def infer_input_type(entry: BrowserSelectorEntry) -> str:
    attributes = parse_attribute_hints(entry)
    if entry.tag_name.casefold() == "textarea":
        return "textarea"
    if entry.tag_name.casefold() == "select":
        return "select"
    value = attributes.get("type", "")
    if value:
        return value.casefold()
    role = entry.role.casefold()
    role_map = {
        "textbox": "text",
        "searchbox": "search",
        "checkbox": "checkbox",
        "radio": "radio",
        "switch": "checkbox",
        "button": "button",
    }
    return role_map.get(role, "text" if entry.tag_name.casefold() == "input" else "")


def parse_attribute_hints(entry: BrowserSelectorEntry) -> dict[str, str]:
    """Parse only non-secret selector hints; authoritative identity stays the digest."""
    hints: dict[str, str] = {}
    corpus = " ".join((entry.css_hint, entry.xpath, entry.text_preview))
    for name in ("type", "action", "method", "formaction", "formmethod"):
        patterns = (
            rf"\[{name}=['\"]?([^'\"\]\s]+)",
            rf"@{name}=['\"]([^'\"]+)",
            rf"\b{name}=([^\s]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, corpus, re.IGNORECASE)
            if match:
                hints[name] = match.group(1)[:2048]
                break
    return hints
