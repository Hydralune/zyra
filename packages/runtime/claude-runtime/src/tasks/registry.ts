import {
  isTerminal,
  type E03Delivery,
  type E03Message,
} from "../e03/contracts.ts";
import {
  assertSnapshotChecksum,
  assertTaskChecksum,
  cloneJson,
  createId,
  digest,
  E03RuntimeError,
  sealSnapshot,
  sealTask,
  type E03Clock,
  type E03CommittedMutation,
  type E03ControlResponse,
  type E03EffectReceipt,
  type E03EffectRequest,
  type E03PhysicalPort,
  type E03PreparedMutation,
  type E03RegistrySnapshot,
  type E03TaskState,
  type E03Transition,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface MutationProposal {
  requestId: string;
  idempotencyKey: string;
  writerId: string;
  taskId: string;
  expectedRevision: number;
  proposed: E03TaskState;
  effectKind?: E03EffectRequest["effectKind"];
  effectOperation?: string;
  effectPayload?: E03EffectRequest["payload"];
}

export class DurableTaskRegistry {
  private snapshotValue: E03RegistrySnapshot;
  private readonly pending = new Map<string, E03PreparedMutation>();
  private readonly committed = new Map<string, E03CommittedMutation>();
  private readonly acknowledgements = new Map<string, E03ControlResponse>();
  private restored = false;
  private readonly invariants = new TaskInvariantRuntime();
  readonly journal = new DurableTaskJournal();
  readonly index = new TaskRegistryIndex();
  readonly recoveryPlanner = new TaskRegistryRecoveryPlanner();

  constructor(
    snapshot: E03RegistrySnapshot,
    private readonly port: E03PhysicalPort,
    private readonly clock: E03Clock = new SystemE03Clock(),
  ) {
    assertSnapshotChecksum(snapshot);
    this.snapshotValue = structuredClone(snapshot);
    this.index.rebuild(this.snapshotValue);
  }

  prepare(proposal: MutationProposal): E03PreparedMutation {
    if (
      !proposal.requestId.trim() ||
      !proposal.idempotencyKey.trim() ||
      !proposal.writerId.trim()
    )
      throw new E03RuntimeError(
        "invalid_mutation_identity",
        "request, idempotency and writer identities are required",
      );
    assertTaskChecksum(proposal.proposed);
    if (proposal.proposed.identity.taskId !== proposal.taskId)
      throw new E03RuntimeError(
        "mutation_task_mismatch",
        "proposed task id differs from mutation task id",
      );
    const replay = this.pending.get(proposal.idempotencyKey);
    if (replay) {
      if (
        replay.transition.requestId !== proposal.requestId ||
        replay.proposed.checksum !== proposal.proposed.checksum
      )
        throw new E03RuntimeError(
          "idempotency_conflict",
          "idempotency key was reused with different mutation content",
        );
      return structuredClone(replay);
    }
    const existing = this.snapshotValue.tasks[proposal.taskId] ?? null;
    if (existing) {
      assertTaskChecksum(existing);
      if (existing.revision !== proposal.expectedRevision)
        throw new E03RuntimeError(
          "stale_revision",
          `task revision ${existing.revision} differs from expected ${proposal.expectedRevision}`,
        );
      if (proposal.proposed.revision !== existing.revision + 1)
        throw new E03RuntimeError(
          "non_monotonic_revision",
          "proposed task revision must increment exactly once",
        );
      if (proposal.proposed.identity.leaseId !== existing.identity.leaseId)
        throw new E03RuntimeError(
          "stale_lease",
          "proposed mutation changed the active task lease",
        );
    } else if (
      proposal.expectedRevision !== 0 ||
      proposal.proposed.revision !== 1
    ) {
      throw new E03RuntimeError(
        "invalid_create_revision",
        "new task must commit revision one from the empty revision-zero slot",
      );
    }
    const leaseOwner =
      this.snapshotValue.writerLeases[proposal.proposed.identity.leaseId];
    if (leaseOwner && leaseOwner !== proposal.writerId)
      throw new E03RuntimeError(
        "writer_lease_conflict",
        `lease belongs to ${leaseOwner}`,
      );
    const proposedTransition = proposal.proposed.transitions.at(-1);
    const transition =
      proposedTransition &&
      proposedTransition.fromRevision === (existing?.revision ?? -1) &&
      proposedTransition.toRevision === proposal.proposed.revision
        ? proposedTransition
        : {
            transitionId: createId("create-transition"),
            taskId: proposal.taskId,
            requestId: proposal.requestId,
            idempotencyKey: proposal.idempotencyKey,
            phase: "prepare" as const,
            fromRevision: existing?.revision ?? 0,
            toRevision: proposal.proposed.revision,
            fromStatus: "created" as const,
            toStatus: proposal.proposed.status,
            eventType: existing
              ? (proposal.effectOperation ?? "agent_task_mutated")
              : "agent_task_created",
            effectId: null,
            receiptId: null,
            writerId: proposal.writerId,
            leaseId: proposal.proposed.identity.leaseId,
            preparedAt: this.clock.now(),
            committedAt: null,
            acknowledgedAt: null,
            digest: "",
          };
    const effect = proposal.effectKind
      ? this.effectRequest(proposal, transition.transitionId)
      : null;
    const preparedTransition = {
      ...transition,
      requestId: proposal.requestId,
      idempotencyKey: proposal.idempotencyKey,
      phase: "prepare" as const,
      effectId: effect?.effectId ?? null,
      receiptId: null,
      writerId: proposal.writerId,
      leaseId: proposal.proposed.identity.leaseId,
      committedAt: null,
      acknowledgedAt: null,
      digest: "",
    };
    const sealedTransition = sealTransition(preparedTransition);
    const prepared = {
      transition: sealedTransition,
      before: existing ? structuredClone(existing) : null,
      proposed: structuredClone(proposal.proposed),
      effect,
      preparedDigest: digest({
        transition: sealedTransition,
        before: existing?.checksum ?? null,
        proposed: proposal.proposed.checksum,
        effect: effect?.digest ?? null,
      }),
    };
    this.pending.set(proposal.idempotencyKey, prepared);
    return structuredClone(prepared);
  }

  async recordReceipt(
    idempotencyKey: string,
  ): Promise<E03EffectReceipt | null> {
    const prepared = this.pending.get(idempotencyKey);
    if (!prepared)
      throw new E03RuntimeError(
        "unknown_prepared_mutation",
        `no prepared mutation for ${idempotencyKey}`,
      );
    if (!prepared.effect) return null;
    const prior = this.snapshotValue.effects[prepared.effect.effectId];
    if (prior) {
      this.validateReceipt(prepared.effect, prior);
      return structuredClone({ ...prior, replayed: true });
    }
    const receipt = await this.port.effect(structuredClone(prepared.effect));
    this.validateReceipt(prepared.effect, receipt);
    if (!receipt.accepted)
      throw new E03RuntimeError(
        "physical_effect_rejected",
        receipt.error || `effect ${receipt.effectId} was rejected`,
        { effectId: receipt.effectId },
      );
    return receipt;
  }

  revisePrepared(
    idempotencyKey: string,
    proposed: E03TaskState,
  ): E03PreparedMutation {
    const prepared = this.pending.get(idempotencyKey);
    if (!prepared)
      throw new E03RuntimeError(
        "unknown_prepared_mutation",
        `no prepared mutation for ${idempotencyKey}`,
      );
    assertTaskChecksum(proposed);
    if (
      proposed.identity.taskId !== prepared.proposed.identity.taskId ||
      proposed.revision !== prepared.proposed.revision
    )
      throw new E03RuntimeError(
        "prepared_revision_conflict",
        "revised prepared state changed task identity or revision",
      );
    const revised = {
      ...prepared,
      proposed: structuredClone(proposed),
      preparedDigest: digest({
        transition: prepared.transition.digest,
        before: prepared.before?.checksum ?? null,
        proposed: proposed.checksum,
        effect: prepared.effect?.digest ?? null,
      }),
    };
    this.pending.set(idempotencyKey, revised);
    return structuredClone(revised);
  }

  async commit(
    idempotencyKey: string,
    receipt: E03EffectReceipt | null,
  ): Promise<E03CommittedMutation> {
    const replay = this.committed.get(idempotencyKey);
    if (replay) {
      if ((replay.receipt?.digest ?? null) !== (receipt?.digest ?? null))
        throw new E03RuntimeError(
          "commit_receipt_conflict",
          "replayed commit uses a different receipt",
        );
      return structuredClone(replay);
    }
    const prepared = this.pending.get(idempotencyKey);
    if (!prepared)
      throw new E03RuntimeError(
        "unknown_prepared_mutation",
        `no prepared mutation for ${idempotencyKey}`,
      );
    if (prepared.effect && !receipt)
      throw new E03RuntimeError(
        "missing_effect_receipt",
        "effectful commit requires a receipt",
      );
    if (prepared.effect && receipt)
      this.validateReceipt(prepared.effect, receipt);
    const committedAt = this.clock.now();
    const transitionPayload = {
      ...prepared.transition,
      phase: "commit" as const,
      receiptId: receipt?.receiptId ?? null,
      committedAt,
      acknowledgedAt: null,
      digest: "",
    };
    const transition = sealTransition(transitionPayload);
    const transitions =
      prepared.proposed.transitions.length &&
      prepared.proposed.transitions.at(-1)?.transitionId ===
        transition.transitionId
        ? [...prepared.proposed.transitions.slice(0, -1), transition]
        : [...prepared.proposed.transitions, transition];
    const state = sealTask({
      ...prepared.proposed,
      transitions,
      updatedAt: committedAt,
      checksum: "",
    });
    this.invariants.assertTask(state);
    const nextSnapshot = sealSnapshot({
      ...this.snapshotValue,
      revision: this.snapshotValue.revision + 1,
      tasks: { ...this.snapshotValue.tasks, [state.identity.taskId]: state },
      effects: receipt
        ? { ...this.snapshotValue.effects, [receipt.effectId]: receipt }
        : this.snapshotValue.effects,
      writerLeases: {
        ...this.snapshotValue.writerLeases,
        [state.identity.leaseId]: transition.writerId,
      },
      updatedAt: committedAt,
      checksum: "",
    });
    this.invariants.assertSnapshot(nextSnapshot);
    const persisted = await this.port.compareAndSwap(
      this.snapshotValue.revision,
      nextSnapshot,
    );
    if (!persisted.accepted)
      throw new E03RuntimeError(
        persisted.error === "stale_revision"
          ? "stale_registry_revision"
          : "registry_commit_rejected",
        persisted.error || "durable CAS rejected",
        { expected: this.snapshotValue.revision, actual: persisted.revision },
      );
    this.snapshotValue = nextSnapshot;
    this.index.rebuild(nextSnapshot);
    this.journal.appendTransition(nextSnapshot, transition);
    if (receipt)
      this.journal.appendEffect(nextSnapshot, receipt, idempotencyKey);
    this.journal.appendTask(
      nextSnapshot,
      state,
      transition.eventType,
      idempotencyKey,
    );
    const committed = {
      transition,
      state,
      receipt: receipt ? structuredClone(receipt) : null,
      committedDigest: digest({
        transition: transition.digest,
        state: state.checksum,
        receipt: receipt?.digest ?? null,
      }),
    };
    this.committed.set(idempotencyKey, committed);
    return structuredClone(committed);
  }

  acknowledge(
    idempotencyKey: string,
    response: E03ControlResponse,
  ): E03ControlResponse {
    const prior =
      this.acknowledgements.get(idempotencyKey) ??
      this.snapshotValue.requests[idempotencyKey];
    if (prior) return structuredClone({ ...prior, replayed: true });
    const committed = this.committed.get(idempotencyKey);
    if (!committed)
      throw new E03RuntimeError(
        "ack_before_commit",
        "cannot acknowledge an uncommitted mutation",
      );
    const acknowledgedAt = this.clock.now();
    const transition = {
      ...committed.transition,
      phase: "ack" as const,
      acknowledgedAt,
      digest: "",
    };
    transition.digest = sealTransition(transition).digest;
    const current = this.snapshotValue.tasks[committed.state.identity.taskId];
    const acknowledgementBase =
      current && current.revision > committed.state.revision
        ? current
        : committed.state;
    const state = sealTask({
      ...acknowledgementBase,
      transitions: acknowledgementBase.transitions.map((item) =>
        item.transitionId === transition.transitionId ? transition : item,
      ),
      updatedAt: acknowledgedAt,
      checksum: "",
    });
    const acknowledged = {
      ...response,
      phase: "ack" as const,
      revision: state.revision,
      state: taskProjection(state),
      replayed: false,
    };
    this.snapshotValue = sealSnapshot({
      ...this.snapshotValue,
      tasks: { ...this.snapshotValue.tasks, [state.identity.taskId]: state },
      requests: {
        ...this.snapshotValue.requests,
        [idempotencyKey]: acknowledged,
      },
      updatedAt: acknowledgedAt,
      checksum: "",
    });
    this.acknowledgements.set(idempotencyKey, acknowledged);
    this.committed.set(idempotencyKey, { ...committed, transition, state });
    return structuredClone(acknowledged);
  }

  async restore(
    runId: string,
    sessionId: string,
  ): Promise<E03RegistrySnapshot> {
    const restored = await this.port.restore(runId, sessionId);
    if (!restored) {
      this.restored = true;
      return this.snapshot();
    }
    assertSnapshotChecksum(restored);
    this.invariants.assertSnapshot(restored);
    for (const task of Object.values(restored.tasks)) {
      if (
        task.identity.runId !== runId ||
        (task.identity.sessionId !== sessionId &&
          task.identity.parentSessionId !== sessionId)
      )
        throw new E03RuntimeError(
          "registry_authority_mismatch",
          `restored task ${task.identity.taskId} belongs to another run/session`,
        );
      assertTaskChecksum(task);
    }
    if (restored.revision < this.snapshotValue.revision)
      throw new E03RuntimeError(
        "registry_revision_regressed",
        "restored registry revision is older than memory",
      );
    this.snapshotValue = structuredClone(restored);
    this.index.rebuild(this.snapshotValue);
    this.recoveryPlanner.plan(this.snapshotValue);
    this.restored = true;
    return this.snapshot();
  }

  recoverLostAck(idempotencyKey: string): E03ControlResponse | null {
    const acknowledged =
      this.acknowledgements.get(idempotencyKey) ??
      this.snapshotValue.requests[idempotencyKey];
    if (acknowledged)
      return structuredClone({ ...acknowledged, replayed: true });
    const committed = this.committed.get(idempotencyKey);
    if (!committed) {
      const task = Object.values(this.snapshotValue.tasks).find((item) =>
        item.transitions.some(
          (transition) =>
            transition.idempotencyKey === idempotencyKey &&
            transition.committedAt,
        ),
      );
      if (!task) return null;
      const transition = task.transitions.find(
        (item) => item.idempotencyKey === idempotencyKey,
      )!;
      return {
        ok: true,
        request_id: transition.requestId,
        command: inferCommand(transition.eventType),
        phase: "ack",
        revision: task.revision,
        replayed: true,
        restored: this.restored,
        dispatch_count: 1,
        runtime_origin: "typescript.E03AgentControlCoordinator",
        python_logical_owner: false,
        python_fallback_attempted: false,
        commit_protocol: ["prepare", "effect", "receipt", "commit", "ack"],
        state: taskProjection(task),
        result: task.result,
        error: "",
      };
    }
    return {
      ok: true,
      request_id: committed.transition.requestId,
      command: inferCommand(committed.transition.eventType),
      phase: "ack",
      revision: committed.state.revision,
      replayed: true,
      restored: this.restored,
      dispatch_count: 1,
      runtime_origin: "typescript.E03AgentControlCoordinator",
      python_logical_owner: false,
      python_fallback_attempted: false,
      commit_protocol: ["prepare", "effect", "receipt", "commit", "ack"],
      state: taskProjection(committed.state),
      result: committed.state.result,
      error: "",
    };
  }

  get(taskId: string): E03TaskState | null {
    const task = this.snapshotValue.tasks[taskId];
    return task ? structuredClone(task) : null;
  }

  require(taskId: string): E03TaskState {
    const task = this.get(taskId);
    if (!task)
      throw new E03RuntimeError("unknown_task", `unknown task ${taskId}`);
    return task;
  }

  list(parentTaskId?: string): E03TaskState[] {
    return Object.values(this.snapshotValue.tasks)
      .filter(
        (task) => !parentTaskId || task.identity.parentTaskId === parentTaskId,
      )
      .map(cloneJson)
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.identity.taskId.localeCompare(right.identity.taskId),
      );
  }

  snapshot(): E03RegistrySnapshot {
    return structuredClone(this.snapshotValue);
  }

  private effectRequest(
    proposal: MutationProposal,
    transitionId: string,
  ): E03EffectRequest {
    const payload = {
      effectId: `effect-${digest({ transitionId, idempotencyKey: proposal.idempotencyKey }).slice(0, 32)}`,
      requestId: proposal.requestId,
      taskId: proposal.taskId,
      leaseId: proposal.proposed.identity.leaseId,
      expectedRevision: proposal.expectedRevision,
      effectKind: proposal.effectKind!,
      operation: proposal.effectOperation ?? "persist_task_transition",
      payload: proposal.effectPayload ?? {},
      idempotencyKey: proposal.idempotencyKey,
      preparedAt: this.clock.now(),
    };
    return { ...payload, digest: digest(payload) };
  }

  private validateReceipt(
    request: E03EffectRequest,
    receipt: E03EffectReceipt,
  ): void {
    const { digest: receiptDigest, ...payload } = receipt;
    if (digest(payload) !== receiptDigest)
      throw new E03RuntimeError(
        "effect_receipt_checksum",
        `receipt ${receipt.receiptId} checksum mismatch`,
      );
    if (
      receipt.effectId !== request.effectId ||
      receipt.requestId !== request.requestId ||
      receipt.taskId !== request.taskId
    )
      throw new E03RuntimeError(
        "effect_receipt_identity",
        "effect receipt identity differs from request",
      );
    if (receipt.leaseId !== request.leaseId)
      throw new E03RuntimeError(
        "effect_receipt_lease",
        "effect receipt uses a stale lease",
      );
    if (receipt.expectedRevision !== request.expectedRevision)
      throw new E03RuntimeError(
        "effect_receipt_revision",
        "effect receipt uses a stale revision",
      );
  }
}

function sealTransition(
  value: Omit<E03Transition, "digest"> | E03Transition,
): E03Transition {
  const { digest: _digest, ...payload } = value as E03Transition;
  return { ...payload, digest: digest(payload) };
}

export function taskProjection(
  task: E03TaskState,
): import("../contracts.ts").JsonObject {
  return {
    task_id: task.identity.taskId,
    parent_task_id: task.identity.parentTaskId,
    session_id: task.identity.sessionId,
    run_id: task.identity.runId,
    lease_id: task.identity.leaseId,
    attempt: task.identity.attempt,
    lineage: task.identity.lineage,
    agent: task.definition.name,
    status: task.status,
    revision: task.revision,
    sequence: task.sequence,
    messages: task.messages.map((message) => ({
      message_id: message.messageId,
      sender_task_id: message.senderTaskId,
      recipient_task_id: message.recipientTaskId,
      kind: message.kind,
      body: message.body,
      sequence: message.sequence,
    })),
    deliveries: task.deliveries.map((delivery) => ({
      delivery_id: delivery.deliveryId,
      kind: delivery.kind,
      sequence: delivery.sequence,
      summary: delivery.summary,
    })),
    child_task_ids: task.childTaskIds,
    result: task.result,
    error: task.error,
    checksum: task.checksum,
  };
}

function inferCommand(eventType: string): E03ControlResponse["command"] {
  if (eventType.includes("cancel")) return "agent.cancel";
  if (eventType.includes("kill")) return "agent.kill";
  if (eventType.includes("steer") || eventType.includes("message"))
    return "agent.steer";
  if (eventType.includes("resume")) return "agent.resume";
  return "agent.create";
}

export type JournalRecordKind =
  | "task"
  | "transition"
  | "effect"
  | "request"
  | "recovery"
  | "checkpoint";

export interface TaskJournalRecord {
  journalId: string;
  sequence: number;
  registryRevision: number;
  taskId: string;
  taskRevision: number;
  leaseId: string;
  kind: JournalRecordKind;
  action: string;
  requestId: string;
  idempotencyKey: string;
  payloadDigest: string;
  payload: import("../contracts.ts").JsonObject;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

export interface TaskJournalCursor {
  sequence: number;
  digest: string;
}

export interface TaskJournalPage {
  records: TaskJournalRecord[];
  nextCursor: TaskJournalCursor | null;
  hasMore: boolean;
  headDigest: string;
}

export class DurableTaskJournal {
  private records: TaskJournalRecord[] = [];
  private readonly idempotency = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  appendTask(
    snapshot: E03RegistrySnapshot,
    task: E03TaskState,
    action: string,
    idempotencyKey: string,
  ): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    assertTaskChecksum(task);
    if (snapshot.tasks[task.identity.taskId]?.checksum !== task.checksum)
      throw new E03RuntimeError(
        "journal_task_not_committed",
        "journal task is not the committed registry state",
      );
    return this.append({
      registryRevision: snapshot.revision,
      taskId: task.identity.taskId,
      taskRevision: task.revision,
      leaseId: task.identity.leaseId,
      kind: "task",
      action,
      requestId:
        task.transitions.at(-1)?.requestId ??
        `task:${task.identity.taskId}:${task.revision}`,
      idempotencyKey,
      payload: taskProjection(task),
      payloadDigest: task.checksum,
    });
  }

  appendTransition(
    snapshot: E03RegistrySnapshot,
    transition: E03Transition,
  ): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    const task = snapshot.tasks[transition.taskId];
    if (!task)
      throw new E03RuntimeError(
        "journal_task_missing",
        `transition task ${transition.taskId} is not committed`,
      );
    const committed = task.transitions.find(
      (item) => item.transitionId === transition.transitionId,
    );
    if (!committed || committed.digest !== transition.digest)
      throw new E03RuntimeError(
        "journal_transition_not_committed",
        "transition is not present in committed task state",
      );
    return this.append({
      registryRevision: snapshot.revision,
      taskId: task.identity.taskId,
      taskRevision: transition.toRevision,
      leaseId: transition.leaseId,
      kind: "transition",
      action: transition.eventType,
      requestId: transition.requestId,
      idempotencyKey: transition.idempotencyKey,
      payload: transition as unknown as import("../contracts.ts").JsonObject,
      payloadDigest: transition.digest,
    });
  }

  appendEffect(
    snapshot: E03RegistrySnapshot,
    receipt: E03EffectReceipt,
    idempotencyKey: string,
  ): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    const committed = snapshot.effects[receipt.effectId];
    if (!committed || committed.digest !== receipt.digest)
      throw new E03RuntimeError(
        "journal_effect_not_committed",
        "effect receipt is not present in committed snapshot",
      );
    return this.append({
      registryRevision: snapshot.revision,
      taskId: receipt.taskId,
      taskRevision: receipt.expectedRevision + 1,
      leaseId: receipt.leaseId,
      kind: "effect",
      action: String(receipt.result.operation ?? "physical_effect"),
      requestId: receipt.requestId,
      idempotencyKey,
      payload: receipt as unknown as import("../contracts.ts").JsonObject,
      payloadDigest: receipt.digest,
    });
  }

  appendRequest(
    snapshot: E03RegistrySnapshot,
    idempotencyKey: string,
  ): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    const value = snapshot.requests[idempotencyKey];
    if (!value)
      throw new E03RuntimeError(
        "journal_request_missing",
        `request ${idempotencyKey} is not committed`,
      );
    const taskId = String(value.state?.task_id ?? "");
    const leaseId = String(value.state?.lease_id ?? "");
    return this.append({
      registryRevision: snapshot.revision,
      taskId,
      taskRevision: value.revision,
      leaseId,
      kind: "request",
      action: value.command,
      requestId: value.request_id,
      idempotencyKey,
      payload: value as unknown as import("../contracts.ts").JsonObject,
      payloadDigest: digest(value),
    });
  }

  checkpoint(snapshot: E03RegistrySnapshot, reason: string): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    return this.append({
      registryRevision: snapshot.revision,
      taskId: "",
      taskRevision: 0,
      leaseId: "",
      kind: "checkpoint",
      action: reason.trim() || "checkpoint",
      requestId: `checkpoint:${snapshot.revision}`,
      idempotencyKey: `checkpoint:${snapshot.revision}:${snapshot.checksum}`,
      payload: {
        snapshot_revision: snapshot.revision,
        snapshot_checksum: snapshot.checksum,
        task_count: Object.keys(snapshot.tasks).length,
        effect_count: Object.keys(snapshot.effects).length,
        request_count: Object.keys(snapshot.requests).length,
      },
      payloadDigest: snapshot.checksum,
    });
  }

  recovery(
    snapshot: E03RegistrySnapshot,
    input: {
      taskId: string;
      action: string;
      requestId: string;
      idempotencyKey: string;
      payload: import("../contracts.ts").JsonObject;
    },
  ): TaskJournalRecord {
    assertSnapshotChecksum(snapshot);
    const task = snapshot.tasks[input.taskId];
    if (!task)
      throw new E03RuntimeError(
        "journal_task_missing",
        `recovery task ${input.taskId} is absent`,
      );
    return this.append({
      registryRevision: snapshot.revision,
      taskId: task.identity.taskId,
      taskRevision: task.revision,
      leaseId: task.identity.leaseId,
      kind: "recovery",
      action: input.action,
      requestId: input.requestId,
      idempotencyKey: input.idempotencyKey,
      payload: structuredClone(input.payload),
      payloadDigest: digest(input.payload),
    });
  }

  page(input: {
    after?: TaskJournalCursor;
    limit?: number;
    taskId?: string;
    kinds?: readonly JournalRecordKind[];
  }): TaskJournalPage {
    const limit = input.limit ?? 100;
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10_000)
      throw new E03RuntimeError(
        "invalid_journal_limit",
        "journal page limit must be 1..10000",
      );
    if (input.after) this.assertCursor(input.after);
    const start = input.after ? input.after.sequence + 1 : 1;
    const kinds = input.kinds ? new Set(input.kinds) : null;
    const eligible = this.records.filter(
      (record) =>
        record.sequence >= start &&
        (!input.taskId || record.taskId === input.taskId) &&
        (!kinds || kinds.has(record.kind)),
    );
    const records = eligible
      .slice(0, limit)
      .map((record) => structuredClone(record));
    const last = records.at(-1);
    return {
      records,
      nextCursor: last
        ? { sequence: last.sequence, digest: last.digest }
        : (input.after ?? null),
      hasMore: eligible.length > records.length,
      headDigest: this.records.at(-1)?.digest ?? "",
    };
  }

  restore(records: readonly TaskJournalRecord[]): void {
    let previous = "";
    let sequence = 0;
    const keys = new Map<string, string>();
    for (const record of records) {
      this.assertRecord(record, previous, sequence + 1);
      const key = `${record.kind}:${record.idempotencyKey}`;
      const prior = keys.get(key);
      if (prior && prior !== record.payloadDigest)
        throw new E03RuntimeError(
          "journal_idempotency_conflict",
          `journal key ${key} changed payload`,
        );
      keys.set(key, record.payloadDigest);
      previous = record.digest;
      sequence = record.sequence;
    }
    this.records = records.map((record) => structuredClone(record));
    this.idempotency.clear();
    for (const [key, value] of keys) this.idempotency.set(key, value);
  }

  snapshot(): TaskJournalRecord[] {
    return this.records.map((record) => structuredClone(record));
  }

  verify(): {
    valid: true;
    records: number;
    headSequence: number;
    headDigest: string;
    taskIds: string[];
    registryRevision: number;
  } {
    let previous = "";
    let sequence = 0;
    let registryRevision = 0;
    const taskIds = new Set<string>();
    for (const record of this.records) {
      this.assertRecord(record, previous, sequence + 1);
      if (record.registryRevision < registryRevision)
        throw new E03RuntimeError(
          "journal_registry_regression",
          "journal registry revision regressed",
        );
      registryRevision = record.registryRevision;
      if (record.taskId) taskIds.add(record.taskId);
      previous = record.digest;
      sequence = record.sequence;
    }
    return {
      valid: true,
      records: this.records.length,
      headSequence: sequence,
      headDigest: previous,
      taskIds: [...taskIds].sort(),
      registryRevision,
    };
  }

  private append(
    input: Omit<
      TaskJournalRecord,
      "journalId" | "sequence" | "recordedAt" | "previousDigest" | "digest"
    >,
  ): TaskJournalRecord {
    if (
      !input.idempotencyKey.trim() ||
      !input.requestId.trim() ||
      !input.action.trim()
    )
      throw new E03RuntimeError(
        "invalid_journal_identity",
        "journal action, request and idempotency identities are required",
      );
    const key = `${input.kind}:${input.idempotencyKey}`;
    const priorDigest = this.idempotency.get(key);
    if (priorDigest) {
      const prior = this.records.find(
        (record) =>
          record.kind === input.kind &&
          record.idempotencyKey === input.idempotencyKey,
      )!;
      if (
        prior.payloadDigest !== input.payloadDigest ||
        prior.taskId !== input.taskId
      )
        throw new E03RuntimeError(
          "journal_idempotency_conflict",
          `journal key ${key} changed payload or task`,
        );
      return structuredClone(prior);
    }
    const sequence = this.records.length + 1;
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      journalId: `task-journal-${digest({ sequence, key, previousDigest }).slice(0, 32)}`,
      sequence,
      ...input,
      recordedAt: this.clock.now(),
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    this.records.push(record);
    this.idempotency.set(key, record.payloadDigest);
    return structuredClone(record);
  }

  private assertRecord(
    record: TaskJournalRecord,
    previousDigest: string,
    expectedSequence: number,
  ): void {
    const { digest: checksum, ...payload } = record;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "journal_digest_mismatch",
        `journal ${record.journalId} digest is invalid`,
      );
    if (record.previousDigest !== previousDigest)
      throw new E03RuntimeError(
        "journal_chain_mismatch",
        `journal ${record.journalId} does not extend prior digest`,
      );
    if (record.sequence !== expectedSequence)
      throw new E03RuntimeError(
        "journal_sequence_gap",
        `expected journal sequence ${expectedSequence}, got ${record.sequence}`,
      );
    if (
      !record.journalId ||
      !record.requestId ||
      !record.idempotencyKey ||
      !record.action ||
      !record.payloadDigest
    )
      throw new E03RuntimeError(
        "invalid_journal_record",
        "journal identity or payload binding is incomplete",
      );
    if (
      digest(record.payload) !== record.payloadDigest &&
      record.kind !== "task" &&
      record.kind !== "checkpoint"
    )
      throw new E03RuntimeError(
        "journal_payload_digest",
        `journal ${record.journalId} payload digest differs`,
      );
  }

  private assertCursor(cursor: TaskJournalCursor): void {
    if (!Number.isSafeInteger(cursor.sequence) || cursor.sequence < 0)
      throw new E03RuntimeError(
        "invalid_journal_cursor",
        "journal cursor sequence is invalid",
      );
    if (cursor.sequence === 0 && cursor.digest === "") return;
    const record = this.records[cursor.sequence - 1];
    if (!record || record.digest !== cursor.digest)
      throw new E03RuntimeError(
        "stale_journal_cursor",
        "journal cursor does not match canonical record",
      );
  }
}

export interface RegistryIndexSnapshot {
  registryRevision: number;
  taskChecksums: Record<string, string>;
  byRun: Record<string, string[]>;
  bySession: Record<string, string[]>;
  byParent: Record<string, string[]>;
  byStatus: Record<string, string[]>;
  byLease: Record<string, string>;
  byIdempotency: Record<string, string>;
  terminalTaskIds: string[];
  activeTaskIds: string[];
  digest: string;
}

export class TaskRegistryIndex {
  private value: RegistryIndexSnapshot | null = null;

  rebuild(snapshot: E03RegistrySnapshot): RegistryIndexSnapshot {
    assertSnapshotChecksum(snapshot);
    const taskChecksums: Record<string, string> = {};
    const byRun: Record<string, string[]> = {};
    const bySession: Record<string, string[]> = {};
    const byParent: Record<string, string[]> = {};
    const byStatus: Record<string, string[]> = {};
    const byLease: Record<string, string> = {};
    const byIdempotency: Record<string, string> = {};
    const terminalTaskIds: string[] = [];
    const activeTaskIds: string[] = [];
    for (const task of Object.values(snapshot.tasks).sort(compareTasks)) {
      assertTaskChecksum(task);
      taskChecksums[task.identity.taskId] = task.checksum;
      pushIndex(byRun, task.identity.runId, task.identity.taskId);
      pushIndex(bySession, task.identity.sessionId, task.identity.taskId);
      if (task.identity.parentSessionId !== task.identity.sessionId)
        pushIndex(
          bySession,
          task.identity.parentSessionId,
          task.identity.taskId,
        );
      pushIndex(byParent, task.identity.parentTaskId, task.identity.taskId);
      pushIndex(byStatus, task.status, task.identity.taskId);
      const leaseTask = byLease[task.identity.leaseId];
      if (leaseTask && leaseTask !== task.identity.taskId)
        throw new E03RuntimeError(
          "duplicate_task_lease",
          `lease ${task.identity.leaseId} belongs to ${leaseTask} and ${task.identity.taskId}`,
        );
      byLease[task.identity.leaseId] = task.identity.taskId;
      for (const transition of task.transitions) {
        const priorTask = byIdempotency[transition.idempotencyKey];
        if (priorTask && priorTask !== task.identity.taskId)
          throw new E03RuntimeError(
            "cross_task_idempotency_conflict",
            `idempotency key ${transition.idempotencyKey} crosses tasks`,
          );
        byIdempotency[transition.idempotencyKey] = task.identity.taskId;
      }
      if (isTerminal(task.status)) terminalTaskIds.push(task.identity.taskId);
      else activeTaskIds.push(task.identity.taskId);
    }
    for (const key of Object.keys(snapshot.requests)) {
      const taskId = String(snapshot.requests[key]!.state?.task_id ?? "");
      if (taskId) byIdempotency[key] = taskId;
    }
    const payload = {
      registryRevision: snapshot.revision,
      taskChecksums,
      byRun: sortIndex(byRun),
      bySession: sortIndex(bySession),
      byParent: sortIndex(byParent),
      byStatus: sortIndex(byStatus),
      byLease,
      byIdempotency,
      terminalTaskIds: terminalTaskIds.sort(),
      activeTaskIds: activeTaskIds.sort(),
    };
    this.value = { ...payload, digest: digest(payload) };
    return structuredClone(this.value);
  }

  restore(value: RegistryIndexSnapshot, snapshot: E03RegistrySnapshot): void {
    const { digest: checksum, ...payload } = value;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "registry_index_digest",
        "registry index digest is invalid",
      );
    if (value.registryRevision !== snapshot.revision)
      throw new E03RuntimeError(
        "stale_registry_index",
        "registry index revision differs from canonical snapshot",
      );
    const expected = this.rebuild(snapshot);
    if (expected.digest !== value.digest)
      throw new E03RuntimeError(
        "registry_index_mismatch",
        "restored registry index differs from canonical rebuild",
      );
    this.value = structuredClone(value);
  }

  snapshot(): RegistryIndexSnapshot {
    if (!this.value)
      throw new E03RuntimeError(
        "registry_index_uninitialized",
        "registry index has not been built",
      );
    return structuredClone(this.value);
  }

  taskForLease(leaseId: string): string | null {
    return this.snapshot().byLease[leaseId] ?? null;
  }

  taskForIdempotency(idempotencyKey: string): string | null {
    return this.snapshot().byIdempotency[idempotencyKey] ?? null;
  }

  tasksForRun(runId: string): string[] {
    return [...(this.snapshot().byRun[runId] ?? [])];
  }

  tasksForSession(sessionId: string): string[] {
    return [...(this.snapshot().bySession[sessionId] ?? [])];
  }

  children(parentTaskId: string): string[] {
    return [...(this.snapshot().byParent[parentTaskId] ?? [])];
  }

  byStatus(status: E03TaskState["status"]): string[] {
    return [...(this.snapshot().byStatus[status] ?? [])];
  }

  changedSince(snapshot: E03RegistrySnapshot): string[] {
    const index = this.snapshot();
    const changed = new Set<string>();
    for (const [taskId, task] of Object.entries(snapshot.tasks))
      if (index.taskChecksums[taskId] !== task.checksum) changed.add(taskId);
    for (const taskId of Object.keys(index.taskChecksums))
      if (!snapshot.tasks[taskId]) changed.add(taskId);
    return [...changed].sort();
  }
}

export type RecoveryActionKind =
  | "replay_ack"
  | "retry_effect"
  | "reject_orphan_effect"
  | "expire_lease"
  | "redeliver"
  | "resume_task"
  | "manual_conflict";

export interface TaskRecoveryAction {
  recoveryId: string;
  taskId: string;
  leaseId: string;
  taskRevision: number;
  kind: RecoveryActionKind;
  priority: number;
  idempotencyKey: string;
  reason: string;
  evidence: import("../contracts.ts").JsonObject;
  digest: string;
}

export interface TaskRecoveryPlan {
  planId: string;
  snapshotRevision: number;
  generatedAt: string;
  actions: TaskRecoveryAction[];
  blocked: TaskRecoveryAction[];
  digest: string;
}

export class TaskRegistryRecoveryPlanner {
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  plan(snapshot: E03RegistrySnapshot): TaskRecoveryPlan {
    assertSnapshotChecksum(snapshot);
    const actions: TaskRecoveryAction[] = [];
    const blocked: TaskRecoveryAction[] = [];
    const acknowledgedEffects = new Set(
      Object.values(snapshot.tasks).flatMap((task) =>
        task.transitions
          .map((transition) => transition.effectId)
          .filter((value): value is string => Boolean(value)),
      ),
    );
    for (const task of Object.values(snapshot.tasks)) {
      assertTaskChecksum(task);
      for (const transition of task.transitions) {
        if (
          transition.phase === "commit" &&
          !snapshot.requests[transition.idempotencyKey]
        )
          actions.push(
            this.action(
              task,
              "replay_ack",
              1000,
              transition.idempotencyKey,
              "commit exists without durable ACK",
              {
                transition_id: transition.transitionId,
                request_id: transition.requestId,
              },
            ),
          );
        if (transition.effectId && !snapshot.effects[transition.effectId])
          actions.push(
            this.action(
              task,
              "retry_effect",
              900,
              `${transition.idempotencyKey}:retry-effect`,
              "transition effect has no receipt",
              {
                transition_id: transition.transitionId,
                effect_id: transition.effectId,
              },
            ),
          );
      }
      const unacknowledged = task.deliveries.filter(
        (delivery) => !delivery.acknowledgedAt,
      );
      for (const delivery of unacknowledged) {
        const target =
          delivery.kind === "final" || delivery.kind === "error"
            ? actions
            : blocked;
        target.push(
          this.action(
            task,
            "redeliver",
            delivery.kind === "final" || delivery.kind === "error" ? 800 : 300,
            `${delivery.idempotencyKey}:recovery`,
            "delivery has no ACK",
            {
              delivery_id: delivery.deliveryId,
              delivery_kind: delivery.kind,
              sequence: delivery.sequence,
            },
          ),
        );
      }
      const deadline = Date.parse(task.definition.budget.deadlineAt);
      if (
        !isTerminal(task.status) &&
        Number.isFinite(deadline) &&
        deadline <= Date.parse(this.clock.now())
      )
        actions.push(
          this.action(
            task,
            "expire_lease",
            950,
            `expire:${task.identity.leaseId}:${task.revision}`,
            "active task deadline elapsed",
            {
              deadline_at: task.definition.budget.deadlineAt,
              status: task.status,
            },
          ),
        );
      if (
        (task.status === "waiting" || task.status === "failed") &&
        task.identity.attempt < 100
      )
        blocked.push(
          this.action(
            task,
            "resume_task",
            200,
            `resume-plan:${task.identity.taskId}:${task.revision}`,
            "task may be resumable but requires a caller policy",
            { status: task.status, attempt: task.identity.attempt },
          ),
        );
      if (task.isolationReceipt?.mergeConflict)
        blocked.push(
          this.action(
            task,
            "manual_conflict",
            700,
            `merge-conflict:${task.isolationReceipt.receiptId}`,
            "workspace merge conflict needs a deterministic resolution plan",
            {
              receipt_id: task.isolationReceipt.receiptId,
              workspace_path: task.isolationReceipt.workspacePath,
            },
          ),
        );
    }
    for (const receipt of Object.values(snapshot.effects)) {
      if (!acknowledgedEffects.has(receipt.effectId)) {
        const task = snapshot.tasks[receipt.taskId];
        const action = task
          ? this.action(
              task,
              "reject_orphan_effect",
              850,
              `orphan-effect:${receipt.effectId}`,
              "effect receipt has no task transition",
              { effect_id: receipt.effectId, receipt_id: receipt.receiptId },
            )
          : orphanAction(receipt);
        actions.push(action);
      }
    }
    actions.sort(compareRecovery);
    blocked.sort(compareRecovery);
    const payload = {
      planId: `task-recovery-${digest({ revision: snapshot.revision, actions: actions.map((action) => action.digest), blocked: blocked.map((action) => action.digest) }).slice(0, 32)}`,
      snapshotRevision: snapshot.revision,
      generatedAt: this.clock.now(),
      actions,
      blocked,
    };
    return { ...payload, digest: digest(payload) };
  }

  validate(plan: TaskRecoveryPlan, snapshot: E03RegistrySnapshot): void {
    const { digest: checksum, ...payload } = plan;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "recovery_plan_digest",
        `recovery plan ${plan.planId} digest is invalid`,
      );
    if (plan.snapshotRevision !== snapshot.revision)
      throw new E03RuntimeError(
        "stale_recovery_plan",
        "recovery plan targets another snapshot revision",
      );
    const ids = new Set<string>();
    for (const action of [...plan.actions, ...plan.blocked]) {
      assertRecoveryAction(action);
      if (ids.has(action.recoveryId))
        throw new E03RuntimeError(
          "duplicate_recovery_action",
          `duplicate recovery action ${action.recoveryId}`,
        );
      ids.add(action.recoveryId);
      const task = snapshot.tasks[action.taskId];
      if (
        task &&
        (task.identity.leaseId !== action.leaseId ||
          task.revision !== action.taskRevision)
      )
        throw new E03RuntimeError(
          "stale_recovery_action",
          `recovery action ${action.recoveryId} has stale task custody`,
        );
    }
  }

  executable(plan: TaskRecoveryPlan): TaskRecoveryAction[] {
    return plan.actions
      .filter(
        (action) =>
          action.kind !== "manual_conflict" && action.kind !== "resume_task",
      )
      .map((action) => structuredClone(action));
  }

  private action(
    task: E03TaskState,
    kind: RecoveryActionKind,
    priority: number,
    idempotencyKey: string,
    reason: string,
    evidence: import("../contracts.ts").JsonObject,
  ): TaskRecoveryAction {
    const payload = {
      recoveryId: `recovery-${digest({ taskId: task.identity.taskId, kind, idempotencyKey }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      taskRevision: task.revision,
      kind,
      priority,
      idempotencyKey,
      reason,
      evidence,
    };
    return { ...payload, digest: digest(payload) };
  }
}

export interface RegistryBatchMutation {
  batchItemId: string;
  taskId: string;
  expectedRevision: number;
  proposed: E03TaskState;
  idempotencyKey: string;
  dependencies: string[];
  readSet: Record<string, string>;
  writeSet: string[];
  digest: string;
}

export interface RegistryBatchPlan {
  batchId: string;
  snapshotRevision: number;
  ordered: RegistryBatchMutation[];
  readSet: Record<string, string>;
  writeSet: string[];
  digest: string;
}

export class RegistryBatchPlanner {
  plan(
    snapshot: E03RegistrySnapshot,
    mutations: readonly RegistryBatchMutation[],
  ): RegistryBatchPlan {
    assertSnapshotChecksum(snapshot);
    if (!mutations.length)
      throw new E03RuntimeError(
        "empty_registry_batch",
        "registry batch requires at least one mutation",
      );
    const byId = new Map<string, RegistryBatchMutation>();
    for (const mutation of mutations) {
      assertBatchMutation(mutation);
      if (byId.has(mutation.batchItemId))
        throw new E03RuntimeError(
          "duplicate_batch_item",
          `duplicate batch item ${mutation.batchItemId}`,
        );
      const current = snapshot.tasks[mutation.taskId];
      if (!current)
        throw new E03RuntimeError(
          "batch_task_missing",
          `batch task ${mutation.taskId} is absent`,
        );
      if (
        current.revision !== mutation.expectedRevision ||
        current.checksum !== mutation.readSet[mutation.taskId]
      )
        throw new E03RuntimeError(
          "batch_read_conflict",
          `batch item ${mutation.batchItemId} read set is stale`,
        );
      if (
        mutation.proposed.revision !== current.revision + 1 ||
        mutation.proposed.identity.leaseId !== current.identity.leaseId
      )
        throw new E03RuntimeError(
          "batch_write_conflict",
          `batch item ${mutation.batchItemId} proposed revision or lease is invalid`,
        );
      byId.set(mutation.batchItemId, structuredClone(mutation));
    }
    for (const mutation of mutations)
      for (const dependency of mutation.dependencies)
        if (!byId.has(dependency))
          throw new E03RuntimeError(
            "batch_dependency_missing",
            `batch dependency ${dependency} is absent`,
          );
    const ordered = topologicalMutations(byId);
    const writeOwner = new Map<string, string>();
    const readSet: Record<string, string> = {};
    const writeSet = new Set<string>();
    for (const mutation of ordered) {
      for (const [key, checksum] of Object.entries(mutation.readSet)) {
        if (readSet[key] && readSet[key] !== checksum)
          throw new E03RuntimeError(
            "batch_read_conflict",
            `batch read set conflicts for ${key}`,
          );
        readSet[key] = checksum;
      }
      for (const key of mutation.writeSet) {
        const owner = writeOwner.get(key);
        if (owner && !mutation.dependencies.includes(owner))
          throw new E03RuntimeError(
            "batch_write_conflict",
            `batch items ${owner} and ${mutation.batchItemId} both write ${key} without ordering`,
          );
        writeOwner.set(key, mutation.batchItemId);
        writeSet.add(key);
      }
    }
    const payload = {
      batchId: `registry-batch-${digest({ revision: snapshot.revision, items: ordered.map((item) => item.digest) }).slice(0, 32)}`,
      snapshotRevision: snapshot.revision,
      ordered,
      readSet,
      writeSet: [...writeSet].sort(),
    };
    return { ...payload, digest: digest(payload) };
  }

  apply(
    snapshot: E03RegistrySnapshot,
    plan: RegistryBatchPlan,
  ): E03RegistrySnapshot {
    const { digest: checksum, ...payload } = plan;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "registry_batch_digest",
        `batch ${plan.batchId} digest is invalid`,
      );
    if (plan.snapshotRevision !== snapshot.revision)
      throw new E03RuntimeError(
        "stale_registry_batch",
        "registry batch targets another snapshot revision",
      );
    const tasks = { ...snapshot.tasks };
    for (const mutation of plan.ordered) {
      const current = tasks[mutation.taskId];
      if (
        !current ||
        current.revision !== mutation.expectedRevision ||
        current.checksum !== mutation.readSet[mutation.taskId]
      )
        throw new E03RuntimeError(
          "batch_commit_conflict",
          `batch item ${mutation.batchItemId} lost its read fence`,
        );
      tasks[mutation.taskId] = structuredClone(mutation.proposed);
    }
    return sealSnapshot({
      ...snapshot,
      revision: snapshot.revision + 1,
      tasks,
      updatedAt: new Date().toISOString(),
      checksum: "",
    });
  }
}

function compareTasks(left: E03TaskState, right: E03TaskState): number {
  return (
    left.createdAt.localeCompare(right.createdAt) ||
    left.identity.taskId.localeCompare(right.identity.taskId)
  );
}

function pushIndex(
  index: Record<string, string[]>,
  key: string,
  taskId: string,
): void {
  const values = index[key] ?? [];
  if (!values.includes(taskId)) values.push(taskId);
  index[key] = values;
}

function sortIndex(index: Record<string, string[]>): Record<string, string[]> {
  return Object.fromEntries(
    Object.entries(index)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, values]) => [key, [...values].sort()]),
  );
}

function compareRecovery(
  left: TaskRecoveryAction,
  right: TaskRecoveryAction,
): number {
  return (
    right.priority - left.priority ||
    left.taskId.localeCompare(right.taskId) ||
    left.kind.localeCompare(right.kind) ||
    left.recoveryId.localeCompare(right.recoveryId)
  );
}

function assertRecoveryAction(action: TaskRecoveryAction): void {
  const { digest: checksum, ...payload } = action;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "recovery_action_digest",
      `recovery action ${action.recoveryId} digest is invalid`,
    );
  if (
    !action.recoveryId ||
    !action.taskId ||
    !action.leaseId ||
    !action.idempotencyKey ||
    !action.reason ||
    action.taskRevision < 1
  )
    throw new E03RuntimeError(
      "invalid_recovery_action",
      "recovery action custody is incomplete",
    );
}

function orphanAction(receipt: E03EffectReceipt): TaskRecoveryAction {
  const payload = {
    recoveryId: `recovery-${digest({ effectId: receipt.effectId, kind: "reject_orphan_effect" }).slice(0, 32)}`,
    taskId: receipt.taskId,
    leaseId: receipt.leaseId,
    taskRevision: receipt.expectedRevision + 1,
    kind: "reject_orphan_effect" as const,
    priority: 850,
    idempotencyKey: `orphan-effect:${receipt.effectId}`,
    reason: "effect receipt refers to a missing task",
    evidence: { effect_id: receipt.effectId, receipt_id: receipt.receiptId },
  };
  return { ...payload, digest: digest(payload) };
}

function assertBatchMutation(mutation: RegistryBatchMutation): void {
  const { digest: checksum, ...payload } = mutation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "batch_mutation_digest",
      `batch item ${mutation.batchItemId} digest is invalid`,
    );
  assertTaskChecksum(mutation.proposed);
  if (
    !mutation.batchItemId ||
    !mutation.taskId ||
    !mutation.idempotencyKey ||
    mutation.expectedRevision < 1
  )
    throw new E03RuntimeError(
      "invalid_batch_mutation",
      "batch item identity is incomplete",
    );
  if (
    mutation.taskId !== mutation.proposed.identity.taskId ||
    !mutation.writeSet.includes(mutation.taskId)
  )
    throw new E03RuntimeError(
      "invalid_batch_write_set",
      "batch item does not write its proposed task",
    );
}

function topologicalMutations(
  byId: Map<string, RegistryBatchMutation>,
): RegistryBatchMutation[] {
  const ordered: RegistryBatchMutation[] = [];
  const temporary = new Set<string>();
  const permanent = new Set<string>();
  const visit = (id: string): void => {
    if (permanent.has(id)) return;
    if (temporary.has(id))
      throw new E03RuntimeError(
        "batch_dependency_cycle",
        `batch dependency cycle at ${id}`,
      );
    temporary.add(id);
    const mutation = byId.get(id)!;
    for (const dependency of [...mutation.dependencies].sort())
      visit(dependency);
    temporary.delete(id);
    permanent.add(id);
    ordered.push(structuredClone(mutation));
  };
  for (const id of [...byId.keys()].sort()) visit(id);
  return ordered;
}

export interface InvariantFinding {
  code: string;
  taskId: string;
  path: string;
  detail: string;
  fatal: boolean;
}

export interface InvariantReport {
  valid: boolean;
  snapshotRevision: number;
  taskCount: number;
  transitionCount: number;
  effectCount: number;
  findings: InvariantFinding[];
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "killed"]);

export class TaskInvariantRuntime {
  assertTask(task: E03TaskState): void {
    const findings = this.inspectTask(task);
    const fatal = findings.find((finding) => finding.fatal);
    if (fatal) {
      throw new E03RuntimeError(fatal.code, fatal.detail, {
        taskId: fatal.taskId,
        path: fatal.path,
        findingCount: findings.length,
      });
    }
  }

  assertSnapshot(snapshot: E03RegistrySnapshot): void {
    const report = this.inspectSnapshot(snapshot);
    const fatal = report.findings.find((finding) => finding.fatal);
    if (fatal) {
      throw new E03RuntimeError(fatal.code, fatal.detail, {
        taskId: fatal.taskId,
        path: fatal.path,
        snapshotRevision: snapshot.revision,
        findingCount: report.findings.length,
      });
    }
  }

  inspectSnapshot(snapshot: E03RegistrySnapshot): InvariantReport {
    const findings: InvariantFinding[] = [];
    try {
      assertSnapshotChecksum(snapshot);
    } catch (error) {
      findings.push(
        this.finding(
          "snapshot_checksum_mismatch",
          "",
          "checksum",
          String(error),
        ),
      );
    }
    if (!Number.isSafeInteger(snapshot.revision) || snapshot.revision < 0) {
      findings.push(
        this.finding(
          "invalid_snapshot_revision",
          "",
          "revision",
          "snapshot revision must be a non-negative integer",
        ),
      );
    }
    if (
      !validTimestamp(snapshot.createdAt) ||
      !validTimestamp(snapshot.updatedAt)
    ) {
      findings.push(
        this.finding(
          "invalid_snapshot_timestamp",
          "",
          "createdAt/updatedAt",
          "snapshot timestamps must be valid ISO instants",
        ),
      );
    }
    if (
      validTimestamp(snapshot.createdAt) &&
      validTimestamp(snapshot.updatedAt) &&
      snapshot.createdAt > snapshot.updatedAt
    ) {
      findings.push(
        this.finding(
          "snapshot_time_reversed",
          "",
          "updatedAt",
          "snapshot update precedes creation",
        ),
      );
    }
    for (const [taskId, task] of Object.entries(snapshot.tasks)) {
      if (taskId !== task.identity.taskId) {
        findings.push(
          this.finding(
            "task_map_identity_mismatch",
            taskId,
            "identity.taskId",
            "task map key differs from canonical identity",
          ),
        );
      }
      findings.push(...this.inspectTask(task));
      const leaseOwner = snapshot.writerLeases[task.identity.leaseId];
      if (!leaseOwner) {
        findings.push(
          this.finding(
            "missing_writer_lease",
            taskId,
            "writerLeases",
            "committed task has no writer lease",
          ),
        );
      } else if (leaseOwner !== "typescript.E03AgentControlCoordinator") {
        findings.push(
          this.finding(
            "noncanonical_writer_lease",
            taskId,
            "writerLeases",
            `task lease belongs to ${leaseOwner}`,
          ),
        );
      }
      for (const childId of task.childTaskIds) {
        const child = snapshot.tasks[childId];
        if (!child) {
          findings.push(
            this.finding(
              "missing_child_task",
              taskId,
              "childTaskIds",
              `child ${childId} is absent from the registry`,
            ),
          );
        } else if (child.identity.parentTaskId !== taskId) {
          findings.push(
            this.finding(
              "child_parent_mismatch",
              taskId,
              "childTaskIds",
              `child ${childId} points to ${child.identity.parentTaskId}`,
            ),
          );
        }
      }
    }
    for (const [effectId, receipt] of Object.entries(snapshot.effects)) {
      findings.push(...this.inspectEffect(effectId, receipt, snapshot));
    }
    for (const [key, response] of Object.entries(snapshot.requests)) {
      if (!key.trim()) {
        findings.push(
          this.finding(
            "empty_request_key",
            "",
            "requests",
            "request idempotency key is empty",
          ),
        );
      }
      if (
        !response.request_id ||
        !response.command ||
        response.phase !== "ack"
      ) {
        findings.push(
          this.finding(
            "invalid_request_ack",
            "",
            `requests.${key}`,
            "durable request entry is not a typed ACK",
          ),
        );
      }
      if (
        response.runtime_origin !== "typescript.E03AgentControlCoordinator" ||
        response.python_logical_owner !== false
      ) {
        findings.push(
          this.finding(
            "request_owner_mismatch",
            "",
            `requests.${key}`,
            "request ACK does not identify the TypeScript owner",
          ),
        );
      }
    }
    for (const [name, definitions] of Object.entries(snapshot.definitions)) {
      for (const definition of definitions) {
        if (definition.name !== name) {
          findings.push(
            this.finding(
              "definition_map_mismatch",
              "",
              `definitions.${name}`,
              `definition payload is named ${definition.name}`,
            ),
          );
        }
        const { digest: checksum, ...payload } = definition;
        if (digest(payload) !== checksum) {
          findings.push(
            this.finding(
              "definition_digest_mismatch",
              "",
              `definitions.${name}`,
              "definition digest is invalid",
            ),
          );
        }
      }
    }
    return {
      valid: findings.every((finding) => !finding.fatal),
      snapshotRevision: snapshot.revision,
      taskCount: Object.keys(snapshot.tasks).length,
      transitionCount: Object.values(snapshot.tasks).reduce(
        (sum, task) => sum + task.transitions.length,
        0,
      ),
      effectCount: Object.keys(snapshot.effects).length,
      findings,
    };
  }

  inspectTask(task: E03TaskState): InvariantFinding[] {
    const findings: InvariantFinding[] = [];
    const taskId = task.identity.taskId;
    try {
      assertTaskChecksum(task);
    } catch (error) {
      findings.push(
        this.finding(
          "task_checksum_mismatch",
          taskId,
          "checksum",
          String(error),
        ),
      );
    }
    if (
      !taskId ||
      !task.identity.runId ||
      !task.identity.sessionId ||
      !task.identity.parentTaskId ||
      !task.identity.parentSessionId
    ) {
      findings.push(
        this.finding(
          "incomplete_task_identity",
          taskId,
          "identity",
          "task authority identity is incomplete",
        ),
      );
    }
    if (
      !task.identity.attemptId ||
      !task.identity.leaseId ||
      task.identity.attempt < 1
    ) {
      findings.push(
        this.finding(
          "invalid_attempt_identity",
          taskId,
          "identity.attempt",
          "attempt and lease identity must be positive and non-empty",
        ),
      );
    }
    if (
      !task.identity.lineage.length ||
      task.identity.lineage.at(-1) !== task.identity.parentTaskId
    ) {
      findings.push(
        this.finding(
          "invalid_task_lineage",
          taskId,
          "identity.lineage",
          "lineage must end at the direct parent",
        ),
      );
    }
    if (
      new Set(task.identity.lineage).size !== task.identity.lineage.length ||
      task.identity.lineage.includes(taskId)
    ) {
      findings.push(
        this.finding(
          "cyclic_task_lineage",
          taskId,
          "identity.lineage",
          "lineage contains a duplicate or the child itself",
        ),
      );
    }
    if (!Number.isSafeInteger(task.revision) || task.revision < 1) {
      findings.push(
        this.finding(
          "invalid_task_revision",
          taskId,
          "revision",
          "committed tasks start at revision one",
        ),
      );
    }
    if (
      !Number.isSafeInteger(task.sequence) ||
      task.sequence < 1 ||
      task.sequence < task.revision
    ) {
      findings.push(
        this.finding(
          "invalid_task_sequence",
          taskId,
          "sequence",
          "task sequence must cover every revision",
        ),
      );
    }
    if (!task.prompt.trim() || digest(task.prompt) !== task.promptDigest) {
      findings.push(
        this.finding(
          "prompt_digest_mismatch",
          taskId,
          "promptDigest",
          "task prompt is empty or its digest changed",
        ),
      );
    }
    if (task.definition.name === "" || task.definition.digest === "") {
      findings.push(
        this.finding(
          "invalid_task_definition",
          taskId,
          "definition",
          "task has no sealed agent definition",
        ),
      );
    }
    if (
      task.context.taskId !== taskId ||
      task.context.sessionId !== task.identity.sessionId
    ) {
      findings.push(
        this.finding(
          "task_context_authority_mismatch",
          taskId,
          "context",
          "context task/session differs from task identity",
        ),
      );
    }
    if (task.context.permissionDigest !== task.scope.permissionCeilingDigest) {
      findings.push(
        this.finding(
          "context_permission_mismatch",
          taskId,
          "context.permissionDigest",
          "context permission ceiling differs from task scope",
        ),
      );
    }
    if (task.executionMode === "background" && !task.scope.allowBackground) {
      findings.push(
        this.finding(
          "background_scope_violation",
          taskId,
          "executionMode",
          "task is background but its capability scope denies background execution",
        ),
      );
    }
    if (task.identity.lineage.length > task.scope.maxDepth) {
      findings.push(
        this.finding(
          "depth_scope_violation",
          taskId,
          "identity.lineage",
          "task lineage exceeds its capability depth ceiling",
        ),
      );
    }
    if (
      task.childTaskIds.length > task.scope.maxChildren ||
      new Set(task.childTaskIds).size !== task.childTaskIds.length
    ) {
      findings.push(
        this.finding(
          "child_scope_violation",
          taskId,
          "childTaskIds",
          "children exceed scope or contain duplicates",
        ),
      );
    }
    if (task.isolation && task.isolation.taskId !== taskId) {
      findings.push(
        this.finding(
          "isolation_task_mismatch",
          taskId,
          "isolation.taskId",
          "isolation request belongs to another task",
        ),
      );
    }
    if (task.isolation && task.isolation.leaseId !== task.identity.leaseId) {
      findings.push(
        this.finding(
          "isolation_lease_mismatch",
          taskId,
          "isolation.leaseId",
          "isolation request uses a stale lease",
        ),
      );
    }
    if (task.isolationReceipt && !task.isolation) {
      findings.push(
        this.finding(
          "orphan_isolation_receipt",
          taskId,
          "isolationReceipt",
          "isolation receipt has no matching request",
        ),
      );
    }
    if (task.isolationReceipt && task.isolation) {
      if (
        task.isolationReceipt.requestId !== task.isolation.requestId ||
        task.isolationReceipt.leaseId !== task.isolation.leaseId
      ) {
        findings.push(
          this.finding(
            "isolation_receipt_mismatch",
            taskId,
            "isolationReceipt",
            "isolation receipt does not match the request custody",
          ),
        );
      }
      if (!task.isolationReceipt.accepted && !task.isolationReceipt.error) {
        findings.push(
          this.finding(
            "unexplained_isolation_rejection",
            taskId,
            "isolationReceipt.error",
            "rejected isolation receipt has no reason",
          ),
        );
      }
    }
    findings.push(...this.inspectTransitions(task));
    findings.push(...this.inspectMessages(task));
    findings.push(...this.inspectDeliveries(task));
    if (isTerminal(task.status)) {
      if (!task.terminalAt || !validTimestamp(task.terminalAt)) {
        findings.push(
          this.finding(
            "terminal_timestamp_missing",
            taskId,
            "terminalAt",
            "terminal task has no valid terminal timestamp",
          ),
        );
      }
      if (task.status === "completed" && task.result === null) {
        findings.push(
          this.finding(
            "completed_result_missing",
            taskId,
            "result",
            "completed task has no typed result",
          ),
        );
      }
      if (task.status !== "completed" && !task.error.trim()) {
        findings.push(
          this.finding(
            "terminal_reason_missing",
            taskId,
            "error",
            `${task.status} task has no terminal reason`,
          ),
        );
      }
    } else if (task.terminalAt !== null) {
      findings.push(
        this.finding(
          "nonterminal_timestamp_present",
          taskId,
          "terminalAt",
          "nonterminal task carries a terminal timestamp",
        ),
      );
    }
    if (
      !validTimestamp(task.createdAt) ||
      !validTimestamp(task.updatedAt) ||
      task.createdAt > task.updatedAt
    ) {
      findings.push(
        this.finding(
          "invalid_task_timestamp",
          taskId,
          "createdAt/updatedAt",
          "task timestamps are invalid or reversed",
        ),
      );
    }
    return findings;
  }

  private inspectTransitions(task: E03TaskState): InvariantFinding[] {
    const findings: InvariantFinding[] = [];
    let revision = 0;
    let status: E03TaskState["status"] = "created";
    const ids = new Set<string>();
    const idempotency = new Map<string, string>();
    for (const [index, transition] of task.transitions.entries()) {
      const path = `transitions.${index}`;
      if (ids.has(transition.transitionId)) {
        findings.push(
          this.finding(
            "duplicate_transition",
            task.identity.taskId,
            path,
            "transition id is duplicated",
          ),
        );
      }
      ids.add(transition.transitionId);
      const priorDigest = idempotency.get(transition.idempotencyKey);
      if (priorDigest && priorDigest !== transition.digest) {
        findings.push(
          this.finding(
            "transition_idempotency_conflict",
            task.identity.taskId,
            path,
            "idempotency key refers to different transitions",
          ),
        );
      }
      idempotency.set(transition.idempotencyKey, transition.digest);
      const { digest: checksum, ...payload } = transition;
      if (digest(payload) !== checksum) {
        findings.push(
          this.finding(
            "transition_digest_mismatch",
            task.identity.taskId,
            path,
            "transition digest is invalid",
          ),
        );
      }
      if (
        transition.taskId !== task.identity.taskId ||
        transition.leaseId !== task.identity.leaseId
      ) {
        findings.push(
          this.finding(
            "transition_custody_mismatch",
            task.identity.taskId,
            path,
            "transition uses another task or lease",
          ),
        );
      }
      if (transition.writerId !== "typescript.E03AgentControlCoordinator") {
        findings.push(
          this.finding(
            "transition_writer_mismatch",
            task.identity.taskId,
            path,
            `transition writer is ${transition.writerId}`,
          ),
        );
      }
      if (
        transition.fromRevision !== revision ||
        transition.toRevision !== revision + 1
      ) {
        findings.push(
          this.finding(
            "transition_revision_gap",
            task.identity.taskId,
            path,
            `expected ${revision}->${revision + 1}`,
          ),
        );
      }
      if (index > 0 && transition.fromStatus !== status) {
        findings.push(
          this.finding(
            "transition_status_gap",
            task.identity.taskId,
            path,
            `expected source status ${status}`,
          ),
        );
      }
      if (transition.phase === "commit" || transition.phase === "ack") {
        if (!transition.committedAt) {
          findings.push(
            this.finding(
              "transition_commit_time_missing",
              task.identity.taskId,
              path,
              "committed transition lacks committedAt",
            ),
          );
        }
      }
      if (transition.phase === "ack" && !transition.acknowledgedAt) {
        findings.push(
          this.finding(
            "transition_ack_time_missing",
            task.identity.taskId,
            path,
            "ACK transition lacks acknowledgedAt",
          ),
        );
      }
      if (transition.receiptId && !transition.effectId) {
        findings.push(
          this.finding(
            "receipt_without_effect",
            task.identity.taskId,
            path,
            "transition has a receipt without an effect",
          ),
        );
      }
      revision = transition.toRevision;
      status = transition.toStatus;
    }
    if (task.transitions.length && revision !== task.revision) {
      findings.push(
        this.finding(
          "transition_head_mismatch",
          task.identity.taskId,
          "transitions",
          `transition head ${revision} differs from task revision ${task.revision}`,
        ),
      );
    }
    if (task.transitions.length && status !== task.status) {
      findings.push(
        this.finding(
          "transition_status_mismatch",
          task.identity.taskId,
          "transitions",
          `transition status ${status} differs from task status ${task.status}`,
        ),
      );
    }
    return findings;
  }

  private inspectMessages(task: E03TaskState): InvariantFinding[] {
    const findings: InvariantFinding[] = [];
    let sequence = 0;
    const ids = new Set<string>();
    const idempotency = new Map<string, string>();
    for (const [index, message] of task.messages.entries()) {
      const path = `messages.${index}`;
      this.inspectMessageDigest(task, message, path, findings);
      if (ids.has(message.messageId))
        findings.push(
          this.finding(
            "duplicate_message",
            task.identity.taskId,
            path,
            "message id is duplicated",
          ),
        );
      ids.add(message.messageId);
      if (message.sequence <= sequence)
        findings.push(
          this.finding(
            "message_sequence_reordered",
            task.identity.taskId,
            path,
            "message sequence is not strictly increasing",
          ),
        );
      sequence = message.sequence;
      const prior = idempotency.get(message.idempotencyKey);
      if (prior && prior !== message.digest)
        findings.push(
          this.finding(
            "message_idempotency_conflict",
            task.identity.taskId,
            path,
            "message idempotency key changed content",
          ),
        );
      idempotency.set(message.idempotencyKey, message.digest);
      if (
        message.recipientTaskId !== task.identity.taskId ||
        message.taskId !== task.identity.taskId
      )
        findings.push(
          this.finding(
            "message_recipient_mismatch",
            task.identity.taskId,
            path,
            "message is stored under the wrong recipient",
          ),
        );
      if (message.acknowledgedAt && !message.deliveredAt)
        findings.push(
          this.finding(
            "message_ack_before_delivery",
            task.identity.taskId,
            path,
            "message was acknowledged before delivery",
          ),
        );
      if (message.deliveredAt && message.deliveredAt < message.createdAt)
        findings.push(
          this.finding(
            "message_delivery_time_reversed",
            task.identity.taskId,
            path,
            "message delivery precedes creation",
          ),
        );
    }
    return findings;
  }

  private inspectMessageDigest(
    task: E03TaskState,
    message: E03Message,
    path: string,
    findings: InvariantFinding[],
  ): void {
    const { digest: checksum, ...payload } = message;
    if (digest(payload) !== checksum)
      findings.push(
        this.finding(
          "message_digest_mismatch",
          task.identity.taskId,
          path,
          "message digest is invalid",
        ),
      );
    if (!message.body.trim() || !message.idempotencyKey.trim())
      findings.push(
        this.finding(
          "invalid_message_content",
          task.identity.taskId,
          path,
          "message body or idempotency key is empty",
        ),
      );
  }

  private inspectDeliveries(task: E03TaskState): InvariantFinding[] {
    const findings: InvariantFinding[] = [];
    let sequence = 0;
    let finalSeen = false;
    const ids = new Set<string>();
    const idempotency = new Map<string, string>();
    for (const [index, delivery] of task.deliveries.entries()) {
      const path = `deliveries.${index}`;
      this.inspectDeliveryDigest(task, delivery, path, findings);
      if (ids.has(delivery.deliveryId))
        findings.push(
          this.finding(
            "duplicate_delivery",
            task.identity.taskId,
            path,
            "delivery id is duplicated",
          ),
        );
      ids.add(delivery.deliveryId);
      if (delivery.sequence <= sequence)
        findings.push(
          this.finding(
            "delivery_sequence_reordered",
            task.identity.taskId,
            path,
            "delivery sequence is not strictly increasing",
          ),
        );
      sequence = delivery.sequence;
      const prior = idempotency.get(delivery.idempotencyKey);
      if (prior && prior !== delivery.digest)
        findings.push(
          this.finding(
            "delivery_idempotency_conflict",
            task.identity.taskId,
            path,
            "delivery idempotency key changed content",
          ),
        );
      idempotency.set(delivery.idempotencyKey, delivery.digest);
      if (finalSeen)
        findings.push(
          this.finding(
            "delivery_after_final",
            task.identity.taskId,
            path,
            "delivery was appended after a final/error record",
          ),
        );
      if (delivery.kind === "final" || delivery.kind === "error")
        finalSeen = true;
      if (delivery.taskId !== task.identity.taskId)
        findings.push(
          this.finding(
            "delivery_task_mismatch",
            task.identity.taskId,
            path,
            "delivery belongs to another task",
          ),
        );
    }
    return findings;
  }

  private inspectDeliveryDigest(
    task: E03TaskState,
    delivery: E03Delivery,
    path: string,
    findings: InvariantFinding[],
  ): void {
    const { digest: checksum, ...payload } = delivery;
    if (digest(payload) !== checksum)
      findings.push(
        this.finding(
          "delivery_digest_mismatch",
          task.identity.taskId,
          path,
          "delivery digest is invalid",
        ),
      );
    if (!delivery.summary.trim() || !delivery.idempotencyKey.trim())
      findings.push(
        this.finding(
          "invalid_delivery_content",
          task.identity.taskId,
          path,
          "delivery summary or idempotency key is empty",
        ),
      );
  }

  private inspectEffect(
    effectId: string,
    receipt: E03EffectReceipt,
    snapshot: E03RegistrySnapshot,
  ): InvariantFinding[] {
    const findings: InvariantFinding[] = [];
    const { digest: checksum, ...payload } = receipt;
    if (effectId !== receipt.effectId)
      findings.push(
        this.finding(
          "effect_map_identity_mismatch",
          receipt.taskId,
          `effects.${effectId}`,
          "effect map key differs from receipt",
        ),
      );
    if (digest(payload) !== checksum)
      findings.push(
        this.finding(
          "effect_digest_mismatch",
          receipt.taskId,
          `effects.${effectId}`,
          "effect receipt digest is invalid",
        ),
      );
    const task = snapshot.tasks[receipt.taskId];
    if (!task)
      findings.push(
        this.finding(
          "orphan_effect_receipt",
          receipt.taskId,
          `effects.${effectId}`,
          "effect receipt refers to a missing task",
        ),
      );
    else if (task.identity.leaseId !== receipt.leaseId)
      findings.push(
        this.finding(
          "effect_lease_mismatch",
          receipt.taskId,
          `effects.${effectId}`,
          "effect receipt uses a stale lease",
        ),
      );
    if (!receipt.accepted && !receipt.error.trim())
      findings.push(
        this.finding(
          "effect_rejection_reason_missing",
          receipt.taskId,
          `effects.${effectId}`,
          "rejected effect has no error",
        ),
      );
    return findings;
  }

  private finding(
    code: string,
    taskId: string,
    path: string,
    detail: string,
    fatal = true,
  ): InvariantFinding {
    return { code, taskId, path, detail, fatal };
  }
}

function validTimestamp(value: string): boolean {
  return Boolean(value && Number.isFinite(Date.parse(value)));
}

export type RegistryCheckpointState =
  | "writing"
  | "sealed"
  | "verified"
  | "superseded"
  | "corrupt";

export interface RegistryCheckpointChunk {
  chunkId: string;
  checkpointId: string;
  ordinal: number;
  kind:
    | "tasks"
    | "requests"
    | "effects"
    | "definitions"
    | "writer-leases"
    | "metadata";
  itemKeys: string[];
  itemCount: number;
  payload: unknown;
  payloadDigest: string;
  previousChunkDigest: string;
  digest: string;
}

export interface RegistryCheckpoint {
  checkpointId: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  sourceRevision: number;
  state: RegistryCheckpointState;
  chunkIds: string[];
  taskCount: number;
  requestCount: number;
  effectCount: number;
  definitionCount: number;
  writerLeaseCount: number;
  sourceChecksum: string;
  rootChunkDigest: string;
  createdAt: string;
  sealedAt: string | null;
  verifiedAt: string | null;
  supersededAt: string | null;
  revision: number;
  digest: string;
}

function assertRegistryCheckpointChunk(
  chunk: RegistryCheckpointChunk,
  previousChunkDigest: string,
): void {
  const { digest: checksum, ...payload } = chunk;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_digest",
      `registry checkpoint chunk ${chunk.chunkId} digest is invalid`,
    );
  if (!chunk.chunkId || !chunk.checkpointId || !chunk.payloadDigest)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_identity",
      "registry checkpoint chunk identity is incomplete",
    );
  if (!Number.isSafeInteger(chunk.ordinal) || chunk.ordinal < 0)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_ordinal",
      "registry checkpoint chunk ordinal is invalid",
    );
  if (!Number.isSafeInteger(chunk.itemCount) || chunk.itemCount < 0)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_count",
      "registry checkpoint chunk item count is invalid",
    );
  if (chunk.itemCount !== chunk.itemKeys.length)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_keys",
      "registry checkpoint chunk item count differs from keys",
    );
  if (digest(chunk.payload) !== chunk.payloadDigest)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_payload",
      `registry checkpoint chunk ${chunk.chunkId} payload is corrupt`,
    );
  if (chunk.previousChunkDigest !== previousChunkDigest)
    throw new E03RuntimeError(
      "registry_checkpoint_chunk_chain",
      `registry checkpoint chunk ${chunk.chunkId} does not extend prior chunk`,
    );
}

function assertRegistryCheckpoint(checkpoint: RegistryCheckpoint): void {
  const { digest: checksum, ...payload } = checkpoint;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "registry_checkpoint_digest",
      `registry checkpoint ${checkpoint.checkpointId} digest is invalid`,
    );
  if (
    !checkpoint.checkpointId ||
    !checkpoint.runId ||
    !checkpoint.sessionId ||
    !checkpoint.parentTaskId ||
    !checkpoint.sourceChecksum
  )
    throw new E03RuntimeError(
      "registry_checkpoint_identity",
      "registry checkpoint identity is incomplete",
    );
  for (const value of [
    checkpoint.sourceRevision,
    checkpoint.taskCount,
    checkpoint.requestCount,
    checkpoint.effectCount,
    checkpoint.definitionCount,
    checkpoint.writerLeaseCount,
  ])
    if (!Number.isSafeInteger(value) || value < 0)
      throw new E03RuntimeError(
        "registry_checkpoint_counter",
        "registry checkpoint counter is invalid",
      );
  if (!Number.isSafeInteger(checkpoint.revision) || checkpoint.revision < 1)
    throw new E03RuntimeError(
      "registry_checkpoint_revision",
      "registry checkpoint revision is invalid",
    );
  if (checkpoint.state === "sealed" && checkpoint.sealedAt === null)
    throw new E03RuntimeError(
      "registry_checkpoint_state",
      "sealed registry checkpoint requires sealedAt",
    );
  if (checkpoint.state === "verified" && checkpoint.verifiedAt === null)
    throw new E03RuntimeError(
      "registry_checkpoint_state",
      "verified registry checkpoint requires verifiedAt",
    );
}

export class RegistryCheckpointRuntime {
  private checkpoints = new Map<string, RegistryCheckpoint>();
  private chunks = new Map<string, RegistryCheckpointChunk>();
  private latestVerifiedBySession = new Map<string, string>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumItemsPerChunk = 128,
  ) {
    if (!Number.isSafeInteger(maximumItemsPerChunk) || maximumItemsPerChunk < 1)
      throw new E03RuntimeError(
        "registry_checkpoint_chunk_limit",
        "registry checkpoint chunk limit is invalid",
      );
  }

  write(
    snapshot: E03RegistrySnapshot,
    identity: { runId: string; sessionId: string; parentTaskId: string },
  ): RegistryCheckpoint {
    assertSnapshotChecksum(snapshot);
    const checkpointId = createId("registry-checkpoint");
    const chunks: RegistryCheckpointChunk[] = [];
    let previousChunkDigest = "root";
    const append = (
      kind: RegistryCheckpointChunk["kind"],
      entries: readonly (readonly [string, unknown])[],
    ): void => {
      for (
        let offset = 0;
        offset < entries.length;
        offset += this.maximumItemsPerChunk
      ) {
        const batch = entries.slice(offset, offset + this.maximumItemsPerChunk);
        const payloadValue = Object.fromEntries(batch);
        const payload = {
          chunkId: createId("registry-checkpoint-chunk"),
          checkpointId,
          ordinal: chunks.length,
          kind,
          itemKeys: batch.map(([key]) => key),
          itemCount: batch.length,
          payload: payloadValue,
          payloadDigest: digest(payloadValue),
          previousChunkDigest,
        };
        const chunk = { ...payload, digest: digest(payload) };
        assertRegistryCheckpointChunk(chunk, previousChunkDigest);
        chunks.push(chunk);
        previousChunkDigest = chunk.digest;
      }
    };
    append("tasks", Object.entries(snapshot.tasks));
    append("requests", Object.entries(snapshot.requests));
    append("effects", Object.entries(snapshot.effects));
    append("definitions", Object.entries(snapshot.definitions));
    append("writer-leases", Object.entries(snapshot.writerLeases));
    append("metadata", [
      [
        "identity",
        {
          schemaVersion: snapshot.schemaVersion,
          revision: snapshot.revision,
          createdAt: snapshot.createdAt,
          updatedAt: snapshot.updatedAt,
        },
      ],
    ]);
    const createdAt = this.clock.now();
    const checkpointPayload = {
      checkpointId,
      runId: identity.runId,
      sessionId: identity.sessionId,
      parentTaskId: identity.parentTaskId,
      sourceRevision: snapshot.revision,
      state: "writing" as const,
      chunkIds: chunks.map((chunk) => chunk.chunkId),
      taskCount: Object.keys(snapshot.tasks).length,
      requestCount: Object.keys(snapshot.requests).length,
      effectCount: Object.keys(snapshot.effects).length,
      definitionCount: Object.keys(snapshot.definitions).length,
      writerLeaseCount: Object.keys(snapshot.writerLeases).length,
      sourceChecksum: snapshot.checksum,
      rootChunkDigest: previousChunkDigest,
      createdAt,
      sealedAt: null,
      verifiedAt: null,
      supersededAt: null,
      revision: 1,
    };
    const checkpoint = {
      ...checkpointPayload,
      digest: digest(checkpointPayload),
    };
    assertRegistryCheckpoint(checkpoint);
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    for (const chunk of chunks) this.chunks.set(chunk.chunkId, chunk);
    return structuredClone(checkpoint);
  }

  seal(checkpointId: string, expectedRevision: number): RegistryCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "writing")
      throw new E03RuntimeError(
        "registry_checkpoint_seal_state",
        `registry checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    const chunks = this.orderedChunks(checkpoint);
    if (chunks.length !== checkpoint.chunkIds.length)
      throw new E03RuntimeError(
        "registry_checkpoint_chunk_missing",
        `registry checkpoint ${checkpointId} is incomplete`,
      );
    let previous = "root";
    for (const chunk of chunks) {
      assertRegistryCheckpointChunk(chunk, previous);
      previous = chunk.digest;
    }
    if (previous !== checkpoint.rootChunkDigest)
      throw new E03RuntimeError(
        "registry_checkpoint_root",
        `registry checkpoint ${checkpointId} root digest is invalid`,
      );
    return this.transition(checkpoint, {
      state: "sealed",
      sealedAt: this.clock.now(),
    });
  }

  verify(checkpointId: string, expectedRevision: number): RegistryCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "sealed")
      throw new E03RuntimeError(
        "registry_checkpoint_verify_state",
        `registry checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    const restored = this.assemble(checkpoint);
    if (restored.checksum !== checkpoint.sourceChecksum)
      return this.transition(checkpoint, { state: "corrupt" });
    const priorId = this.latestVerifiedBySession.get(checkpoint.sessionId);
    if (priorId) {
      const prior = this.requireCheckpoint(priorId);
      if (prior.sourceRevision >= checkpoint.sourceRevision)
        throw new E03RuntimeError(
          "registry_checkpoint_revision_regression",
          `registry checkpoint revision ${checkpoint.sourceRevision} does not advance ${prior.sourceRevision}`,
        );
      this.transition(prior, {
        state: "superseded",
        supersededAt: this.clock.now(),
      });
    }
    const verified = this.transition(checkpoint, {
      state: "verified",
      verifiedAt: this.clock.now(),
    });
    this.latestVerifiedBySession.set(
      checkpoint.sessionId,
      checkpoint.checkpointId,
    );
    return verified;
  }

  restoreLatest(sessionId: string): E03RegistrySnapshot {
    const checkpointId = this.latestVerifiedBySession.get(sessionId);
    if (!checkpointId)
      throw new E03RuntimeError(
        "registry_checkpoint_latest_missing",
        `session ${sessionId} has no verified registry checkpoint`,
      );
    const checkpoint = this.requireCheckpoint(checkpointId);
    if (checkpoint.state !== "verified")
      throw new E03RuntimeError(
        "registry_checkpoint_latest_state",
        `latest registry checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    return this.assemble(checkpoint);
  }

  get(checkpointId: string): RegistryCheckpoint {
    return structuredClone(this.requireCheckpoint(checkpointId));
  }

  list(sessionId?: string): RegistryCheckpoint[] {
    return [...this.checkpoints.values()]
      .filter((checkpoint) => !sessionId || checkpoint.sessionId === sessionId)
      .sort(
        (left, right) =>
          left.sourceRevision - right.sourceRevision ||
          left.createdAt.localeCompare(right.createdAt),
      )
      .map((checkpoint) => structuredClone(checkpoint));
  }

  snapshot(): {
    checkpoints: RegistryCheckpoint[];
    chunks: RegistryCheckpointChunk[];
  } {
    return {
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      chunks: [...this.chunks.values()].map((value) => structuredClone(value)),
    };
  }

  restore(input: {
    checkpoints: readonly RegistryCheckpoint[];
    chunks: readonly RegistryCheckpointChunk[];
  }): void {
    const checkpoints = new Map<string, RegistryCheckpoint>();
    const chunks = new Map<string, RegistryCheckpointChunk>();
    const latestVerifiedBySession = new Map<string, string>();
    for (const checkpoint of input.checkpoints) {
      assertRegistryCheckpoint(checkpoint);
      if (checkpoints.has(checkpoint.checkpointId))
        throw new E03RuntimeError(
          "registry_checkpoint_restore_duplicate",
          `duplicate registry checkpoint ${checkpoint.checkpointId}`,
        );
      checkpoints.set(checkpoint.checkpointId, structuredClone(checkpoint));
      if (checkpoint.state === "verified") {
        const priorId = latestVerifiedBySession.get(checkpoint.sessionId);
        const prior = priorId ? checkpoints.get(priorId) : undefined;
        if (!prior || checkpoint.sourceRevision > prior.sourceRevision)
          latestVerifiedBySession.set(
            checkpoint.sessionId,
            checkpoint.checkpointId,
          );
      }
    }
    const byCheckpoint = new Map<string, RegistryCheckpointChunk[]>();
    for (const chunk of input.chunks) {
      const checkpoint = checkpoints.get(chunk.checkpointId);
      if (!checkpoint)
        throw new E03RuntimeError(
          "registry_checkpoint_chunk_restore_orphan",
          `registry checkpoint chunk ${chunk.chunkId} has no checkpoint`,
        );
      if (chunks.has(chunk.chunkId))
        throw new E03RuntimeError(
          "registry_checkpoint_chunk_restore_duplicate",
          `duplicate registry checkpoint chunk ${chunk.chunkId}`,
        );
      chunks.set(chunk.chunkId, structuredClone(chunk));
      byCheckpoint.set(chunk.checkpointId, [
        ...(byCheckpoint.get(chunk.checkpointId) ?? []),
        chunk,
      ]);
    }
    for (const checkpoint of checkpoints.values()) {
      const values = (byCheckpoint.get(checkpoint.checkpointId) ?? []).sort(
        (left, right) => left.ordinal - right.ordinal,
      );
      let previous = "root";
      for (let index = 0; index < values.length; index += 1) {
        const chunk = values[index]!;
        if (chunk.ordinal !== index)
          throw new E03RuntimeError(
            "registry_checkpoint_chunk_restore_gap",
            `registry checkpoint ${checkpoint.checkpointId} has a chunk gap`,
          );
        assertRegistryCheckpointChunk(chunk, previous);
        previous = chunk.digest;
      }
      if (
        checkpoint.state !== "writing" &&
        (values.length !== checkpoint.chunkIds.length ||
          previous !== checkpoint.rootChunkDigest)
      )
        throw new E03RuntimeError(
          "registry_checkpoint_restore_incomplete",
          `registry checkpoint ${checkpoint.checkpointId} is incomplete`,
        );
    }
    this.checkpoints = checkpoints;
    this.chunks = chunks;
    this.latestVerifiedBySession = latestVerifiedBySession;
  }

  private assemble(checkpoint: RegistryCheckpoint): E03RegistrySnapshot {
    const values: Record<
      RegistryCheckpointChunk["kind"],
      Record<string, unknown>
    > = {
      tasks: {},
      requests: {},
      effects: {},
      definitions: {},
      "writer-leases": {},
      metadata: {},
    };
    let previous = "root";
    for (const chunk of this.orderedChunks(checkpoint)) {
      assertRegistryCheckpointChunk(chunk, previous);
      Object.assign(
        values[chunk.kind],
        chunk.payload as Record<string, unknown>,
      );
      previous = chunk.digest;
    }
    const identity = values.metadata.identity as Omit<
      E03RegistrySnapshot,
      | "tasks"
      | "requests"
      | "effects"
      | "definitions"
      | "writerLeases"
      | "checksum"
    >;
    const unsigned = {
      ...identity,
      tasks: values.tasks as E03RegistrySnapshot["tasks"],
      requests: values.requests as E03RegistrySnapshot["requests"],
      effects: values.effects as E03RegistrySnapshot["effects"],
      definitions: values.definitions as E03RegistrySnapshot["definitions"],
      writerLeases: values[
        "writer-leases"
      ] as E03RegistrySnapshot["writerLeases"],
    };
    const snapshot = { ...unsigned, checksum: digest(unsigned) };
    assertSnapshotChecksum(snapshot);
    if (
      Object.keys(snapshot.tasks).length !== checkpoint.taskCount ||
      Object.keys(snapshot.requests).length !== checkpoint.requestCount ||
      Object.keys(snapshot.effects).length !== checkpoint.effectCount ||
      Object.keys(snapshot.definitions).length !== checkpoint.definitionCount ||
      Object.keys(snapshot.writerLeases).length !== checkpoint.writerLeaseCount
    )
      throw new E03RuntimeError(
        "registry_checkpoint_count_mismatch",
        `registry checkpoint ${checkpoint.checkpointId} item counts are invalid`,
      );
    return snapshot;
  }

  private orderedChunks(
    checkpoint: RegistryCheckpoint,
  ): RegistryCheckpointChunk[] {
    return checkpoint.chunkIds.map((chunkId) => {
      const chunk = this.chunks.get(chunkId);
      if (!chunk)
        throw new E03RuntimeError(
          "registry_checkpoint_chunk_missing",
          `registry checkpoint chunk ${chunkId} does not exist`,
        );
      return chunk;
    });
  }

  private requireCheckpoint(checkpointId: string): RegistryCheckpoint {
    const checkpoint = this.checkpoints.get(checkpointId);
    if (!checkpoint)
      throw new E03RuntimeError(
        "registry_checkpoint_missing",
        `registry checkpoint ${checkpointId} does not exist`,
      );
    assertRegistryCheckpoint(checkpoint);
    return checkpoint;
  }

  private assertRevision(
    checkpoint: RegistryCheckpoint,
    expected: number,
  ): void {
    if (checkpoint.revision !== expected)
      throw new E03RuntimeError(
        "registry_checkpoint_stale_revision",
        `registry checkpoint ${checkpoint.checkpointId} revision is stale`,
      );
  }

  private transition(
    checkpoint: RegistryCheckpoint,
    patch: Partial<
      Omit<RegistryCheckpoint, "checkpointId" | "revision" | "digest">
    >,
  ): RegistryCheckpoint {
    const { digest: _, ...prior } = checkpoint;
    const payload = {
      ...prior,
      ...patch,
      checkpointId: checkpoint.checkpointId,
      revision: checkpoint.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRegistryCheckpoint(next);
    this.checkpoints.set(next.checkpointId, next);
    return structuredClone(next);
  }
}

export function assertNoLateRevival(
  before: E03TaskState,
  after: E03TaskState,
): void {
  if (TERMINAL.has(before.status) && after.checksum !== before.checksum) {
    throw new E03RuntimeError(
      "late_terminal_mutation",
      `${before.status} task ${before.identity.taskId} cannot be mutated`,
    );
  }
  if (before.identity.leaseId !== after.identity.leaseId) {
    throw new E03RuntimeError(
      "stale_lease",
      "task mutation changed the active lease",
    );
  }
  if (after.revision !== before.revision + 1) {
    throw new E03RuntimeError(
      "non_monotonic_revision",
      "task mutation must increment revision exactly once",
    );
  }
}

export function assertTransitionReceiptBinding(
  transition: E03Transition,
  receipt: E03EffectReceipt | null,
): void {
  if (!transition.effectId && receipt)
    throw new E03RuntimeError(
      "unexpected_effect_receipt",
      "non-effect transition received a physical receipt",
    );
  if (transition.effectId && !receipt)
    throw new E03RuntimeError(
      "missing_effect_receipt",
      "effect transition is missing its physical receipt",
    );
  if (!receipt) return;
  if (
    transition.effectId !== receipt.effectId ||
    transition.taskId !== receipt.taskId ||
    transition.leaseId !== receipt.leaseId
  ) {
    throw new E03RuntimeError(
      "effect_receipt_custody_mismatch",
      "effect receipt does not match transition custody",
    );
  }
  if (transition.toRevision !== receipt.expectedRevision + 1) {
    throw new E03RuntimeError(
      "effect_receipt_revision_mismatch",
      "effect receipt revision does not fence the transition",
    );
  }
}
