from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin

from .models import digest_value, stable_id
from .network_policy import canonicalize_url
from .selector_guard import ElementSensitivity, SelectorReceipt


class FormPolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class FormMethod(StrEnum):
    GET = "get"
    POST = "post"
    DIALOG = "dialog"


class FormEffect(StrEnum):
    NAVIGATION = "navigation"
    NETWORK_WRITE = "network_write"
    CREDENTIAL_SUBMISSION = "credential_submission"
    FILE_EGRESS = "file_egress"
    PAYMENT = "payment"
    DESTRUCTIVE = "destructive"
    PUBLISH = "publish"
    ACCOUNT_CHANGE = "account_change"
    UNKNOWN_EXTERNAL_EFFECT = "unknown_external_effect"


@dataclass(frozen=True, slots=True)
class FormPolicyConfig:
    allow_cross_origin_get: bool = False
    allow_cross_origin_post: bool = False
    allow_http_downgrade: bool = False
    maximum_field_count: int = 256
    maximum_public_value_chars: int = 64_000
    policy_version: str = "browser-form-v1"

    def __post_init__(self) -> None:
        if self.maximum_field_count < 1 or self.maximum_public_value_chars < 1:
            raise ValueError("browser form policy limits must be positive")

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "allow_cross_origin_get": self.allow_cross_origin_get,
                "allow_cross_origin_post": self.allow_cross_origin_post,
                "allow_http_downgrade": self.allow_http_downgrade,
                "maximum_field_count": self.maximum_field_count,
                "maximum_public_value_chars": self.maximum_public_value_chars,
                "policy_version": self.policy_version,
            }
        )


@dataclass(frozen=True, slots=True)
class FormFieldSummary:
    name: str
    value_kind: str
    value_chars: int
    secret_reference: bool
    file_reference: bool
    field_digest: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_kind": self.value_kind,
            "value_chars": self.value_chars,
            "secret_reference": self.secret_reference,
            "file_reference": self.file_reference,
            "field_digest": self.field_digest,
        }


@dataclass(frozen=True, slots=True)
class FormReceipt:
    receipt_id: str
    action_id: str
    selector_binding_digest: str
    current_origin: str
    destination_origin: str
    action_url: str
    method: FormMethod
    fields: tuple[FormFieldSummary, ...]
    effects: tuple[FormEffect, ...]
    policy_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "effects", tuple(self.effects))

    @property
    def cross_origin(self) -> bool:
        return self.current_origin != self.destination_origin

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "selector_binding_digest": self.selector_binding_digest,
            "current_origin": self.current_origin,
            "destination_origin": self.destination_origin,
            "action_url": self.action_url,
            "method": str(self.method),
            "fields": [item.public_dict() for item in self.fields],
            "effects": [str(item) for item in self.effects],
            "policy_digest": self.policy_digest,
            "cross_origin": self.cross_origin,
        }


class BrowserFormPolicy:
    """Deterministic form effect classifier and origin downgrade fence."""

    SENSITIVE_FIELD = re.compile(
        r"(?:pass|pwd|token|otp|totp|secret|card|cvv|cvc|iban|routing|account|ssn|identity)",
        re.IGNORECASE,
    )

    def __init__(self, config: FormPolicyConfig | None = None, *, disabled: bool = False) -> None:
        self.config = config or FormPolicyConfig()
        self.disabled = disabled

    def preflight(
        self,
        *,
        action_id: str,
        selector: SelectorReceipt,
        current_url: str,
        arguments: Mapping[str, Any],
    ) -> FormReceipt:
        if self.disabled:
            raise FormPolicyError("form_policy_disabled", "browser form policy is disabled")
        current = canonicalize_url(current_url)
        # Selector/DOM semantics are authoritative.  Model-supplied values are
        # assertions used to detect a changed or misunderstood form, never an
        # authority for choosing a destination or method.
        raw_action = selector.semantics.form_action or current.url
        action_url = urljoin(current.url, raw_action)
        destination = canonicalize_url(action_url)
        method_raw = str(selector.semantics.form_method or "get").lower()
        try:
            method = FormMethod(method_raw)
        except ValueError as exc:
            raise FormPolicyError("form_method_denied", f"unsupported browser form method {method_raw!r}") from exc
        asserted_action = arguments.get("form_action_url") or arguments.get("form_action")
        if asserted_action:
            asserted = canonicalize_url(urljoin(current.url, str(asserted_action)))
            if asserted.url != destination.url:
                raise FormPolicyError(
                    "form_action_hint_mismatch",
                    "requested form destination does not match current DOM semantics",
                    details={"dom_origin": destination.origin, "asserted_origin": asserted.origin},
                )
        asserted_method = arguments.get("method") or arguments.get("form_method")
        if asserted_method and str(asserted_method).lower() != str(method):
            raise FormPolicyError(
                "form_method_hint_mismatch",
                "requested form method does not match current DOM semantics",
                details={"dom_method": str(method), "asserted_method": str(asserted_method).lower()},
            )
        if current.scheme == "https" and destination.scheme == "http" and not self.config.allow_http_downgrade:
            raise FormPolicyError("form_https_downgrade", "HTTPS form submission cannot downgrade to HTTP")
        cross_origin = current.origin != destination.origin
        if cross_origin and method == FormMethod.GET and not self.config.allow_cross_origin_get:
            raise FormPolicyError("cross_origin_form_get_denied", "cross-origin form GET is prohibited")
        if cross_origin and method == FormMethod.POST and not self.config.allow_cross_origin_post:
            raise FormPolicyError("cross_origin_form_post_denied", "cross-origin form POST is prohibited")
        fields = summarize_fields(arguments.get("field_summary", arguments.get("fields", {})), config=self.config)
        effects = infer_form_effects(selector, method=method, fields=fields)
        receipt_id = stable_id(
            "brform",
            action_id,
            selector.binding.identity_digest,
            current.origin,
            destination.origin,
            action_url,
            method,
            [item.public_dict() for item in fields],
            effects,
            self.config.digest,
        )
        return FormReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            selector_binding_digest=selector.binding.identity_digest,
            current_origin=current.origin,
            destination_origin=destination.origin,
            action_url=action_url,
            method=method,
            fields=fields,
            effects=effects,
            policy_digest=self.config.digest,
        )

    def revalidate(
        self,
        receipt: FormReceipt,
        *,
        selector: SelectorReceipt,
        current_url: str,
        arguments: Mapping[str, Any],
    ) -> FormReceipt:
        if receipt.policy_digest != self.config.digest:
            raise FormPolicyError("form_policy_changed", "browser form policy changed after approval")
        current = self.preflight(
            action_id=receipt.action_id,
            selector=selector,
            current_url=current_url,
            arguments=arguments,
        )
        if current.receipt_id != receipt.receipt_id:
            raise FormPolicyError(
                "form_binding_changed",
                "browser form action, method, fields or selector changed after approval",
                details={"approved": receipt.binding_digest, "current": current.binding_digest},
            )
        return current


def summarize_fields(value: Any, *, config: FormPolicyConfig) -> tuple[FormFieldSummary, ...]:
    if value in (None, {}):
        return ()
    if not isinstance(value, Mapping):
        raise FormPolicyError("form_fields_invalid", "browser form fields must be an object")
    if len(value) > config.maximum_field_count:
        raise FormPolicyError("form_field_count_exceeded", "browser form contains too many fields")
    summaries: list[FormFieldSummary] = []
    total_chars = 0
    for raw_name, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
        name = str(raw_name)
        if not name or len(name) > 512:
            raise FormPolicyError("form_field_name_invalid", "browser form field name is empty or too long")
        secret_reference = isinstance(raw_value, Mapping) and "$zyra_secret_ref" in raw_value
        file_reference = isinstance(raw_value, Mapping) and "$zyra_file_ref" in raw_value
        if secret_reference:
            kind = "secret_reference"
            chars = 0
        elif file_reference:
            kind = "file_reference"
            chars = 0
        elif isinstance(raw_value, bool):
            kind = "boolean"
            chars = 0
        elif isinstance(raw_value, int | float):
            kind = "number"
            chars = len(str(raw_value))
        elif isinstance(raw_value, str):
            kind = "string"
            chars = len(raw_value)
        elif isinstance(raw_value, Sequence) and not isinstance(raw_value, bytes | bytearray | str):
            kind = "array"
            chars = sum(len(str(item)) for item in raw_value)
        else:
            raise FormPolicyError("form_field_value_invalid", f"browser form field {name!r} has unsupported value")
        total_chars += chars
        if total_chars > config.maximum_public_value_chars:
            raise FormPolicyError("form_value_budget_exceeded", "browser form public values exceed character budget")
        summaries.append(
            FormFieldSummary(
                name=name,
                value_kind=kind,
                value_chars=chars,
                secret_reference=secret_reference,
                file_reference=file_reference,
                field_digest=digest_value({"name": name, "kind": kind, "value": raw_value}),
            )
        )
    return tuple(summaries)


def infer_form_effects(
    selector: SelectorReceipt,
    *,
    method: FormMethod,
    fields: Sequence[FormFieldSummary],
) -> tuple[FormEffect, ...]:
    effects: list[FormEffect] = [FormEffect.NAVIGATION]
    if method == FormMethod.POST:
        effects.append(FormEffect.NETWORK_WRITE)
    if any(item.secret_reference or BrowserFormPolicy.SENSITIVE_FIELD.search(item.name) for item in fields):
        effects.append(FormEffect.CREDENTIAL_SUBMISSION)
    if any(item.file_reference for item in fields):
        effects.append(FormEffect.FILE_EGRESS)
    sensitivity_map = {
        ElementSensitivity.PAYMENT: FormEffect.PAYMENT,
        ElementSensitivity.DESTRUCTIVE: FormEffect.DESTRUCTIVE,
        ElementSensitivity.PUBLISH: FormEffect.PUBLISH,
        ElementSensitivity.ACCOUNT: FormEffect.ACCOUNT_CHANGE,
        ElementSensitivity.UNKNOWN: FormEffect.UNKNOWN_EXTERNAL_EFFECT,
    }
    selected = sensitivity_map.get(selector.semantics.sensitivity)
    if selected is not None:
        effects.append(selected)
    return tuple(dict.fromkeys(effects))
