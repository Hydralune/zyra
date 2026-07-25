from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonicalize, digest, file_digest, path_within, utc_now
from .errors import conflict, invalid, unavailable
from .live_models import DomainVerification, LiveDomain, LiveDomainResult


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    entry_id: str
    kind: str
    path: str
    sha256: str
    size_bytes: int
    owner: str
    source_event_ids: tuple[str, ...]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "owner": self.owner,
            "source_event_ids": list(self.source_event_ids),
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CausalArchive:
    archive_id: str
    scenario_run_id: str
    owner_run_id: str
    task_id: str
    domain: LiveDomain
    configuration_digest: str
    input_digest: str
    policy_digest: str
    source_commit: str
    entries: tuple[ArchiveEntry, ...]
    root_event_id: str
    leaf_event_id: str
    event_count: int
    effective_transition_count: int
    human_intervention_count: int
    created_at: str
    manifest_path: str
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-causal-archive/v1",
            "archive_id": self.archive_id,
            "scenario_run_id": self.scenario_run_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "domain": self.domain.value,
            "configuration_digest": self.configuration_digest,
            "input_digest": self.input_digest,
            "policy_digest": self.policy_digest,
            "source_commit": self.source_commit,
            "entries": [item.to_dict() for item in self.entries],
            "root_event_id": self.root_event_id,
            "leaf_event_id": self.leaf_event_id,
            "event_count": self.event_count,
            "effective_transition_count": self.effective_transition_count,
            "human_intervention_count": self.human_intervention_count,
            "created_at": self.created_at,
            "manifest_path": self.manifest_path,
            "manifest_digest": self.manifest_digest,
        }


class AtomicArchiveWriter:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, value: Any) -> Path:
        selected = self._path(name)
        temporary = selected.with_suffix(selected.suffix + ".tmp")
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selected)
        return selected

    def write_jsonl(self, name: str, values: Iterable[Mapping[str, Any]]) -> Path:
        selected = self._path(name)
        temporary = selected.with_suffix(selected.suffix + ".tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            for value in values:
                stream.write(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selected)
        return selected

    def _path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise invalid(
                "archive_name_invalid",
                "Causal archive entry name must be a single safe path component.",
                phase="live-archive",
            )
        selected = (self.root / name).resolve(strict=False)
        if not path_within(selected, self.root):
            raise invalid(
                "archive_path_escape",
                "Causal archive path escaped its owner root.",
                phase="live-archive",
            )
        if selected.exists():
            raise conflict(
                "archive_entry_exists",
                "Causal archive entries are immutable.",
                phase="live-archive",
                detail={"path": str(selected)},
            )
        return selected


class CausalArchiveBuilder:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        source_commit: str,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.source_commit = source_commit

    def build(
        self,
        *,
        scenario_run_id: str,
        owner_run_id: str,
        task_id: str,
        configuration_digest: str,
        input_digest: str,
        policy_digest: str,
        domain: LiveDomain,
        events: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
        owner_receipts: Sequence[Mapping[str, Any]],
        fault_receipts: Sequence[Mapping[str, Any]],
        placement_receipt: Mapping[str, Any],
        verification: DomainVerification,
        raw_samples: Sequence[Mapping[str, Any]],
        environment: Mapping[str, Any] | None = None,
    ) -> CausalArchive:
        if not _commit(self.source_commit):
            raise invalid(
                "archive_source_commit_invalid",
                "Live causal archive requires an immutable source commit.",
                phase="live-archive",
                detail={"commit": self.source_commit},
            )
        if not events:
            raise conflict(
                "archive_events_missing",
                "Live causal archive requires canonical events.",
                phase="live-archive",
            )
        archive_id = f"live-archive-{scenario_run_id}"
        root = self.artifact_root / "causal-archives" / archive_id
        if root.exists():
            raise conflict(
                "archive_already_exists",
                "Live causal archive is immutable and cannot be overwritten.",
                phase="live-archive",
                detail={"archive_id": archive_id},
            )
        writer = AtomicArchiveWriter(root)
        event_path = writer.write_jsonl("canonical-events.jsonl", events)
        receipt_path = writer.write_json(
            "owner-receipts.json",
            {
                "schema": "zyra.live-owner-receipts/v1",
                "owner_receipts": list(owner_receipts),
                "fault_receipts": list(fault_receipts),
                "placement_receipt": dict(placement_receipt),
            },
        )
        sample_path = writer.write_jsonl("raw-samples.jsonl", raw_samples)
        verification_path = writer.write_json(
            "domain-verification.json",
            verification.to_dict(),
        )
        environment_path = writer.write_json(
            "environment.json",
            {
                "schema": "zyra.live-environment/v1",
                "source_commit": self.source_commit,
                "captured_at": utc_now(),
                "runtime": runtime_environment(),
                "scenario": canonicalize(environment or {}),
            },
        )
        artifact_projection = [
            self._artifact_projection(item)
            for item in artifacts
        ]
        artifacts_path = writer.write_json(
            "artifact-manifest.json",
            {
                "schema": "zyra.live-artifact-manifest/v1",
                "artifacts": artifact_projection,
            },
        )
        entries = (
            self._entry(
                "archive-events",
                "canonical-events",
                event_path,
                owner="RuntimeEventSpine/ScenarioExecutionPort",
                source_event_ids=tuple(str(item.get("event_id") or "") for item in events),
            ),
            self._entry(
                "archive-owner-receipts",
                "owner-receipts",
                receipt_path,
                owner="canonical-owner-ports",
                source_event_ids=tuple(
                    str(item.get("event_id") or item.get("receipt_id") or "")
                    for item in (*owner_receipts, *fault_receipts)
                    if str(item.get("event_id") or item.get("receipt_id") or "")
                ),
            ),
            self._entry(
                "archive-raw-samples",
                "raw-samples",
                sample_path,
                owner="ScenarioMetricCollector",
                source_event_ids=tuple(
                    str(item.get("event_id") or "")
                    for item in raw_samples
                    if item.get("event_id")
                ),
            ),
            self._entry(
                "archive-verification",
                "verification",
                verification_path,
                owner=verification.verifier_id,
                source_event_ids=(
                    str(events[-1].get("event_id") or ""),
                ),
            ),
            self._entry(
                "archive-environment",
                "environment",
                environment_path,
                owner="ScenarioRunnerService",
                source_event_ids=(
                    str(events[0].get("event_id") or ""),
                ),
            ),
            self._entry(
                "archive-artifacts",
                "artifact-manifest",
                artifacts_path,
                owner="LocalArtifactStore",
                source_event_ids=tuple(
                    str(item.get("source_event_id") or "")
                    for item in artifact_projection
                    if item.get("source_event_id")
                ),
            ),
        )
        manifest_path = root / "manifest.json"
        created_at = utc_now()
        root_event_id = str(events[0].get("event_id") or "")
        leaf_event_id = str(events[-1].get("event_id") or "")
        manifest = {
            "schema": "zyra.live-causal-archive/v1",
            "archive_id": archive_id,
            "scenario_run_id": scenario_run_id,
            "owner_run_id": owner_run_id,
            "task_id": task_id,
            "domain": domain.value,
            "configuration_digest": configuration_digest,
            "input_digest": input_digest,
            "policy_digest": policy_digest,
            "source_commit": self.source_commit,
            "entries": [item.to_dict() for item in entries],
            "root_event_id": root_event_id,
            "leaf_event_id": leaf_event_id,
            "event_count": len(events),
            "effective_transition_count": len(events),
            "human_intervention_count": 0,
            "created_at": created_at,
            "manifest_path": str(manifest_path),
        }
        manifest_digest = digest(manifest)
        manifest["manifest_digest"] = manifest_digest
        manifest_written = writer.write_json("manifest.json", manifest)
        if manifest_written != manifest_path:
            raise conflict(
                "archive_manifest_path_mismatch",
                "Causal archive manifest owner returned another path.",
                phase="live-archive",
            )
        archive = CausalArchive(
            archive_id=archive_id,
            scenario_run_id=scenario_run_id,
            owner_run_id=owner_run_id,
            task_id=task_id,
            domain=domain,
            configuration_digest=configuration_digest,
            input_digest=input_digest,
            policy_digest=policy_digest,
            source_commit=self.source_commit,
            entries=entries,
            root_event_id=root_event_id,
            leaf_event_id=leaf_event_id,
            event_count=len(events),
            effective_transition_count=len(events),
            human_intervention_count=0,
            created_at=created_at,
            manifest_path=str(manifest_path),
            manifest_digest=manifest_digest,
        )
        self.verify(archive)
        return archive

    def verify(self, archive: CausalArchive) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        if archive.human_intervention_count != 0:
            failures.append({"code": "human_intervention_count_nonzero"})
        if archive.event_count < 2_000 or archive.effective_transition_count < 2_000:
            failures.append(
                {
                    "code": "effective_transition_minimum",
                    "observed": archive.effective_transition_count,
                    "minimum": 2_000,
                }
            )
        if not archive.root_event_id or not archive.leaf_event_id:
            failures.append({"code": "causal_endpoints_missing"})
        entry_ids: set[str] = set()
        for item in archive.entries:
            if item.entry_id in entry_ids:
                failures.append(
                    {"code": "archive_entry_duplicate", "entry_id": item.entry_id}
                )
            entry_ids.add(item.entry_id)
            path = Path(item.path).resolve(strict=False)
            if not path_within(path, self.artifact_root):
                failures.append(
                    {
                        "code": "archive_entry_path_escape",
                        "entry_id": item.entry_id,
                    }
                )
                continue
            if not path.is_file():
                failures.append(
                    {
                        "code": "archive_entry_missing",
                        "entry_id": item.entry_id,
                    }
                )
                continue
            checksum, size = file_digest(path)
            if checksum != item.sha256 or size != item.size_bytes:
                failures.append(
                    {
                        "code": "archive_entry_stale",
                        "entry_id": item.entry_id,
                    }
                )
        manifest_path = Path(archive.manifest_path).resolve(strict=False)
        if not manifest_path.is_file():
            failures.append({"code": "archive_manifest_missing"})
        else:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = str(value.pop("manifest_digest", ""))
            observed = digest(value)
            if expected != archive.manifest_digest or observed != expected:
                failures.append(
                    {
                        "code": "archive_manifest_digest_mismatch",
                        "expected": archive.manifest_digest,
                        "stored": expected,
                        "observed": observed,
                    }
                )
        receipt = {
            "schema": "zyra.live-causal-archive-verification/v1",
            "archive_id": archive.archive_id,
            "valid": not failures,
            "entry_count": len(archive.entries),
            "event_count": archive.event_count,
            "effective_transition_count": archive.effective_transition_count,
            "manifest_digest": archive.manifest_digest,
            "failures": failures,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if failures:
            raise conflict(
                "live_causal_archive_invalid",
                "Live causal archive failed integrity verification.",
                phase="live-archive",
                detail=receipt,
            )
        return receipt

    def _artifact_projection(self, value: Mapping[str, Any]) -> dict[str, Any]:
        artifact_id = str(value.get("artifact_id") or "")
        path = Path(
            str(
                value.get("uri")
                or value.get("path")
                or value.get("absolute_path")
                or ""
            )
        ).resolve(strict=False)
        if not artifact_id or not path.is_file():
            raise conflict(
                "archive_artifact_invalid",
                "Causal archive cannot project a missing artifact.",
                phase="live-archive",
                detail={"artifact_id": artifact_id, "path": str(path)},
            )
        if not path_within(path, self.artifact_root):
            raise conflict(
                "archive_artifact_outside_owner",
                "Causal archive artifact is outside the artifact owner.",
                phase="live-archive",
                detail={"artifact_id": artifact_id, "path": str(path)},
            )
        checksum, size = file_digest(path)
        declared = str(
            value.get("sha256")
            or (value.get("metadata") or {}).get("sha256")
            or ""
        )
        if declared and declared != checksum:
            raise conflict(
                "archive_artifact_digest_mismatch",
                "Artifact checksum changed before archive settlement.",
                phase="live-archive",
                detail={
                    "artifact_id": artifact_id,
                    "expected": declared,
                    "observed": checksum,
                },
            )
        return {
            "artifact_id": artifact_id,
            "kind": str(value.get("kind") or "file"),
            "path": str(path),
            "sha256": checksum,
            "size_bytes": size,
            "producer_node_id": str(value.get("producer_node_id") or ""),
            "source_event_id": str(
                value.get("source_event_id")
                or (value.get("metadata") or {}).get("source_event_id")
                or ""
            ),
            "metadata_digest": digest(value.get("metadata") or {}),
        }

    @staticmethod
    def _entry(
        entry_id: str,
        kind: str,
        path: Path,
        *,
        owner: str,
        source_event_ids: Sequence[str],
        metadata: Mapping[str, Any] | None = None,
    ) -> ArchiveEntry:
        checksum, size = file_digest(path)
        return ArchiveEntry(
            entry_id=entry_id,
            kind=kind,
            path=str(path),
            sha256=checksum,
            size_bytes=size,
            owner=owner,
            source_event_ids=tuple(item for item in source_event_ids if item),
            metadata=dict(metadata or {}),
        )


class StabilityAnalyzer:
    def analyze(
        self,
        runs: Sequence[Mapping[str, Any]],
        *,
        expected_domain: LiveDomain | None = None,
    ) -> dict[str, Any]:
        if len(runs) < 2:
            raise invalid(
                "live_stability_sample_minimum",
                "Stability analysis requires at least two independently executed runs.",
                phase="live-archive",
            )
        domains = {str(item.get("domain") or "") for item in runs}
        if expected_domain is not None and domains != {expected_domain.value}:
            raise invalid(
                "live_stability_domain_mismatch",
                "Stability samples do not share the expected domain.",
                phase="live-archive",
                detail={
                    "expected": expected_domain.value,
                    "observed": sorted(domains),
                },
            )
        input_digests = [str(item.get("input_digest") or "") for item in runs]
        run_ids = [str(item.get("scenario_run_id") or "") for item in runs]
        manifest_digests = [str(item.get("manifest_digest") or "") for item in runs]
        transition_counts = [
            int(
                item.get("effective_transition_count")
                or item.get("event_count")
                or 0
            )
            for item in runs
        ]
        verification_values = [
            item.get("verification")
            if isinstance(item.get("verification"), Mapping)
            else {}
            for item in runs
        ]
        check_keys = set.intersection(
            *(
                set((value.get("checks") or {}).keys())
                for value in verification_values
            )
        )
        check_stability = {
            key: {
                "stable": len(
                    {
                        bool((value.get("checks") or {}).get(key))
                        for value in verification_values
                    }
                )
                == 1,
                "values": [
                    bool((value.get("checks") or {}).get(key))
                    for value in verification_values
                ],
            }
            for key in sorted(check_keys)
        }
        artifact_sets = [
            {
                str(item.get("artifact_id") or ""): str(item.get("sha256") or "")
                for item in run.get("artifacts") or ()
                if isinstance(item, Mapping)
            }
            for run in runs
        ]
        common_artifacts = set.intersection(*(set(item) for item in artifact_sets))
        artifact_stability = {
            artifact_id: {
                "stable": len(
                    {item[artifact_id] for item in artifact_sets}
                )
                == 1,
                "digests": [item[artifact_id] for item in artifact_sets],
            }
            for artifact_id in sorted(common_artifacts)
        }
        mean = sum(transition_counts) / len(transition_counts)
        spread = max(transition_counts) - min(transition_counts)
        transition_variation = spread / mean if mean else 1.0
        result = {
            "schema": "zyra.live-run-stability/v1",
            "run_count": len(runs),
            "domains": sorted(domains),
            "run_ids_unique": len(set(run_ids)) == len(run_ids),
            "inputs_unique": len(set(input_digests)) == len(input_digests),
            "manifests_unique": len(set(manifest_digests)) == len(manifest_digests),
            "all_verified": all(
                value.get("valid") is True for value in verification_values
            ),
            "transition_counts": transition_counts,
            "transition_variation": round(transition_variation, 8),
            "check_stability": check_stability,
            "artifact_stability": artifact_stability,
            "human_intervention_count": sum(
                int(item.get("human_intervention_count") or 0)
                for item in runs
            ),
        }
        result["stable"] = bool(
            result["run_ids_unique"]
            and result["inputs_unique"]
            and result["manifests_unique"]
            and result["all_verified"]
            and result["human_intervention_count"] == 0
            and transition_variation <= 0.20
            and all(item["stable"] for item in check_stability.values())
        )
        result["stability_digest"] = digest(result)
        return result


def raw_metric_samples(
    *,
    scenario_run_id: str,
    owner_run_id: str,
    task_id: str,
    events: Sequence[Mapping[str, Any]],
    result: LiveDomainResult,
) -> tuple[dict[str, Any], ...]:
    dimensions = {
        "scenario_run_id": scenario_run_id,
        "owner_run_id": owner_run_id,
        "task_id": task_id,
        "domain": result.domain.value,
    }
    samples: list[dict[str, Any]] = []
    effect_counts = Counter(
        str((item.get("metadata") or {}).get("semantic_effect") or "")
        for item in events
    )

    def add(
        metric: str,
        value: float,
        unit: str,
        *,
        source_event_ids: Sequence[str] = (),
        extra: Mapping[str, str] | None = None,
    ) -> None:
        sample = {
            "sample_id": f"raw-sample-{len(samples) + 1:05d}-{digest((metric, value, extra))[:12]}",
            "metric": metric,
            "value": value,
            "unit": unit,
            "dimensions": {**dimensions, **dict(extra or {})},
            "source_event_ids": list(source_event_ids),
            "observed_at": utc_now(),
        }
        sample["sample_digest"] = digest(sample)
        samples.append(sample)

    add(
        "effective_transitions",
        float(len(events)),
        "count",
        source_event_ids=(
            str(events[0].get("event_id") or ""),
            str(events[-1].get("event_id") or ""),
        )
        if events
        else (),
    )
    for effect, count in sorted(effect_counts.items()):
        if effect:
            add(
                "effect_count",
                float(count),
                "count",
                extra={"effect": effect},
            )
    add("action_count", float(len(result.action_results)), "count")
    add("fault_count", float(len(result.faults)), "count")
    add(
        "fault_recovered_count",
        float(sum(item.resolved for item in result.faults)),
        "count",
    )
    add("artifact_count", float(len(result.artifacts)), "count")
    add("tier_observation_count", float(len(result.tier_observations)), "count")
    add(
        "provider_observation_count",
        float(len(result.provider_observations)),
        "count",
    )
    add(
        "provider_cost_usd",
        float(sum(item.cost_usd for item in result.provider_observations)),
        "usd",
    )
    for item in result.provider_observations:
        add(
            "provider_latency_ms",
            float(item.latency_ms),
            "milliseconds",
            extra={
                "provider_id": item.provider_id,
                "model_id": item.model_id,
            },
        )
    for item in result.tier_observations:
        add(
            "tier_task_success",
            1.0 if item.task_success else 0.0,
            "ratio",
            extra={
                "tier": item.tier.value,
                "runtime_id": item.runtime_id,
            },
        )
    add("human_intervention_count", 0.0, "count")
    return tuple(samples)


def runtime_environment() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_executable": str(Path(sys.executable).resolve(strict=False)),
        "process_id": os.getpid(),
        "cpu_count": os.cpu_count(),
        "timezone": str(
            getattr(
                __import__("datetime").datetime.now().astimezone().tzinfo,
                "key",
                __import__("datetime").datetime.now().astimezone().tzinfo,
            )
        ),
        "environment_flags": {
            name: bool(os.environ.get(name))
            for name in (
                "CI",
                "ZYRA_WORKER_POOL_INTEGRATION_DISABLED",
                "ZYRA_DISABLE_RECOVERY_RUNTIME",
                "ZYRA_MEMORY_RETRIEVAL_DISABLED",
                "ZYRA_TARGETED_COMMUNICATION_DISABLED",
                "ZYRA_SCENARIO_DOMAIN_VERIFIER_DISABLED",
                "ZYRA_SCENARIO_LIVE_SOURCE_DISABLED",
            )
        },
    }


def _commit(value: str) -> bool:
    return bool(
        value
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


__all__ = [
    "ArchiveEntry",
    "AtomicArchiveWriter",
    "CausalArchive",
    "CausalArchiveBuilder",
    "StabilityAnalyzer",
    "raw_metric_samples",
    "runtime_environment",
]
