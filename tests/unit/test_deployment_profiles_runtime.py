from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from zyra_orchestration.deployment.errors import (
    NodeAuthenticationError,
    NodeReplayRejected,
    PlacementDisabled,
    PlacementRejected,
    StateConflict,
)
from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    LifecycleStatus,
    NodeObservation,
    Sensitivity,
    Workload,
    new_id,
    now_iso,
)
from zyra_orchestration.deployment.placement import (
    PlacementContext,
    PlacementPolicyRuntime,
)
from zyra_orchestration.deployment.profiles import ProfileCatalog
from zyra_orchestration.deployment.security import (
    RequestAuthenticator,
    RequestSigner,
)
from zyra_orchestration.deployment.state_store import DeploymentStateStore


def _catalog(project_root: Path, *, environment: dict[str, str] | None = None) -> ProfileCatalog:
    return ProfileCatalog.defaults(
        project_root,
        host="127.0.0.1",
        base_port=32100,
        environment=environment or {},
    )


def _observation(
    catalog: ProfileCatalog,
    profile: DeploymentProfile,
    *,
    pid: int,
    status: LifecycleStatus = LifecycleStatus.READY,
    network_down: bool = False,
    credential_presence: dict[str, bool] | None = None,
) -> NodeObservation:
    policy = catalog.policy(profile)
    return NodeObservation(
        node_id=f"node-{profile.value}",
        profile=profile,
        generation_id=f"generation-{profile.value}",
        pid=pid,
        endpoint=f"http://127.0.0.1:{policy.port}",
        status=status,
        capabilities=policy.capabilities,
        heartbeat_sequence=1,
        resource={"rss_mb": 10, "active_dispatches": 0},
        network={
            "in_process": False,
            "network_down": network_down,
            "observed_latency_ms": policy.latency_budget_ms,
        },
        credential_presence=credential_presence or {},
        observed_at=now_iso(),
        configuration_digest=policy.configuration_digest,
        semantic_digest=f"semantic-{profile.value}",
    )


def _workload(
    *,
    operation: str = "deterministic-transform",
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    complexity: int = 2,
    latency_sla_ms: int = 5_000,
    required_capabilities: tuple[str, ...] = (),
    provider_required: bool = False,
    preferred_provider: str = "",
) -> Workload:
    suffix = new_id("test")
    return Workload(
        workload_id=f"workload-{suffix}",
        task_id=f"task-{suffix}",
        run_id=f"run-{suffix}",
        operation=operation,
        payload={"text": "semantic deployment"},
        sensitivity=sensitivity,
        complexity=complexity,
        latency_sla_ms=latency_sla_ms,
        cpu_units=1,
        memory_mb=32,
        required_capabilities=required_capabilities,
        provider_required=provider_required,
        preferred_provider=preferred_provider,
        idempotency_key=f"idempotency-{suffix}",
    )


def test_default_profiles_are_behaviorally_distinct_and_secret_safe(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        environment={
            "OPENAI_API_KEY": "secret-value-must-not-escape",
            "ANTHROPIC_API_KEY": "",
        },
    )

    device = catalog.policy(DeploymentProfile.DEVICE)
    edge = catalog.policy(DeploymentProfile.EDGE)
    cloud = catalog.policy(DeploymentProfile.CLOUD)
    projection = catalog.public_projection()
    serialized = json.dumps(projection, sort_keys=True)

    assert {device.port, edge.port, cloud.port} == {32100, 32101, 32102}
    assert len({device.network_mode, edge.network_mode, cloud.network_mode}) == 3
    assert len(
        {
            device.resource.memory_mb,
            edge.resource.memory_mb,
            cloud.resource.memory_mb,
        }
    ) == 3
    assert Sensitivity.RESTRICTED in device.allowed_sensitivity
    assert Sensitivity.RESTRICTED not in edge.allowed_sensitivity
    assert Sensitivity.CONFIDENTIAL not in cloud.allowed_sensitivity
    assert projection["independent_process_required"] is True
    assert projection["same_process_labels_accepted"] is False
    assert "secret-value-must-not-escape" not in serialized
    assert catalog.credential_presence(DeploymentProfile.CLOUD)["OPENAI_API_KEY"] is True


def test_request_authentication_rejects_tampering_staleness_and_replay() -> None:
    now = 1_900_000_000
    secret = b"x" * 32
    signer = RequestSigner(secret)
    authenticator = RequestAuthenticator(secret, clock=lambda: now)
    request = signer.sign(
        "POST",
        "/v1/execute",
        {"operation": "deterministic-transform"},
        timestamp=now,
        nonce="nonce-with-at-least-sixteen",
    )

    nonce = authenticator.authenticate(
        method=request.method,
        path=request.path,
        body=request.body,
        headers=request.headers(),
    )
    assert nonce == request.nonce

    with pytest.raises(NodeReplayRejected, match="already admitted"):
        authenticator.authenticate(
            method=request.method,
            path=request.path,
            body=request.body,
            headers=request.headers(),
        )

    stale = signer.sign(
        "GET",
        "/v1/health",
        timestamp=now - 100,
        nonce="another-valid-long-nonce",
    )
    with pytest.raises(NodeAuthenticationError, match="outside the accepted window"):
        authenticator.authenticate(
            method=stale.method,
            path=stale.path,
            body=stale.body,
            headers=stale.headers(),
        )

    tampered = signer.sign(
        "POST",
        "/v1/execute",
        {"value": "original"},
        timestamp=now,
        nonce="third-valid-long-nonce",
    )
    with pytest.raises(NodeAuthenticationError, match="signature is invalid"):
        authenticator.authenticate(
            method=tampered.method,
            path=tampered.path,
            body=b'{"value":"tampered"}',
            headers=tampered.headers(),
        )


def test_placement_binds_restricted_low_latency_and_provider_workloads(
    tmp_path: Path,
) -> None:
    environment = {"OPENAI_API_KEY": "present-for-routing-only"}
    catalog = _catalog(tmp_path, environment=environment)
    store = DeploymentStateStore(tmp_path / "state" / "deployment.sqlite3")
    runtime = PlacementPolicyRuntime(catalog, store, environment=environment)
    observations = {
        profile: _observation(
            catalog,
            profile,
            pid=100 + index,
            credential_presence=(
                {"OPENAI_API_KEY": True, "ANTHROPIC_API_KEY": False}
                if profile is DeploymentProfile.CLOUD
                else {}
            ),
        )
        for index, profile in enumerate(DeploymentProfile)
    }

    restricted = runtime.decide(
        _workload(sensitivity=Sensitivity.RESTRICTED),
        PlacementContext(observations=observations),
    )
    edge = runtime.decide(
        _workload(
            operation="edge-window-aggregate",
            latency_sla_ms=100,
            required_capabilities=("edge-compute",),
        ),
        PlacementContext(observations=observations),
    )
    cloud = runtime.decide(
        _workload(
            operation="provider-plan",
            sensitivity=Sensitivity.PUBLIC,
            complexity=10,
            required_capabilities=("provider-dispatch",),
            provider_required=True,
            preferred_provider="openai",
        ),
        PlacementContext(observations=observations),
    )

    assert restricted.selected_profile is DeploymentProfile.DEVICE
    assert edge.selected_profile is DeploymentProfile.EDGE
    assert cloud.selected_profile is DeploymentProfile.CLOUD
    assert store.placement(cloud.decision_id)["decision_id"] == cloud.decision_id


def test_placement_fails_closed_for_disabled_missing_credential_and_same_process(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    store = DeploymentStateStore(tmp_path / "deployment.sqlite3")
    observations = {
        profile: _observation(catalog, profile, pid=777 + index)
        for index, profile in enumerate(DeploymentProfile)
    }
    provider_workload = _workload(
        operation="provider-plan",
        required_capabilities=("provider-dispatch",),
        provider_required=True,
        preferred_provider="openai",
    )

    with pytest.raises(PlacementRejected, match="no deployment profile"):
        PlacementPolicyRuntime(catalog, store, environment={}).decide(
            provider_workload,
            PlacementContext(observations=observations),
        )

    with pytest.raises(PlacementDisabled, match="placement binding is disabled"):
        PlacementPolicyRuntime(catalog, store, enabled=False).decide(
            _workload(),
            PlacementContext(observations=observations),
        )

    reused = {
        profile: replace(observation, pid=777)
        for profile, observation in observations.items()
    }
    with pytest.raises(PlacementRejected, match="share a process identity"):
        PlacementPolicyRuntime(catalog, store).decide(
            _workload(),
            PlacementContext(observations=reused),
        )


def test_store_revision_events_and_integrity_are_durable(tmp_path: Path) -> None:
    store = DeploymentStateStore(tmp_path / "deployment.sqlite3")
    first_revision = store.write_meta(
        "deployment-test",
        {"phase": "created"},
        expected_revision=0,
    )
    second_revision = store.write_meta(
        "deployment-test",
        {"phase": "updated"},
        expected_revision=first_revision,
    )
    event = store.append_event(
        "deployment.test_transition",
        {"from": "created", "to": "updated"},
        component_id="test-component",
        task_id="task-test",
        run_id="run-test",
    )

    reopened = DeploymentStateStore(store.path)
    assert second_revision == first_revision + 1
    assert reopened.read_meta("deployment-test") == {"phase": "updated"}
    assert reopened.events(task_id="task-test")[0]["event_id"] == event["event_id"]
    integrity = reopened.integrity_report()
    assert integrity["ready"] is True
    assert integrity["quick_check"] == "ok"
    assert integrity["counts"]["deployment_events"] == 1

    with pytest.raises(StateConflict, match="revision changed"):
        reopened.write_meta(
            "deployment-test",
            {"phase": "stale"},
            expected_revision=first_revision,
        )


def test_configuration_drift_and_network_loss_are_not_silently_admitted(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    store = DeploymentStateStore(tmp_path / "deployment.sqlite3")
    runtime = PlacementPolicyRuntime(catalog, store)
    observations = {
        profile: _observation(catalog, profile, pid=900 + index)
        for index, profile in enumerate(DeploymentProfile)
    }
    observations[DeploymentProfile.EDGE] = replace(
        observations[DeploymentProfile.EDGE],
        configuration_digest="sha256:drift",
    )
    observations[DeploymentProfile.CLOUD] = replace(
        observations[DeploymentProfile.CLOUD],
        network={"in_process": False, "network_down": True},
    )

    decision = runtime.decide(
        _workload(sensitivity=Sensitivity.RESTRICTED),
        PlacementContext(observations=observations),
    )
    candidates = {item.profile: item for item in decision.candidates}

    assert "node_configuration_drift" in candidates[DeploymentProfile.EDGE].blockers
    assert "node_network_unavailable" in candidates[DeploymentProfile.CLOUD].blockers
    assert decision.selected_profile is DeploymentProfile.DEVICE


def test_signed_response_verification_detects_body_substitution() -> None:
    secret = b"z" * 32
    signer = RequestSigner(secret)
    authenticator = RequestAuthenticator(secret)
    request = signer.sign("GET", "/v1/semantic-readiness")
    response = b'{"ready":true}'
    signature = authenticator.sign_response(
        request_nonce=request.nonce,
        status=200,
        body=response,
    )

    signer.verify_response(
        request,
        status=200,
        body=response,
        signature=signature,
    )
    with pytest.raises(NodeAuthenticationError, match="response signature"):
        signer.verify_response(
            request,
            status=200,
            body=b'{"ready":false}',
            signature=signature,
        )
