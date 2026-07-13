from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .file_policy import FileReceipt
from .form_policy import FormEffect, FormReceipt
from .models import (
    ActionAccess,
    ActionDefinition,
    ActionRequest,
    ActionRisk,
    ActionRiskAssessment,
    PermissionDisposition,
    digest_value,
)
from .network_policy import NetworkReceipt
from .secret_policy import SecretReceipt
from .selector_guard import ElementSensitivity, SelectorReceipt


class RiskPolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class ExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    SEALED_AUTONOMOUS = "sealed_autonomous"


@dataclass(frozen=True, slots=True)
class RiskRule:
    rule_id: str
    description: str
    risk: ActionRisk
    disposition_interactive: PermissionDisposition
    disposition_sealed: PermissionDisposition
    actions: frozenset[str] = frozenset()
    access: frozenset[ActionAccess] = frozenset()
    required_tags: frozenset[str] = frozenset()
    capabilities: tuple[str, ...] = ()
    safety_flags: tuple[str, ...] = ()
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.rule_id or not self.description:
            raise ValueError("browser risk rule requires id and description")
        object.__setattr__(self, "actions", frozenset(self.actions))
        object.__setattr__(self, "access", frozenset(self.access))
        object.__setattr__(self, "required_tags", frozenset(str(item) for item in self.required_tags))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "safety_flags", tuple(self.safety_flags))

    def matches(self, definition: ActionDefinition, dynamic_tags: frozenset[str]) -> bool:
        if self.actions and definition.name not in self.actions:
            return False
        if self.access and not definition.access.intersection(self.access):
            return False
        if self.required_tags and not self.required_tags.issubset(dynamic_tags):
            return False
        return bool(self.actions or self.access or self.required_tags)


@dataclass(frozen=True, slots=True)
class SensitivePolicyConfig:
    version: str = "browser-sensitive-v1"
    sealed_low_risk_allowlist: frozenset[str] = frozenset(
        {"extract_text", "snapshot_state", "search_page", "get_dropdown_options", "list_targets", "wait"}
    )
    interactive_auto_allow: frozenset[str] = frozenset(
        {"extract_text", "snapshot_state", "search_page", "get_dropdown_options", "list_targets", "wait"}
    )
    prohibit_arbitrary_evaluate_in_sealed: bool = True
    prohibit_clipboard_in_sealed: bool = True
    prohibit_file_egress_in_sealed: bool = True
    unknown_is_deny: bool = True
    rules: tuple[RiskRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sealed_low_risk_allowlist", frozenset(self.sealed_low_risk_allowlist))
        object.__setattr__(self, "interactive_auto_allow", frozenset(self.interactive_auto_allow))
        selected = tuple(self.rules) if self.rules else default_risk_rules()
        object.__setattr__(self, "rules", tuple(sorted(selected, key=lambda item: (item.priority, item.rule_id))))

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "version": self.version,
                "sealed_low_risk_allowlist": sorted(self.sealed_low_risk_allowlist),
                "interactive_auto_allow": sorted(self.interactive_auto_allow),
                "prohibit_arbitrary_evaluate_in_sealed": self.prohibit_arbitrary_evaluate_in_sealed,
                "prohibit_clipboard_in_sealed": self.prohibit_clipboard_in_sealed,
                "prohibit_file_egress_in_sealed": self.prohibit_file_egress_in_sealed,
                "unknown_is_deny": self.unknown_is_deny,
                "rules": [rule.rule_id for rule in self.rules],
            }
        )


@dataclass(frozen=True, slots=True)
class RiskContext:
    request: ActionRequest
    definition: ActionDefinition
    selector: SelectorReceipt | None = None
    network: NetworkReceipt | None = None
    file: FileReceipt | None = None
    secret: SecretReceipt | None = None
    form: FormReceipt | None = None
    hook_tags: tuple[str, ...] = ()
    mode: ExecutionMode = ExecutionMode.INTERACTIVE

    @property
    def dynamic_tags(self) -> frozenset[str]:
        tags: set[str] = set(self.definition.tags)
        tags.update(self.hook_tags)
        if self.selector:
            tags.add(f"element:{self.selector.semantics.intent}")
            tags.add(f"sensitivity:{self.selector.semantics.sensitivity}")
            tags.update(f"effect:{item}" for item in self.selector.semantics.inferred_effects)
        if self.network:
            tags.add("network:resolved")
            tags.add(f"scheme:{self.network.canonical_url.scheme}")
        if self.file:
            tags.add(f"file:{self.file.intent}")
        if self.secret and self.secret.references:
            tags.add("secret:materialize")
        if self.form:
            tags.add(f"form:method:{self.form.method}")
            tags.add("form:cross_origin" if self.form.cross_origin else "form:same_origin")
            tags.update(f"form_effect:{effect}" for effect in self.form.effects)
        if self.mode == ExecutionMode.SEALED_AUTONOMOUS:
            tags.add("mode:sealed")
        return frozenset(tags)


class BrowserSensitiveActionClassifier:
    """Pure browser risk classifier; 03A remains the decision/grant owner."""

    _RISK_ORDER = {
        ActionRisk.PASSIVE: 0,
        ActionRisk.LOW: 1,
        ActionRisk.GUARDED: 2,
        ActionRisk.SENSITIVE: 3,
        ActionRisk.HIGH: 4,
        ActionRisk.PROHIBITED: 5,
    }

    _DISPOSITION_ORDER = {
        PermissionDisposition.ALLOW: 0,
        PermissionDisposition.ASK: 1,
        PermissionDisposition.DENY: 2,
    }

    def __init__(self, config: SensitivePolicyConfig | None = None, *, disabled: bool = False) -> None:
        self.config = config or SensitivePolicyConfig()
        self.disabled = disabled

    def classify(self, context: RiskContext) -> ActionRiskAssessment:
        if self.disabled:
            raise RiskPolicyError("risk_classifier_disabled", "browser risk classifier is disabled")
        definition = context.definition
        tags = context.dynamic_tags
        risk = definition.base_risk
        disposition = self._base_disposition(context)
        reasons: list[str] = [f"base_risk:{definition.base_risk}"]
        capabilities: set[str] = browser_capabilities(definition)
        safety_flags: set[str] = set()
        for rule in self.config.rules:
            if not rule.matches(definition, tags):
                continue
            reasons.append(rule.rule_id)
            capabilities.update(rule.capabilities)
            safety_flags.update(rule.safety_flags)
            if self._RISK_ORDER[rule.risk] > self._RISK_ORDER[risk]:
                risk = rule.risk
            selected = (
                rule.disposition_sealed
                if context.mode == ExecutionMode.SEALED_AUTONOMOUS
                else rule.disposition_interactive
            )
            if self._DISPOSITION_ORDER[selected] > self._DISPOSITION_ORDER[disposition]:
                disposition = selected
        disposition, risk = self._enforce_invariants(context, disposition, risk, reasons, safety_flags)
        return ActionRiskAssessment(
            action=definition.name,
            risk=risk,
            permission=disposition,
            capabilities=tuple(sorted(capabilities)),
            risk_tags=tuple(sorted(tags)),
            safety_flags=tuple(sorted(safety_flags)),
            reasons=tuple(reasons),
            policy_version=f"{self.config.version}:{self.config.digest}",
        )

    def _base_disposition(self, context: RiskContext) -> PermissionDisposition:
        name = context.definition.name
        if context.mode == ExecutionMode.SEALED_AUTONOMOUS:
            return (
                PermissionDisposition.ALLOW
                if name in self.config.sealed_low_risk_allowlist and context.definition.read_only
                else PermissionDisposition.DENY
                if context.definition.base_risk == ActionRisk.PROHIBITED
                else PermissionDisposition.ASK
            )
        if name in self.config.interactive_auto_allow and context.definition.read_only:
            return PermissionDisposition.ALLOW
        return PermissionDisposition.ASK if context.definition.permission_required else PermissionDisposition.ALLOW

    def _enforce_invariants(
        self,
        context: RiskContext,
        disposition: PermissionDisposition,
        risk: ActionRisk,
        reasons: list[str],
        safety_flags: set[str],
    ) -> tuple[PermissionDisposition, ActionRisk]:
        definition = context.definition
        sealed = context.mode == ExecutionMode.SEALED_AUTONOMOUS
        if definition.name == "evaluate_js":
            risk = ActionRisk.PROHIBITED if sealed else ActionRisk.HIGH
            disposition = PermissionDisposition.DENY if sealed and self.config.prohibit_arbitrary_evaluate_in_sealed else PermissionDisposition.ASK
            reasons.append("arbitrary_script_execution")
            safety_flags.update({"arbitrary_javascript", "dom_mutation", "network_egress_possible"})
        if ActionAccess.CLIPBOARD in definition.access:
            risk = max_risk(risk, ActionRisk.HIGH)
            disposition = PermissionDisposition.DENY if sealed and self.config.prohibit_clipboard_in_sealed else PermissionDisposition.ASK
            reasons.append("clipboard_boundary")
            safety_flags.add("clipboard_access")
        if context.secret and context.secret.references:
            risk = max_risk(risk, ActionRisk.HIGH)
            disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
            reasons.append("secret_materialization")
            safety_flags.add("secret_value_present_at_executor")
        if context.form:
            risk = max_risk(risk, ActionRisk.HIGH)
            disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
            reasons.append(f"form_submission:{context.form.method}")
            safety_flags.add("form_destination_bound")
            if context.form.cross_origin:
                reasons.append("cross_origin_form_submission")
                safety_flags.add("cross_origin_form")
            if FormEffect.CREDENTIAL_SUBMISSION in context.form.effects:
                reasons.append("credential_submission")
                safety_flags.add("credential_egress")
            if FormEffect.FILE_EGRESS in context.form.effects:
                reasons.append("form_file_egress")
                safety_flags.add("form_file_reference")
        if context.file and context.file.uploads:
            risk = max_risk(risk, ActionRisk.HIGH)
            disposition = PermissionDisposition.DENY if sealed and self.config.prohibit_file_egress_in_sealed else PermissionDisposition.ASK
            reasons.append("file_egress")
            safety_flags.add("upload_read_after_grant_only")
        if context.file and context.file.destination_path:
            risk = max_risk(risk, ActionRisk.HIGH)
            disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
            reasons.append("untrusted_file_write")
            safety_flags.add("owned_destination_required")
        if context.selector:
            sensitivity = context.selector.semantics.sensitivity
            if sensitivity != ElementSensitivity.ORDINARY:
                risk = max_risk(risk, ActionRisk.HIGH)
                disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
                reasons.append(f"element_sensitivity:{sensitivity}")
            if "form_submit" in context.selector.semantics.inferred_effects:
                risk = max_risk(risk, ActionRisk.HIGH)
                disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
                reasons.append("form_submission")
        if context.network and context.request.current_url:
            try:
                current_origin = context.request.current_url.split("/", 3)[:3]
                current_origin_text = "/".join(current_origin)
            except Exception:
                current_origin_text = ""
            if current_origin_text and current_origin_text != context.network.canonical_url.origin:
                risk = max_risk(risk, ActionRisk.SENSITIVE)
                disposition = PermissionDisposition.DENY if sealed else PermissionDisposition.ASK
                reasons.append("cross_origin_navigation")
        if sealed and disposition == PermissionDisposition.ASK:
            disposition = PermissionDisposition.DENY
            reasons.append("sealed_ask_becomes_deny")
            safety_flags.add("human_intervention_count_zero")
        if risk == ActionRisk.PROHIBITED:
            disposition = PermissionDisposition.DENY
        return disposition, risk


def default_risk_rules() -> tuple[RiskRule, ...]:
    return (
        RiskRule(
            "read_only_metadata",
            "Passive DOM and target metadata reads are low risk within result budgets.",
            ActionRisk.LOW,
            PermissionDisposition.ALLOW,
            PermissionDisposition.ALLOW,
            actions=frozenset({"snapshot_state", "list_targets", "wait"}),
            capabilities=("browser.read_metadata",),
            priority=10,
        ),
        RiskRule(
            "content_disclosure",
            "Page extraction and rendered artifacts can disclose sensitive content.",
            ActionRisk.SENSITIVE,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            actions=frozenset({"extract_text", "take_screenshot", "save_as_pdf"}),
            capabilities=("browser.read_content",),
            safety_flags=("content_disclosure", "result_budget_required"),
            priority=20,
        ),
        RiskRule(
            "navigation_egress",
            "Navigation can send cookies and create external effects.",
            ActionRisk.SENSITIVE,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            actions=frozenset({"open_url", "go_back", "reload_page"}),
            capabilities=("browser.navigate", "network.egress"),
            safety_flags=("dns_pin_required", "redirect_recheck_required"),
            priority=30,
        ),
        RiskRule(
            "element_mutation",
            "Element interaction requires current identity and topmost geometry.",
            ActionRisk.GUARDED,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            access=frozenset({ActionAccess.BROWSER_MUTATION}),
            capabilities=("browser.mutate",),
            safety_flags=("selector_current_required", "geometry_fresh_required"),
            priority=40,
        ),
        RiskRule(
            "external_side_effect",
            "Potential external side effects require explicit approval.",
            ActionRisk.HIGH,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            access=frozenset({ActionAccess.EXTERNAL_SIDE_EFFECT}),
            capabilities=("browser.external_side_effect",),
            safety_flags=("exact_grant_required",),
            priority=50,
        ),
        RiskRule(
            "file_read_egress",
            "Browser upload reads workspace content for network egress.",
            ActionRisk.HIGH,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            access=frozenset({ActionAccess.FILE_READ}),
            capabilities=("file.read", "network.egress"),
            safety_flags=("file_identity_revalidate",),
            priority=60,
        ),
        RiskRule(
            "file_write_ingress",
            "Download and print create untrusted workspace-owned artifacts.",
            ActionRisk.HIGH,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            access=frozenset({ActionAccess.FILE_WRITE}),
            capabilities=("file.write",),
            safety_flags=("destination_containment", "quota_required"),
            priority=70,
        ),
        RiskRule(
            "target_control",
            "Target focus and close mutate browser session topology.",
            ActionRisk.SENSITIVE,
            PermissionDisposition.ASK,
            PermissionDisposition.DENY,
            access=frozenset({ActionAccess.TARGET_CONTROL}),
            capabilities=("browser.target_control",),
            safety_flags=("session_lease_required",),
            priority=80,
        ),
    )


def browser_capabilities(definition: ActionDefinition) -> set[str]:
    capabilities = {f"browser.action.{definition.name}"}
    mapping = {
        ActionAccess.READ_ONLY: "browser.read",
        ActionAccess.BROWSER_MUTATION: "browser.mutate",
        ActionAccess.EXTERNAL_SIDE_EFFECT: "external.side_effect",
        ActionAccess.FILE_READ: "file.read",
        ActionAccess.FILE_WRITE: "file.write",
        ActionAccess.NETWORK: "network.egress",
        ActionAccess.SCRIPT: "browser.script",
        ActionAccess.CLIPBOARD: "browser.clipboard",
        ActionAccess.TARGET_CONTROL: "browser.target_control",
    }
    capabilities.update(mapping[item] for item in definition.access)
    return capabilities


def max_risk(left: ActionRisk, right: ActionRisk) -> ActionRisk:
    order = BrowserSensitiveActionClassifier._RISK_ORDER
    return left if order[left] >= order[right] else right
