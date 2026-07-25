from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from threading import RLock
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    content_digest,
    digest,
    identity,
    normalized_string_map,
    optional_identity,
    resolve_path,
    stable_unique,
)
from .errors import conflict, invalid, not_found
from .models import (
    ExecutionProfile,
    FaultInjection,
    PreflightKind,
    PreflightPolicy,
    PreflightTarget,
    ScenarioConfiguration,
    ScenarioDefinition,
    ScenarioMode,
    SealedPolicy,
    StepEffect,
)


DEFAULT_ACTIONS: tuple[dict[str, Any], ...] = (
    {"action_id": "create-task", "action": "task.create", "effect": "allow"},
    {"action_id": "route-worker", "action": "scheduler.route", "effect": "allow"},
    {"action_id": "curate-memory", "action": "memory.curate", "effect": "allow"},
    {"action_id": "evaluate-permission", "action": "permission.evaluate", "effect": "allow"},
    {"action_id": "inject-fault", "action": "fault.inject", "effect": "allow"},
    {"action_id": "recover-task", "action": "recovery.replan", "effect": "allow"},
    {"action_id": "write-artifact", "action": "artifact.write", "effect": "allow"},
    {"action_id": "verify-evidence", "action": "evidence.verify", "effect": "allow"},
)


def foundation_definition() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id="foundation.short-owner-chain",
        version="1.0.0",
        title="Short canonical owner-chain foundation",
        domain="cross-domain-foundation",
        goal_template=(
            "Execute this newly supplied scenario input through Zyra's canonical "
            "task, scheduler, memory, permission, recovery and artifact owners: {input}"
        ),
        required_owner_stages=(
            "api",
            "task",
            "scheduler",
            "memory",
            "permission",
            "fault",
            "recovery",
            "artifact",
            "evidence",
        ),
        planned_actions=DEFAULT_ACTIONS,
        default_faults=(
            FaultInjection(
                injection_id="foundation-requirement-change",
                stage="runtime",
                kind="requirement_change",
                after_effective_step=2,
                target="root",
                payload={"requirement": "preserve causal evidence during recovery"},
            ),
            FaultInjection(
                injection_id="foundation-worker-failure",
                stage="runtime",
                kind="worker_unavailable",
                after_effective_step=4,
                target="execute",
                payload={"recover": True, "route": "alternate"},
            ),
        ),
        expected_effects=(
            StepEffect.STATE_MUTATION,
            StepEffect.ROUTE,
            StepEffect.MEMORY,
            StepEffect.PERMISSION,
            StepEffect.FAULT,
            StepEffect.RECOVERY,
            StepEffect.ARTIFACT,
            StepEffect.VERIFICATION,
        ),
        minimum_effective_steps=8,
        source_roles=(
            "zyra_owned_primary",
            "existing_owner_integration",
            "role_aware_audit_only",
        ),
        metadata={
            "formal_long_run": False,
            "slice": "M2-S05-01",
            "legacy_demo_fallback": False,
        },
    )


def default_profile() -> ExecutionProfile:
    return ExecutionProfile(
        profile_id="foundation.local-sealed",
        provider_id="zyra-local",
        model_id="deterministic-owner-chain",
        backend_id="local-runtime",
        worker_classes=("Router", "Researcher", "Executor", "Verifier", "Memory"),
        maximum_effective_steps=2_500,
        maximum_wall_time_ms=15 * 60 * 1000,
        metadata={
            "formal": True,
            "dispatch_claim": "local-only-foundation",
            "edge_cloud_claim": False,
        },
    )


def default_policy() -> SealedPolicy:
    return SealedPolicy(
        policy_id="sealed-autonomous-foundation",
        version="1.0.0",
        allow_actions=tuple(item["action"] for item in DEFAULT_ACTIONS),
        deny_actions=(
            "operator.approve",
            "operator.steer",
            "operator.mutate",
            "shell.destructive",
            "credential.export",
        ),
        high_risk_actions=(
            "shell.destructive",
            "credential.export",
            "network.unknown",
            "workspace.outside-boundary",
        ),
        ask_disposition="deny_and_replan",
        unknown_disposition="deny_and_replan",
        maximum_denials=64,
        manual_mutation_disposition="record_reject_fail",
        metadata={
            "sealed": True,
            "human_intervention_count": 0,
            "interactive_wait_allowed": False,
        },
    )


class ScenarioRegistry:
    def __init__(
        self,
        definitions: Iterable[ScenarioDefinition] = (),
        profiles: Iterable[ExecutionProfile] = (),
        policies: Iterable[SealedPolicy] = (),
    ) -> None:
        self._lock = RLock()
        self._definitions: dict[tuple[str, str], ScenarioDefinition] = {}
        self._profiles: dict[str, ExecutionProfile] = {}
        self._policies: dict[str, SealedPolicy] = {}
        self._revision = 0
        for definition in definitions:
            self.register_definition(definition)
        for profile in profiles:
            self.register_profile(profile)
        for policy in policies:
            self.register_policy(policy)

    @classmethod
    def defaults(cls) -> "ScenarioRegistry":
        return cls(
            definitions=(foundation_definition(),),
            profiles=(default_profile(),),
            policies=(default_policy(),),
        )

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def register_definition(
        self,
        definition: ScenarioDefinition,
        *,
        replace_existing: bool = False,
    ) -> ScenarioDefinition:
        normalized = validate_definition(definition)
        key = (normalized.scenario_id, normalized.version)
        with self._lock:
            existing = self._definitions.get(key)
            if existing and existing.definition_digest != normalized.definition_digest:
                if not replace_existing:
                    raise conflict(
                        "scenario_definition_conflict",
                        "A different scenario definition already owns this id and version.",
                        phase="registry",
                        detail={
                            "scenario_id": normalized.scenario_id,
                            "version": normalized.version,
                            "existing_digest": existing.definition_digest,
                            "incoming_digest": normalized.definition_digest,
                        },
                    )
            if existing and existing.definition_digest == normalized.definition_digest:
                return existing
            self._definitions[key] = normalized
            self._revision += 1
            return normalized

    def register_profile(
        self,
        profile: ExecutionProfile,
        *,
        replace_existing: bool = False,
    ) -> ExecutionProfile:
        normalized = validate_profile(profile)
        with self._lock:
            existing = self._profiles.get(normalized.profile_id)
            if existing and digest(existing.to_dict()) != digest(normalized.to_dict()):
                if not replace_existing:
                    raise conflict(
                        "scenario_profile_conflict",
                        "A different execution profile already owns this id.",
                        phase="registry",
                        detail={"profile_id": normalized.profile_id},
                    )
            if existing and digest(existing.to_dict()) == digest(normalized.to_dict()):
                return existing
            self._profiles[normalized.profile_id] = normalized
            self._revision += 1
            return normalized

    def register_policy(
        self,
        policy: SealedPolicy,
        *,
        replace_existing: bool = False,
    ) -> SealedPolicy:
        normalized = validate_policy(policy)
        with self._lock:
            existing = self._policies.get(normalized.policy_id)
            if existing and existing.policy_digest != normalized.policy_digest:
                if not replace_existing:
                    raise conflict(
                        "scenario_policy_conflict",
                        "A different sealed policy already owns this id.",
                        phase="registry",
                        detail={"policy_id": normalized.policy_id},
                    )
            if existing and existing.policy_digest == normalized.policy_digest:
                return existing
            self._policies[normalized.policy_id] = normalized
            self._revision += 1
            return normalized

    def definition(self, scenario_id: str, version: str = "") -> ScenarioDefinition:
        selected_id = identity(scenario_id, "scenario id")
        selected_version = str(version or "").strip()
        with self._lock:
            if selected_version:
                selected = self._definitions.get((selected_id, selected_version))
            else:
                candidates = [
                    item
                    for (definition_id, _), item in self._definitions.items()
                    if definition_id == selected_id
                ]
                selected = sorted(candidates, key=lambda item: _semver_key(item.version))[-1] if candidates else None
            if selected is None:
                raise not_found(
                    "scenario_definition_not_found",
                    "The requested scenario definition is not registered.",
                    phase="registry",
                )
            return selected

    def profile(self, profile_id: str) -> ExecutionProfile:
        selected_id = identity(profile_id, "profile id")
        with self._lock:
            selected = self._profiles.get(selected_id)
            if selected is None:
                raise not_found(
                    "scenario_profile_not_found",
                    "The requested execution profile is not registered.",
                    phase="registry",
                )
            return selected

    def policy(self, policy_id: str) -> SealedPolicy:
        selected_id = identity(policy_id, "policy id")
        with self._lock:
            selected = self._policies.get(selected_id)
            if selected is None:
                raise not_found(
                    "scenario_policy_not_found",
                    "The requested sealed policy is not registered.",
                    phase="registry",
                )
            return selected

    def list_definitions(
        self,
        *,
        domain: str = "",
        include_versions: bool = True,
    ) -> tuple[ScenarioDefinition, ...]:
        selected_domain = str(domain or "").strip().casefold()
        with self._lock:
            values = [
                item
                for item in self._definitions.values()
                if not selected_domain or item.domain.casefold() == selected_domain
            ]
        values.sort(key=lambda item: (item.scenario_id, _semver_key(item.version)))
        if include_versions:
            return tuple(values)
        latest: dict[str, ScenarioDefinition] = {}
        for item in values:
            latest[item.scenario_id] = item
        return tuple(latest[key] for key in sorted(latest))

    def catalog(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "zyra.scenario-registry/v1",
                "revision": self._revision,
                "definitions": [
                    item.to_dict()
                    for item in self.list_definitions(include_versions=True)
                ],
                "profiles": [
                    self._profiles[key].to_dict()
                    for key in sorted(self._profiles)
                ],
                "policies": [
                    self._policies[key].to_dict()
                    for key in sorted(self._policies)
                ],
                "registry_digest": digest(
                    {
                        "definitions": [
                            item.definition_digest
                            for item in self.list_definitions(include_versions=True)
                        ],
                        "profiles": [
                            digest(self._profiles[key].to_dict())
                            for key in sorted(self._profiles)
                        ],
                        "policies": [
                            self._policies[key].policy_digest
                            for key in sorted(self._policies)
                        ],
                    }
                ),
            }


def build_configuration(
    registry: ScenarioRegistry,
    request: Mapping[str, Any],
    *,
    project_root: str,
    default_preflight_paths: Mapping[str, str],
) -> ScenarioConfiguration:
    scenario_id = str(request.get("scenario_id") or "foundation.short-owner-chain")
    definition = registry.definition(
        scenario_id,
        str(request.get("definition_version") or ""),
    )
    profile = registry.profile(
        str(request.get("profile_id") or "foundation.local-sealed")
    )
    policy = registry.policy(
        str(request.get("policy_id") or "sealed-autonomous-foundation")
    )
    mode = _mode(request.get("mode") or "sealed")
    input_text = bounded_text(
        request.get("input"),
        "scenario input",
        maximum_bytes=256 * 1024,
    )
    seed = bounded_integer(
        request.get("seed"),
        "scenario seed",
        minimum=0,
        maximum=(2**63) - 1,
        fallback=0,
    )
    labels = normalized_string_map(request.get("labels"), "scenario labels", maximum=64)
    faults = _faults(request.get("faults"), definition.default_faults)
    preflight_targets = _preflight_targets(
        request.get("preflight"),
        default_preflight_paths,
        project_root=project_root,
    )
    expected_digest = str(request.get("policy_digest") or policy.policy_digest).strip()
    requested_by = optional_identity(
        request.get("requested_by") or "api-operator",
        "scenario requester",
    )
    return ScenarioConfiguration(
        scenario_id=definition.scenario_id,
        definition_version=definition.version,
        definition_digest=definition.definition_digest,
        mode=mode,
        input_text=input_text,
        input_digest=content_digest(input_text),
        seed=seed,
        profile=profile,
        faults=faults,
        preflight_targets=preflight_targets,
        policy=policy,
        expected_policy_digest=expected_digest,
        requested_by=requested_by,
        labels=labels,
        metadata={
            "definition_title": definition.title,
            "definition_domain": definition.domain,
            "required_owner_stages": list(definition.required_owner_stages),
            "formal": mode is ScenarioMode.SEALED,
            "review_replay": mode is ScenarioMode.REVIEW_REPLAY,
            "project_root": str(resolve_path(project_root, "project root")),
        },
    )


def validate_definition(definition: ScenarioDefinition) -> ScenarioDefinition:
    scenario_id = identity(definition.scenario_id, "scenario id")
    version = _version(definition.version)
    title = bounded_text(definition.title, "scenario title", maximum_bytes=4 * 1024)
    domain = identity(definition.domain, "scenario domain")
    goal_template = bounded_text(
        definition.goal_template,
        "scenario goal template",
        maximum_bytes=256 * 1024,
    )
    if "{input}" not in goal_template:
        raise invalid(
            "scenario_goal_template_invalid",
            "Scenario goal template must contain the {input} placeholder.",
            phase="registry",
        )
    owner_stages = stable_unique(definition.required_owner_stages)
    if not owner_stages:
        raise invalid(
            "scenario_owner_stages_missing",
            "Scenario definition must declare canonical owner stages.",
            phase="registry",
        )
    action_ids: set[str] = set()
    actions: list[dict[str, Any]] = []
    for position, raw in enumerate(definition.planned_actions):
        if not isinstance(raw, Mapping):
            raise invalid(
                "scenario_action_invalid",
                "Scenario planned action must be an object.",
                phase="registry",
            )
        action_id = identity(
            raw.get("action_id") or f"action-{position + 1}",
            "scenario action id",
        )
        if action_id in action_ids:
            raise invalid(
                "scenario_action_duplicate",
                "Scenario planned action ids must be unique.",
                phase="registry",
                detail={"action_id": action_id},
            )
        action_ids.add(action_id)
        actions.append(
            {
                "action_id": action_id,
                "action": identity(raw.get("action"), "scenario action"),
                "effect": str(raw.get("effect") or "ask").strip().casefold(),
                "metadata": dict(raw.get("metadata") or {}),
            }
        )
    minimum = bounded_integer(
        definition.minimum_effective_steps,
        "minimum effective steps",
        minimum=1,
        maximum=10_000_000,
    )
    return replace(
        definition,
        scenario_id=scenario_id,
        version=version,
        title=title,
        domain=domain,
        goal_template=goal_template,
        required_owner_stages=owner_stages,
        planned_actions=tuple(actions),
        minimum_effective_steps=minimum,
        source_roles=stable_unique(definition.source_roles),
    )


def validate_profile(profile: ExecutionProfile) -> ExecutionProfile:
    return replace(
        profile,
        profile_id=identity(profile.profile_id, "profile id"),
        provider_id=identity(profile.provider_id, "provider id"),
        model_id=identity(profile.model_id, "model id"),
        backend_id=identity(profile.backend_id, "backend id"),
        worker_classes=stable_unique(profile.worker_classes),
        maximum_effective_steps=bounded_integer(
            profile.maximum_effective_steps,
            "maximum effective steps",
            minimum=1,
            maximum=10_000_000,
        ),
        maximum_wall_time_ms=bounded_integer(
            profile.maximum_wall_time_ms,
            "maximum wall time",
            minimum=100,
            maximum=30 * 24 * 60 * 60 * 1000,
        ),
    )


def validate_policy(policy: SealedPolicy) -> SealedPolicy:
    allow = stable_unique(identity(item, "allowed action") for item in policy.allow_actions)
    deny = stable_unique(identity(item, "denied action") for item in policy.deny_actions)
    overlap = sorted(set(allow) & set(deny))
    if overlap:
        raise invalid(
            "scenario_policy_overlap",
            "Sealed policy cannot both allow and deny an action.",
            phase="registry",
            detail={"actions": overlap},
        )
    if policy.ask_disposition != "deny_and_replan":
        raise invalid(
            "scenario_policy_ask_unsafe",
            "Formal sealed policy must convert ask into deny_and_replan.",
            phase="registry",
        )
    if policy.unknown_disposition != "deny_and_replan":
        raise invalid(
            "scenario_policy_unknown_unsafe",
            "Formal sealed policy must deny and replan unknown actions.",
            phase="registry",
        )
    return replace(
        policy,
        policy_id=identity(policy.policy_id, "policy id"),
        version=_version(policy.version),
        allow_actions=allow,
        deny_actions=deny,
        high_risk_actions=stable_unique(
            identity(item, "high-risk action") for item in policy.high_risk_actions
        ),
        maximum_denials=bounded_integer(
            policy.maximum_denials,
            "maximum sealed denials",
            minimum=1,
            maximum=100_000,
        ),
    )


def _mode(value: Any) -> ScenarioMode:
    try:
        return ScenarioMode(str(value).strip().casefold())
    except ValueError as error:
        raise invalid(
            "scenario_mode_invalid",
            "Scenario mode must be sealed, interactive, or review_replay.",
        ) from error


def _version(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 64:
        raise invalid("scenario_version_invalid", "Scenario version is invalid.")
    parts = normalized.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts):
        raise invalid(
            "scenario_version_invalid",
            "Scenario version must be a numeric dotted version.",
        )
    return normalized


def _semver_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _faults(
    raw: Any,
    defaults: tuple[FaultInjection, ...],
) -> tuple[FaultInjection, ...]:
    if raw is None:
        return defaults
    if not isinstance(raw, list) or len(raw) > 1_024:
        raise invalid(
            "scenario_fault_schedule_invalid",
            "Scenario faults must be a bounded array.",
        )
    output: list[FaultInjection] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise invalid(
                "scenario_fault_invalid",
                "Each scenario fault must be an object.",
            )
        injection_id = identity(
            item.get("injection_id") or f"fault-{index + 1}",
            "fault injection id",
        )
        if injection_id in seen:
            raise invalid(
                "scenario_fault_duplicate",
                "Fault injection ids must be unique.",
                detail={"injection_id": injection_id},
            )
        seen.add(injection_id)
        output.append(
            FaultInjection(
                injection_id=injection_id,
                stage=identity(item.get("stage") or "runtime", "fault stage"),
                kind=identity(item.get("kind"), "fault kind"),
                after_effective_step=bounded_integer(
                    item.get("after_effective_step"),
                    "fault effective-step offset",
                    minimum=0,
                    maximum=10_000_000,
                    fallback=0,
                ),
                target=optional_identity(item.get("target"), "fault target"),
                payload=dict(item.get("payload") or {}),
            )
        )
    return tuple(output)


def _preflight_targets(
    raw: Any,
    defaults: Mapping[str, str],
    *,
    project_root: str,
) -> tuple[PreflightTarget, ...]:
    values: list[Mapping[str, Any]]
    if raw is None:
        values = [
            {
                "kind": kind,
                "path": path,
                "policy": (
                    "sqlite_no_user_rows"
                    if kind == "database"
                    else "absent_or_empty"
                ),
            }
            for kind, path in defaults.items()
        ]
    elif isinstance(raw, list):
        values = raw
    else:
        raise invalid(
            "scenario_preflight_invalid",
            "Scenario preflight targets must be an array.",
        )
    output: list[PreflightTarget] = []
    kinds: set[PreflightKind] = set()
    for item in values:
        if not isinstance(item, Mapping):
            raise invalid(
                "scenario_preflight_target_invalid",
                "Each preflight target must be an object.",
            )
        try:
            kind = PreflightKind(str(item.get("kind") or "").strip().casefold())
            policy = PreflightPolicy(
                str(item.get("policy") or "absent_or_empty").strip().casefold()
            )
        except ValueError as error:
            raise invalid(
                "scenario_preflight_target_invalid",
                "Preflight kind or policy is unsupported.",
            ) from error
        path = str(
            resolve_path(
                item.get("path"),
                f"{kind.value} preflight path",
                base=project_root,
            )
        )
        ignored = stable_unique(str(value) for value in item.get("ignored_names") or ())
        output.append(
            PreflightTarget(
                kind=kind,
                path=path,
                policy=policy,
                ignored_names=ignored,
                required=item.get("required", True) is not False,
            )
        )
        kinds.add(kind)
    required = set(PreflightKind)
    missing = sorted(kind.value for kind in required - kinds)
    if missing:
        raise invalid(
            "scenario_preflight_kind_missing",
            "Formal scenario preflight must cover database, cache, index, artifact and build.",
            detail={"missing": missing},
        )
    return tuple(output)
