from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin

from zyra_workers.browser_state.contracts import BrowserSelectorEntry

from .models import digest_value
from .selector_guard import (
    ElementIntent,
    ElementSemantics,
    ElementSensitivity,
    SENSITIVE_TERMS,
    SelectorGuardError,
    action_intent,
)


@dataclass(frozen=True, slots=True)
class LiveSemanticEvidence:
    backend_node_id: int
    cdp_session_id: str
    connected: bool
    tag_name: str
    role: str
    input_type: str
    disabled: bool
    content_editable: bool
    form_action: str
    form_method: str
    form_enctype: str
    form_target: str
    form_owner_backend_node_id: int
    form_no_validate: bool
    button_type: str
    href: str
    download_name: str
    accessible_label: str
    text_preview: str
    probe_revision: int = 1

    def __post_init__(self) -> None:
        if self.backend_node_id < 1 or not self.cdp_session_id:
            raise ValueError("live element semantic evidence identity is incomplete")

    @property
    def digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "backend_node_id": self.backend_node_id,
            "cdp_session_id": self.cdp_session_id,
            "connected": self.connected,
            "tag_name": self.tag_name,
            "role": self.role,
            "input_type": self.input_type,
            "disabled": self.disabled,
            "content_editable": self.content_editable,
            "form_action": self.form_action,
            "form_method": self.form_method,
            "form_enctype": self.form_enctype,
            "form_target": self.form_target,
            "form_owner_backend_node_id": self.form_owner_backend_node_id,
            "form_no_validate": self.form_no_validate,
            "button_type": self.button_type,
            "href": self.href,
            "download_name": self.download_name,
            "accessible_label": self.accessible_label,
            "text_preview": self.text_preview,
            "probe_revision": self.probe_revision,
        }


class CdpElementSemanticProbe:
    """Read direct attributes and nearest ancestor form from the live DOM.

    Model parameters never become authoritative form action/method metadata.
    The probe resolves the exact 04B backend node in its exact OOPIF CDP
    session, then executes a side-effect-free DOM introspection function.
    """

    _FUNCTION = """function(){
const element=this;
const form=(element.form instanceof HTMLFormElement?element.form:element.closest&&element.closest('form'))||null;
const text=(element.innerText||element.textContent||'').replace(/\\s+/g,' ').trim().slice(0,500);
const label=(element.getAttribute('aria-label')||element.getAttribute('title')||'').slice(0,500);
return {
 connected:Boolean(element.isConnected),
 tag:String(element.tagName||'').toLowerCase(),
 role:String(element.getAttribute&&element.getAttribute('role')||'').toLowerCase(),
 inputType:String(element.getAttribute&&element.getAttribute('type')||element.type||'').toLowerCase(),
 disabled:Boolean(element.disabled||element.getAttribute&&element.getAttribute('aria-disabled')==='true'),
 contentEditable:Boolean(element.isContentEditable),
 buttonType:String(element.type||'').toLowerCase(),
 href:String(element.href||''),
 downloadName:String(element.getAttribute&&element.getAttribute('download')||''),
 accessibleLabel:label,
 textPreview:text,
 form:form?{
  action:String(form.action||''),
  method:String(form.method||'get').toLowerCase(),
  enctype:String(form.enctype||''),
  target:String(form.target||''),
  noValidate:Boolean(form.noValidate),
  backendMarker:String(form.getAttribute('data-zyra-backend-node-id')||'')
 }:null
};
}"""

    def __init__(self, transport: Any, *, disabled: bool = False) -> None:
        self.transport = transport
        self.disabled = disabled
        self.probes = 0
        self.failures = 0

    def probe(self, entry: BrowserSelectorEntry) -> LiveSemanticEvidence:
        if self.disabled:
            raise SelectorGuardError(
                "selector_semantic_probe_disabled",
                "live element semantic probe is disabled",
            )
        try:
            resolved = self.transport.send(
                "DOM.resolveNode",
                {"backendNodeId": entry.backend_node_id},
                cdp_session_id=entry.cdp_session_id,
            )
            object_id = str(dict(resolved.get("object") or {}).get("objectId") or "")
            if not object_id:
                raise SelectorGuardError(
                    "selector_semantic_object_missing",
                    "live browser element could not be resolved for semantic capture",
                )
            response = self.transport.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": self._FUNCTION,
                    "returnByValue": True,
                    "throwOnSideEffect": True,
                    "silent": True,
                },
                cdp_session_id=entry.cdp_session_id,
            )
            if response.get("exceptionDetails"):
                raise SelectorGuardError(
                    "selector_semantic_probe_exception",
                    "live element semantic probe raised an exception",
                )
            value = dict(dict(response.get("result") or {}).get("value") or {})
            form = dict(value.get("form") or {})
            evidence = LiveSemanticEvidence(
                backend_node_id=entry.backend_node_id,
                cdp_session_id=entry.cdp_session_id,
                connected=bool(value.get("connected")),
                tag_name=str(value.get("tag") or entry.tag_name).casefold(),
                role=str(value.get("role") or entry.role).casefold(),
                input_type=str(value.get("inputType") or "").casefold(),
                disabled=bool(value.get("disabled")),
                content_editable=bool(value.get("contentEditable")),
                form_action=str(form.get("action") or ""),
                form_method=str(form.get("method") or "get").casefold(),
                form_enctype=str(form.get("enctype") or ""),
                form_target=str(form.get("target") or ""),
                form_owner_backend_node_id=_safe_int(form.get("backendMarker")),
                form_no_validate=bool(form.get("noValidate")),
                button_type=str(value.get("buttonType") or "").casefold(),
                href=str(value.get("href") or ""),
                download_name=str(value.get("downloadName") or ""),
                accessible_label=str(value.get("accessibleLabel") or "")[:500],
                text_preview=str(value.get("textPreview") or "")[:500],
            )
        except SelectorGuardError:
            self.failures += 1
            raise
        except Exception as exc:
            self.failures += 1
            raise SelectorGuardError(
                "selector_semantic_probe_failed",
                f"live element semantic capture failed: {type(exc).__name__}: {exc}",
            ) from exc
        self.probes += 1
        return evidence

    def enrich(
        self,
        entry: BrowserSelectorEntry,
        semantics: ElementSemantics,
        *,
        action: str,
        current_url: str,
    ) -> ElementSemantics:
        evidence = self.probe(entry)
        if not evidence.connected:
            raise SelectorGuardError(
                "selector_disconnected",
                "live browser element is disconnected from the document",
            )
        if evidence.tag_name and evidence.tag_name != entry.tag_name.casefold():
            raise SelectorGuardError(
                "selector_live_tag_changed",
                "live browser element tag changed after 04B capture",
                details={"captured": entry.tag_name, "live": evidence.tag_name},
            )
        if evidence.disabled and not entry.disabled:
            raise SelectorGuardError(
                "selector_live_disabled",
                "live browser element became disabled after 04B capture",
            )
        input_type = evidence.input_type or semantics.input_type
        form_action = urljoin(current_url, evidence.form_action) if evidence.form_action else ""
        form_method = evidence.form_method or "get"
        intent = action_intent(
            action,
            tag=evidence.tag_name or semantics.tag_name,
            role=evidence.role or semantics.role,
            input_type=input_type,
            secret_input=(semantics.intent == ElementIntent.SECRET_INPUT),
        )
        text = " ".join(
            (
                evidence.accessible_label,
                evidence.text_preview,
                evidence.role,
                evidence.tag_name,
            )
        ).casefold()
        sensitivity = semantics.sensitivity
        for candidate, terms in SENSITIVE_TERMS.items():
            if any(term in text for term in terms):
                sensitivity = candidate
                break
        effects = list(semantics.inferred_effects)
        if form_action:
            effects.extend(("form_owner_live", "network_request_possible"))
        if form_method not in {"", "get"}:
            effects.append("state_changing_form_method")
        if evidence.form_target and evidence.form_target not in {"", "_self"}:
            effects.append("new_target_possible")
        if evidence.download_name:
            effects.extend(("download_possible", "file_write_possible"))
        if evidence.href:
            effects.append("navigation_possible")
        if evidence.form_no_validate:
            effects.append("form_validation_disabled")
        return replace(
            semantics,
            tag_name=evidence.tag_name or semantics.tag_name,
            role=evidence.role or semantics.role,
            accessible_name=evidence.accessible_label or semantics.accessible_name,
            text_preview=evidence.text_preview or semantics.text_preview,
            input_type=input_type,
            form_action=form_action,
            form_method=form_method,
            intent=intent,
            sensitivity=sensitivity,
            inferred_effects=tuple(dict.fromkeys(effects)),
            live_probe_digest=evidence.digest,
            form_owner_backend_node_id=evidence.form_owner_backend_node_id,
            form_enctype=evidence.form_enctype,
            form_target=evidence.form_target,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-cdp-element-semantic-probe",
            "owner_unit": "M1-S04C-02",
            "disabled": self.disabled,
            "probes": self.probes,
            "failures": self.failures,
        }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
