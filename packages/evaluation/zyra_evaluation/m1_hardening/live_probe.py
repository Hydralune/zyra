from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import GateResult, utc_now
from .execution_tiers import ExecutionTierProbeSuite, ExecutionTierRunReceipt
from .integration_contracts import stable_digest
from .live_evidence import (
    EndpointAttestation,
    LiveEvidenceSuite,
    ProviderWireAttestation,
    WireDialect,
)
from .managed_provider import (
    ManagedProviderProbe,
    ManagedProviderReceipt,
    ManagedProviderSpec,
)


@dataclass(frozen=True, slots=True)
class ManagedLiveEvidenceReceipt:
    run_id: str
    started_at: str
    completed_at: str
    providers: tuple[ManagedProviderReceipt, ...]
    execution_tiers: ExecutionTierRunReceipt
    gates: tuple[GateResult, ...]
    receipt_path: str
    content_digest: str

    @property
    def accepted(self) -> bool:
        return bool(self.gates) and all(item.ok for item in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.managed-live-evidence/v1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "accepted": self.accepted,
            "providers": [item.to_dict() for item in self.providers],
            "execution_tiers": self.execution_tiers.to_dict(),
            "gates": [item.to_dict() for item in self.gates],
            "receipt_path": self.receipt_path,
            "content_digest": self.content_digest,
        }


class ManagedLiveEvidenceRuntime:
    """Run two authenticated provider tools and local/edge/cloud dispatch as one receipt."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        artifact_root: str | Path,
        provider_probe: ManagedProviderProbe | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.provider_probe = provider_probe or ManagedProviderProbe()

    def execute(
        self,
        *,
        claude_executable: str,
        claude_model: str,
        codex_executable: str,
        codex_model: str,
        edge_host: str = "",
        run_id: str = "",
        maximum_budget_usd: float = 0.20,
    ) -> ManagedLiveEvidenceReceipt:
        selected_run_id = run_id or f"m1-live-{uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,120}", selected_run_id):
            raise ValueError("managed live-evidence run id is invalid")
        started_at = utc_now()
        root = self.artifact_root / "live" / selected_run_id
        root.mkdir(parents=True, exist_ok=False)
        specs = (
            ManagedProviderSpec(
                provider_id="anthropic",
                cli_kind="claude",
                executable=claude_executable,
                model_id=claude_model,
                dialect=WireDialect.ANTHROPIC_COMPATIBLE,
                endpoint="https://api.anthropic.com/v1/messages",
                request_path="/v1/messages",
                maximum_budget_usd=maximum_budget_usd,
            ),
            ManagedProviderSpec(
                provider_id="openai",
                cli_kind="codex",
                executable=codex_executable,
                model_id=codex_model,
                dialect=WireDialect.OPENAI_COMPATIBLE,
                endpoint="https://api.openai.com/v1/responses",
                request_path="/v1/responses",
                maximum_budget_usd=maximum_budget_usd,
            ),
        )
        providers = tuple(
            self.provider_probe.execute(
                spec,
                artifact_root=root,
                run_id=selected_run_id,
            )
            for spec in specs
        )
        tiers = ExecutionTierProbeSuite(self.project_root).execute(
            artifact_root=root,
            cloud_provider=providers[1],
            edge_host=edge_host,
            run_id=selected_run_id,
        )
        gates = LiveEvidenceSuite().evaluate(
            tier_observations=tiers.attestations,
            provider_observations=tuple(item.attestation for item in providers),
            final_completion=True,
        )
        completed_at = utc_now()
        receipt_path = root / "managed-live-evidence.json"
        payload = {
            "schema": "zyra.managed-live-evidence/v1",
            "run_id": selected_run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "providers": [item.to_dict() for item in providers],
            "execution_tiers": tiers.to_dict(),
            "gates": [item.to_dict() for item in gates],
            "receipt_path": str(receipt_path),
        }
        content_digest = stable_digest(payload)
        receipt_path.write_text(
            json.dumps(
                {**payload, "content_digest": content_digest},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ManagedLiveEvidenceReceipt(
            run_id=selected_run_id,
            started_at=started_at,
            completed_at=completed_at,
            providers=providers,
            execution_tiers=tiers,
            gates=gates,
            receipt_path=str(receipt_path),
            content_digest=content_digest,
        )

    @staticmethod
    def load_receipt(path: str | Path) -> ManagedLiveEvidenceReceipt:
        """Reload and independently revalidate one persisted live receipt."""

        target = Path(path).resolve()
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != "zyra.managed-live-evidence/v1":
            raise ValueError("managed live-evidence receipt schema is invalid")
        expected_digest = str(value.get("content_digest") or "")
        unsigned = dict(value)
        unsigned.pop("content_digest", None)
        if stable_digest(unsigned) != expected_digest:
            raise ValueError("managed live-evidence receipt digest mismatch")
        provider_values = value.get("providers")
        tier_value = value.get("execution_tiers")
        if not isinstance(provider_values, list) or not isinstance(tier_value, dict):
            raise ValueError("managed live-evidence receipt is incomplete")
        providers: list[ManagedProviderReceipt] = []
        for item in provider_values:
            if not isinstance(item, dict) or not isinstance(item.get("attestation"), dict):
                raise ValueError("managed provider receipt is invalid")
            trace_path = Path(str(item.get("trace_path") or "")).resolve()
            trace_digest = str(item.get("trace_digest") or "")
            if not trace_path.is_file():
                raise ValueError(f"managed provider trace is missing: {trace_path}")
            if stable_file_digest(trace_path) != trace_digest:
                raise ValueError(f"managed provider trace digest mismatch: {trace_path}")
            providers.append(
                ManagedProviderReceipt(
                    attestation=ProviderWireAttestation.from_mapping(item["attestation"]),
                    trace_path=str(trace_path),
                    trace_digest=trace_digest,
                    command_id=str(item.get("command_id") or ""),
                )
            )
        tier_receipt_path = Path(str(tier_value.get("receipt_path") or "")).resolve()
        tier_digest = str(tier_value.get("receipt_digest") or "")
        if not tier_receipt_path.is_file():
            raise ValueError(f"execution-tier receipt is missing: {tier_receipt_path}")
        if stable_file_digest(tier_receipt_path) != tier_digest:
            raise ValueError("execution-tier receipt digest mismatch")
        tiers = ExecutionTierRunReceipt(
            run_id=str(tier_value.get("run_id") or ""),
            attestations=tuple(
                EndpointAttestation.from_mapping(item)
                for item in tier_value.get("attestations") or ()
                if isinstance(item, dict)
            ),
            receipt_path=str(tier_receipt_path),
            receipt_digest=tier_digest,
        )
        run_id = str(value.get("run_id") or "")
        if tiers.run_id != run_id or any(
            item.attestation.metadata.get("run_id") not in {None, "", run_id}
            for item in providers
        ):
            raise ValueError("managed live-evidence run partition mismatch")
        gates = LiveEvidenceSuite().evaluate(
            tier_observations=tiers.attestations,
            provider_observations=tuple(item.attestation for item in providers),
            final_completion=True,
        )
        if not gates or not all(item.ok for item in gates):
            raise ValueError("persisted managed live-evidence no longer passes validation")
        return ManagedLiveEvidenceReceipt(
            run_id=run_id,
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            providers=tuple(providers),
            execution_tiers=tiers,
            gates=gates,
            receipt_path=str(target),
            content_digest=expected_digest,
        )


def stable_file_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "ManagedLiveEvidenceReceipt",
    "ManagedLiveEvidenceRuntime",
]
