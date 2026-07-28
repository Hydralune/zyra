from __future__ import annotations

import json
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .archive import EvidenceArchiveVerifier
from .canonical import (
    bytes_digest,
    digest,
    require_digest,
    require_identity,
    require_mapping,
    require_sequence,
)
from .errors import blocker, fail, require_no_blockers


class ProjectionReplayVerifier:
    """Checks evidence projections without treating replay as task execution."""

    def verify_archive(
        self,
        archive_path: str | Path,
        *,
        expected_commit: str | None = None,
    ) -> dict[str, Any]:
        archive_receipt = EvidenceArchiveVerifier().verify(
            archive_path,
            expected_commit=expected_commit,
        )
        selected = Path(archive_path).resolve(strict=True)
        with zipfile.ZipFile(selected, mode="r") as archive:
            manifest = require_mapping(
                json.loads(archive.read("manifest.json").decode("utf-8")),
                "archive manifest",
            )
            names = {item.filename for item in archive.infolist()}
            replay_receipts = []
            if "generated/100-point-evidence-index.json" in names:
                index = require_mapping(
                    json.loads(
                        archive.read(
                            "generated/100-point-evidence-index.json"
                        ).decode("utf-8")
                    ),
                    "archived evidence index",
                )
                replay_receipts.append(self.verify_index_projection(index))
            if "inputs/benchmark/run-receipts.json" in names:
                runs = require_mapping(
                    json.loads(
                        archive.read(
                            "inputs/benchmark/run-receipts.json"
                        ).decode("utf-8")
                    ),
                    "archived run receipts",
                )
                replay_receipts.append(self.verify_run_projection(runs))
            if "inputs/benchmark/source-runs.json" in names:
                sources = require_mapping(
                    json.loads(
                        archive.read(
                            "inputs/benchmark/source-runs.json"
                        ).decode("utf-8")
                    ),
                    "archived source runs",
                )
                replay_receipts.append(self.verify_source_projection(sources))
            if not replay_receipts:
                raise fail(
                    "replay-projection-members-missing",
                    "Archive contains no supported projection members.",
                    phase="replay",
                )
        receipt = {
            "schema": "zyra.first-stage-projection-replay/v1",
            "valid": True,
            "task_success_recomputed": False,
            "task_success_source": "original-live-verifier-only",
            "archive_digest": archive_receipt["archive_sha256"],
            "manifest_digest": archive_receipt["manifest_digest"],
            "projection_count": len(replay_receipts),
            "projections": replay_receipts,
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def verify_index_projection(
        self,
        index: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = require_mapping(index, "evidence index projection")
        requirements = require_mapping(
            selected.get("requirements"),
            "evidence index requirements",
        )
        findings: list[dict[str, Any]] = []
        reference_ids: set[str] = set()
        target_digests: dict[str, str] = {}
        kind_counts: Counter[str] = Counter()
        for requirement_id, entry in sorted(requirements.items()):
            selected_entry = require_mapping(entry, requirement_id)
            for reference in require_sequence(
                selected_entry.get("references"),
                f"{requirement_id} references",
            ):
                selected_reference = require_mapping(
                    reference,
                    "evidence index reference",
                )
                reference_id = require_identity(
                    selected_reference.get("reference_id"),
                    "reference id",
                )
                if reference_id in reference_ids:
                    findings.append(
                        blocker(
                            "replay-index-reference-duplicate",
                            "Evidence index projection duplicates a reference.",
                            reference_id=reference_id,
                        )
                    )
                reference_ids.add(reference_id)
                kind = str(selected_reference.get("kind") or "")
                kind_counts[kind] += 1
                path = str(selected_reference.get("path") or "")
                target_digest = require_digest(
                    selected_reference.get("sha256"),
                    "reference target digest",
                )
                previous = target_digests.setdefault(path, target_digest)
                if previous != target_digest:
                    findings.append(
                        blocker(
                            "replay-index-target-digest-conflict",
                            "Same evidence path has conflicting digests.",
                            path=path,
                            first=previous,
                            second=target_digest,
                        )
                    )
        projection = dict(selected)
        declared = projection.pop("index_digest", "")
        observed = digest(projection)
        if declared != observed:
            findings.append(
                blocker(
                    "replay-index-digest-mismatch",
                    "Archived evidence index digest is invalid.",
                    declared=declared,
                    observed=observed,
                )
            )
        require_no_blockers(
            findings,
            code="replay-index-inconsistent",
            message="Evidence index projection is inconsistent.",
            phase="replay",
        )
        return {
            "projection": "score-index",
            "valid": True,
            "requirement_count": len(requirements),
            "reference_count": len(reference_ids),
            "target_count": len(target_digests),
            "kind_counts": dict(sorted(kind_counts.items())),
            "projection_digest": observed,
        }

    def verify_run_projection(
        self,
        run_receipts: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = require_mapping(run_receipts, "run receipt projection")
        runs = [
            require_mapping(item, "run receipt")
            for item in require_sequence(selected.get("runs"), "run receipts")
        ]
        findings: list[dict[str, Any]] = []
        run_ids: set[str] = set()
        cell_ids: set[str] = set()
        domains: Counter[str] = Counter()
        variants: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        repetitions = defaultdict(set)
        for run in runs:
            run_id = require_identity(run.get("run_id"), "run id")
            cell = require_mapping(run.get("cell"), f"{run_id} cell")
            cell_id = require_identity(cell.get("cell_id"), "cell id")
            if run_id in run_ids:
                findings.append(
                    blocker(
                        "replay-run-id-duplicate",
                        "Run projection contains duplicate run identifiers.",
                        run_id=run_id,
                    )
                )
            if cell_id in cell_ids:
                findings.append(
                    blocker(
                        "replay-cell-id-duplicate",
                        "Run projection contains duplicate cell identifiers.",
                        cell_id=cell_id,
                    )
                )
            run_ids.add(run_id)
            cell_ids.add(cell_id)
            domain = str(cell.get("domain") or "")
            variant = str(cell.get("variant_id") or "")
            repetition = int(cell.get("repetition") or 0)
            domains[domain] += 1
            variants[variant] += 1
            repetitions[(domain, variant)].add(repetition)
            for field in (
                "result_digest",
                "live_receipt_digest",
                "verifier_receipt_digest",
                "deployment_receipt_digest",
                "fault_receipt_digest",
            ):
                require_digest(run.get(field), f"{run_id} {field}")
            semantic = require_mapping(
                run.get("semantic_step_receipt"),
                f"{run_id} semantic step receipt",
            )
            require_digest(
                semantic.get("receipt_digest"),
                f"{run_id} semantic_step_receipt receipt_digest",
            )
            outcome = (
                "succeeded"
                if run.get("task_succeeded") is True
                and run.get("final_delivery_succeeded") is True
                else "not-succeeded"
            )
            outcomes[outcome] += 1
        declared_count = selected.get("run_count")
        if declared_count != len(runs):
            findings.append(
                blocker(
                    "replay-run-count-mismatch",
                    "Run projection count is inconsistent.",
                    declared=declared_count,
                    observed=len(runs),
                )
            )
        for key, observed in repetitions.items():
            if observed != set(range(1, max(observed, default=0) + 1)):
                findings.append(
                    blocker(
                        "replay-repetition-gap",
                        "Run projection contains a repetition gap.",
                        domain=key[0],
                        variant=key[1],
                        repetitions=sorted(observed),
                    )
                )
        require_no_blockers(
            findings,
            code="replay-run-projection-inconsistent",
            message="Run receipt projection is inconsistent.",
            phase="replay",
        )
        return {
            "projection": "benchmark-runs",
            "valid": True,
            "run_count": len(runs),
            "domain_counts": dict(sorted(domains.items())),
            "variant_counts": dict(sorted(variants.items())),
            "original_outcome_counts": dict(sorted(outcomes.items())),
            "task_success_recomputed": False,
            "projection_digest": digest(runs),
        }

    def verify_source_projection(
        self,
        source_runs: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = require_mapping(source_runs, "source-run projection")
        values = selected.get("sources")
        if values is None:
            values = selected.get("runs")
        sources = [
            require_mapping(item, "source-run receipt")
            for item in require_sequence(values, "source-run receipts")
        ]
        findings: list[dict[str, Any]] = []
        identities: set[tuple[str, int]] = set()
        domains: Counter[str] = Counter()
        total_transitions = 0
        for source in sources:
            domain = str(source.get("domain") or "")
            repetition = int(source.get("repetition") or 0)
            identity = (domain, repetition)
            if identity in identities:
                findings.append(
                    blocker(
                        "replay-source-run-duplicate",
                        "Source-run projection duplicates domain/repetition.",
                        domain=domain,
                        repetition=repetition,
                    )
                )
            identities.add(identity)
            domains[domain] += 1
            outcome_digest = source.get("outcome_digest")
            require_digest(outcome_digest, "source-run outcome digest")
            payload = require_mapping(source.get("source"), "source-run payload")
            transitions = int(payload.get("effective_transition_count") or 0)
            if transitions < 2_000:
                findings.append(
                    blocker(
                        "replay-source-transition-threshold-missing",
                        "Source live run does not reach 2,000 effective transitions.",
                        domain=domain,
                        repetition=repetition,
                        transitions=transitions,
                    )
                )
            total_transitions += transitions
            if int(payload.get("human_intervention_count") or 0) != 0:
                findings.append(
                    blocker(
                        "replay-source-human-intervention",
                        "Source live run contains human intervention.",
                        domain=domain,
                        repetition=repetition,
                    )
                )
            for field in (
                "archive_digest",
                "configuration_digest",
                "event_stream_digest",
                "input_digest",
                "raw_sample_stream_digest",
            ):
                require_digest(payload.get(field), f"source-run {field}")
        if len(domains) < 2:
            findings.append(
                blocker(
                    "replay-source-domain-count-insufficient",
                    "Source projection contains fewer than two domains.",
                    domains=sorted(domains),
                )
            )
        require_no_blockers(
            findings,
            code="replay-source-projection-inconsistent",
            message="Source live-run projection is inconsistent.",
            phase="replay",
        )
        return {
            "projection": "source-live-runs",
            "valid": True,
            "source_run_count": len(sources),
            "domain_counts": dict(sorted(domains.items())),
            "effective_transition_total": total_transitions,
            "task_success_recomputed": False,
            "projection_digest": digest(sources),
        }

    def verify_event_chain(
        self,
        events: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        previous = "0" * 64
        event_ids: set[str] = set()
        counts: Counter[str] = Counter()
        last_sequence = 0
        observed = 0
        for item in events:
            event = require_mapping(item, "canonical event")
            observed += 1
            event_id = require_identity(event.get("event_id"), "event id")
            if event_id in event_ids:
                findings.append(
                    blocker(
                        "replay-event-id-duplicate",
                        "Canonical event identifier is duplicated.",
                        event_id=event_id,
                    )
                )
            event_ids.add(event_id)
            sequence = int(event.get("sequence") or observed)
            if sequence <= last_sequence:
                findings.append(
                    blocker(
                        "replay-event-sequence-nonmonotonic",
                        "Canonical event sequence is not monotonic.",
                        event_id=event_id,
                        sequence=sequence,
                        previous=last_sequence,
                    )
                )
            last_sequence = sequence
            declared_previous = str(
                event.get("previous_digest")
                or event.get("previous_event_digest")
                or previous
            )
            if declared_previous != previous:
                findings.append(
                    blocker(
                        "replay-event-predecessor-mismatch",
                        "Canonical event predecessor digest is inconsistent.",
                        event_id=event_id,
                    )
                )
            projection = dict(event)
            declared = projection.pop(
                "event_digest",
                projection.pop("digest", ""),
            )
            calculated = digest(projection)
            if declared and declared != calculated:
                findings.append(
                    blocker(
                        "replay-event-digest-mismatch",
                        "Canonical event digest is invalid.",
                        event_id=event_id,
                    )
                )
            previous = declared or calculated
            counts[str(event.get("event_type") or event.get("kind") or "unknown")] += 1
        require_no_blockers(
            findings,
            code="replay-event-chain-inconsistent",
            message="Canonical event hash chain is inconsistent.",
            phase="replay",
        )
        return {
            "projection": "canonical-event-chain",
            "valid": True,
            "event_count": observed,
            "leaf_digest": previous,
            "event_type_counts": dict(sorted(counts.items())),
            "task_success_recomputed": False,
        }
