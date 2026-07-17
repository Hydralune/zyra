import type { JsonObject } from "../contracts.ts";
import { cloneJson, digest } from "../e02/canonical.ts";
import type {
  CommitReceipt,
  E02Clock,
  E02RuntimeIdentity,
  PermissionDecisionRecord,
  TransitionJournalSnapshot,
  TransitionPrepareResult,
} from "../e02/contracts.ts";
import { DurableTransitionJournal } from "../e02/transition-journal.ts";
import type { PermissionIdentityRecord } from "./model.ts";

export class PermissionJournal {
  private readonly journal: DurableTransitionJournal;
  private readonly runtime: E02RuntimeIdentity;

  constructor(runtime: E02RuntimeIdentity, clock?: E02Clock) {
    this.runtime = cloneJson(runtime);
    this.journal = new DurableTransitionJournal(runtime, clock);
  }

  prepare(identity: PermissionIdentityRecord, policyRevision: number): TransitionPrepareResult {
    const entityId = `permission:${identity.context.sessionId}:${identity.context.toolCallId}`;
    const entity = this.journal.entityState(entityId);
    return this.journal.prepare({
      domain: "permission",
      operation: "evaluate",
      binding: {
        // The journal is owned by the long-lived TypeScript host runtime.
        // The evaluated physical subject can belong to another run/session
        // (for example BrowserWorker through the typed E02 API port) and is
        // retained verbatim in payload.identity and the decision receipt.
        runId: this.runtime.runId,
        taskId: this.runtime.taskId,
        sessionId: this.runtime.sessionId,
        workerRequestId: this.runtime.workerRequestId,
        toolCallId: identity.context.toolCallId,
        requestId: identity.requestFingerprint,
        entityId,
        expectedRevision: entity.revision,
      },
      payload: {
        identity,
        policy_revision: policyRevision,
      },
      idempotencyKey: `permission:${identity.requestFingerprint}:policy:${policyRevision}`,
      replayPolicy: "resume-pending-or-return-committed",
      nonIdempotentEffect: false,
      metadata: {
        canonical_owner: "typescript",
        subject_run_id: identity.context.runId,
        subject_task_id: identity.context.taskId,
        subject_session_id: identity.context.sessionId,
        subject_worker_request_id: identity.context.workerRequestId,
      },
    });
  }

  commit(transitionId: string, decision: PermissionDecisionRecord): CommitReceipt {
    const transition = this.journal.transition(transitionId);
    if (!transition) throw new Error(`unknown permission journal transition ${transitionId}`);
    return this.journal.commit({
      transitionId,
      nextState: {
        decision_id: decision.decisionId,
        effect: decision.effect,
        request_fingerprint: decision.requestFingerprint,
        policy_revision: decision.policyRevision,
        mode_revision: decision.modeRevision,
        decision_hash: digest(decision),
      },
      output: { decision },
      expectedRevision: transition.binding.expectedRevision,
    });
  }

  acknowledge(transitionId: string, commitHash: string): CommitReceipt {
    return this.journal.acknowledge(transitionId, commitHash);
  }

  committedDecision(idempotencyKey: string): PermissionDecisionRecord | null {
    const receipt = this.journal.committedReceiptByIdempotency(idempotencyKey);
    const decision = receipt?.output.decision;
    return decision && typeof decision === "object" && !Array.isArray(decision)
      ? cloneJson(decision as unknown as PermissionDecisionRecord)
      : null;
  }

  snapshot(): TransitionJournalSnapshot {
    return this.journal.snapshot();
  }

  restore(snapshot: TransitionJournalSnapshot, targetEpoch: number): void {
    this.journal.restore(snapshot, targetEpoch);
  }

  state(): JsonObject {
    return this.journal.snapshot() as unknown as JsonObject;
  }
}
