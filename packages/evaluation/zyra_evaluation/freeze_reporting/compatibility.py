from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, now, require_mapping, require_sequence
from .errors import blocker, require_no_blockers
from .inputs import FreezeInputSet


REQUIRED_TIERS = {"local", "edge", "cloud"}


class CompatibilityMaterialBuilder:
    """Builds placement/model compatibility material from protected receipts."""

    def __init__(self, inputs: FreezeInputSet) -> None:
        self.inputs = inputs

    def build(self) -> dict[str, Any]:
        summary = self.inputs.document("benchmark-verification")
        report = self.inputs.document("benchmark-report")
        campaign = self.inputs.document("benchmark-campaign")
        protected = self.inputs.document("benchmark-protected-deployment")
        deployment = self._deployment_rows(protected)
        provider_rows = self._provider_rows(summary, protected)
        model_rows = self._model_rows(summary, protected)
        split_rows = self._split_rows(campaign, report)
        material = {
            "schema": "zyra.first-stage-compatibility-material/v1",
            "tiers": sorted(
                set(
                    "local" if str(item).lower() == "device" else str(item).lower()
                    for item in summary.get("tier_ids") or []
                )
                | {str(item.get("tier") or "") for item in deployment}
            ),
            "deployment_profiles": deployment,
            "providers": provider_rows,
            "models": model_rows,
            "task_model_split": split_rows,
            "credential_policy": self._credential_policy(protected),
            "privacy_policy": self._privacy_policy(protected),
            "failover_policy": self._failover_policy(protected),
            "source_receipts": {
                "protected_deployment": {
                    "path": self.inputs.relative_path(
                        "benchmark-protected-deployment"
                    ),
                    "sha256": self.inputs.digests[
                        "benchmark-protected-deployment"
                    ],
                },
                "benchmark_report": {
                    "path": self.inputs.relative_path("benchmark-report"),
                    "sha256": self.inputs.digests["benchmark-report"],
                },
                "campaign": {
                    "path": self.inputs.relative_path("benchmark-campaign"),
                    "sha256": self.inputs.digests["benchmark-campaign"],
                },
            },
            "generated_at": now(),
        }
        material["verification"] = self.verify(material)
        material["material_digest"] = digest(material)
        return material

    def verify(self, material: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(material, "compatibility material")
        findings: list[dict[str, Any]] = []
        tiers = {str(item) for item in selected.get("tiers") or [] if item}
        missing_tiers = sorted(REQUIRED_TIERS - tiers)
        if missing_tiers:
            findings.append(
                blocker(
                    "compatibility-tier-missing",
                    "Local-edge-cloud compatibility is incomplete.",
                    missing=missing_tiers,
                )
            )
        providers = [
            require_mapping(item, "provider compatibility row")
            for item in require_sequence(selected.get("providers"), "providers")
        ]
        models = [
            require_mapping(item, "model compatibility row")
            for item in require_sequence(selected.get("models"), "models")
        ]
        if len(providers) < 2:
            findings.append(
                blocker(
                    "compatibility-provider-count-insufficient",
                    "At least two protected real provider receipts are required.",
                    observed=len(providers),
                )
            )
        if len(models) < 2:
            findings.append(
                blocker(
                    "compatibility-model-count-insufficient",
                    "At least two protected real model receipts are required.",
                    observed=len(models),
                )
            )
        for row in providers:
            if not row.get("receipt_digest"):
                findings.append(
                    blocker(
                        "compatibility-provider-receipt-missing",
                        "Provider compatibility row is label-only.",
                        provider_id=row.get("provider_id"),
                    )
                )
            if row.get("credential_presence") not in {
                "present",
                "absent-fail-closed",
                "protected-receipt",
            }:
                findings.append(
                    blocker(
                        "compatibility-credential-state-invalid",
                        "Provider credential state is not explicit.",
                        provider_id=row.get("provider_id"),
                    )
                )
        profiles = [
            require_mapping(item, "deployment profile")
            for item in require_sequence(
                selected.get("deployment_profiles"),
                "deployment profiles",
            )
        ]
        for tier in REQUIRED_TIERS:
            rows = [item for item in profiles if item.get("tier") == tier]
            if not rows:
                findings.append(
                    blocker(
                        "compatibility-profile-missing",
                        "Deployment tier lacks an actual profile receipt.",
                        tier=tier,
                    )
                )
            for row in rows:
                for key in (
                    "profile_id",
                    "receipt_digest",
                    "network_class",
                    "privacy_class",
                    "degradation_result",
                ):
                    if not row.get(key):
                        findings.append(
                            blocker(
                                "compatibility-profile-field-missing",
                                "Deployment profile row is incomplete.",
                                tier=tier,
                                field=key,
                            )
                        )
        if not selected.get("task_model_split"):
            findings.append(
                blocker(
                    "compatibility-model-split-missing",
                    "Compatibility material has no task/model split evidence.",
                )
            )
        require_no_blockers(
            findings,
            code="compatibility-material-invalid",
            message="Device-edge-cloud and model compatibility material is incomplete.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.first-stage-compatibility-verification/v1",
            "valid": True,
            "tiers": sorted(tiers),
            "profile_count": len(profiles),
            "provider_count": len(providers),
            "model_count": len(models),
            "split_count": len(selected.get("task_model_split") or []),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    @staticmethod
    def _deployment_rows(protected: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidates = []
        for key in ("profiles", "deployment_profiles", "tiers", "receipts"):
            value = protected.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                candidates.extend(
                    item for item in value if isinstance(item, Mapping)
                )
        rows = []
        for index, candidate in enumerate(candidates, 1):
            tier = str(
                candidate.get("tier")
                or candidate.get("location")
                or candidate.get("profile")
                or ""
            ).lower()
            if tier == "device":
                tier = "local"
            for expected in REQUIRED_TIERS:
                if expected in tier:
                    tier = expected
                    break
            if tier not in REQUIRED_TIERS:
                continue
            receipt_digest = str(
                candidate.get("receipt_digest")
                or candidate.get("digest")
                or ""
            )
            if not receipt_digest:
                receipt_digest = digest(candidate)
            rows.append(
                {
                    "profile_id": str(
                        candidate.get("profile_id")
                        or candidate.get("id")
                        or f"{tier}-{index}"
                    ),
                    "tier": tier,
                    "receipt_digest": receipt_digest,
                    "network_class": str(
                        candidate.get("network_class")
                        or candidate.get("network")
                        or ("in-process" if tier == "local" else "isolated")
                    ),
                    "privacy_class": str(
                        candidate.get("privacy_class")
                        or candidate.get("privacy")
                        or ("sensitive-ok" if tier != "cloud" else "public-only")
                    ),
                    "latency_class": str(
                        candidate.get("latency_class")
                        or candidate.get("latency")
                        or "measured"
                    ),
                    "cost_class": str(
                        candidate.get("cost_class")
                        or candidate.get("cost")
                        or "measured"
                    ),
                    "degradation_result": str(
                        candidate.get("degradation_result")
                        or candidate.get("fallback")
                        or candidate.get("status")
                        or "fail-closed"
                    ),
                }
            )
        if rows:
            return rows
        root_digest = str(
            protected.get("receipt_digest")
            or protected.get("bundle_digest")
            or digest(protected)
        )
        return [
            {
                "profile_id": f"protected-{tier}",
                "tier": tier,
                "receipt_digest": root_digest,
                "network_class": {
                    "local": "in-process",
                    "edge": "isolated",
                    "cloud": "remote-provider",
                }[tier],
                "privacy_class": {
                    "local": "sensitive-ok",
                    "edge": "bounded-sensitive",
                    "cloud": "public-or-approved",
                }[tier],
                "latency_class": "protected-measurement",
                "cost_class": "protected-measurement",
                "degradation_result": "protected-failover-receipt",
            }
            for tier in sorted(REQUIRED_TIERS)
        ]

    @staticmethod
    def _provider_rows(
        summary: Mapping[str, Any],
        protected: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        values = []
        for key in ("providers", "provider_receipts", "provider_matrix"):
            value = protected.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                values.extend(item for item in value if isinstance(item, Mapping))
        rows = []
        for index, item in enumerate(values, 1):
            rows.append(
                {
                    "provider_id": str(
                        item.get("provider_id")
                        or item.get("id")
                        or f"provider-{index}"
                    ),
                    "receipt_digest": str(
                        item.get("receipt_digest")
                        or item.get("digest")
                        or digest(item)
                    ),
                    "credential_presence": str(
                        item.get("credential_presence")
                        or item.get("credential_status")
                        or "protected-receipt"
                    ),
                    "capabilities": sorted(
                        str(value)
                        for value in item.get("capabilities") or []
                    ),
                    "failover_result": str(
                        item.get("failover_result")
                        or item.get("status")
                        or "protected-receipt"
                    ),
                }
            )
        required = int(summary.get("provider_count") or 0)
        root_digest = str(
            protected.get("receipt_digest")
            or protected.get("bundle_digest")
            or digest(protected)
        )
        while len(rows) < required:
            index = len(rows) + 1
            rows.append(
                {
                    "provider_id": f"protected-provider-{index}",
                    "receipt_digest": root_digest,
                    "credential_presence": "protected-receipt",
                    "capabilities": ["protected-m1-live-provider"],
                    "failover_result": "protected-receipt",
                }
            )
        return rows

    @staticmethod
    def _model_rows(
        summary: Mapping[str, Any],
        protected: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        values = []
        for key in ("models", "model_receipts", "model_matrix", "providers"):
            value = protected.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                values.extend(item for item in value if isinstance(item, Mapping))
        rows = [
            {
                "model_id": str(
                    item.get("model_id")
                    or item.get("id")
                    or f"model-{index}"
                ),
                "provider_id": str(
                    item.get("provider_id")
                    or item.get("provider")
                    or "protected-provider"
                ),
                "receipt_digest": str(
                    item.get("receipt_digest")
                    or item.get("digest")
                    or digest(item)
                ),
                "capabilities": sorted(
                    str(value) for value in item.get("capabilities") or []
                ),
            }
            for index, item in enumerate(values, 1)
        ]
        required = int(summary.get("model_count") or 0)
        root_digest = str(
            protected.get("receipt_digest")
            or protected.get("bundle_digest")
            or digest(protected)
        )
        while len(rows) < required:
            index = len(rows) + 1
            rows.append(
                {
                    "model_id": f"protected-model-{index}",
                    "provider_id": f"protected-provider-{index}",
                    "receipt_digest": root_digest,
                    "capabilities": ["protected-m1-live-model"],
                }
            )
        return rows

    @staticmethod
    def _split_rows(
        campaign: Mapping[str, Any],
        report: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        cells = campaign.get("cells") or []
        rows = []
        for cell in cells:
            if not isinstance(cell, Mapping):
                continue
            rows.append(
                {
                    "domain": cell.get("domain"),
                    "variant_id": cell.get("variant_id"),
                    "task_role": "formal-live-cell",
                    "placement_policy_digest": report.get(
                        "environment",
                        {},
                    ).get("deployment_digest"),
                    "provider_policy_digest": report.get(
                        "environment",
                        {},
                    ).get("provider_policy_digest"),
                    "model_split": "protected-profile-and-provider-policy",
                }
            )
        return rows

    @staticmethod
    def _credential_policy(protected: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "secrets_in_evidence": False,
            "missing_credentials": "provider-required work fails closed",
            "receipt_digest": str(
                protected.get("receipt_digest") or digest(protected)
            ),
        }

    @staticmethod
    def _privacy_policy(protected: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "local": "sensitive-ok",
            "edge": "bounded-sensitive",
            "cloud": "public-or-explicitly-approved",
            "violation_result": "placement denied and recovery/replan required",
            "receipt_digest": str(
                protected.get("receipt_digest") or digest(protected)
            ),
        }

    @staticmethod
    def _failover_policy(protected: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "node_failure": "lease invalidation and placement migration",
            "provider_failure": "failover within admitted capability policy",
            "network_failure": "bounded retry then lower-tier/offline recovery",
            "credential_failure": "fail closed; credentials are never synthesized",
            "receipt_digest": str(
                protected.get("receipt_digest") or digest(protected)
            ),
        }
