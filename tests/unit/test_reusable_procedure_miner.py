from __future__ import annotations

import os
import gc
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from zyra_core import EventRecord, EventType
from zyra_memory import (
    MemoryLayer,
    MemoryRecord,
    ProcedureConsumer,
    ProcedureMiningDisposition,
    ProcedureMiningError,
    ProcedureValidationStatus,
    ReusableProcedureMiner,
    ReusableProcedureRuntime,
    ReusableProcedureStore,
    SQLiteStore,
    typescript_projection_digest,
)
from zyra_memory.curator_integration_models import (
    CuratorConsumer,
    CuratorOutcome,
    CuratorOutcomeKind,
    CuratorOutcomeState,
)
from zyra_memory.curator_integration_store import CuratorIntegrationStore
from zyra_memory.procedure_models import stable_digest


class ReusableProcedureMinerTests(unittest.TestCase):
    def test_validated_06b_skill_outcome_becomes_durable_multi_consumer_procedure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canonical.sqlite3"
            canonical = SQLiteStore(path)
            procedure_store = ReusableProcedureStore(path)
            memory, outcome = _canonical_skill_trajectory(canonical)
            emitted: list[dict[str, object]] = []
            miner = ReusableProcedureMiner(
                canonical_store=canonical,
                procedure_store=procedure_store,
                event_sink=emitted.append,
            )

            result = miner.mine(outcome)

            self.assertEqual(result.disposition, ProcedureMiningDisposition.CREATED)
            self.assertIsNotNone(result.procedure)
            procedure = result.procedure
            assert procedure is not None
            self.assertEqual(procedure.state, ProcedureValidationStatus.VALIDATED)
            self.assertEqual(procedure.provenance.curator_outcome_id, outcome.outcome_id)
            self.assertEqual(procedure.provenance.curator_decision_id, outcome.decision_id)
            self.assertEqual(procedure.provenance.memory_id, memory.memory_id)
            self.assertEqual(procedure.provenance.tool_call_ids, ("tool-call-06c",))
            self.assertEqual(procedure.provenance.artifact_ids, ("artifact-06c",))
            self.assertEqual(procedure.provenance.session_ids, ("session-06c",))
            self.assertEqual(
                procedure.provenance.skill_versions[0]["descriptor_digest"],
                stable_digest("descriptor"),
            )
            self.assertTrue(procedure.routable)
            self.assertTrue(procedure.recoverable)
            self.assertIn(ProcedureConsumer.CONTEXT, procedure.consumers)
            self.assertTrue(emitted)
            self.assertEqual(emitted[0]["event_type"], "memory.procedure_validated")

            replay = miner.mine(outcome)
            self.assertEqual(replay.disposition, ProcedureMiningDisposition.REPLAYED)
            self.assertEqual(replay.procedure.procedure_id, procedure.procedure_id)
            self.assertEqual(len(procedure_store.list(task_id=outcome.task_id)), 1)

            runtime = ReusableProcedureRuntime(
                canonical_store=canonical,
                curator_store=CuratorIntegrationStore(path),
                procedure_store=procedure_store,
            )
            routed = runtime.routing(
                outcome.task_id,
                {
                    "goal": "Review a TypeScript runtime change",
                    "languages": ["typescript"],
                    "workspace_kinds": ["repository"],
                    "available_tools": ["file_read"],
                },
            )
            self.assertEqual(len(routed.matches), 1)
            self.assertTrue(routed.matches[0].applicable)
            self.assertEqual(routed.matches[0].procedure.procedure_id, procedure.procedure_id)
            recovered = runtime.recovery(
                outcome.task_id,
                {"available_tools": ["file_read"]},
            )
            self.assertEqual(len(recovered.matches), 1)
            contextual = runtime.context(
                outcome.task_id,
                {"available_tools": ["file_read"]},
            )
            self.assertEqual(len(contextual.matches), 1)

            exported = runtime.export_for_typescript(outcome.task_id)
            self.assertEqual(len(exported), 1)
            projection = dict(exported[0])
            projection_digest = projection.pop("procedureDigest")
            self.assertEqual(
                typescript_projection_digest(projection),
                projection_digest,
            )
            self.assertEqual(exported[0]["provenance"]["toolCallIds"], ["tool-call-06c"])
            self.assertEqual(exported[0]["state"], "validated")
            del runtime
            gc.collect()

    def test_ineligible_or_disabled_inputs_cannot_activate_procedure_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canonical.sqlite3"
            canonical = SQLiteStore(path)
            procedure_store = ReusableProcedureStore(path)
            _, outcome = _canonical_skill_trajectory(canonical)
            miner = ReusableProcedureMiner(
                canonical_store=canonical,
                procedure_store=procedure_store,
            )

            with patch.dict(os.environ, {"ZYRA_DISABLE_REUSABLE_PROCEDURE_MINER": "1"}):
                with self.assertRaisesRegex(ProcedureMiningError, "disabled"):
                    miner.mine(outcome)
            self.assertEqual(procedure_store.list(task_id=outcome.task_id), ())

            forged = replace(
                outcome,
                deterministic_validation=False,
                canonical_memory_changed=False,
                outcome_digest="0" * 64,
            )
            forged = replace(
                forged,
                outcome_digest=stable_digest(forged.semantic_projection()),
            )
            rejected = miner.mine(forged)
            self.assertEqual(rejected.disposition, ProcedureMiningDisposition.REJECTED)
            self.assertIsNone(rejected.procedure)
            self.assertIn("procedure_outcome_not_deterministic", rejected.receipt.reason)
            self.assertEqual(procedure_store.list(task_id=outcome.task_id), ())
            self.assertFalse(rejected.receipt.metadata["static_document_activated"])
            self.assertFalse(rejected.receipt.metadata["model_summary_activated"])


def _canonical_skill_trajectory(
    store: SQLiteStore,
) -> tuple[MemoryRecord, CuratorOutcome]:
    run_id = "run-06c"
    task_id = "task-06c"
    occurred_at = "2026-07-21T08:00:00+00:00"
    skill_version = {
        "skill_id": "reviewer",
        "skill_name": "reviewer",
        "registry_revision": 7,
        "descriptor_digest": stable_digest("descriptor"),
        "body_digest": stable_digest("body"),
        "resource_digests": {"checklist.md": stable_digest("resource")},
        "source_revision": "registry-revision-7",
    }
    memory = MemoryRecord(
        memory_id="memory-skill-06c",
        run_id=run_id,
        task_id=task_id,
        layer=MemoryLayer.SKILL,
        source_type="memory_curator",
        source_id="candidate-06c",
        summary="A verified TypeScript review trajectory completed successfully.",
        content={
            "goal": "Review a TypeScript runtime change",
            "language": "typescript",
            "workspace_kind": "repository",
            "skill_version": skill_version,
            "denied_tools": ["shell"],
        },
        keywords=["review", "typescript"],
        artifact_ids=["artifact-06c"],
        evidence_ids=["event-tool-06c"],
        score=0.98,
        created_at=occurred_at,
        updated_at=occurred_at,
        metadata={"skill_version": skill_version, "validated": True},
    )
    store.save_memory_records([memory])
    store.append_event(
        EventRecord(
            event_id="event-tool-06c",
            run_id=run_id,
            task_id=task_id,
            event_type=EventType.SKILL_INVOKED,
            created_at=occurred_at,
            payload={
                "memory_id": memory.memory_id,
                "candidate_id": "candidate-06c",
                "tool_call_id": "tool-call-06c",
                "tool_name": "file_read",
                "summary": "Read and verify the TypeScript runtime source",
                "arguments": {"path": "src/runtime.ts"},
                "arguments_digest": stable_digest({"path": "src/runtime.ts"}),
                "status": "succeeded",
                "tool_result": {"ok": True, "status": "completed"},
                "artifact_id": "artifact-06c",
                "artifact_kind": "review",
                "session_id": "session-06c",
                "skill_version": skill_version,
            },
        )
    )
    semantic = {
        "run_id": run_id,
        "task_id": task_id,
        "curator_job_id": "curator-job-06c",
        "curator_request_id": "curator-request-06c",
        "kind": CuratorOutcomeKind.SKILL_CANDIDATE.value,
        "candidate_id": "candidate-06c",
        "decision_id": "decision-06c",
        "evidence_bundle_id": "evidence-bundle-06c",
        "evidence_digest": stable_digest("evidence-bundle-06c"),
        "subject": "Review a TypeScript runtime change",
        "summary": "Deterministically validated successful skill trajectory.",
        "payload": {
            "goal": "Review a TypeScript runtime change",
            "language": "typescript",
            "workspace_kind": "repository",
            "skill_version": skill_version,
        },
        "target_consumers": [CuratorConsumer.SKILL_MEMORY.value],
        "memory_id": memory.memory_id,
        "memory_revision": 1,
        "commit_receipt_id": "commit-receipt-06c",
        "model_assisted": False,
        "deterministic_validation": True,
        "canonical_memory_changed": True,
        "index_published": False,
        "supersedes_outcome_id": "",
    }
    outcome = CuratorOutcome(
        outcome_id="curator-outcome-06c",
        run_id=run_id,
        task_id=task_id,
        curator_job_id="curator-job-06c",
        curator_request_id="curator-request-06c",
        kind=CuratorOutcomeKind.SKILL_CANDIDATE,
        state=CuratorOutcomeState.PUBLISHED,
        candidate_id="candidate-06c",
        decision_id="decision-06c",
        evidence_bundle_id="evidence-bundle-06c",
        evidence_digest=stable_digest("evidence-bundle-06c"),
        subject="Review a TypeScript runtime change",
        summary="Deterministically validated successful skill trajectory.",
        payload=semantic["payload"],
        target_consumers=(CuratorConsumer.SKILL_MEMORY,),
        outcome_digest=stable_digest(semantic),
        created_at=occurred_at,
        memory_id=memory.memory_id,
        memory_revision=1,
        commit_receipt_id="commit-receipt-06c",
        causation_id="decision-06c",
        model_assisted=False,
        deterministic_validation=True,
        canonical_memory_changed=True,
        index_published=False,
        metadata={"source": "06B", "validated": True},
    ).validated()
    return memory, outcome


if __name__ == "__main__":
    unittest.main()
