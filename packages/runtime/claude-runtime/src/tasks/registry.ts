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
  private persistenceQueue: Promise<void> = Promise.resolve();
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
      const leaseRotated =
        proposal.proposed.identity.leaseId !== existing.identity.leaseId;
      const validResumeLeaseRotation =
        leaseRotated &&
        proposal.effectOperation === "persist_agent_task_resume" &&
        (existing.status === "waiting" || existing.status === "failed") &&
        proposal.proposed.status === "queued" &&
        proposal.proposed.identity.runId === existing.identity.runId &&
        proposal.proposed.identity.sessionId === existing.identity.sessionId &&
        proposal.proposed.identity.taskId === existing.identity.taskId &&
        proposal.proposed.identity.parentTaskId ===
          existing.identity.parentTaskId &&
        proposal.proposed.identity.parentSessionId ===
          existing.identity.parentSessionId &&
        proposal.proposed.identity.attempt === existing.identity.attempt + 1 &&
        proposal.proposed.identity.attemptId ===
          `${existing.identity.taskId}:attempt:${existing.identity.attempt + 1}` &&
        proposal.proposed.identity.lineage.length ===
          existing.identity.lineage.length &&
        proposal.proposed.identity.lineage.every(
          (entry, index) => entry === existing.identity.lineage[index],
        );
      if (leaseRotated && !validResumeLeaseRotation)
        throw new E03RuntimeError(
          "stale_lease",
          "proposed mutation changed the active task lease outside a valid resume attempt",
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
            fromStatus: existing?.status ?? ("created" as const),
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
    return this.serializePersistence(() =>
      this.commitSerialized(idempotencyKey, receipt),
    );
  }

  private async commitSerialized(
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

  async acknowledgeDurably(
    idempotencyKey: string,
    response: E03ControlResponse,
  ): Promise<E03ControlResponse> {
    return this.serializePersistence(async () => {
      const acknowledged = this.acknowledge(idempotencyKey, response);
      if (acknowledged.replayed) return acknowledged;
      const expectedRevision = this.snapshotValue.revision;
      const persistedSnapshot = sealSnapshot({
        ...this.snapshotValue,
        revision: expectedRevision + 1,
        updatedAt: this.clock.now(),
        checksum: "",
      });
      this.invariants.assertSnapshot(persistedSnapshot);
      const persisted = await this.port.compareAndSwap(
        expectedRevision,
        persistedSnapshot,
      );
      if (!persisted.accepted)
        throw new E03RuntimeError(
          persisted.error === "stale_revision"
            ? "stale_registry_revision"
            : "registry_ack_persist_rejected",
          persisted.error || "durable acknowledgement CAS rejected",
          { expected: expectedRevision, actual: persisted.revision },
        );
      this.snapshotValue = persistedSnapshot;
      this.index.rebuild(persistedSnapshot);
      return acknowledged;
    });
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
    const acknowledged = {
      ...response,
      phase: "ack" as const,
      revision: Math.max(response.revision, committed.state.revision),
      state: response.state ?? taskProjection(committed.state),
      replayed: false,
    };
    this.snapshotValue = sealSnapshot({
      ...this.snapshotValue,
      requests: {
        ...this.snapshotValue.requests,
        [idempotencyKey]: acknowledged,
      },
      updatedAt: acknowledgedAt,
      checksum: "",
    });
    this.acknowledgements.set(idempotencyKey, acknowledged);
    return structuredClone(acknowledged);
  }

  private async serializePersistence<T>(operation: () => Promise<T>): Promise<T> {
    const prior = this.persistenceQueue;
    let release!: () => void;
    this.persistenceQueue = new Promise<void>((resolve) => {
      release = resolve;
    });
    await prior;
    try {
      return await operation();
    } finally {
      release();
    }
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
    physical_dispatch: task.physicalDispatch,
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
    let activeLeaseId = task.transitions[0]?.leaseId ?? task.identity.leaseId;
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
      if (transition.taskId !== task.identity.taskId) {
        findings.push(
          this.finding(
            "transition_custody_mismatch",
            task.identity.taskId,
            path,
            "transition uses another task",
          ),
        );
      }
      if (transition.leaseId !== activeLeaseId) {
        const validResumeBoundary =
          transition.eventType === "persist_agent_task_resume" &&
          (transition.fromStatus === "waiting" ||
            transition.fromStatus === "failed") &&
          transition.toStatus === "queued";
        if (validResumeBoundary) activeLeaseId = transition.leaseId;
        else
          findings.push(
            this.finding(
              "transition_custody_mismatch",
              task.identity.taskId,
              path,
              "transition changes lease outside a resume boundary",
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
    if (task.transitions.length && activeLeaseId !== task.identity.leaseId) {
      findings.push(
        this.finding(
          "transition_custody_mismatch",
          task.identity.taskId,
          "transitions",
          "transition lease head differs from task lease",
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
    let priorFinalAt = "";
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
      if (finalSeen) {
        const resumed = task.transitions.some(
          (transition) =>
            transition.eventType === "persist_agent_task_resume" &&
            transition.committedAt !== null &&
            transition.committedAt > priorFinalAt &&
            transition.committedAt <= delivery.createdAt,
        );
        if (resumed) finalSeen = false;
        else
          findings.push(
            this.finding(
              "delivery_after_final",
              task.identity.taskId,
              path,
              "delivery was appended after a final/error record without a resume boundary",
            ),
          );
      }
      if (delivery.kind === "final" || delivery.kind === "error") {
        finalSeen = true;
        priorFinalAt = delivery.createdAt;
      }
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
    else if (
      !task.transitions.some(
        (transition) =>
          transition.effectId === receipt.effectId &&
          transition.leaseId === receipt.leaseId,
      )
    )
      findings.push(
        this.finding(
          "effect_lease_mismatch",
          receipt.taskId,
          `effects.${effectId}`,
          "effect receipt has no matching transition lease",
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

export interface TaskRegistryReplica {
  replicaId: string;
  endpointId: string;
  state: "joining" | "active" | "lagging" | "draining" | "removed";
  appliedRevision: number;
  advertisedRevision: number;
  generation: number;
  leaseExpiresAt: string;
  lastHeartbeatAt: string | null;
  joinedAt: string;
  removedAt: string | null;
  revision: number;
  digest: string;
}
export interface RegistryReplicationEnvelope {
  envelopeId: string;
  generation: number;
  sourceRevision: number;
  targetRevision: number;
  state: "prepared" | "published" | "committed" | "rejected" | "expired";
  snapshotChecksum: string;
  mutationDigests: string[];
  requiredReplicaIds: string[];
  acknowledgedReplicaIds: string[];
  rejectedReplicaIds: string[];
  preparedAt: string;
  publishedAt: string | null;
  committedAt: string | null;
  expiresAt: string;
  revision: number;
  digest: string;
}
export interface RegistryReplicationAck {
  ackId: string;
  envelopeId: string;
  replicaId: string;
  outcome: "applied" | "rejected";
  appliedRevision: number;
  snapshotChecksum: string;
  reason: string | null;
  acknowledgedAt: string;
  digest: string;
}
function assertRegistryReplica(value: TaskRegistryReplica): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "registry_replica_digest",
      `registry replica ${value.replicaId} is corrupt`,
    );
  if (
    !value.replicaId ||
    !value.endpointId ||
    !Number.isSafeInteger(value.appliedRevision) ||
    value.appliedRevision < 0 ||
    !Number.isSafeInteger(value.advertisedRevision) ||
    value.advertisedRevision < value.appliedRevision ||
    !Number.isSafeInteger(value.generation) ||
    value.generation < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.leaseExpiresAt))
  )
    throw new E03RuntimeError(
      "registry_replica",
      `registry replica ${value.replicaId} is invalid`,
    );
}
function assertRegistryReplicationEnvelope(
  value: RegistryReplicationEnvelope,
): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "registry_replication_envelope_digest",
      `registry replication envelope ${value.envelopeId} is corrupt`,
    );
  if (
    !value.envelopeId ||
    !Number.isSafeInteger(value.generation) ||
    value.generation < 1 ||
    !Number.isSafeInteger(value.sourceRevision) ||
    value.sourceRevision < 0 ||
    !Number.isSafeInteger(value.targetRevision) ||
    value.targetRevision <= value.sourceRevision ||
    !value.snapshotChecksum ||
    !value.requiredReplicaIds.length ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "registry_replication_envelope",
      `registry replication envelope ${value.envelopeId} is invalid`,
    );
}
function assertRegistryReplicationAck(value: RegistryReplicationAck): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "registry_replication_ack_digest",
      `registry replication ACK ${value.ackId} is corrupt`,
    );
  if (
    !value.ackId ||
    !value.envelopeId ||
    !value.replicaId ||
    !Number.isSafeInteger(value.appliedRevision) ||
    value.appliedRevision < 0 ||
    !value.snapshotChecksum ||
    (value.outcome === "rejected" && !value.reason)
  )
    throw new E03RuntimeError(
      "registry_replication_ack",
      `registry replication ACK ${value.ackId} is invalid`,
    );
}
export class TaskRegistryReplicationRuntime {
  private replicas = new Map<string, TaskRegistryReplica>();
  private envelopes = new Map<string, RegistryReplicationEnvelope>();
  private acknowledgements = new Map<string, RegistryReplicationAck[]>();
  private snapshots = new Map<number, E03RegistrySnapshot>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  join(input: {
    endpointId: string;
    appliedRevision: number;
    generation: number;
    ttlMs: number;
  }): TaskRegistryReplica {
    if (
      !input.endpointId.trim() ||
      !Number.isSafeInteger(input.appliedRevision) ||
      input.appliedRevision < 0 ||
      !Number.isSafeInteger(input.generation) ||
      input.generation < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "registry_replica_join",
        "registry replica join input is invalid",
      );
    const existing = [...this.replicas.values()].find(
      (value) =>
        value.endpointId === input.endpointId && value.state !== "removed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      replicaId: createId("registry-replica"),
      endpointId: input.endpointId.trim(),
      state: "joining" as const,
      appliedRevision: input.appliedRevision,
      advertisedRevision: input.appliedRevision,
      generation: input.generation,
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      lastHeartbeatAt: null,
      joinedAt: this.clock.now(),
      removedAt: null,
      revision: 1,
    };
    const replica = { ...payload, digest: digest(payload) };
    assertRegistryReplica(replica);
    this.replicas.set(replica.replicaId, replica);
    return structuredClone(replica);
  }
  activate(
    replicaId: string,
    expectedRevision: number,
    advertisedRevision: number,
  ): TaskRegistryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state !== "joining" && replica.state !== "lagging")
      throw new E03RuntimeError(
        "registry_replica_activate_state",
        `registry replica ${replicaId} is ${replica.state}`,
      );
    if (
      !Number.isSafeInteger(advertisedRevision) ||
      advertisedRevision < replica.appliedRevision
    )
      throw new E03RuntimeError(
        "registry_replica_advertised_revision",
        "registry replica advertised revision is invalid",
      );
    return this.transitionReplica(replica, {
      advertisedRevision,
      state:
        advertisedRevision === replica.appliedRevision ? "active" : "lagging",
      lastHeartbeatAt: this.clock.now(),
    });
  }
  heartbeat(
    replicaId: string,
    expectedRevision: number,
    advertisedRevision: number,
    ttlMs: number,
  ): TaskRegistryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state === "removed" || replica.state === "draining")
      throw new E03RuntimeError(
        "registry_replica_heartbeat_state",
        `registry replica ${replicaId} is ${replica.state}`,
      );
    if (
      !Number.isSafeInteger(advertisedRevision) ||
      advertisedRevision < replica.appliedRevision ||
      !Number.isSafeInteger(ttlMs) ||
      ttlMs < 1
    )
      throw new E03RuntimeError(
        "registry_replica_heartbeat",
        "registry replica heartbeat is invalid",
      );
    return this.transitionReplica(replica, {
      advertisedRevision,
      state:
        advertisedRevision > replica.appliedRevision
          ? "lagging"
          : replica.state,
      lastHeartbeatAt: this.clock.now(),
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + ttlMs,
      ).toISOString(),
    });
  }
  prepare(input: {
    snapshot: E03RegistrySnapshot;
    sourceRevision: number;
    generation: number;
    mutationDigests: readonly string[];
    requiredReplicaIds: readonly string[];
    ttlMs: number;
  }): RegistryReplicationEnvelope {
    assertSnapshotChecksum(input.snapshot);
    if (
      input.snapshot.revision <= input.sourceRevision ||
      !Number.isSafeInteger(input.generation) ||
      input.generation < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "registry_replication_prepare",
        "registry replication prepare input is invalid",
      );
    const requiredReplicaIds = [...new Set(input.requiredReplicaIds)].sort();
    if (!requiredReplicaIds.length)
      throw new E03RuntimeError(
        "registry_replication_replicas",
        "registry replication requires replicas",
      );
    for (const id of requiredReplicaIds) {
      const replica = this.requireReplica(id);
      if (
        replica.generation !== input.generation ||
        (replica.state !== "active" && replica.state !== "lagging")
      )
        throw new E03RuntimeError(
          "registry_replication_replica_state",
          `registry replica ${id} cannot receive envelope`,
        );
    }
    const mutationDigests = [...new Set(input.mutationDigests)];
    const existing = [...this.envelopes.values()].find(
      (value) =>
        value.generation === input.generation &&
        value.sourceRevision === input.sourceRevision &&
        value.targetRevision === input.snapshot.revision &&
        value.snapshotChecksum === input.snapshot.checksum &&
        value.state !== "rejected" &&
        value.state !== "expired",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      envelopeId: createId("registry-replication-envelope"),
      generation: input.generation,
      sourceRevision: input.sourceRevision,
      targetRevision: input.snapshot.revision,
      state: "prepared" as const,
      snapshotChecksum: input.snapshot.checksum,
      mutationDigests,
      requiredReplicaIds,
      acknowledgedReplicaIds: [],
      rejectedReplicaIds: [],
      preparedAt: this.clock.now(),
      publishedAt: null,
      committedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      revision: 1,
    };
    const envelope = { ...payload, digest: digest(payload) };
    assertRegistryReplicationEnvelope(envelope);
    this.envelopes.set(envelope.envelopeId, envelope);
    this.snapshots.set(
      envelope.targetRevision,
      structuredClone(input.snapshot),
    );
    return structuredClone(envelope);
  }
  publish(
    envelopeId: string,
    expectedRevision: number,
  ): RegistryReplicationEnvelope {
    const envelope = this.requireEnvelope(envelopeId);
    this.assertEnvelopeRevision(envelope, expectedRevision);
    if (envelope.state !== "prepared")
      throw new E03RuntimeError(
        "registry_replication_publish_state",
        `registry replication envelope ${envelopeId} is ${envelope.state}`,
      );
    if (Date.parse(envelope.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionEnvelope(envelope, { state: "expired" });
    return this.transitionEnvelope(envelope, {
      state: "published",
      publishedAt: this.clock.now(),
    });
  }
  acknowledge(input: {
    envelopeId: string;
    expectedRevision: number;
    replicaId: string;
    appliedRevision: number;
    snapshotChecksum: string;
    reason?: string | null;
  }): {
    envelope: RegistryReplicationEnvelope;
    ack: RegistryReplicationAck;
    replica: TaskRegistryReplica;
  } {
    const envelope = this.requireEnvelope(input.envelopeId);
    this.assertEnvelopeRevision(envelope, input.expectedRevision);
    if (envelope.state !== "published")
      throw new E03RuntimeError(
        "registry_replication_ack_state",
        `registry replication envelope ${envelope.envelopeId} is ${envelope.state}`,
      );
    if (!envelope.requiredReplicaIds.includes(input.replicaId))
      throw new E03RuntimeError(
        "registry_replication_ack_replica",
        `registry replica ${input.replicaId} is not required`,
      );
    const prior = (this.acknowledgements.get(envelope.envelopeId) ?? []).find(
      (value) => value.replicaId === input.replicaId,
    );
    if (prior)
      return {
        envelope: structuredClone(envelope),
        ack: structuredClone(prior),
        replica: structuredClone(this.requireReplica(input.replicaId)),
      };
    const applied =
      input.appliedRevision === envelope.targetRevision &&
      input.snapshotChecksum === envelope.snapshotChecksum;
    const payload = {
      ackId: createId("registry-replication-ack"),
      envelopeId: envelope.envelopeId,
      replicaId: input.replicaId,
      outcome: applied ? ("applied" as const) : ("rejected" as const),
      appliedRevision: input.appliedRevision,
      snapshotChecksum: input.snapshotChecksum,
      reason: applied
        ? null
        : input.reason?.trim() || "replica snapshot mismatch",
      acknowledgedAt: this.clock.now(),
    };
    const ack = { ...payload, digest: digest(payload) };
    assertRegistryReplicationAck(ack);
    const entries = this.acknowledgements.get(envelope.envelopeId) ?? [];
    entries.push(ack);
    this.acknowledgements.set(envelope.envelopeId, entries);
    const acknowledgedReplicaIds = applied
      ? [...envelope.acknowledgedReplicaIds, input.replicaId].sort()
      : envelope.acknowledgedReplicaIds;
    const rejectedReplicaIds = applied
      ? envelope.rejectedReplicaIds
      : [...envelope.rejectedReplicaIds, input.replicaId].sort();
    const complete =
      acknowledgedReplicaIds.length + rejectedReplicaIds.length ===
      envelope.requiredReplicaIds.length;
    const nextEnvelope = this.transitionEnvelope(envelope, {
      state: rejectedReplicaIds.length
        ? "rejected"
        : complete
          ? "committed"
          : "published",
      acknowledgedReplicaIds,
      rejectedReplicaIds,
      committedAt:
        complete && !rejectedReplicaIds.length ? this.clock.now() : null,
    });
    const replica = this.requireReplica(input.replicaId);
    const nextReplica = this.transitionReplica(
      replica,
      applied
        ? {
            appliedRevision: envelope.targetRevision,
            advertisedRevision: Math.max(
              replica.advertisedRevision,
              envelope.targetRevision,
            ),
            state: "active",
          }
        : { state: "lagging" },
    );
    return {
      envelope: nextEnvelope,
      ack: structuredClone(ack),
      replica: nextReplica,
    };
  }
  materialize(envelopeId: string): E03RegistrySnapshot {
    const envelope = this.requireEnvelope(envelopeId);
    if (envelope.state !== "committed")
      throw new E03RuntimeError(
        "registry_replication_materialize_state",
        `registry replication envelope ${envelopeId} is ${envelope.state}`,
      );
    const snapshot = this.snapshots.get(envelope.targetRevision);
    if (!snapshot || snapshot.checksum !== envelope.snapshotChecksum)
      throw new E03RuntimeError(
        "registry_replication_snapshot_missing",
        `registry replication snapshot ${envelope.targetRevision} is missing`,
      );
    assertSnapshotChecksum(snapshot);
    return structuredClone(snapshot);
  }
  drain(replicaId: string, expectedRevision: number): TaskRegistryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state !== "active" && replica.state !== "lagging")
      throw new E03RuntimeError(
        "registry_replica_drain_state",
        `registry replica ${replicaId} is ${replica.state}`,
      );
    return this.transitionReplica(replica, { state: "draining" });
  }
  remove(replicaId: string, expectedRevision: number): TaskRegistryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (
      replica.state !== "draining" &&
      Date.parse(replica.leaseExpiresAt) > Date.parse(this.clock.now())
    )
      throw new E03RuntimeError(
        "registry_replica_remove_state",
        `registry replica ${replicaId} is ${replica.state}`,
      );
    if (
      [...this.envelopes.values()].some(
        (value) =>
          value.requiredReplicaIds.includes(replicaId) &&
          (value.state === "prepared" || value.state === "published"),
      )
    )
      throw new E03RuntimeError(
        "registry_replica_pending_envelope",
        `registry replica ${replicaId} has pending envelope`,
      );
    return this.transitionReplica(replica, {
      state: "removed",
      removedAt: this.clock.now(),
    });
  }
  snapshot(): {
    replicas: TaskRegistryReplica[];
    envelopes: RegistryReplicationEnvelope[];
    acknowledgements: RegistryReplicationAck[];
    snapshots: E03RegistrySnapshot[];
  } {
    return {
      replicas: [...this.replicas.values()].map((value) =>
        structuredClone(value),
      ),
      envelopes: [...this.envelopes.values()].map((value) =>
        structuredClone(value),
      ),
      acknowledgements: [...this.acknowledgements.values()]
        .flat()
        .map((value) => structuredClone(value)),
      snapshots: [...this.snapshots.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    replicas: readonly TaskRegistryReplica[];
    envelopes: readonly RegistryReplicationEnvelope[];
    acknowledgements: readonly RegistryReplicationAck[];
    snapshots: readonly E03RegistrySnapshot[];
  }): void {
    const replicas = new Map<string, TaskRegistryReplica>();
    const envelopes = new Map<string, RegistryReplicationEnvelope>();
    const acknowledgements = new Map<string, RegistryReplicationAck[]>();
    const snapshots = new Map<number, E03RegistrySnapshot>();
    for (const value of snapshot.replicas) {
      assertRegistryReplica(value);
      if (replicas.has(value.replicaId))
        throw new E03RuntimeError(
          "registry_replica_restore_duplicate",
          `duplicate registry replica ${value.replicaId}`,
        );
      replicas.set(value.replicaId, structuredClone(value));
    }
    for (const value of snapshot.snapshots) {
      assertSnapshotChecksum(value);
      if (snapshots.has(value.revision))
        throw new E03RuntimeError(
          "registry_replication_snapshot_duplicate",
          `duplicate registry snapshot revision ${value.revision}`,
        );
      snapshots.set(value.revision, structuredClone(value));
    }
    for (const value of snapshot.envelopes) {
      assertRegistryReplicationEnvelope(value);
      const stored = snapshots.get(value.targetRevision);
      if (
        envelopes.has(value.envelopeId) ||
        value.requiredReplicaIds.some((id) => !replicas.has(id)) ||
        !stored ||
        stored.checksum !== value.snapshotChecksum
      )
        throw new E03RuntimeError(
          "registry_replication_envelope_restore",
          `registry replication envelope ${value.envelopeId} is invalid`,
        );
      envelopes.set(value.envelopeId, structuredClone(value));
    }
    for (const value of snapshot.acknowledgements) {
      assertRegistryReplicationAck(value);
      if (!envelopes.has(value.envelopeId) || !replicas.has(value.replicaId))
        throw new E03RuntimeError(
          "registry_replication_ack_restore",
          `registry replication ACK ${value.ackId} is invalid`,
        );
      const entries = acknowledgements.get(value.envelopeId) ?? [];
      if (entries.some((entry) => entry.replicaId === value.replicaId))
        throw new E03RuntimeError(
          "registry_replication_ack_restore_duplicate",
          `duplicate registry replication ACK for ${value.replicaId}`,
        );
      entries.push(structuredClone(value));
      acknowledgements.set(value.envelopeId, entries);
    }
    this.replicas = replicas;
    this.envelopes = envelopes;
    this.acknowledgements = acknowledgements;
    this.snapshots = snapshots;
  }
  private requireReplica(id: string): TaskRegistryReplica {
    const value = this.replicas.get(id);
    if (!value)
      throw new E03RuntimeError(
        "registry_replica_missing",
        `registry replica ${id} does not exist`,
      );
    assertRegistryReplica(value);
    return value;
  }
  private requireEnvelope(id: string): RegistryReplicationEnvelope {
    const value = this.envelopes.get(id);
    if (!value)
      throw new E03RuntimeError(
        "registry_replication_envelope_missing",
        `registry replication envelope ${id} does not exist`,
      );
    assertRegistryReplicationEnvelope(value);
    return value;
  }
  private assertReplicaRevision(
    value: TaskRegistryReplica,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "registry_replica_stale_revision",
        `registry replica ${value.replicaId} revision is stale`,
      );
  }
  private assertEnvelopeRevision(
    value: RegistryReplicationEnvelope,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "registry_replication_envelope_stale_revision",
        `registry replication envelope ${value.envelopeId} revision is stale`,
      );
  }
  private transitionReplica(
    value: TaskRegistryReplica,
    patch: Partial<
      Omit<TaskRegistryReplica, "replicaId" | "revision" | "digest">
    >,
  ): TaskRegistryReplica {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      replicaId: value.replicaId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRegistryReplica(next);
    this.replicas.set(next.replicaId, next);
    return structuredClone(next);
  }
  private transitionEnvelope(
    value: RegistryReplicationEnvelope,
    patch: Partial<
      Omit<RegistryReplicationEnvelope, "envelopeId" | "revision" | "digest">
    >,
  ): RegistryReplicationEnvelope {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      envelopeId: value.envelopeId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRegistryReplicationEnvelope(next);
    this.envelopes.set(next.envelopeId, next);
    return structuredClone(next);
  }
}

export interface TaskChangeEvent {
  eventId: string;
  partition: number;
  offset: number;
  taskId: string;
  leaseId: string;
  taskRevision: number;
  kind: "created" | "transition" | "effect" | "ack" | "snapshot" | "tombstone";
  payloadDigest: string;
  writerId: string;
  committedAt: string;
  previousPartitionDigest: string;
  digest: string;
}

export interface TaskChangeConsumer {
  consumerId: string;
  groupId: string;
  ownerId: string;
  partitions: number[];
  leaseExpiresAt: string;
  generation: number;
  state: "joining" | "active" | "revoking" | "left" | "expired";
  lastHeartbeatAt: string;
  revision: number;
  digest: string;
}

export interface TaskChangeCheckpoint {
  checkpointId: string;
  groupId: string;
  consumerId: string;
  partition: number;
  committedOffset: number;
  eventDigest: string;
  generation: number;
  committedAt: string;
  revision: number;
  digest: string;
}

export interface TaskChangeDelivery {
  deliveryId: string;
  consumerId: string;
  eventId: string;
  partition: number;
  offset: number;
  state: "offered" | "acknowledged" | "rejected" | "expired";
  attempt: number;
  offeredAt: string;
  deadlineAt: string;
  settledAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface TaskChangeFeedSnapshot {
  partitionCount: number;
  events: TaskChangeEvent[];
  consumers: TaskChangeConsumer[];
  checkpoints: TaskChangeCheckpoint[];
  deliveries: TaskChangeDelivery[];
  activeConsumerByOwner: [string, string][];
  activeDeliveryByConsumerEvent: [string, string][];
  groupGeneration: [string, number][];
}

function assertTaskChangeEvent(
  value: TaskChangeEvent,
  partitionCount: number,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.eventId ||
    value.partition < 0 ||
    value.partition >= partitionCount ||
    value.offset < 0 ||
    !value.taskId ||
    !value.leaseId ||
    value.taskRevision < 1 ||
    !value.payloadDigest ||
    !value.writerId ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_change_event_corrupt",
      `task change event ${value.eventId || "<empty>"} is corrupt`,
    );
}

function assertTaskChangeConsumer(
  value: TaskChangeConsumer,
  partitionCount: number,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.consumerId ||
    !value.groupId ||
    !value.ownerId ||
    value.generation < 1 ||
    value.revision < 1 ||
    new Set(value.partitions).size !== value.partitions.length ||
    value.partitions.some(
      (partition) => partition < 0 || partition >= partitionCount,
    ) ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_change_consumer_corrupt",
      `task change consumer ${value.consumerId || "<empty>"} is corrupt`,
    );
}

function assertTaskChangeCheckpoint(
  value: TaskChangeCheckpoint,
  partitionCount: number,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.checkpointId ||
    !value.groupId ||
    !value.consumerId ||
    value.partition < 0 ||
    value.partition >= partitionCount ||
    value.committedOffset < -1 ||
    value.generation < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_change_checkpoint_corrupt",
      `task change checkpoint ${value.checkpointId || "<empty>"} is corrupt`,
    );
}

function assertTaskChangeDelivery(
  value: TaskChangeDelivery,
  partitionCount: number,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.deliveryId ||
    !value.consumerId ||
    !value.eventId ||
    value.partition < 0 ||
    value.partition >= partitionCount ||
    value.offset < 0 ||
    value.attempt < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_change_delivery_corrupt",
      `task change delivery ${value.deliveryId || "<empty>"} is corrupt`,
    );
}

export class DurableTaskChangeFeed {
  private readonly partitionCount: number;
  private events = new Map<number, TaskChangeEvent[]>();
  private consumers = new Map<string, TaskChangeConsumer>();
  private checkpoints = new Map<string, TaskChangeCheckpoint>();
  private deliveries = new Map<string, TaskChangeDelivery>();
  private activeConsumerByOwner = new Map<string, string>();
  private activeDeliveryByConsumerEvent = new Map<string, string>();
  private groupGeneration = new Map<string, number>();

  constructor(
    partitionCount: number,
    private readonly clock: E03Clock = new SystemE03Clock(),
  ) {
    if (!Number.isSafeInteger(partitionCount) || partitionCount < 1)
      throw new E03RuntimeError(
        "task_change_partition_count",
        "task change feed partition count must be positive",
      );
    this.partitionCount = partitionCount;
    for (let partition = 0; partition < partitionCount; partition += 1)
      this.events.set(partition, []);
  }

  append(input: {
    eventId?: string;
    taskId: string;
    leaseId: string;
    taskRevision: number;
    kind: TaskChangeEvent["kind"];
    payloadDigest: string;
    writerId: string;
  }): TaskChangeEvent {
    if (input.taskRevision < 1 || !Number.isSafeInteger(input.taskRevision))
      throw new E03RuntimeError(
        "task_change_revision",
        "task change revision must be positive",
      );
    const partition = this.partitionFor(input.taskId);
    const entries = this.eventEntries(partition);
    const latestForTask = [...entries]
      .reverse()
      .find((value) => value.taskId === input.taskId);
    if (latestForTask && input.taskRevision <= latestForTask.taskRevision)
      throw new E03RuntimeError(
        "task_change_stale_revision",
        `task change for ${input.taskId} is stale`,
      );
    const eventId = input.eventId ?? createId("task-change-event");
    if (
      [...this.events.values()]
        .flat()
        .some((value) => value.eventId === eventId)
    )
      throw new E03RuntimeError(
        "task_change_event_duplicate",
        `task change event ${eventId} already exists`,
      );
    const payload = {
      eventId,
      partition,
      offset: entries.length,
      taskId: input.taskId,
      leaseId: input.leaseId,
      taskRevision: input.taskRevision,
      kind: input.kind,
      payloadDigest: input.payloadDigest,
      writerId: input.writerId,
      committedAt: this.clock.now(),
      previousPartitionDigest: entries.at(-1)?.digest ?? "",
    };
    const event = { ...payload, digest: digest(payload) };
    assertTaskChangeEvent(event, this.partitionCount);
    entries.push(event);
    this.events.set(partition, entries);
    return structuredClone(event);
  }

  join(input: {
    consumerId?: string;
    groupId: string;
    ownerId: string;
    leaseMs: number;
  }): TaskChangeConsumer {
    const ownerKey = `${input.groupId}\u0000${input.ownerId}`;
    const existingId = this.activeConsumerByOwner.get(ownerKey);
    if (existingId) return structuredClone(this.requireConsumer(existingId));
    if (!Number.isSafeInteger(input.leaseMs) || input.leaseMs < 1)
      throw new E03RuntimeError(
        "task_change_consumer_lease",
        "task change consumer lease must be positive",
      );
    const consumerId = input.consumerId ?? createId("task-change-consumer");
    if (this.consumers.has(consumerId))
      throw new E03RuntimeError(
        "task_change_consumer_duplicate",
        `task change consumer ${consumerId} already exists`,
      );
    const generation = (this.groupGeneration.get(input.groupId) ?? 0) + 1;
    const now = this.clock.now();
    const payload = {
      consumerId,
      groupId: input.groupId,
      ownerId: input.ownerId,
      partitions: [] as number[],
      leaseExpiresAt: new Date(Date.parse(now) + input.leaseMs).toISOString(),
      generation,
      state: "joining" as const,
      lastHeartbeatAt: now,
      revision: 1,
    };
    const consumer = { ...payload, digest: digest(payload) };
    assertTaskChangeConsumer(consumer, this.partitionCount);
    this.consumers.set(consumerId, consumer);
    this.activeConsumerByOwner.set(ownerKey, consumerId);
    this.groupGeneration.set(input.groupId, generation);
    this.rebalance(input.groupId);
    return structuredClone(this.requireConsumer(consumerId));
  }

  heartbeat(
    consumerId: string,
    expectedRevision: number,
    leaseMs: number,
  ): TaskChangeConsumer {
    const consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (consumer.state !== "active")
      throw new E03RuntimeError(
        "task_change_consumer_heartbeat_state",
        `task change consumer ${consumerId} is ${consumer.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(consumer.leaseExpiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "task_change_consumer_lease_expired",
        `task change consumer ${consumerId} lease expired`,
      );
    return this.transitionConsumer(consumer, {
      lastHeartbeatAt: now,
      leaseExpiresAt: new Date(Date.parse(now) + leaseMs).toISOString(),
    });
  }

  offer(
    consumerId: string,
    partition: number,
    deadlineMs: number,
  ): TaskChangeDelivery | null {
    const consumer = this.requireConsumer(consumerId);
    if (consumer.state !== "active" || !consumer.partitions.includes(partition))
      throw new E03RuntimeError(
        "task_change_consumer_partition_ownership",
        `task change consumer ${consumerId} does not own partition ${partition}`,
      );
    const checkpoint = this.checkpointFor(consumer.groupId, partition);
    const nextOffset = (checkpoint?.committedOffset ?? -1) + 1;
    const event = this.eventEntries(partition)[nextOffset];
    if (!event) return null;
    const key = this.deliveryKey(consumerId, event.eventId);
    const activeId = this.activeDeliveryByConsumerEvent.get(key);
    if (activeId) return structuredClone(this.requireDelivery(activeId));
    const previousAttempts = [...this.deliveries.values()].filter(
      (value) =>
        value.consumerId === consumerId && value.eventId === event.eventId,
    ).length;
    const now = this.clock.now();
    const payload = {
      deliveryId: createId("task-change-delivery"),
      consumerId,
      eventId: event.eventId,
      partition,
      offset: event.offset,
      state: "offered" as const,
      attempt: previousAttempts + 1,
      offeredAt: now,
      deadlineAt: new Date(Date.parse(now) + deadlineMs).toISOString(),
      settledAt: "",
      errorCode: "",
      revision: 1,
    };
    const delivery = { ...payload, digest: digest(payload) };
    assertTaskChangeDelivery(delivery, this.partitionCount);
    this.deliveries.set(delivery.deliveryId, delivery);
    this.activeDeliveryByConsumerEvent.set(key, delivery.deliveryId);
    return structuredClone(delivery);
  }

  acknowledge(
    deliveryId: string,
    expectedRevision: number,
    eventDigest: string,
  ): TaskChangeCheckpoint {
    const delivery = this.requireDelivery(deliveryId);
    this.assertDeliveryRevision(delivery, expectedRevision);
    if (delivery.state !== "offered")
      throw new E03RuntimeError(
        "task_change_delivery_ack_state",
        `task change delivery ${deliveryId} is ${delivery.state}`,
      );
    const consumer = this.requireConsumer(delivery.consumerId);
    const event = this.eventEntries(delivery.partition)[delivery.offset];
    if (
      !event ||
      event.eventId !== delivery.eventId ||
      event.digest !== eventDigest
    )
      throw new E03RuntimeError(
        "task_change_delivery_event_mismatch",
        `task change delivery ${deliveryId} event mismatch`,
      );
    const prior = this.checkpointFor(consumer.groupId, delivery.partition);
    if (delivery.offset !== (prior?.committedOffset ?? -1) + 1)
      throw new E03RuntimeError(
        "task_change_checkpoint_gap",
        `task change checkpoint for partition ${delivery.partition} has a gap`,
      );
    const now = this.clock.now();
    const payload = {
      checkpointId: prior?.checkpointId ?? createId("task-change-checkpoint"),
      groupId: consumer.groupId,
      consumerId: consumer.consumerId,
      partition: delivery.partition,
      committedOffset: delivery.offset,
      eventDigest,
      generation: consumer.generation,
      committedAt: now,
      revision: (prior?.revision ?? 0) + 1,
    };
    const checkpoint = { ...payload, digest: digest(payload) };
    assertTaskChangeCheckpoint(checkpoint, this.partitionCount);
    this.checkpoints.set(
      this.checkpointKey(consumer.groupId, delivery.partition),
      checkpoint,
    );
    this.transitionDelivery(delivery, {
      state: "acknowledged",
      settledAt: now,
    });
    this.activeDeliveryByConsumerEvent.delete(
      this.deliveryKey(consumer.consumerId, event.eventId),
    );
    return structuredClone(checkpoint);
  }

  reject(
    deliveryId: string,
    expectedRevision: number,
    errorCode: string,
  ): TaskChangeDelivery {
    const delivery = this.requireDelivery(deliveryId);
    this.assertDeliveryRevision(delivery, expectedRevision);
    if (delivery.state !== "offered")
      throw new E03RuntimeError(
        "task_change_delivery_reject_state",
        `task change delivery ${deliveryId} is ${delivery.state}`,
      );
    const next = this.transitionDelivery(delivery, {
      state: "rejected",
      settledAt: this.clock.now(),
      errorCode,
    });
    this.activeDeliveryByConsumerEvent.delete(
      this.deliveryKey(delivery.consumerId, delivery.eventId),
    );
    return next;
  }

  expire(at = this.clock.now()): {
    consumers: TaskChangeConsumer[];
    deliveries: TaskChangeDelivery[];
  } {
    const consumers: TaskChangeConsumer[] = [];
    const deliveries: TaskChangeDelivery[] = [];
    const affectedGroups = new Set<string>();
    for (const consumer of [...this.consumers.values()]) {
      if (
        consumer.state === "active" &&
        Date.parse(consumer.leaseExpiresAt) <= Date.parse(at)
      ) {
        const next = this.transitionConsumer(consumer, {
          state: "expired",
          partitions: [],
        });
        this.activeConsumerByOwner.delete(
          `${consumer.groupId}\u0000${consumer.ownerId}`,
        );
        affectedGroups.add(consumer.groupId);
        consumers.push(next);
      }
    }
    for (const delivery of [...this.deliveries.values()]) {
      if (
        delivery.state === "offered" &&
        Date.parse(delivery.deadlineAt) <= Date.parse(at)
      ) {
        const next = this.transitionDelivery(delivery, {
          state: "expired",
          settledAt: at,
          errorCode: "delivery_deadline",
        });
        this.activeDeliveryByConsumerEvent.delete(
          this.deliveryKey(delivery.consumerId, delivery.eventId),
        );
        deliveries.push(next);
      }
    }
    for (const groupId of affectedGroups) this.rebalance(groupId);
    return { consumers, deliveries };
  }

  leave(consumerId: string, expectedRevision: number): TaskChangeConsumer {
    const consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (!["joining", "active", "revoking"].includes(consumer.state))
      throw new E03RuntimeError(
        "task_change_consumer_leave_state",
        `task change consumer ${consumerId} is ${consumer.state}`,
      );
    const next = this.transitionConsumer(consumer, {
      state: "left",
      partitions: [],
    });
    this.activeConsumerByOwner.delete(
      `${consumer.groupId}\u0000${consumer.ownerId}`,
    );
    this.rebalance(consumer.groupId);
    return next;
  }

  snapshot(): TaskChangeFeedSnapshot {
    return {
      partitionCount: this.partitionCount,
      events: [...this.events.values()]
        .flat()
        .map((value) => structuredClone(value)),
      consumers: [...this.consumers.values()].map((value) =>
        structuredClone(value),
      ),
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      deliveries: [...this.deliveries.values()].map((value) =>
        structuredClone(value),
      ),
      activeConsumerByOwner: [...this.activeConsumerByOwner.entries()],
      activeDeliveryByConsumerEvent: [
        ...this.activeDeliveryByConsumerEvent.entries(),
      ],
      groupGeneration: [...this.groupGeneration.entries()],
    };
  }

  restore(snapshot: TaskChangeFeedSnapshot): void {
    if (snapshot.partitionCount !== this.partitionCount)
      throw new E03RuntimeError(
        "task_change_restore_partition_count",
        "task change feed partition count changed",
      );
    const events = new Map<number, TaskChangeEvent[]>();
    for (let partition = 0; partition < this.partitionCount; partition += 1)
      events.set(partition, []);
    for (const value of [...snapshot.events].sort(
      (a, b) => a.partition - b.partition || a.offset - b.offset,
    )) {
      assertTaskChangeEvent(value, this.partitionCount);
      const entries = events.get(value.partition)!;
      if (
        value.offset !== entries.length ||
        value.previousPartitionDigest !== (entries.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "task_change_restore_event_chain",
          `task change event ${value.eventId} breaks partition chain`,
        );
      entries.push(structuredClone(value));
    }
    const consumers = new Map<string, TaskChangeConsumer>();
    for (const value of snapshot.consumers) {
      assertTaskChangeConsumer(value, this.partitionCount);
      if (consumers.has(value.consumerId))
        throw new E03RuntimeError(
          "task_change_restore_consumer_duplicate",
          `duplicate task change consumer ${value.consumerId}`,
        );
      consumers.set(value.consumerId, structuredClone(value));
    }
    const checkpoints = new Map<string, TaskChangeCheckpoint>();
    for (const value of snapshot.checkpoints) {
      assertTaskChangeCheckpoint(value, this.partitionCount);
      const key = this.checkpointKey(value.groupId, value.partition);
      const event = events.get(value.partition)?.[value.committedOffset];
      if (
        checkpoints.has(key) ||
        !consumers.has(value.consumerId) ||
        (value.committedOffset >= 0 &&
          (!event || event.digest !== value.eventDigest))
      )
        throw new E03RuntimeError(
          "task_change_restore_checkpoint",
          `task change checkpoint ${value.checkpointId} is invalid`,
        );
      checkpoints.set(key, structuredClone(value));
    }
    const deliveries = new Map<string, TaskChangeDelivery>();
    for (const value of snapshot.deliveries) {
      assertTaskChangeDelivery(value, this.partitionCount);
      const event = events.get(value.partition)?.[value.offset];
      if (
        !event ||
        event.eventId !== value.eventId ||
        !consumers.has(value.consumerId)
      )
        throw new E03RuntimeError(
          "task_change_restore_delivery",
          `task change delivery ${value.deliveryId} is invalid`,
        );
      deliveries.set(value.deliveryId, structuredClone(value));
    }
    const activeConsumerByOwner = new Map(snapshot.activeConsumerByOwner);
    const activeDeliveryByConsumerEvent = new Map(
      snapshot.activeDeliveryByConsumerEvent,
    );
    const groupGeneration = new Map(snapshot.groupGeneration);
    if (
      activeConsumerByOwner.size !== snapshot.activeConsumerByOwner.length ||
      activeDeliveryByConsumerEvent.size !==
        snapshot.activeDeliveryByConsumerEvent.length
    )
      throw new E03RuntimeError(
        "task_change_restore_index_duplicate",
        "task change feed indexes contain duplicates",
      );
    for (const [key, consumerId] of activeConsumerByOwner) {
      const consumer = consumers.get(consumerId);
      if (
        !consumer ||
        key !== `${consumer.groupId}\u0000${consumer.ownerId}` ||
        !["joining", "active"].includes(consumer.state)
      )
        throw new E03RuntimeError(
          "task_change_restore_consumer_index",
          `task change consumer index ${key} is invalid`,
        );
    }
    for (const [key, deliveryId] of activeDeliveryByConsumerEvent) {
      const delivery = deliveries.get(deliveryId);
      if (
        !delivery ||
        key !== this.deliveryKey(delivery.consumerId, delivery.eventId) ||
        delivery.state !== "offered"
      )
        throw new E03RuntimeError(
          "task_change_restore_delivery_index",
          `task change delivery index ${key} is invalid`,
        );
    }
    this.events = events;
    this.consumers = consumers;
    this.checkpoints = checkpoints;
    this.deliveries = deliveries;
    this.activeConsumerByOwner = activeConsumerByOwner;
    this.activeDeliveryByConsumerEvent = activeDeliveryByConsumerEvent;
    this.groupGeneration = groupGeneration;
  }

  private rebalance(groupId: string): void {
    const active = [...this.consumers.values()]
      .filter(
        (value) =>
          value.groupId === groupId &&
          ["joining", "active"].includes(value.state),
      )
      .sort((a, b) => a.consumerId.localeCompare(b.consumerId));
    if (!active.length) return;
    const generation = (this.groupGeneration.get(groupId) ?? 0) + 1;
    this.groupGeneration.set(groupId, generation);
    for (const [index, consumer] of active.entries()) {
      const partitions = Array.from(
        { length: this.partitionCount },
        (_, value) => value,
      ).filter((partition) => partition % active.length === index);
      this.transitionConsumer(consumer, {
        state: "active",
        partitions,
        generation,
      });
    }
  }

  private partitionFor(taskId: string): number {
    return (
      Number.parseInt(
        digest({ taskId, domain: "task-change-feed" }).slice(0, 8),
        16,
      ) % this.partitionCount
    );
  }

  private eventEntries(partition: number): TaskChangeEvent[] {
    const entries = this.events.get(partition);
    if (!entries)
      throw new E03RuntimeError(
        "task_change_partition_missing",
        `task change partition ${partition} does not exist`,
      );
    return entries;
  }

  private checkpointKey(groupId: string, partition: number): string {
    return `${groupId}\u0000${partition}`;
  }

  private checkpointFor(
    groupId: string,
    partition: number,
  ): TaskChangeCheckpoint | null {
    return this.checkpoints.get(this.checkpointKey(groupId, partition)) ?? null;
  }

  private deliveryKey(consumerId: string, eventId: string): string {
    return `${consumerId}\u0000${eventId}`;
  }

  private requireConsumer(id: string): TaskChangeConsumer {
    const value = this.consumers.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_change_consumer_missing",
        `task change consumer ${id} does not exist`,
      );
    assertTaskChangeConsumer(value, this.partitionCount);
    return value;
  }

  private requireDelivery(id: string): TaskChangeDelivery {
    const value = this.deliveries.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_change_delivery_missing",
        `task change delivery ${id} does not exist`,
      );
    assertTaskChangeDelivery(value, this.partitionCount);
    return value;
  }

  private assertConsumerRevision(
    value: TaskChangeConsumer,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_change_consumer_stale_revision",
        `task change consumer ${value.consumerId} revision is stale`,
      );
  }

  private assertDeliveryRevision(
    value: TaskChangeDelivery,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_change_delivery_stale_revision",
        `task change delivery ${value.deliveryId} revision is stale`,
      );
  }

  private transitionConsumer(
    value: TaskChangeConsumer,
    patch: Partial<
      Omit<TaskChangeConsumer, "consumerId" | "revision" | "digest">
    >,
  ): TaskChangeConsumer {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      consumerId: value.consumerId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskChangeConsumer(next, this.partitionCount);
    this.consumers.set(next.consumerId, next);
    return structuredClone(next);
  }

  private transitionDelivery(
    value: TaskChangeDelivery,
    patch: Partial<
      Omit<TaskChangeDelivery, "deliveryId" | "revision" | "digest">
    >,
  ): TaskChangeDelivery {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      deliveryId: value.deliveryId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskChangeDelivery(next, this.partitionCount);
    this.deliveries.set(next.deliveryId, next);
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

export type TaskJournalArchiveState =
  | "open"
  | "sealed"
  | "verified"
  | "archived"
  | "purged";

export interface TaskJournalArchivePolicy {
  policyId: string;
  maximumRecordsPerSegment: number;
  minimumVerifiedSegments: number;
  retentionMs: number;
  legalHoldTaskIds: string[];
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface TaskJournalArchiveSegment {
  segmentId: string;
  policyId: string;
  state: TaskJournalArchiveState;
  firstSequence: number;
  lastSequence: number;
  recordCount: number;
  taskIds: string[];
  headDigest: string;
  tailDigest: string;
  recordsDigest: string;
  archiveLocation: string;
  archiveDigest: string;
  openedAt: string;
  sealedAt: string;
  verifiedAt: string;
  expiresAt: string;
  revision: number;
  digest: string;
}

export interface TaskJournalArchiveCursor {
  cursorId: string;
  consumerId: string;
  segmentId: string;
  sequence: number;
  recordDigest: string;
  leaseExpiresAt: string;
  generation: number;
  state: "active" | "released" | "expired";
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface TaskJournalArchiveReceipt {
  receiptId: string;
  segmentId: string;
  operation: "seal" | "verify" | "archive" | "purge";
  actorId: string;
  sourceDigest: string;
  resultDigest: string;
  previousReceiptDigest: string;
  recordedAt: string;
  revision: number;
  digest: string;
}

export interface TaskJournalArchiveSnapshot {
  policies: TaskJournalArchivePolicy[];
  segments: TaskJournalArchiveSegment[];
  recordsBySegment: Array<[string, TaskJournalRecord[]]>;
  cursors: TaskJournalArchiveCursor[];
  receipts: TaskJournalArchiveReceipt[];
  activeSegmentIdByPolicy: Array<[string, string]>;
  activeCursorIdByConsumer: Array<[string, string]>;
  digest: string;
}

function assertTaskJournalArchivePolicy(value: TaskJournalArchivePolicy): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.policyId ||
    value.maximumRecordsPerSegment < 1 ||
    value.minimumVerifiedSegments < 0 ||
    value.retentionMs < 1 ||
    new Set(value.legalHoldTaskIds).size !== value.legalHoldTaskIds.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_journal_archive_policy_corrupt",
      `task journal archive policy ${value.policyId || "<empty>"} is corrupt`,
    );
}

function assertTaskJournalArchiveSegment(
  value: TaskJournalArchiveSegment,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.segmentId ||
    !value.policyId ||
    value.firstSequence < 1 ||
    value.lastSequence < value.firstSequence - 1 ||
    value.recordCount < 0 ||
    new Set(value.taskIds).size !== value.taskIds.length ||
    !value.headDigest ||
    !value.tailDigest ||
    !value.recordsDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_journal_archive_segment_corrupt",
      `task journal archive segment ${value.segmentId || "<empty>"} is corrupt`,
    );
  if (
    (value.state === "archived" || value.state === "purged") &&
    (!value.archiveLocation || !value.archiveDigest)
  )
    throw new E03RuntimeError(
      "task_journal_archive_location_missing",
      "archived task journal segment requires location and digest",
    );
}

function assertTaskJournalArchiveCursor(value: TaskJournalArchiveCursor): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.cursorId ||
    !value.consumerId ||
    !value.segmentId ||
    value.sequence < 0 ||
    !value.recordDigest ||
    value.generation < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_journal_archive_cursor_corrupt",
      `task journal archive cursor ${value.cursorId || "<empty>"} is corrupt`,
    );
}

function assertTaskJournalArchiveReceipt(
  value: TaskJournalArchiveReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.segmentId ||
    !value.actorId ||
    !value.sourceDigest ||
    !value.resultDigest ||
    !value.previousReceiptDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_journal_archive_receipt_corrupt",
      `task journal archive receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

export class TaskJournalArchiveRuntime {
  private policies = new Map<string, TaskJournalArchivePolicy>();
  private segments = new Map<string, TaskJournalArchiveSegment>();
  private recordsBySegment = new Map<string, TaskJournalRecord[]>();
  private cursors = new Map<string, TaskJournalArchiveCursor>();
  private receipts = new Map<string, TaskJournalArchiveReceipt[]>();
  private activeSegmentIdByPolicy = new Map<string, string>();
  private activeCursorIdByConsumer = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerPolicy(input: {
    policyId?: string;
    maximumRecordsPerSegment: number;
    minimumVerifiedSegments?: number;
    retentionMs: number;
  }): TaskJournalArchivePolicy {
    const policyId = input.policyId ?? createId("task-journal-archive-policy");
    const existing = this.policies.get(policyId);
    if (existing) return structuredClone(existing);
    const now = this.clock.now();
    const payload = {
      policyId,
      maximumRecordsPerSegment: input.maximumRecordsPerSegment,
      minimumVerifiedSegments: input.minimumVerifiedSegments ?? 1,
      retentionMs: input.retentionMs,
      legalHoldTaskIds: [] as string[],
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertTaskJournalArchivePolicy(policy);
    this.policies.set(policyId, policy);
    return structuredClone(policy);
  }

  setLegalHold(input: {
    policyId: string;
    expectedRevision: number;
    taskId: string;
    enabled: boolean;
  }): TaskJournalArchivePolicy {
    const policy = this.requirePolicy(input.policyId);
    this.assertPolicyRevision(policy, input.expectedRevision);
    if (!input.taskId)
      throw new E03RuntimeError(
        "task_journal_archive_hold_task_required",
        "task journal legal hold requires task",
      );
    const held = new Set(policy.legalHoldTaskIds);
    if (input.enabled) held.add(input.taskId);
    else held.delete(input.taskId);
    return this.transitionPolicy(policy, {
      legalHoldTaskIds: [...held].sort(),
      updatedAt: this.clock.now(),
    });
  }

  append(input: {
    policyId: string;
    record: TaskJournalRecord;
  }): TaskJournalArchiveSegment {
    const policy = this.requirePolicy(input.policyId);
    this.assertRecord(input.record);
    const duplicate = [...this.recordsBySegment.values()].some((records) =>
      records.some((record) => record.journalId === input.record.journalId),
    );
    if (duplicate) {
      const segment = [...this.segments.values()].find((candidate) =>
        (this.recordsBySegment.get(candidate.segmentId) ?? []).some(
          (record) => record.journalId === input.record.journalId,
        ),
      );
      if (!segment)
        throw new E03RuntimeError(
          "task_journal_archive_duplicate_orphaned",
          "duplicate task journal record has no segment",
        );
      return structuredClone(segment);
    }
    let segment = this.openSegment(policy, input.record);
    const records = this.recordsBySegment.get(segment.segmentId) ?? [];
    if (records.length > 0) {
      const previous = records.at(-1)!;
      if (
        input.record.sequence !== previous.sequence + 1 ||
        input.record.previousDigest !== previous.digest
      )
        throw new E03RuntimeError(
          "task_journal_archive_chain_break",
          "task journal archive record breaks sequence or digest chain",
        );
    } else if (input.record.sequence !== segment.firstSequence)
      throw new E03RuntimeError(
        "task_journal_archive_segment_sequence_mismatch",
        "task journal archive segment starts at unexpected sequence",
      );
    const nextRecords = [...records, structuredClone(input.record)];
    this.recordsBySegment.set(segment.segmentId, nextRecords);
    segment = this.transitionSegment(segment, {
      lastSequence: input.record.sequence,
      recordCount: nextRecords.length,
      taskIds: [...new Set([...segment.taskIds, input.record.taskId])].sort(),
      headDigest: nextRecords[0]!.digest,
      tailDigest: input.record.digest,
      recordsDigest: digest(nextRecords.map((record) => record.digest)),
    });
    if (nextRecords.length >= policy.maximumRecordsPerSegment)
      segment = this.seal({
        segmentId: segment.segmentId,
        expectedRevision: segment.revision,
        actorId: "archive:auto-seal",
      }).segment;
    return segment;
  }

  seal(input: {
    segmentId: string;
    expectedRevision: number;
    actorId: string;
  }): {
    segment: TaskJournalArchiveSegment;
    receipt: TaskJournalArchiveReceipt;
  } {
    const segment = this.requireSegment(input.segmentId);
    this.assertSegmentRevision(segment, input.expectedRevision);
    if (segment.state === "sealed") {
      const receipt = (this.receipts.get(segment.segmentId) ?? []).find(
        (value) =>
          value.operation === "seal" && value.resultDigest === segment.digest,
      );
      if (!receipt)
        throw new E03RuntimeError(
          "task_journal_archive_seal_receipt_missing",
          "sealed task journal segment lacks receipt",
        );
      return {
        segment: structuredClone(segment),
        receipt: structuredClone(receipt),
      };
    }
    if (segment.state !== "open" || segment.recordCount < 1)
      throw new E03RuntimeError(
        "task_journal_archive_seal_invalid_state",
        `cannot seal task journal segment from ${segment.state}`,
      );
    const sourceDigest = segment.digest;
    const next = this.transitionSegment(segment, {
      state: "sealed",
      sealedAt: this.clock.now(),
    });
    if (
      this.activeSegmentIdByPolicy.get(segment.policyId) === segment.segmentId
    )
      this.activeSegmentIdByPolicy.delete(segment.policyId);
    const receipt = this.recordReceipt(
      next,
      "seal",
      input.actorId,
      sourceDigest,
    );
    return { segment: next, receipt };
  }

  verify(input: {
    segmentId: string;
    expectedRevision: number;
    actorId: string;
    expectedRecordsDigest: string;
  }): {
    segment: TaskJournalArchiveSegment;
    receipt: TaskJournalArchiveReceipt;
  } {
    const segment = this.requireSegment(input.segmentId);
    this.assertSegmentRevision(segment, input.expectedRevision);
    if (segment.state !== "sealed")
      throw new E03RuntimeError(
        "task_journal_archive_verify_invalid_state",
        "task journal archive segment must be sealed before verification",
      );
    const records = this.recordsBySegment.get(segment.segmentId) ?? [];
    this.assertRecordChain(records);
    const recordsDigest = digest(records.map((record) => record.digest));
    if (
      recordsDigest !== segment.recordsDigest ||
      recordsDigest !== input.expectedRecordsDigest
    )
      throw new E03RuntimeError(
        "task_journal_archive_verify_digest_mismatch",
        "task journal archive records digest mismatch",
      );
    const sourceDigest = segment.digest;
    const next = this.transitionSegment(segment, {
      state: "verified",
      verifiedAt: this.clock.now(),
    });
    return {
      segment: next,
      receipt: this.recordReceipt(next, "verify", input.actorId, sourceDigest),
    };
  }

  archive(input: {
    segmentId: string;
    expectedRevision: number;
    actorId: string;
    archiveLocation: string;
    archiveDigest: string;
  }): {
    segment: TaskJournalArchiveSegment;
    receipt: TaskJournalArchiveReceipt;
  } {
    const segment = this.requireSegment(input.segmentId);
    this.assertSegmentRevision(segment, input.expectedRevision);
    if (segment.state !== "verified")
      throw new E03RuntimeError(
        "task_journal_archive_upload_invalid_state",
        "task journal segment must be verified before archive",
      );
    if (!input.archiveLocation || !input.archiveDigest)
      throw new E03RuntimeError(
        "task_journal_archive_destination_invalid",
        "task journal archive requires location and digest",
      );
    if (input.archiveDigest !== segment.recordsDigest)
      throw new E03RuntimeError(
        "task_journal_archive_upload_digest_mismatch",
        "task journal archive upload digest differs from verified segment",
      );
    const policy = this.requirePolicy(segment.policyId);
    const sourceDigest = segment.digest;
    const next = this.transitionSegment(segment, {
      state: "archived",
      archiveLocation: input.archiveLocation,
      archiveDigest: input.archiveDigest,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + policy.retentionMs,
      ).toISOString(),
    });
    return {
      segment: next,
      receipt: this.recordReceipt(next, "archive", input.actorId, sourceDigest),
    };
  }

  acquireCursor(input: {
    consumerId: string;
    segmentId: string;
    leaseMs: number;
    now?: string;
  }): TaskJournalArchiveCursor {
    const segment = this.requireSegment(input.segmentId);
    if (segment.state === "purged")
      throw new E03RuntimeError(
        "task_journal_archive_cursor_segment_purged",
        "cannot acquire cursor for purged task journal segment",
      );
    const now = input.now ?? this.clock.now();
    const activeId = this.activeCursorIdByConsumer.get(input.consumerId);
    const active = activeId ? this.cursors.get(activeId) : undefined;
    if (
      active &&
      active.state === "active" &&
      Date.parse(active.leaseExpiresAt) > Date.parse(now)
    ) {
      if (active.segmentId !== input.segmentId)
        throw new E03RuntimeError(
          "task_journal_archive_cursor_conflict",
          `consumer ${input.consumerId} already holds another cursor`,
        );
      return structuredClone(active);
    }
    if (!Number.isInteger(input.leaseMs) || input.leaseMs < 1)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_lease_invalid",
        "task journal archive cursor lease must be positive",
      );
    const payload = {
      cursorId: createId("task-journal-archive-cursor"),
      consumerId: input.consumerId,
      segmentId: segment.segmentId,
      sequence: segment.firstSequence - 1,
      recordDigest: digest("task-journal-archive-cursor-root"),
      leaseExpiresAt: new Date(Date.parse(now) + input.leaseMs).toISOString(),
      generation: (active?.generation ?? 0) + 1,
      state: "active" as const,
      updatedAt: now,
      revision: 1,
    };
    const cursor = { ...payload, digest: digest(payload) };
    assertTaskJournalArchiveCursor(cursor);
    this.cursors.set(cursor.cursorId, cursor);
    this.activeCursorIdByConsumer.set(cursor.consumerId, cursor.cursorId);
    return structuredClone(cursor);
  }

  advanceCursor(input: {
    cursorId: string;
    expectedRevision: number;
    generation: number;
    sequence: number;
    recordDigest: string;
    now?: string;
  }): TaskJournalArchiveCursor {
    const cursor = this.requireCursor(input.cursorId);
    this.assertCursorRevision(cursor, input.expectedRevision);
    const now = input.now ?? this.clock.now();
    if (
      cursor.state !== "active" ||
      Date.parse(cursor.leaseExpiresAt) <= Date.parse(now)
    )
      throw new E03RuntimeError(
        "task_journal_archive_cursor_expired",
        "task journal archive cursor is not active",
      );
    if (cursor.generation !== input.generation)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_generation_stale",
        "task journal archive cursor generation is stale",
      );
    const records = this.recordsBySegment.get(cursor.segmentId) ?? [];
    const record = records.find((value) => value.sequence === input.sequence);
    if (!record || record.digest !== input.recordDigest)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_record_mismatch",
        "task journal archive cursor does not match retained record",
      );
    if (input.sequence <= cursor.sequence)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_non_monotonic",
        "task journal archive cursor must advance monotonically",
      );
    return this.transitionCursor(cursor, {
      sequence: input.sequence,
      recordDigest: input.recordDigest,
      updatedAt: now,
    });
  }

  purge(input: {
    segmentId: string;
    expectedRevision: number;
    actorId: string;
    now?: string;
  }): {
    segment: TaskJournalArchiveSegment;
    receipt: TaskJournalArchiveReceipt;
  } {
    const segment = this.requireSegment(input.segmentId);
    this.assertSegmentRevision(segment, input.expectedRevision);
    const now = input.now ?? this.clock.now();
    if (segment.state !== "archived")
      throw new E03RuntimeError(
        "task_journal_archive_purge_invalid_state",
        "only archived task journal segments may purge",
      );
    if (Date.parse(segment.expiresAt) > Date.parse(now))
      throw new E03RuntimeError(
        "task_journal_archive_retention_active",
        "task journal archive retention window is active",
      );
    const policy = this.requirePolicy(segment.policyId);
    if (
      segment.taskIds.some((taskId) => policy.legalHoldTaskIds.includes(taskId))
    )
      throw new E03RuntimeError(
        "task_journal_archive_legal_hold",
        "task journal archive segment contains held task",
      );
    const verifiedSegments = [...this.segments.values()].filter(
      (candidate) =>
        candidate.policyId === policy.policyId &&
        candidate.segmentId !== segment.segmentId &&
        (candidate.state === "verified" || candidate.state === "archived"),
    );
    if (verifiedSegments.length < policy.minimumVerifiedSegments)
      throw new E03RuntimeError(
        "task_journal_archive_minimum_segments",
        "task journal archive minimum verified segments would be violated",
      );
    const activeCursor = [...this.cursors.values()].find(
      (cursor) =>
        cursor.segmentId === segment.segmentId && cursor.state === "active",
    );
    if (activeCursor)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_active",
        "task journal archive segment has active cursor",
      );
    const sourceDigest = segment.digest;
    const next = this.transitionSegment(segment, { state: "purged" });
    this.recordsBySegment.delete(segment.segmentId);
    return {
      segment: next,
      receipt: this.recordReceipt(next, "purge", input.actorId, sourceDigest),
    };
  }

  snapshot(): TaskJournalArchiveSnapshot {
    const payload = {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      segments: [...this.segments.values()].map((value) =>
        structuredClone(value),
      ),
      recordsBySegment: [...this.recordsBySegment.entries()].map(
        ([segmentId, records]) =>
          [segmentId, structuredClone(records)] as [
            string,
            TaskJournalRecord[],
          ],
      ),
      cursors: [...this.cursors.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeSegmentIdByPolicy: [...this.activeSegmentIdByPolicy.entries()],
      activeCursorIdByConsumer: [...this.activeCursorIdByConsumer.entries()],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: TaskJournalArchiveSnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "task_journal_archive_snapshot_corrupt",
        "task journal archive snapshot digest mismatch",
      );
    const policies = new Map<string, TaskJournalArchivePolicy>();
    const segments = new Map<string, TaskJournalArchiveSegment>();
    const recordsBySegment = new Map<string, TaskJournalRecord[]>();
    const cursors = new Map<string, TaskJournalArchiveCursor>();
    const receipts = new Map<string, TaskJournalArchiveReceipt[]>();
    for (const value of payload.policies) {
      assertTaskJournalArchivePolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_policy_duplicate",
          `duplicate task journal archive policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    for (const value of payload.segments) {
      assertTaskJournalArchiveSegment(value);
      if (!policies.has(value.policyId))
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_policy_missing",
          `task journal archive segment ${value.segmentId} lacks policy`,
        );
      segments.set(value.segmentId, structuredClone(value));
    }
    for (const [segmentId, records] of payload.recordsBySegment) {
      const segment = segments.get(segmentId);
      if (!segment || segment.state === "purged")
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_records_orphaned",
          `task journal records for ${segmentId} are orphaned`,
        );
      this.assertRecordChain(records);
      if (
        digest(records.map((record) => record.digest)) !== segment.recordsDigest
      )
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_records_corrupt",
          `task journal archive records for ${segmentId} are corrupt`,
        );
      recordsBySegment.set(segmentId, structuredClone(records));
    }
    for (const value of payload.cursors) {
      assertTaskJournalArchiveCursor(value);
      if (!segments.has(value.segmentId))
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_cursor_orphaned",
          `task journal archive cursor ${value.cursorId} is orphaned`,
        );
      cursors.set(value.cursorId, structuredClone(value));
    }
    for (const value of payload.receipts) {
      assertTaskJournalArchiveReceipt(value);
      const list = receipts.get(value.segmentId) ?? [];
      list.push(structuredClone(value));
      receipts.set(value.segmentId, list);
    }
    const activeSegments = new Map(payload.activeSegmentIdByPolicy);
    for (const [policyId, segmentId] of activeSegments)
      if (!policies.has(policyId) || segments.get(segmentId)?.state !== "open")
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_active_segment_invalid",
          "task journal archive active segment index is corrupt",
        );
    const activeCursors = new Map(payload.activeCursorIdByConsumer);
    for (const cursorId of activeCursors.values())
      if (!cursors.has(cursorId))
        throw new E03RuntimeError(
          "task_journal_archive_snapshot_active_cursor_invalid",
          "task journal archive active cursor index is corrupt",
        );
    this.policies = policies;
    this.segments = segments;
    this.recordsBySegment = recordsBySegment;
    this.cursors = cursors;
    this.receipts = receipts;
    this.activeSegmentIdByPolicy = activeSegments;
    this.activeCursorIdByConsumer = activeCursors;
  }

  private openSegment(
    policy: TaskJournalArchivePolicy,
    firstRecord: TaskJournalRecord,
  ): TaskJournalArchiveSegment {
    const activeId = this.activeSegmentIdByPolicy.get(policy.policyId);
    const active = activeId ? this.segments.get(activeId) : undefined;
    if (active) return active;
    const now = this.clock.now();
    const emptyDigest = digest([]);
    const payload = {
      segmentId: createId("task-journal-archive-segment"),
      policyId: policy.policyId,
      state: "open" as const,
      firstSequence: firstRecord.sequence,
      lastSequence: firstRecord.sequence - 1,
      recordCount: 0,
      taskIds: [] as string[],
      headDigest: digest("task-journal-archive-head"),
      tailDigest: digest("task-journal-archive-tail"),
      recordsDigest: emptyDigest,
      archiveLocation: "",
      archiveDigest: "",
      openedAt: now,
      sealedAt: "",
      verifiedAt: "",
      expiresAt: "",
      revision: 1,
    };
    const segment = { ...payload, digest: digest(payload) };
    assertTaskJournalArchiveSegment(segment);
    this.segments.set(segment.segmentId, segment);
    this.recordsBySegment.set(segment.segmentId, []);
    this.activeSegmentIdByPolicy.set(policy.policyId, segment.segmentId);
    return segment;
  }

  private recordReceipt(
    segment: TaskJournalArchiveSegment,
    operation: TaskJournalArchiveReceipt["operation"],
    actorId: string,
    sourceDigest: string,
  ): TaskJournalArchiveReceipt {
    if (!actorId)
      throw new E03RuntimeError(
        "task_journal_archive_receipt_actor_required",
        "task journal archive receipt requires actor",
      );
    const list = this.receipts.get(segment.segmentId) ?? [];
    const payload = {
      receiptId: createId("task-journal-archive-receipt"),
      segmentId: segment.segmentId,
      operation,
      actorId,
      sourceDigest,
      resultDigest: segment.digest,
      previousReceiptDigest:
        list.at(-1)?.digest ?? digest("task-journal-archive-receipt-root"),
      recordedAt: this.clock.now(),
      revision: list.length + 1,
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertTaskJournalArchiveReceipt(receipt);
    this.receipts.set(segment.segmentId, [...list, receipt]);
    return structuredClone(receipt);
  }

  private assertRecord(value: TaskJournalRecord): void {
    const { digest: expected, ...payload } = value;
    if (
      !value.journalId ||
      value.sequence < 1 ||
      !value.taskId ||
      !value.leaseId ||
      !value.payloadDigest ||
      !value.previousDigest ||
      digest(payload) !== expected
    )
      throw new E03RuntimeError(
        "task_journal_archive_record_corrupt",
        `task journal archive record ${value.journalId || "<empty>"} is corrupt`,
      );
  }

  private assertRecordChain(records: TaskJournalRecord[]): void {
    records.forEach((record, index) => {
      this.assertRecord(record);
      const previous = records[index - 1];
      if (
        previous &&
        (record.sequence !== previous.sequence + 1 ||
          record.previousDigest !== previous.digest)
      )
        throw new E03RuntimeError(
          "task_journal_archive_record_chain_corrupt",
          `task journal archive record ${record.journalId} breaks chain`,
        );
    });
  }

  private requirePolicy(id: string): TaskJournalArchivePolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_journal_archive_policy_missing",
        `task journal archive policy ${id} does not exist`,
      );
    assertTaskJournalArchivePolicy(value);
    return value;
  }

  private requireSegment(id: string): TaskJournalArchiveSegment {
    const value = this.segments.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_journal_archive_segment_missing",
        `task journal archive segment ${id} does not exist`,
      );
    assertTaskJournalArchiveSegment(value);
    return value;
  }

  private requireCursor(id: string): TaskJournalArchiveCursor {
    const value = this.cursors.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_missing",
        `task journal archive cursor ${id} does not exist`,
      );
    assertTaskJournalArchiveCursor(value);
    return value;
  }

  private assertPolicyRevision(
    value: TaskJournalArchivePolicy,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_journal_archive_policy_stale_revision",
        `task journal archive policy ${value.policyId} revision is stale`,
      );
  }

  private assertSegmentRevision(
    value: TaskJournalArchiveSegment,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_journal_archive_segment_stale_revision",
        `task journal archive segment ${value.segmentId} revision is stale`,
      );
  }

  private assertCursorRevision(
    value: TaskJournalArchiveCursor,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_journal_archive_cursor_stale_revision",
        `task journal archive cursor ${value.cursorId} revision is stale`,
      );
  }

  private transitionPolicy(
    value: TaskJournalArchivePolicy,
    patch: Partial<
      Omit<TaskJournalArchivePolicy, "policyId" | "revision" | "digest">
    >,
  ): TaskJournalArchivePolicy {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      policyId: value.policyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskJournalArchivePolicy(next);
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }

  private transitionSegment(
    value: TaskJournalArchiveSegment,
    patch: Partial<
      Omit<TaskJournalArchiveSegment, "segmentId" | "revision" | "digest">
    >,
  ): TaskJournalArchiveSegment {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      segmentId: value.segmentId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskJournalArchiveSegment(next);
    this.segments.set(next.segmentId, next);
    return structuredClone(next);
  }

  private transitionCursor(
    value: TaskJournalArchiveCursor,
    patch: Partial<
      Omit<TaskJournalArchiveCursor, "cursorId" | "revision" | "digest">
    >,
  ): TaskJournalArchiveCursor {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      cursorId: value.cursorId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskJournalArchiveCursor(next);
    this.cursors.set(next.cursorId, next);
    return structuredClone(next);
  }
}
