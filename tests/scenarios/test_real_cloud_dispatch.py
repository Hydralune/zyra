from __future__ import annotations

import json

from tests.integration.physical_dispatch_harness import (
    ROOT,
    build_physical_harness,
    read_env_file,
)


GLM_ENV = "ZAI_API_KEY"


def _real_cloud_environment() -> dict[str, str]:
    path = ROOT / ".env.glm.local"
    assert path.is_file(), (
        "real cloud gate is unclosed: .env.glm.local is missing"
    )
    values = read_env_file(path)
    assert values.get(GLM_ENV), (
        "real cloud gate is unclosed: ZAI_API_KEY is missing"
    )
    return values


def _lower_priority_cloud_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (
        ROOT / ".env.kimi.local",
        ROOT / ".env.deepseek.local",
    ):
        assert path.is_file(), (
            f"real fallback gate is unclosed: {path.name} is missing"
        )
        values.update(read_env_file(path))
    values["ZAI_API_KEY"] = ""
    assert values.get("KIMI_API_KEY")
    assert values.get("DEEPSEEK_API_KEY")
    return values


def test_real_cloud_provider_request_closes_receipt_gate(tmp_path) -> None:
    environment = _real_cloud_environment()
    secret = environment[GLM_ENV]
    harness = build_physical_harness(
        tmp_path,
        location="cloud",
        environment=environment,
    )
    try:
        result = harness.execute()
        receipt = harness.physical_port.receipts[0]
        validation = harness.physical_port.validation_reports[0]
        provider = dict(receipt.provider_evidence)
        usage = dict(provider["usage"])
        attempt = result.attempt_receipts[0]

        assert validation.real_gate_closed is True
        assert validation.blockers == ()
        assert receipt.physical_identity["location"] == "cloud"
        assert provider["provider_id"] == "zhipu"
        assert provider["model_id"] == "glm-5.2"
        assert provider["request_id"]
        assert provider["provider_attempt_id"]
        assert 200 <= int(provider["http_status"]) < 300
        assert int(usage["total_tokens"]) > 0
        assert float(provider["cost_usd"]) > 0
        assert int(provider["latency_ms"]) > 0
        assert provider["marker_verified"] is True
        assert provider["credential_ref"] == "env://ZAI_API_KEY"
        assert provider["credential_material_persisted"] is False
        assert attempt.actual_tokens == int(usage["total_tokens"])
        assert attempt.actual_cost_usd == float(provider["cost_usd"])
        assert attempt.physical_dispatch_receipt_digest == receipt.digest

        persisted_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (tmp_path / "physical-evidence").glob("*.json")
        )
        assert secret not in persisted_text
        assert '"credential_material_persisted":false' in persisted_text
    finally:
        harness.close()


def test_cloud_fallback_prefers_kimi_before_deepseek(tmp_path) -> None:
    environment = _lower_priority_cloud_environment()
    harness = build_physical_harness(
        tmp_path,
        location="cloud",
        environment=environment,
    )
    try:
        harness.execute()
        provider = dict(harness.physical_port.receipts[0].provider_evidence)

        assert provider["provider_id"] == "kimi-platform"
        assert provider["model_id"] == "kimi-k2.7-code"
        assert provider["credential_ref"] == "env://KIMI_API_KEY"
        assert provider["credential_material_persisted"] is False
        assert provider["marker_verified"] is True
    finally:
        harness.close()


def test_cloud_without_credential_is_blocked_and_safely_rerouted(
    tmp_path,
) -> None:
    harness = build_physical_harness(
        tmp_path,
        location="cloud",
        fallback_locations=("local",),
        environment={GLM_ENV: ""},
    )
    try:
        result = harness.execute()
        receipt = harness.physical_port.receipts[-1]
        failure = harness.physical_port.failure_receipts[0]

        assert result.recovery_plan_refs
        assert receipt.physical_identity["location"] == "local"
        assert receipt.provider_evidence == {}
        assert failure.location == "cloud"
        assert (
            failure.failure_code
            == "physical_dispatch_cloud_credential_missing"
        )
        assert failure.side_effect_started is False
        assert failure.retry_safe is True
        assert receipt.recovery_evidence
        persisted_receipts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (tmp_path / "physical-evidence").glob("receipt-*.json")
        ]
        assert all(
            item["payload"]["physical_identity"]["location"] != "cloud"
            for item in persisted_receipts
        )
    finally:
        harness.close()
