from __future__ import annotations

from pathlib import Path

import pytest

from zyra_orchestration.topology_policy import canonical_digest
from zyra_runtime.provider_control_plane import (
    ModelCapabilities,
    ModelDefinition,
)
from zyra_runtime.tools import ToolRegistry, ToolSpec
from zyra_scheduler import (
    OperatorCatalogBuilder,
    OperatorCatalogError,
    OperatorType,
    ResourceLocation,
    WorkerBackendKind,
    WorkerHealth,
    WorkerHealthStatus,
    WorkerManifest,
    WorkerPool,
)
from zyra_skills.models import (
    SkillBodyDescriptor,
    SkillDeclaredMetadata,
    SkillProvenance,
    SkillRegistrySnapshot,
    SkillRevision,
    SkillRevisionLifecycle,
    SkillSourceKind,
    SkillTrustTier,
    SkillVersionRef,
)
from zyra_workers.subagents.models import AgentDefinition


ROOT = Path(__file__).resolve().parents[3]


def _digest(value: object) -> str:
    return canonical_digest(value)


def _skill_snapshot(
    names: tuple[str, ...],
    *,
    generation: int,
) -> SkillRegistrySnapshot:
    revisions: dict[str, SkillRevision] = {}
    active: dict[str, str] = {}
    for name in names:
        qualified_name = f"project:{name}"
        content_digest = _digest(("skill-content", name, generation))
        body_digest = _digest(("skill-body", name, generation))
        version_ref = SkillVersionRef(
            skill_id=f"skill-{name}",
            qualified_name=qualified_name,
            source_id="project-skills",
            declared_version=str(generation),
            content_digest=content_digest,
            body_digest=body_digest,
            resource_manifest_digest=_digest(("resources", name)),
            policy_digest=_digest(("policy", name)),
            provenance_digest=_digest(("provenance", name)),
            registry_generation=generation,
        )
        metadata = SkillDeclaredMetadata(
            name=name,
            description=f"{name} creates and verifies a release artifact",
            declared_version=str(generation),
            extensions={
                "operator": {
                    "capabilities": [
                        "artifact_production",
                        "verification",
                    ],
                    "required_permissions": ["skill.invoke"],
                    "allowed_locations": ["local"],
                    "allowed_privacy_classes": ["internal"],
                    "verifier_contracts": ["release_artifact_verifier"],
                    "minimum_evidence_contract": [
                        "skill_invocation_receipt",
                        "release_artifact",
                    ],
                    "estimated_tokens": 256,
                    "outcome_count": 0,
                }
            },
        )
        revision = SkillRevision(
            metadata=metadata,
            provenance=SkillProvenance(
                source_kind=SkillSourceKind.PROJECT,
                source_id="project-skills",
                source_namespace="project",
                origin_uri=f"project://skills/{name}",
                canonical_root=str(ROOT / "skills"),
                discovery_root=str(ROOT / "skills"),
                trust_tier=SkillTrustTier.PROJECT,
            ),
            version_ref=version_ref,
            body=SkillBodyDescriptor(
                size_bytes=100,
                token_estimate=25,
                digest=body_digest,
                immutable_ref=f"skill-body://{name}/{body_digest}",
                line_count=5,
            ),
            resources=(),
            skill_root=str(ROOT / "skills" / name),
            skill_file=str(ROOT / "skills" / name / "SKILL.md"),
            lifecycle=SkillRevisionLifecycle.AVAILABLE,
        )
        revisions[version_ref.immutable_ref] = revision
        active[qualified_name] = version_ref.immutable_ref
    return SkillRegistrySnapshot(
        generation=generation,
        revisions_by_ref=revisions,
        active_by_qualified_name=active,
        aliases={},
        shadowed={},
        tombstones={},
        source_status={"project-skills": {"status": "healthy"}},
        snapshot_id=f"skillsnap-{generation}",
        created_at="2026-07-30T00:00:00Z",
    )


def test_catalog_builds_all_operator_types_from_current_registry_objects() -> None:
    skill_snapshot = _skill_snapshot(("release-verifier",), generation=1)
    agent = AgentDefinition(
        agent_type="release-reviewer",
        description="Review a release artifact",
        capabilities=("review", "verification"),
        tools=("trace",),
        metadata={
            "operator": {
                "required_permissions": ["agent.spawn"],
                "allowed_locations": ["local"],
                "allowed_privacy_classes": ["internal"],
                "verifier_contracts": ["review_verifier"],
                "minimum_evidence_contract": ["subagent_result"],
                "outcome_count": 5,
            }
        },
    )
    worker = WorkerManifest(
        worker_id="fresh-release-worker",
        display_name="Fresh Release Worker",
        runtime_worker="CodeWorkerRuntime",
        location=ResourceLocation.LOCAL,
        backend=WorkerBackendKind.LOCAL_PROCESS,
        capabilities=["artifact_production", "verification"],
        tools=["trace"],
        privacy_level="sensitive_ok",
        max_concurrency=2,
        metadata={
            "operator": {
                "required_permissions": ["worker.dispatch"],
                "verifier_contracts": ["worker_result_verifier"],
                "minimum_evidence_contract": ["worker_dispatch_receipt"],
            }
        },
    )
    tool_registry = ToolRegistry(
        [
            ToolSpec(
                name="release_note",
                purpose="Generate a release note",
                source="zyra-test",
                input_schema={
                    "type": "object",
                    "required": ["changes"],
                    "properties": {"changes": {"type": "array"}},
                },
                output_schema={
                    "type": "object",
                    "properties": {"artifact": {"type": "string"}},
                },
                metadata={
                    "operator": {
                        "capabilities": ["artifact_production"],
                        "required_permissions": ["tool:release_note"],
                        "allowed_locations": ["local"],
                        "allowed_privacy_classes": ["internal"],
                        "verifier_contracts": ["release_artifact_verifier"],
                        "minimum_evidence_contract": ["tool_result"],
                    }
                },
            )
        ]
    )
    model = ModelDefinition(
        provider_id="cloud-a",
        model_id="reasoner-v1",
        display_name="Reasoner V1",
        family="reasoner",
        capabilities=ModelCapabilities(
            reasoning=True,
            structured_output=True,
        ),
        pricing=({"input_cost_per_1k": 0.02},),
        metadata={
            "operator": {
                "required_permissions": ["model.invoke"],
                "allowed_locations": ["cloud"],
                "allowed_privacy_classes": ["internal"],
                "verifier_contracts": ["provider_response_verifier"],
                "minimum_evidence_contract": ["provider_request_receipt"],
            }
        },
    )
    catalog = OperatorCatalogBuilder().build(
        skill_snapshot=skill_snapshot,
        agent_definitions=(agent,),
        worker_manifests=(worker,),
        worker_health=(
            WorkerHealth(
                worker_id=worker.worker_id,
                status=WorkerHealthStatus.HEALTHY,
                recent_successes=4,
                current_load=0,
            ),
        ),
        tool_registry=tool_registry,
        model_definitions=(model,),
        built_at="2026-07-30T00:00:00Z",
    )

    assert {item.operator_type for item in catalog.entries} == set(OperatorType)
    assert set(catalog.source_versions) == {
        "skill_registry",
        "agent_registry",
        "worker_pool",
        "tool_registry",
        "model_registry",
    }
    assert all(item.input_contract for item in catalog.entries)
    assert all(item.output_contract for item in catalog.entries)
    assert all(item.required_permissions for item in catalog.entries)
    assert all(item.allowed_locations for item in catalog.entries)
    assert all(item.allowed_privacy_classes for item in catalog.entries)
    assert all(item.verifier_contracts for item in catalog.entries)
    assert all(item.minimum_evidence_contract for item in catalog.entries)
    assert catalog.get("skill:project:release-verifier") is not None
    assert catalog.get("worker:fresh-release-worker").cold_start is False
    assert catalog.get("skill:project:release-verifier").cold_start is True
    assert catalog.get("skill:project:release-verifier").confidence == 0.55


def test_new_skill_enters_catalog_without_selector_source_change() -> None:
    builder = OperatorCatalogBuilder()
    first = builder.build(
        skill_snapshot=_skill_snapshot(("release-verifier",), generation=1),
    )
    second = builder.build(
        skill_snapshot=_skill_snapshot(
            ("release-verifier", "security-review"),
            generation=2,
        ),
    )

    assert first.get("skill:project:security-review") is None
    added = second.get("skill:project:security-review")
    assert added is not None
    assert added.cold_start is True
    assert second.catalog_version != first.catalog_version
    assert second.digest != first.digest


def test_new_worker_and_model_enter_rebuilt_catalog_without_enum_changes() -> None:
    builder = OperatorCatalogBuilder()
    first_worker = WorkerManifest(
        worker_id="worker-a",
        display_name="Worker A",
        runtime_worker="CodeWorkerRuntime",
        location=ResourceLocation.LOCAL,
        backend=WorkerBackendKind.LOCAL_PROCESS,
        capabilities=["code-change"],
        privacy_level="sensitive_ok",
    )
    second_worker = WorkerManifest(
        worker_id="worker-b",
        display_name="Worker B",
        runtime_worker="CodeWorkerRuntime",
        location=ResourceLocation.EDGE,
        backend=WorkerBackendKind.DOCKER_SANDBOX,
        capabilities=["verification"],
        privacy_level="public_or_masked",
    )
    first_model = ModelDefinition(
        provider_id="cloud-a",
        model_id="model-a",
        display_name="Model A",
        family="reasoner",
    )
    second_model = ModelDefinition(
        provider_id="cloud-b",
        model_id="model-b",
        display_name="Model B",
        family="verifier",
    )
    first = builder.build(
        worker_manifests=(first_worker,),
        model_definitions=(first_model,),
    )
    second = builder.build(
        worker_manifests=(first_worker, second_worker),
        model_definitions=(first_model, second_model),
    )

    assert first.get("worker:worker-b") is None
    assert first.get("model:cloud-b/model-b") is None
    assert second.get("worker:worker-b") is not None
    assert second.get("model:cloud-b/model-b") is not None
    assert second.catalog_version != first.catalog_version


def test_disabled_model_is_excluded_and_catalog_rejects_stale_references() -> None:
    builder = OperatorCatalogBuilder()
    active = ModelDefinition(
        provider_id="cloud-a",
        model_id="active",
        display_name="Active",
        family="reasoner",
        metadata={
            "operator": {
                "required_permissions": ["model.invoke"],
                "allowed_privacy_classes": ["public"],
            }
        },
    )
    retired = ModelDefinition(
        provider_id="cloud-a",
        model_id="retired",
        display_name="Retired",
        family="reasoner",
        status="retired",
        enabled=False,
    )
    first = builder.build(model_definitions=(active,))
    second = builder.build(model_definitions=(active, retired))

    assert second.get("model:cloud-a/retired") is None
    with pytest.raises(OperatorCatalogError, match="different catalog version"):
        second.validate_references(
            catalog_version=first.catalog_version,
            catalog_digest=first.digest,
            references=(("model:cloud-a/active", "1"),),
        )


def test_duplicate_registry_identity_fails_closed() -> None:
    worker = WorkerPool().manifests()[0]
    with pytest.raises(
        OperatorCatalogError,
        match="duplicate stable operator ids",
    ):
        OperatorCatalogBuilder().build(
            worker_manifests=(worker, worker),
        )
