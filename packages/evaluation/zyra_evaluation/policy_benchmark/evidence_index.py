from __future__ import annotations

import hashlib
import json
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .contracts import P2_BASE_COMMIT, canonical_digest


BASELINE_MANIFEST_DIGEST = (
    "85fa3a4a05d3ce7906e7af8507b3e413b26a5c53042bf0073d8f3039b9e5db37"
)
REQUIRED_REFERENCE_IDS = {
    "benchmark-campaign",
    "benchmark-index",
    "benchmark-source-runs",
    "benchmark-run-receipts",
    "benchmark-raw-samples",
    "benchmark-current-campaign",
    "benchmark-verification",
}
REQUIRED_ARCHIVE_MEMBERS = {
    "artifact-manifest.json",
    "canonical-events.jsonl",
    "domain-verification.json",
    "environment.json",
    "manifest.json",
    "owner-receipts.json",
    "raw-samples.jsonl",
}


class EvidenceIndexError(ValueError):
    """Frozen evidence is missing, mutated, ambiguous, or structurally invalid."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "path": self.path}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceIndexError(
            "evidence-json-invalid",
            f"{label} is not readable JSON.",
            path=str(path),
        ) from exc
    if not isinstance(value, Mapping):
        raise EvidenceIndexError(
            "evidence-object-required",
            f"{label} must be a JSON object.",
            path=str(path),
        )
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceIndexError(
            "evidence-mapping-required",
            f"{label} must be a mapping.",
            path=label,
        )
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise EvidenceIndexError(
            "evidence-sequence-required",
            f"{label} must be a sequence.",
            path=label,
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceIndexError(
            "evidence-text-required",
            f"{label} must be non-empty text.",
            path=label,
        )
    return value.strip()


def _safe_repository_path(repository_root: Path, value: object, label: str) -> Path:
    relative = Path(_text(value, label).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceIndexError(
            "evidence-path-outside-repository",
            f"{label} must remain repository-relative.",
            path=str(relative),
        )
    root = repository_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise EvidenceIndexError(
            "evidence-path-outside-repository",
            f"{label} resolves outside the Zyra repository.",
            path=str(relative),
        ) from exc
    return target


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    selected = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(selected)
    except ValueError:
        return None


@dataclass(frozen=True)
class IndexedEvidenceReference:
    reference_id: str
    kind: str
    path: str
    sha256: str
    size_bytes: int
    source_commit: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "reference_id": self.reference_id,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "read_only": True,
        }
        if self.source_commit:
            result["source_commit"] = self.source_commit
        return result


@dataclass(frozen=True)
class EvidenceEvent:
    event_id: str
    event_type: str
    sequence: int
    created_at: str
    causation_id: str
    parent_event_id: str
    run_id: str
    task_id: str
    stage: str
    semantic_effect: str
    mutation: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceEvent":
        metadata = value.get("metadata")
        selected_metadata = metadata if isinstance(metadata, Mapping) else {}
        payload = value.get("payload")
        selected_payload = payload if isinstance(payload, Mapping) else {}
        mutation = selected_payload.get("mutation")
        selected_mutation = mutation if isinstance(mutation, Mapping) else {}
        return cls(
            event_id=str(value.get("event_id", "")),
            event_type=str(value.get("event_type", "")),
            sequence=int(value.get("sequence", 0) or 0),
            created_at=str(value.get("created_at", "")),
            causation_id=str(
                value.get("causation_id")
                or selected_payload.get("causation_id")
                or ""
            ),
            parent_event_id=str(value.get("parent_event_id", "")),
            run_id=str(value.get("run_id", "")),
            task_id=str(value.get("task_id", "")),
            stage=str(selected_metadata.get("stage", "")),
            semantic_effect=str(
                selected_metadata.get("semantic_effect")
                or selected_payload.get("semantic_effect")
                or ""
            ),
            mutation=MappingProxyType(dict(selected_mutation)),
        )

    def to_index_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "causation_id": self.causation_id,
            "parent_event_id": self.parent_event_id,
            "stage": self.stage,
            "semantic_effect": self.semantic_effect,
            "mutation_keys": sorted(self.mutation),
        }


@dataclass(frozen=True)
class SourceRunEvidence:
    source_run_id: str
    owner_run_id: str
    task_id: str
    domain: str
    repetition: int
    archive_path: str
    archive_digest: str
    archive_container_sha256: str
    archive_member_digests: Mapping[str, str]
    events: tuple[EvidenceEvent, ...]
    environment: Mapping[str, Any]
    owner_receipts: Mapping[str, Any]
    artifact_manifest: Mapping[str, Any]
    domain_verification: Mapping[str, Any]
    source_summary: Mapping[str, Any]
    run_receipts: tuple[Mapping[str, Any], ...] = ()
    raw_samples: tuple[Mapping[str, Any], ...] = ()
    provider_observations: tuple[Mapping[str, Any], ...] = ()
    tier_observations: tuple[Mapping[str, Any], ...] = ()

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(event.event_type for event in self.events)

    @property
    def maximum_sequence(self) -> int:
        return max((event.sequence for event in self.events), default=0)

    def first_event(self, event_type: str) -> EvidenceEvent | None:
        return next(
            (event for event in self.events if event.event_type == event_type),
            None,
        )

    def events_of_type(self, event_type: str) -> tuple[EvidenceEvent, ...]:
        return tuple(
            event for event in self.events if event.event_type == event_type
        )

    def raw_samples_for(self, metric_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            sample
            for sample in self.raw_samples
            if sample.get("metric_id") == metric_id
        )

    def to_index_dict(self) -> dict[str, Any]:
        return {
            "source_run_id": self.source_run_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "domain": self.domain,
            "repetition": self.repetition,
            "archive_path": self.archive_path,
            "archive_digest": self.archive_digest,
            "archive_container_sha256": self.archive_container_sha256,
            "archive_member_digests": dict(
                sorted(self.archive_member_digests.items())
            ),
            "event_count": self.event_count,
            "event_stream_digest": self.source_summary.get(
                "event_stream_digest", ""
            ),
            "run_receipt_count": len(self.run_receipts),
            "raw_sample_count": len(self.raw_samples),
            "provider_observation_count": len(self.provider_observations),
            "tier_observation_count": len(self.tier_observations),
            "artifact_count": len(
                _sequence(
                    self.artifact_manifest.get("artifacts", ()),
                    f"{self.source_run_id}.artifacts",
                )
            ),
            "event_type_counts": dict(
                sorted(Counter(event.event_type for event in self.events).items())
            ),
            "read_only": True,
        }


@dataclass(frozen=True)
class ReadOnlyEvidenceIndex:
    repository_root: Path = field(repr=False)
    baseline_manifest_path: str
    baseline_manifest_digest: str
    references: tuple[IndexedEvidenceReference, ...]
    source_runs: tuple[SourceRunEvidence, ...]
    campaign: Mapping[str, Any] = field(repr=False)
    current_campaign: Mapping[str, Any] = field(repr=False)
    run_receipt_count: int
    raw_sample_count: int
    formal_cell_count: int
    event_count: int
    event_type_counts: Mapping[str, int]
    index_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-read-only-evidence-index/v1",
            "classification": "read_only_evidence_index",
            "baseline_manifest": {
                "path": self.baseline_manifest_path,
                "manifest_digest": self.baseline_manifest_digest,
                "p2_base_commit": P2_BASE_COMMIT,
            },
            "references": [item.to_dict() for item in self.references],
            "source_runs": [item.to_index_dict() for item in self.source_runs],
            "volume": {
                "independent_source_run_count": len(self.source_runs),
                "derived_formal_cell_count": self.formal_cell_count,
                "run_receipt_count": self.run_receipt_count,
                "raw_event_count": self.event_count,
                "raw_sample_count": self.raw_sample_count,
                "training_sample_count": 0,
                "semantic_label": "evidence_volume_only",
            },
            "event_type_counts": dict(sorted(self.event_type_counts.items())),
            "prohibited_uses": [
                "training_dataset",
                "fine_tuning",
                "online_learning",
                "policy_gradient",
                "textual_gradient",
                "success_label_synthesis",
            ],
            "index_digest": self.index_digest,
        }


class FrozenEvidenceIndexer:
    """Build a digest-checked index without copying frozen evidence payloads."""

    def __init__(
        self,
        repository_root: Path,
        *,
        baseline_manifest: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        selected_manifest = baseline_manifest or (
            self.repository_root / "docs" / "release" / "phase2-baseline-manifest.json"
        )
        self.baseline_manifest = selected_manifest.resolve()

    def build(self) -> ReadOnlyEvidenceIndex:
        manifest = self._validate_manifest()
        references, values = self._index_references(manifest)
        source_runs_value = values["benchmark-source-runs"]
        run_receipts_value = values["benchmark-run-receipts"]
        raw_samples_value = values["benchmark-raw-samples"]
        current_campaign_value = values["benchmark-current-campaign"]
        campaign_value = values["benchmark-campaign"]

        run_receipts = tuple(
            _mapping(item, "run receipt")
            for item in _sequence(
                run_receipts_value.get("runs"),
                "benchmark-run-receipts.runs",
            )
        )
        raw_samples = tuple(
            _mapping(item, "raw sample")
            for item in _sequence(
                raw_samples_value.get("samples"),
                "benchmark-raw-samples.samples",
            )
        )
        cases = tuple(
            _mapping(item, "current campaign case")
            for item in _sequence(
                current_campaign_value.get("cases"),
                "benchmark-current-campaign.cases",
            )
        )

        run_receipts_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        benchmark_run_to_source: dict[str, str] = {}
        for receipt in run_receipts:
            source_id = _text(
                receipt.get("source_live_run_id"),
                "run receipt source_live_run_id",
            )
            benchmark_run_id = _text(receipt.get("run_id"), "run receipt run_id")
            run_receipts_by_source[source_id].append(receipt)
            benchmark_run_to_source[benchmark_run_id] = source_id

        raw_samples_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for sample in raw_samples:
            benchmark_run_id = _text(sample.get("run_id"), "raw sample run_id")
            source_id = benchmark_run_to_source.get(benchmark_run_id)
            if source_id is None:
                raise EvidenceIndexError(
                    "raw-sample-run-unbound",
                    f"Raw sample run is not present in the run receipt index: "
                    f"{benchmark_run_id}.",
                    path="benchmark-raw-samples",
                )
            raw_samples_by_source[source_id].append(sample)

        current_cases_by_source: dict[str, Mapping[str, Any]] = {}
        for case in cases:
            source_id = _text(case.get("source_run_id"), "case source_run_id")
            if source_id in current_cases_by_source:
                raise EvidenceIndexError(
                    "current-case-source-duplicate",
                    f"Current provider evidence duplicates source run {source_id}.",
                    path="benchmark-current-campaign.cases",
                )
            current_cases_by_source[source_id] = case

        archive_by_digest = self._discover_archives(source_runs_value)
        source_records: list[SourceRunEvidence] = []
        seen_source_ids: set[str] = set()
        for item in _sequence(
            source_runs_value.get("sources"),
            "benchmark-source-runs.sources",
        ):
            source_item = _mapping(item, "source run entry")
            source = _mapping(source_item.get("source"), "source run summary")
            source_id = _text(source.get("scenario_run_id"), "source scenario_run_id")
            if source_id in seen_source_ids:
                raise EvidenceIndexError(
                    "independent-source-run-duplicate",
                    f"Source run {source_id} appears more than once.",
                    path="benchmark-source-runs.sources",
                )
            seen_source_ids.add(source_id)
            archive_digest = _text(source.get("archive_digest"), "source archive_digest")
            archive = archive_by_digest.get(archive_digest)
            if archive is None:
                raise EvidenceIndexError(
                    "source-archive-missing",
                    f"No frozen source archive matches {archive_digest}.",
                    path="source-archives",
                )
            case = current_cases_by_source.get(source_id, {})
            source_records.append(
                self._read_source_archive(
                    archive,
                    source_item=source_item,
                    source=source,
                    run_receipts=run_receipts_by_source.get(source_id, ()),
                    raw_samples=raw_samples_by_source.get(source_id, ()),
                    current_case=case,
                )
            )

        declared_source_count = int(source_runs_value.get("source_run_count", -1))
        if declared_source_count != len(source_records):
            raise EvidenceIndexError(
                "source-run-count-mismatch",
                "The source-run count does not match the frozen source list.",
                path="benchmark-source-runs.source_run_count",
            )
        if int(run_receipts_value.get("run_count", -1)) != len(run_receipts):
            raise EvidenceIndexError(
                "run-receipt-count-mismatch",
                "The run-receipt count does not match the frozen receipt list.",
                path="benchmark-run-receipts.run_count",
            )
        if int(raw_samples_value.get("sample_count", -1)) != len(raw_samples):
            raise EvidenceIndexError(
                "raw-sample-count-mismatch",
                "The raw-sample count does not match the frozen sample list.",
                path="benchmark-raw-samples.sample_count",
            )

        event_counts = Counter(
            event.event_type
            for source in source_records
            for event in source.events
        )
        event_count = sum(event_counts.values())
        index_payload = {
            "schema": "zyra.phase2-read-only-evidence-index/v1",
            "baseline_manifest_digest": manifest["manifest_digest"],
            "references": [item.to_dict() for item in references],
            "source_runs": [item.to_index_dict() for item in source_records],
            "volume": {
                "independent_source_run_count": len(source_records),
                "derived_formal_cell_count": len(run_receipts),
                "run_receipt_count": len(run_receipts),
                "raw_event_count": event_count,
                "raw_sample_count": len(raw_samples),
                "training_sample_count": 0,
            },
            "event_type_counts": dict(sorted(event_counts.items())),
        }
        manifest_relative = self.baseline_manifest.relative_to(
            self.repository_root
        ).as_posix()
        return ReadOnlyEvidenceIndex(
            repository_root=self.repository_root,
            baseline_manifest_path=manifest_relative,
            baseline_manifest_digest=str(manifest["manifest_digest"]),
            references=tuple(references),
            source_runs=tuple(
                sorted(
                    source_records,
                    key=lambda item: (item.domain, item.repetition, item.source_run_id),
                )
            ),
            campaign=MappingProxyType(dict(campaign_value)),
            current_campaign=MappingProxyType(dict(current_campaign_value)),
            run_receipt_count=len(run_receipts),
            raw_sample_count=len(raw_samples),
            formal_cell_count=len(run_receipts),
            event_count=event_count,
            event_type_counts=MappingProxyType(dict(event_counts)),
            index_digest=canonical_digest(index_payload),
        )

    def _validate_manifest(self) -> Mapping[str, Any]:
        manifest = _load_json(self.baseline_manifest, "Phase 2 baseline manifest")
        if manifest.get("schema") != "zyra.phase2-baseline-manifest/v1":
            raise EvidenceIndexError(
                "baseline-manifest-schema-invalid",
                "The Phase 2 baseline manifest schema is unsupported.",
                path=str(self.baseline_manifest),
            )
        identity = _mapping(manifest.get("identity"), "baseline identity")
        if identity.get("p2_base_commit") != P2_BASE_COMMIT:
            raise EvidenceIndexError(
                "baseline-commit-mismatch",
                "The readiness audit must use the frozen P2 base commit.",
                path="baseline.identity.p2_base_commit",
            )
        expected = _text(manifest.get("manifest_digest"), "manifest_digest")
        observed = canonical_digest(
            {key: value for key, value in manifest.items() if key != "manifest_digest"}
        )
        if expected != observed or expected != BASELINE_MANIFEST_DIGEST:
            raise EvidenceIndexError(
                "baseline-manifest-digest-mismatch",
                "The frozen Phase 2 baseline manifest changed.",
                path=str(self.baseline_manifest),
            )
        inventory = _mapping(
            manifest.get("evidence_inventory"),
            "baseline evidence_inventory",
        )
        volume = _mapping(inventory.get("evidence_volume"), "evidence volume")
        training = _mapping(manifest.get("training_policy"), "training policy")
        if (
            inventory.get("classification") != "read_only_evidence_inventory"
            or inventory.get("training_eligible") is not False
            or int(volume.get("training_sample_count", -1)) != 0
            or training.get("training_allowed") is not False
            or training.get("evidence_reuse") != "read_only"
        ):
            raise EvidenceIndexError(
                "baseline-training-boundary-invalid",
                "Frozen evidence is not eligible for training.",
                path="baseline.evidence_inventory",
            )
        return manifest

    def _index_references(
        self,
        manifest: Mapping[str, Any],
    ) -> tuple[list[IndexedEvidenceReference], dict[str, Mapping[str, Any]]]:
        references: list[IndexedEvidenceReference] = []
        values: dict[str, Mapping[str, Any]] = {}
        seen: set[str] = set()
        for item in _sequence(manifest.get("references"), "baseline references"):
            reference = _mapping(item, "baseline reference")
            reference_id = _text(reference.get("id"), "baseline reference id")
            if reference_id in seen:
                raise EvidenceIndexError(
                    "baseline-reference-duplicate",
                    f"Duplicate baseline reference: {reference_id}.",
                    path="baseline.references",
                )
            seen.add(reference_id)
            target = _safe_repository_path(
                self.repository_root,
                reference.get("path"),
                f"{reference_id}.path",
            )
            if not target.is_file():
                raise EvidenceIndexError(
                    "baseline-reference-missing",
                    f"Baseline reference is missing: {reference_id}.",
                    path=str(target),
                )
            expected_digest = _text(reference.get("sha256"), f"{reference_id}.sha256")
            actual_digest = _sha256_file(target)
            if actual_digest != expected_digest:
                raise EvidenceIndexError(
                    "baseline-reference-digest-mismatch",
                    f"Baseline reference changed: {reference_id}.",
                    path=str(target),
                )
            expected_size = int(reference.get("size_bytes", -1))
            if target.stat().st_size != expected_size:
                raise EvidenceIndexError(
                    "baseline-reference-size-mismatch",
                    f"Baseline reference size changed: {reference_id}.",
                    path=str(target),
                )
            relative = target.relative_to(self.repository_root).as_posix()
            indexed = IndexedEvidenceReference(
                reference_id=reference_id,
                kind=_text(reference.get("kind"), f"{reference_id}.kind"),
                path=relative,
                sha256=actual_digest,
                size_bytes=expected_size,
                source_commit=str(reference.get("source_commit", "")),
            )
            references.append(indexed)
            if reference_id in REQUIRED_REFERENCE_IDS:
                values[reference_id] = _load_json(target, reference_id)

        missing = sorted(REQUIRED_REFERENCE_IDS - set(values))
        if missing:
            raise EvidenceIndexError(
                "readiness-reference-missing",
                f"Required frozen readiness references are missing: {', '.join(missing)}.",
                path="baseline.references",
            )
        return references, values

    def _discover_archives(
        self,
        source_runs: Mapping[str, Any],
    ) -> dict[str, Path]:
        source_reference = next(
            (
                item
                for item in _sequence(
                    _load_json(
                        self.baseline_manifest,
                        "Phase 2 baseline manifest",
                    ).get("references"),
                    "baseline references",
                )
                if isinstance(item, Mapping)
                and item.get("id") == "benchmark-source-runs"
            ),
            None,
        )
        if not isinstance(source_reference, Mapping):
            raise EvidenceIndexError(
                "source-run-reference-missing",
                "The source-run reference is unavailable.",
                path="baseline.references",
            )
        source_run_path = _safe_repository_path(
            self.repository_root,
            source_reference.get("path"),
            "benchmark-source-runs.path",
        )
        archive_root = source_run_path.parent / "source-archives"
        if not archive_root.is_dir():
            raise EvidenceIndexError(
                "source-archive-directory-missing",
                "The frozen source archive directory is missing.",
                path=str(archive_root),
            )
        archive_by_digest: dict[str, Path] = {}
        for archive in sorted(archive_root.glob("*.zip")):
            try:
                with zipfile.ZipFile(archive, "r") as bundle:
                    manifest = json.loads(bundle.read("manifest.json"))
            except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
                raise EvidenceIndexError(
                    "source-archive-container-invalid",
                    "A frozen source archive cannot be read.",
                    path=str(archive),
                ) from exc
            digest = _text(
                _mapping(manifest, "source archive manifest").get(
                    "manifest_digest"
                ),
                "source archive manifest digest",
            )
            if digest in archive_by_digest:
                raise EvidenceIndexError(
                    "source-archive-digest-duplicate",
                    f"Two archives share digest {digest}.",
                    path=str(archive_root),
                )
            archive_by_digest[digest] = archive
        declared = {
            str(_mapping(item, "source run").get("source", {}).get("archive_digest", ""))
            for item in _sequence(source_runs.get("sources"), "source runs")
        }
        undeclared = sorted(set(archive_by_digest) - declared)
        if undeclared:
            raise EvidenceIndexError(
                "source-archive-unindexed",
                "The source archive directory contains evidence not declared by "
                "the frozen source-run index.",
                path=str(archive_root),
            )
        return archive_by_digest

    def _read_source_archive(
        self,
        archive: Path,
        *,
        source_item: Mapping[str, Any],
        source: Mapping[str, Any],
        run_receipts: Iterable[Mapping[str, Any]],
        raw_samples: Iterable[Mapping[str, Any]],
        current_case: Mapping[str, Any],
    ) -> SourceRunEvidence:
        expected_member_digests = {
            str(key): str(value)
            for key, value in _mapping(
                source.get("member_digests"),
                "source member_digests",
            ).items()
        }
        with zipfile.ZipFile(archive, "r") as bundle:
            member_names = set(bundle.namelist())
            missing_members = sorted(REQUIRED_ARCHIVE_MEMBERS - member_names)
            if missing_members:
                raise EvidenceIndexError(
                    "source-archive-member-missing",
                    f"Source archive lacks: {', '.join(missing_members)}.",
                    path=str(archive),
                )
            member_payloads: dict[str, bytes] = {}
            for name in sorted(REQUIRED_ARCHIVE_MEMBERS):
                payload = bundle.read(name)
                member_payloads[name] = payload
                expected = expected_member_digests.get(name)
                if expected is None or _sha256_bytes(payload) != expected:
                    raise EvidenceIndexError(
                        "source-archive-member-digest-mismatch",
                        f"Source archive member changed: {name}.",
                        path=str(archive),
                    )

        def json_member(name: str) -> Mapping[str, Any]:
            try:
                value = json.loads(member_payloads[name].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidenceIndexError(
                    "source-archive-json-invalid",
                    f"Source archive member is not valid JSON: {name}.",
                    path=str(archive),
                ) from exc
            return _mapping(value, name)

        events: list[EvidenceEvent] = []
        for line_number, line in enumerate(
            member_payloads["canonical-events.jsonl"].decode("utf-8").splitlines(),
            start=1,
        ):
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvidenceIndexError(
                    "canonical-event-invalid",
                    f"Invalid canonical event at line {line_number}.",
                    path=str(archive),
                ) from exc
            events.append(EvidenceEvent.from_dict(_mapping(raw_event, "canonical event")))
        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise EvidenceIndexError(
                "canonical-event-id-duplicate",
                "A source archive contains duplicate canonical event IDs.",
                path=str(archive),
            )
        sequences = [event.sequence for event in events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise EvidenceIndexError(
                "canonical-event-order-invalid",
                "Canonical events are not in a stable unique sequence order.",
                path=str(archive),
            )
        expected_event_count = int(source.get("event_count", -1))
        if len(events) != expected_event_count:
            raise EvidenceIndexError(
                "canonical-event-count-mismatch",
                "The archive event count differs from the source-run index.",
                path=str(archive),
            )

        environment = json_member("environment.json")
        owner_receipts = json_member("owner-receipts.json")
        artifact_manifest = json_member("artifact-manifest.json")
        domain_verification = json_member("domain-verification.json")
        source_id = _text(source.get("scenario_run_id"), "source scenario_run_id")
        owner_run_id = _text(source.get("owner_run_id"), "source owner_run_id")
        task_id = _text(source.get("task_id"), "source task_id")
        if any(
            event.run_id != owner_run_id or event.task_id != task_id for event in events
        ):
            raise EvidenceIndexError(
                "canonical-event-owner-mismatch",
                "Canonical events do not share the frozen run/task owner.",
                path=str(archive),
            )
        if domain_verification.get("valid") is not True:
            raise EvidenceIndexError(
                "source-domain-verification-invalid",
                "A frozen source run has an invalid domain verifier receipt.",
                path=str(archive),
            )

        case_provider_values = current_case.get("provider_observations", ())
        case_tier_values = current_case.get("tier_observations", ())
        providers = tuple(
            _mapping(item, "provider observation")
            for item in _sequence(case_provider_values, "provider observations")
        )
        tiers = tuple(
            _mapping(item, "tier observation")
            for item in _sequence(case_tier_values, "tier observations")
        )
        relative_archive = archive.relative_to(self.repository_root).as_posix()
        return SourceRunEvidence(
            source_run_id=source_id,
            owner_run_id=owner_run_id,
            task_id=task_id,
            domain=_text(source_item.get("domain"), "source domain"),
            repetition=int(source_item.get("repetition", 0)),
            archive_path=relative_archive,
            archive_digest=_text(
                source.get("archive_digest"),
                "source archive_digest",
            ),
            archive_container_sha256=_sha256_file(archive),
            archive_member_digests=MappingProxyType(expected_member_digests),
            events=tuple(events),
            environment=MappingProxyType(dict(environment)),
            owner_receipts=MappingProxyType(dict(owner_receipts)),
            artifact_manifest=MappingProxyType(dict(artifact_manifest)),
            domain_verification=MappingProxyType(dict(domain_verification)),
            source_summary=MappingProxyType(dict(source)),
            run_receipts=tuple(run_receipts),
            raw_samples=tuple(raw_samples),
            provider_observations=providers,
            tier_observations=tiers,
        )


def build_read_only_evidence_index(
    repository_root: Path,
    *,
    baseline_manifest: Path | None = None,
) -> ReadOnlyEvidenceIndex:
    return FrozenEvidenceIndexer(
        repository_root,
        baseline_manifest=baseline_manifest,
    ).build()


__all__ = [
    "BASELINE_MANIFEST_DIGEST",
    "EvidenceEvent",
    "EvidenceIndexError",
    "FrozenEvidenceIndexer",
    "IndexedEvidenceReference",
    "ReadOnlyEvidenceIndex",
    "SourceRunEvidence",
    "build_read_only_evidence_index",
    "parse_timestamp",
]
