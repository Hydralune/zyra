from __future__ import annotations

import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import (
    canonicalize,
    content_digest,
    digest,
    identity,
    parse_utc,
    require_digest,
    stable_unique,
    utc_now,
)
from .errors import invalid


REQUIRED_ARCHIVE_MEMBERS = {
    "manifest.json",
    "canonical-events.jsonl",
    "owner-receipts.json",
    "raw-samples.jsonl",
    "domain-verification.json",
    "artifact-manifest.json",
    "environment.json",
}


@dataclass(frozen=True, slots=True)
class SourceEvent:
    event_id: str
    sequence: int
    event_type: str
    semantic_effect: str
    stage: str
    worker_id: str
    provider_id: str
    node_id: str
    causation_id: str
    parent_event_id: str
    run_id: str
    task_id: str
    created_at: str
    payload_digest: str
    event_digest: str
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "semantic_effect": self.semantic_effect,
            "stage": self.stage,
            "worker_id": self.worker_id,
            "provider_id": self.provider_id,
            "node_id": self.node_id,
            "causation_id": self.causation_id,
            "parent_event_id": self.parent_event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "payload_digest": self.payload_digest,
            "event_digest": self.event_digest,
            "payload": canonicalize(self.payload),
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SourceMetric:
    sample_id: str
    metric: str
    value: float
    unit: str
    observed_at: str
    source_event_ids: tuple[str, ...]
    dimensions: dict[str, str]
    sample_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "observed_at": self.observed_at,
            "source_event_ids": list(self.source_event_ids),
            "dimensions": dict(self.dimensions),
            "sample_digest": self.sample_digest,
        }


@dataclass(frozen=True, slots=True)
class SourceArchive:
    archive_id: str
    domain: str
    scenario_run_id: str
    owner_run_id: str
    task_id: str
    configuration_digest: str
    input_digest: str
    archive_digest: str
    event_stream_digest: str
    raw_sample_stream_digest: str
    member_digests: dict[str, str]
    events: tuple[SourceEvent, ...]
    raw_metrics: tuple[SourceMetric, ...]
    owner_receipts: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    domain_verification: dict[str, Any]
    environment: dict[str, Any]
    manifest: dict[str, Any]
    admitted_at: str
    source_path: str = ""

    @property
    def effective_transition_count(self) -> int:
        return len(
            tuple(
                event
                for event in self.events
                if event.semantic_effect
                not in {"", "none", "heartbeat", "ui_repaint", "log"}
            )
        )

    @property
    def fault_events(self) -> tuple[SourceEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.semantic_effect == "fault"
            or "fault" in event.event_type
            or event.event_type
            in {"tool_timeout", "worker_lost", "network_lost", "provider_failed"}
        )

    @property
    def recovery_events(self) -> tuple[SourceEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.semantic_effect == "recovery"
            or "recovery" in event.event_type
            or "resume" in event.event_type
        )

    @property
    def workers(self) -> tuple[str, ...]:
        return stable_unique(
            event.worker_id or event.stage
            for event in self.events
            if event.worker_id or event.stage
        )

    @property
    def providers(self) -> tuple[str, ...]:
        return stable_unique(
            event.provider_id
            for event in self.events
            if event.provider_id
        )

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-source-archive/v1",
            "archive_id": self.archive_id,
            "domain": self.domain,
            "scenario_run_id": self.scenario_run_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "configuration_digest": self.configuration_digest,
            "input_digest": self.input_digest,
            "archive_digest": self.archive_digest,
            "event_stream_digest": self.event_stream_digest,
            "raw_sample_stream_digest": self.raw_sample_stream_digest,
            "member_digests": dict(self.member_digests),
            "event_count": len(self.events),
            "effective_transition_count": self.effective_transition_count,
            "raw_metric_count": len(self.raw_metrics),
            "owner_receipt_count": len(self.owner_receipts),
            "artifact_count": len(self.artifacts),
            "fault_count": len(self.fault_events),
            "recovery_event_count": len(self.recovery_events),
            "workers": list(self.workers),
            "providers": list(self.providers),
            "domain_verification": canonicalize(self.domain_verification),
            "environment": canonicalize(self.environment),
            "manifest": canonicalize(self.manifest),
            "admitted_at": self.admitted_at,
            "source_path": self.source_path,
        }
        if include_events:
            value["events"] = [item.to_dict() for item in self.events]
            value["raw_metrics"] = [item.to_dict() for item in self.raw_metrics]
            value["owner_receipts"] = canonicalize(self.owner_receipts)
            value["artifacts"] = canonicalize(self.artifacts)
        return value


class EvidenceArchiveLoader:
    def __init__(
        self,
        *,
        allowed_roots: Iterable[str | Path] = (),
        maximum_archive_bytes: int = 2 * 1024 * 1024 * 1024,
        maximum_member_bytes: int = 1024 * 1024 * 1024,
        maximum_events: int = 10_000_000,
    ) -> None:
        self.allowed_roots = tuple(
            Path(item).resolve(strict=False) for item in allowed_roots
        )
        self.maximum_archive_bytes = maximum_archive_bytes
        self.maximum_member_bytes = maximum_member_bytes
        self.maximum_events = maximum_events

    def load(self, path: str | Path) -> SourceArchive:
        selected = Path(path).resolve(strict=False)
        self._require_allowed_path(selected)
        if not selected.is_file():
            raise invalid(
                "experiment_source_archive_missing",
                "Source evidence archive does not exist.",
                detail={"path": str(selected)},
            )
        size = selected.stat().st_size
        if size <= 0 or size > self.maximum_archive_bytes:
            raise invalid(
                "experiment_source_archive_size_invalid",
                "Source evidence archive has an invalid size.",
                detail={
                    "path": str(selected),
                    "size": size,
                    "maximum": self.maximum_archive_bytes,
                },
            )
        archive_bytes = selected.read_bytes()
        archive_digest = content_digest(archive_bytes)
        try:
            with zipfile.ZipFile(selected, "r") as archive:
                payloads = self._read_members(archive)
        except (OSError, zipfile.BadZipFile) as error:
            raise invalid(
                "experiment_source_archive_invalid",
                "Source evidence archive is not a valid ZIP.",
                detail={"path": str(selected), "reason": str(error)},
            ) from error
        return self.from_members(
            payloads,
            archive_digest=archive_digest,
            source_path=str(selected),
        )

    def from_members(
        self,
        members: Mapping[str, bytes],
        *,
        archive_digest: str | None = None,
        source_path: str = "",
    ) -> SourceArchive:
        normalized = {
            self._safe_member_name(name): bytes(payload)
            for name, payload in members.items()
        }
        missing = sorted(REQUIRED_ARCHIVE_MEMBERS - set(normalized))
        if missing:
            raise invalid(
                "experiment_source_members_missing",
                "Source evidence archive is incomplete.",
                detail={"missing": missing},
            )
        manifest = self._json(normalized["manifest.json"], "manifest.json")
        owner_receipts_value = self._json(
            normalized["owner-receipts.json"],
            "owner-receipts.json",
        )
        domain_verification = self._json(
            normalized["domain-verification.json"],
            "domain-verification.json",
        )
        artifacts_value = self._json(
            normalized["artifact-manifest.json"],
            "artifact-manifest.json",
        )
        environment = self._json(
            normalized["environment.json"],
            "environment.json",
        )
        events = self._events(normalized["canonical-events.jsonl"])
        raw_metrics = self._metrics(normalized["raw-samples.jsonl"])
        owner_receipts = self._objects(
            owner_receipts_value,
            "owner receipts",
            preferred_keys=("receipts", "owner_receipts", "items"),
        )
        artifacts = self._objects(
            artifacts_value,
            "artifacts",
            preferred_keys=("artifacts", "entries", "items"),
        )
        member_digests = {
            name: content_digest(payload)
            for name, payload in sorted(normalized.items())
        }
        source = SourceArchive(
            archive_id=identity(
                manifest.get("archive_id"),
                "source archive id",
            ),
            domain=str(manifest.get("domain") or "").strip(),
            scenario_run_id=self._bound_identity(
                manifest,
                owner_receipts,
                events,
                "scenario_run_id",
            ),
            owner_run_id=self._bound_identity(
                manifest,
                owner_receipts,
                events,
                "owner_run_id",
                event_field="run_id",
            ),
            task_id=self._bound_identity(
                manifest,
                owner_receipts,
                events,
                "task_id",
            ),
            configuration_digest=require_digest(
                manifest.get("configuration_digest"),
                "source configuration digest",
            ),
            input_digest=require_digest(
                self._find_input_digest(manifest, owner_receipts, events),
                "source input digest",
            ),
            archive_digest=require_digest(
                archive_digest
                or digest({name: value for name, value in member_digests.items()}),
                "source archive digest",
            ),
            event_stream_digest=content_digest(
                normalized["canonical-events.jsonl"]
            ),
            raw_sample_stream_digest=content_digest(
                normalized["raw-samples.jsonl"]
            ),
            member_digests=member_digests,
            events=events,
            raw_metrics=raw_metrics,
            owner_receipts=owner_receipts,
            artifacts=artifacts,
            domain_verification=domain_verification,
            environment=environment,
            manifest=manifest,
            admitted_at=utc_now(),
            source_path=source_path,
        )
        SourceEvidenceVerifier().require_valid(source)
        return source

    def _read_members(self, archive: zipfile.ZipFile) -> dict[str, bytes]:
        names: set[str] = set()
        output: dict[str, bytes] = {}
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = self._safe_member_name(info.filename)
            if name in names:
                raise invalid(
                    "experiment_source_member_duplicate",
                    "Source evidence archive contains duplicate member names.",
                    detail={"path": name},
                )
            names.add(name)
            if info.file_size < 0 or info.file_size > self.maximum_member_bytes:
                raise invalid(
                    "experiment_source_member_size_invalid",
                    "Source evidence archive member is too large.",
                    detail={
                        "path": name,
                        "size": info.file_size,
                        "maximum": self.maximum_member_bytes,
                    },
                )
            total += info.file_size
            if total > self.maximum_archive_bytes * 4:
                raise invalid(
                    "experiment_source_archive_expansion_invalid",
                    "Source evidence archive expansion exceeds its safety bound.",
                    detail={"expanded_bytes": total},
                )
            output[name] = archive.read(info)
        return output

    def _safe_member_name(self, value: str) -> str:
        selected = str(value or "").replace("\\", "/").strip("/")
        path = PurePosixPath(selected)
        if (
            not selected
            or path.is_absolute()
            or ".." in path.parts
            or ":" in path.parts[0]
        ):
            raise invalid(
                "experiment_source_member_path_invalid",
                "Source evidence archive member path is unsafe.",
                detail={"path": selected},
            )
        if len(path.parts) != 1:
            raise invalid(
                "experiment_source_member_nested",
                "Source evidence archive members must use the canonical flat layout.",
                detail={"path": selected},
            )
        return selected

    def _require_allowed_path(self, selected: Path) -> None:
        if not self.allowed_roots:
            return
        for root in self.allowed_roots:
            try:
                selected.relative_to(root)
                return
            except ValueError:
                continue
        raise invalid(
            "experiment_source_path_outside_boundary",
            "Source evidence archive is outside configured roots.",
            phase="policy",
            detail={
                "path": str(selected),
                "allowed_roots": [str(item) for item in self.allowed_roots],
            },
        )

    def _json(self, value: bytes, label: str) -> dict[str, Any] | list[Any]:
        try:
            parsed = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid(
                "experiment_source_json_invalid",
                f"{label} is not valid UTF-8 JSON.",
                detail={"path": label, "reason": str(error)},
            ) from error
        if not isinstance(parsed, (dict, list)):
            raise invalid(
                "experiment_source_json_shape_invalid",
                f"{label} must contain an object or array.",
            )
        return parsed

    def _events(self, value: bytes) -> tuple[SourceEvent, ...]:
        output: list[SourceEvent] = []
        for line_number, line in enumerate(value.splitlines(), start=1):
            if not line.strip():
                continue
            if len(output) >= self.maximum_events:
                raise invalid(
                    "experiment_source_event_limit",
                    "Source evidence archive exceeds the event limit.",
                    detail={"maximum": self.maximum_events},
                )
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise invalid(
                    "experiment_source_event_invalid",
                    "Canonical event stream contains invalid JSON.",
                    detail={"line": line_number, "reason": str(error)},
                ) from error
            if not isinstance(item, Mapping):
                raise invalid(
                    "experiment_source_event_shape_invalid",
                    "Canonical event stream item must be an object.",
                    detail={"line": line_number},
                )
            metadata = dict(item.get("metadata") or {})
            payload = dict(item.get("payload") or {})
            semantic_effect = str(
                metadata.get("semantic_effect")
                or payload.get("semantic_effect")
                or ""
            ).strip().casefold()
            event = SourceEvent(
                event_id=identity(item.get("event_id"), "source event id"),
                sequence=int(item.get("sequence") or line_number),
                event_type=str(item.get("event_type") or "").strip().casefold(),
                semantic_effect=semantic_effect,
                stage=str(metadata.get("stage") or "").strip().casefold(),
                worker_id=str(metadata.get("worker_id") or "").strip(),
                provider_id=str(metadata.get("provider_id") or "").strip(),
                node_id=str(item.get("node_id") or "").strip(),
                causation_id=str(item.get("causation_id") or "").strip(),
                parent_event_id=str(item.get("parent_event_id") or "").strip(),
                run_id=identity(item.get("run_id"), "source owner run id"),
                task_id=identity(item.get("task_id"), "source task id"),
                created_at=str(item.get("created_at") or "").strip(),
                payload_digest=digest(payload),
                event_digest=digest(item),
                payload=payload,
                metadata=metadata,
            )
            output.append(event)
        return tuple(output)

    def _metrics(self, value: bytes) -> tuple[SourceMetric, ...]:
        output: list[SourceMetric] = []
        for line_number, line in enumerate(value.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise invalid(
                    "experiment_source_metric_invalid",
                    "Raw source metric stream contains invalid JSON.",
                    detail={"line": line_number, "reason": str(error)},
                ) from error
            if not isinstance(item, Mapping):
                raise invalid(
                    "experiment_source_metric_shape_invalid",
                    "Raw source metric item must be an object.",
                    detail={"line": line_number},
                )
            body = dict(item)
            declared = str(body.pop("sample_digest", "") or "").strip()
            calculated = digest(body)
            if declared and declared != calculated:
                raise invalid(
                    "experiment_source_metric_digest_mismatch",
                    "Raw source metric digest does not match its content.",
                    detail={
                        "line": line_number,
                        "declared": declared,
                        "calculated": calculated,
                    },
                )
            output.append(
                SourceMetric(
                    sample_id=identity(
                        item.get("sample_id"),
                        "source metric sample id",
                    ),
                    metric=str(item.get("metric") or "").strip(),
                    value=float(item.get("value")),
                    unit=str(item.get("unit") or "").strip(),
                    observed_at=str(item.get("observed_at") or "").strip(),
                    source_event_ids=tuple(
                        str(value)
                        for value in item.get("source_event_ids") or ()
                        if str(value)
                    ),
                    dimensions={
                        str(key): str(selected)
                        for key, selected in dict(
                            item.get("dimensions") or {}
                        ).items()
                    },
                    sample_digest=declared or calculated,
                )
            )
        return tuple(output)

    def _objects(
        self,
        value: dict[str, Any] | list[Any],
        label: str,
        *,
        preferred_keys: tuple[str, ...],
    ) -> tuple[dict[str, Any], ...]:
        selected: Any = value
        if isinstance(value, Mapping):
            for key in preferred_keys:
                candidate = value.get(key)
                if isinstance(candidate, list):
                    selected = candidate
                    break
            else:
                selected = [value]
        if not isinstance(selected, list) or not all(
            isinstance(item, Mapping) for item in selected
        ):
            raise invalid(
                "experiment_source_collection_invalid",
                f"{label} must contain objects.",
            )
        return tuple(dict(item) for item in selected)

    def _bound_identity(
        self,
        manifest: Mapping[str, Any],
        receipts: tuple[dict[str, Any], ...],
        events: tuple[SourceEvent, ...],
        key: str,
        *,
        event_field: str | None = None,
    ) -> str:
        values: list[str] = []
        direct = str(manifest.get(key) or "").strip()
        if direct:
            values.append(direct)
        for receipt in receipts:
            selected = str(receipt.get(key) or "").strip()
            if selected:
                values.append(selected)
        field = event_field or key
        for event in events[:10]:
            selected = str(getattr(event, field, "") or "").strip()
            if selected:
                values.append(selected)
        unique = stable_unique(values)
        if len(unique) != 1:
            raise invalid(
                "experiment_source_identity_binding_invalid",
                "Source evidence identities are missing or inconsistent.",
                detail={"field": key, "values": list(unique)},
            )
        return identity(unique[0], f"source {key}")

    def _find_input_digest(
        self,
        manifest: Mapping[str, Any],
        receipts: tuple[dict[str, Any], ...],
        events: tuple[SourceEvent, ...],
    ) -> str:
        values: list[str] = []
        direct = str(manifest.get("input_digest") or "").strip()
        if direct:
            values.append(direct)
        for receipt in receipts:
            selected = str(receipt.get("input_digest") or "").strip()
            if selected:
                values.append(selected)
            configuration = receipt.get("configuration")
            if isinstance(configuration, Mapping):
                selected = str(configuration.get("input_digest") or "").strip()
                if selected:
                    values.append(selected)
        for event in events:
            mutation = event.payload.get("mutation")
            if isinstance(mutation, Mapping):
                selected = str(mutation.get("input_digest") or "").strip()
                if selected:
                    values.append(selected)
            if values:
                break
        unique = stable_unique(values)
        if len(unique) != 1:
            raise invalid(
                "experiment_source_input_binding_invalid",
                "Source input digest is missing or inconsistent.",
                detail={"values": list(unique)},
            )
        return unique[0]


class SourceEvidenceVerifier:
    def verify(self, source: SourceArchive) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        if not source.domain:
            findings.append({"code": "domain_missing"})
        if len(source.events) < 1:
            findings.append({"code": "events_missing"})
        if len(source.raw_metrics) < 1:
            findings.append({"code": "raw_metrics_missing"})
        if len(source.artifacts) < 1:
            findings.append({"code": "artifacts_missing"})
        if len(source.owner_receipts) < 1:
            findings.append({"code": "owner_receipts_missing"})
        verification_valid = self._verification_valid(source.domain_verification)
        if not verification_valid:
            findings.append({"code": "domain_verification_invalid"})
        if self._is_replay(source):
            findings.append({"code": "replay_or_fixture_source"})
        if self._human_interventions(source) != 0:
            findings.append(
                {
                    "code": "human_intervention_nonzero",
                    "count": self._human_interventions(source),
                }
            )
        sequences = [item.sequence for item in source.events]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            findings.append({"code": "event_sequence_invalid"})
        event_ids = [item.event_id for item in source.events]
        if len(set(event_ids)) != len(event_ids):
            findings.append({"code": "event_id_duplicate"})
        run_ids = set(item.run_id for item in source.events)
        task_ids = set(item.task_id for item in source.events)
        if run_ids != {source.owner_run_id}:
            findings.append(
                {
                    "code": "owner_run_binding_invalid",
                    "observed": sorted(run_ids),
                }
            )
        if task_ids != {source.task_id}:
            findings.append(
                {
                    "code": "task_binding_invalid",
                    "observed": sorted(task_ids),
                }
            )
        event_id_set = set(event_ids)
        broken_causes = [
            item.event_id
            for item in source.events
            if item.causation_id
            and item.causation_id not in event_id_set
            and item.sequence > 1
        ]
        if broken_causes:
            findings.append(
                {
                    "code": "event_causation_missing",
                    "event_ids": broken_causes[:100],
                    "count": len(broken_causes),
                }
            )
        metric_ids = [item.sample_id for item in source.raw_metrics]
        if len(set(metric_ids)) != len(metric_ids):
            findings.append({"code": "raw_metric_id_duplicate"})
        unknown_metric_events = sorted(
            {
                event_id
                for sample in source.raw_metrics
                for event_id in sample.source_event_ids
                if event_id not in event_id_set
            }
        )
        if unknown_metric_events:
            findings.append(
                {
                    "code": "raw_metric_event_binding_invalid",
                    "event_ids": unknown_metric_events[:100],
                    "count": len(unknown_metric_events),
                }
            )
        effect_counts = Counter(item.semantic_effect for item in source.events)
        required_effects = {
            "state_mutation",
            "topology",
            "memory",
            "fault",
            "recovery",
            "artifact",
            "verification",
        }
        missing_effects = sorted(required_effects - set(effect_counts))
        if missing_effects:
            findings.append(
                {
                    "code": "semantic_effect_coverage_missing",
                    "effects": missing_effects,
                }
            )
        if len(source.fault_events) < 1:
            findings.append({"code": "fault_evidence_missing"})
        if len(source.recovery_events) < 1:
            findings.append({"code": "recovery_evidence_missing"})
        receipt = {
            "schema": "zyra.experiment-source-verification/v1",
            "valid": not findings,
            "archive_id": source.archive_id,
            "archive_digest": source.archive_digest,
            "scenario_run_id": source.scenario_run_id,
            "owner_run_id": source.owner_run_id,
            "task_id": source.task_id,
            "event_count": len(source.events),
            "raw_metric_count": len(source.raw_metrics),
            "artifact_count": len(source.artifacts),
            "owner_receipt_count": len(source.owner_receipts),
            "effective_transition_count": source.effective_transition_count,
            "effect_counts": dict(sorted(effect_counts.items())),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def require_valid(self, source: SourceArchive) -> dict[str, Any]:
        receipt = self.verify(source)
        if not receipt["valid"]:
            raise invalid(
                "experiment_source_evidence_invalid",
                "Source live evidence failed admission.",
                phase="source_admission",
                detail=receipt,
            )
        return receipt

    def _verification_valid(self, value: Mapping[str, Any]) -> bool:
        if value.get("valid") is True:
            return True
        receipt = value.get("verification_receipt")
        if isinstance(receipt, Mapping) and receipt.get("valid") is True:
            return True
        checks = value.get("checks")
        if isinstance(checks, Mapping) and checks:
            return all(bool(item) for item in checks.values())
        return False

    def _is_replay(self, source: SourceArchive) -> bool:
        values: list[Any] = [
            source.manifest.get("replay"),
            source.manifest.get("fixture"),
            source.manifest.get("live"),
            source.environment.get("replay"),
            source.environment.get("fixture"),
            source.environment.get("live"),
        ]
        for receipt in source.owner_receipts:
            values.extend(
                [
                    receipt.get("replay"),
                    receipt.get("fixture"),
                    receipt.get("live"),
                ]
            )
        if any(value is True for value in values[0:2]):
            return True
        explicit_live = [
            value
            for index, value in enumerate(values)
            if index % 3 == 2 and value is not None
        ]
        return bool(explicit_live and not all(value is True for value in explicit_live))

    def _human_interventions(self, source: SourceArchive) -> int:
        values: list[int] = []
        for item in (
            source.manifest,
            source.domain_verification,
            source.environment,
            *source.owner_receipts,
        ):
            if "human_intervention_count" in item:
                try:
                    values.append(int(item["human_intervention_count"]))
                except (TypeError, ValueError):
                    values.append(1)
        return max(values, default=0)
