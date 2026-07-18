import {
  type E03Clock,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";
import {
  createId,
  digest,
  E03RuntimeError,
  type E03Identity,
} from "../e03/contracts.ts";

export interface TaskIdentityRequest {
  runId: string;
  sessionId: string;
  taskId?: string;
  parentTaskId: string;
  parentSessionId: string;
  parentLineage?: readonly string[];
  attempt?: number;
  leaseId?: string;
  idempotencyKey: string;
}

export class TaskIdentityRuntime {
  allocate(request: TaskIdentityRequest): E03Identity {
    const runId = token(request.runId, "runId");
    const sessionId = token(request.sessionId, "sessionId");
    const parentTaskId = token(request.parentTaskId, "parentTaskId");
    const parentSessionId = token(request.parentSessionId, "parentSessionId");
    const idempotencyKey = token(request.idempotencyKey, "idempotencyKey");
    const attempt = request.attempt ?? 1;
    if (!Number.isSafeInteger(attempt) || attempt < 1)
      throw new E03RuntimeError(
        "invalid_attempt",
        "task attempt must be a positive integer",
      );
    const taskId = request.taskId
      ? token(request.taskId, "taskId")
      : `agent-${digest({ runId, sessionId, parentTaskId, idempotencyKey }).slice(0, 24)}`;
    const lineage = [...(request.parentLineage ?? []), parentTaskId];
    if (lineage.includes(taskId))
      throw new E03RuntimeError(
        "task_lineage_cycle",
        `task ${taskId} appears in its ancestry`,
        { lineage },
      );
    if (new Set(lineage).size !== lineage.length)
      throw new E03RuntimeError(
        "task_lineage_cycle",
        "parent lineage already contains a cycle",
        { lineage },
      );
    return {
      runId,
      sessionId,
      taskId,
      parentTaskId,
      parentSessionId,
      attemptId: `${taskId}:attempt:${attempt}`,
      attempt,
      leaseId: request.leaseId
        ? token(request.leaseId, "leaseId")
        : createId("task-lease"),
      lineage,
    };
  }

  nextAttempt(
    identity: E03Identity,
    leaseId = createId("task-lease"),
  ): E03Identity {
    this.validate(identity);
    const attempt = identity.attempt + 1;
    return {
      ...identity,
      attempt,
      attemptId: `${identity.taskId}:attempt:${attempt}`,
      leaseId,
    };
  }

  validate(identity: E03Identity): void {
    for (const [field, value] of Object.entries(identity)) {
      if (["attempt", "lineage"].includes(field)) continue;
      token(String(value), field);
    }
    if (!Number.isSafeInteger(identity.attempt) || identity.attempt < 1)
      throw new E03RuntimeError(
        "invalid_attempt",
        "restored task attempt is invalid",
      );
    if (identity.attemptId !== `${identity.taskId}:attempt:${identity.attempt}`)
      throw new E03RuntimeError(
        "attempt_identity_mismatch",
        "task attempt id does not match task/attempt",
      );
    if (
      identity.lineage.includes(identity.taskId) ||
      new Set(identity.lineage).size !== identity.lineage.length
    )
      throw new E03RuntimeError(
        "task_lineage_cycle",
        "restored task lineage contains a cycle",
      );
    if (identity.lineage.at(-1) !== identity.parentTaskId)
      throw new E03RuntimeError(
        "task_parent_mismatch",
        "task lineage does not terminate at parent task",
      );
  }
}

function token(value: string, field: string): string {
  const normalized = value.trim();
  if (!normalized || normalized.length > 512 || /[\r\n\0]/.test(normalized))
    throw new E03RuntimeError(
      "invalid_identity",
      `${field} is not a valid identity token`,
      { field },
    );
  return normalized;
}

export type LeaseState = "active" | "released" | "expired" | "superseded";

export interface TaskLease {
  leaseId: string;
  taskId: string;
  attemptId: string;
  attempt: number;
  owner: "typescript.E03AgentControlCoordinator";
  state: LeaseState;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string | null;
  supersededBy: string | null;
  fence: string;
  digest: string;
}

export interface LeaseDecision {
  accepted: boolean;
  code: string;
  current: TaskLease;
  candidateLeaseId: string;
  candidateAttempt: number;
}

export class TaskLeaseRuntime {
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  acquire(identity: E03Identity, maximumWallTimeMs: number): TaskLease {
    if (!identity.taskId || !identity.attemptId || !identity.leaseId)
      throw new E03RuntimeError(
        "invalid_lease_identity",
        "task, attempt and lease ids are required",
      );
    if (!Number.isSafeInteger(identity.attempt) || identity.attempt < 1)
      throw new E03RuntimeError(
        "invalid_lease_attempt",
        "lease attempt must be positive",
      );
    if (
      !Number.isSafeInteger(maximumWallTimeMs) ||
      maximumWallTimeMs < 1 ||
      maximumWallTimeMs > 86_400_000
    )
      throw new E03RuntimeError(
        "invalid_lease_duration",
        "lease duration must be between one millisecond and one day",
      );
    const acquiredAt = this.clock.now();
    const expiresAt = new Date(
      Date.parse(acquiredAt) + maximumWallTimeMs,
    ).toISOString();
    return sealLease({
      leaseId: identity.leaseId,
      taskId: identity.taskId,
      attemptId: identity.attemptId,
      attempt: identity.attempt,
      owner: "typescript.E03AgentControlCoordinator",
      state: "active",
      acquiredAt,
      expiresAt,
      releasedAt: null,
      supersededBy: null,
      fence: digest({
        taskId: identity.taskId,
        attemptId: identity.attemptId,
        leaseId: identity.leaseId,
        attempt: identity.attempt,
      }),
    });
  }

  restore(value: TaskLease): TaskLease {
    this.assertValid(value);
    return structuredClone(value);
  }

  validateMutation(
    current: TaskLease,
    identity: E03Identity,
    now = this.clock.now(),
  ): LeaseDecision {
    this.assertValid(current);
    const candidate = { leaseId: identity.leaseId, attempt: identity.attempt };
    if (current.taskId !== identity.taskId)
      return this.decision(false, "lease_task_mismatch", current, candidate);
    if (current.owner !== "typescript.E03AgentControlCoordinator")
      return this.decision(false, "lease_owner_mismatch", current, candidate);
    if (current.state !== "active")
      return this.decision(false, `lease_${current.state}`, current, candidate);
    if (Date.parse(current.expiresAt) <= Date.parse(now))
      return this.decision(false, "lease_expired", current, candidate);
    if (
      current.leaseId !== identity.leaseId ||
      current.attemptId !== identity.attemptId
    )
      return this.decision(false, "stale_lease", current, candidate);
    if (current.attempt !== identity.attempt)
      return this.decision(false, "stale_attempt", current, candidate);
    return this.decision(true, "lease_active", current, candidate);
  }

  assertMutation(
    current: TaskLease,
    identity: E03Identity,
    now = this.clock.now(),
  ): void {
    const decision = this.validateMutation(current, identity, now);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `task ${identity.taskId} cannot mutate with lease ${identity.leaseId}`,
        {
          currentLeaseId: current.leaseId,
          currentAttempt: current.attempt,
          candidateAttempt: identity.attempt,
        },
      );
  }

  renew(current: TaskLease, durationMs: number): TaskLease {
    this.assertValid(current);
    if (current.state !== "active")
      throw new E03RuntimeError(
        "lease_not_active",
        `cannot renew ${current.state} lease`,
      );
    if (
      !Number.isSafeInteger(durationMs) ||
      durationMs < 1 ||
      durationMs > 86_400_000
    )
      throw new E03RuntimeError(
        "invalid_lease_duration",
        "lease duration is out of range",
      );
    const now = this.clock.now();
    if (Date.parse(current.expiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "lease_expired",
        "cannot renew an expired lease",
      );
    return sealLease({
      ...current,
      expiresAt: new Date(Date.parse(now) + durationMs).toISOString(),
    });
  }

  release(current: TaskLease, task: E03TaskState): TaskLease {
    this.assertValid(current);
    if (
      current.taskId !== task.identity.taskId ||
      current.leaseId !== task.identity.leaseId
    )
      throw new E03RuntimeError(
        "lease_task_mismatch",
        "release task does not own lease",
      );
    if (!task.terminalAt)
      throw new E03RuntimeError(
        "release_before_terminal",
        "task lease can only release after a terminal transition",
      );
    if (current.state === "released") return structuredClone(current);
    if (current.state !== "active")
      throw new E03RuntimeError(
        "lease_not_active",
        `cannot release ${current.state} lease`,
      );
    return sealLease({
      ...current,
      state: "released",
      releasedAt: this.clock.now(),
    });
  }

  expire(current: TaskLease, now = this.clock.now()): TaskLease {
    this.assertValid(current);
    if (current.state !== "active") return structuredClone(current);
    if (Date.parse(current.expiresAt) > Date.parse(now))
      throw new E03RuntimeError(
        "lease_not_expired",
        "lease deadline has not elapsed",
      );
    return sealLease({ ...current, state: "expired", releasedAt: now });
  }

  supersede(
    current: TaskLease,
    nextIdentity: E03Identity,
    maximumWallTimeMs: number,
  ): { previous: TaskLease; next: TaskLease } {
    this.assertValid(current);
    if (current.taskId !== nextIdentity.taskId)
      throw new E03RuntimeError(
        "lease_task_mismatch",
        "new attempt changes task identity",
      );
    if (nextIdentity.attempt !== current.attempt + 1)
      throw new E03RuntimeError(
        "non_monotonic_attempt",
        "new lease attempt must increment exactly once",
      );
    if (
      nextIdentity.leaseId === current.leaseId ||
      nextIdentity.attemptId === current.attemptId
    )
      throw new E03RuntimeError(
        "lease_identity_reused",
        "new attempt must use fresh attempt and lease ids",
      );
    const next = this.acquire(nextIdentity, maximumWallTimeMs);
    const previous = sealLease({
      ...current,
      state: "superseded",
      releasedAt: this.clock.now(),
      supersededBy: next.leaseId,
    });
    return { previous, next };
  }

  fromTask(task: E03TaskState): TaskLease {
    const duration = task.definition.budget.maxWallTimeMs;
    const startedAt = task.definition.budget.startedAt || task.createdAt;
    const deadline =
      task.definition.budget.deadlineAt ||
      new Date(Date.parse(startedAt) + duration).toISOString();
    return sealLease({
      leaseId: task.identity.leaseId,
      taskId: task.identity.taskId,
      attemptId: task.identity.attemptId,
      attempt: task.identity.attempt,
      owner: "typescript.E03AgentControlCoordinator",
      state: task.terminalAt
        ? "released"
        : Date.parse(deadline) <= Date.parse(this.clock.now())
          ? "expired"
          : "active",
      acquiredAt: startedAt,
      expiresAt: deadline,
      releasedAt: task.terminalAt,
      supersededBy: null,
      fence: digest({
        taskId: task.identity.taskId,
        attemptId: task.identity.attemptId,
        leaseId: task.identity.leaseId,
        attempt: task.identity.attempt,
      }),
    });
  }

  assertValid(lease: TaskLease): void {
    const { digest: checksum, ...payload } = lease;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "lease_digest_mismatch",
        `lease ${lease.leaseId} digest is invalid`,
      );
    if (
      !lease.leaseId ||
      !lease.taskId ||
      !lease.attemptId ||
      lease.owner !== "typescript.E03AgentControlCoordinator"
    )
      throw new E03RuntimeError(
        "invalid_lease",
        "lease identity or owner is invalid",
      );
    if (lease.attempt < 1 || !Number.isSafeInteger(lease.attempt))
      throw new E03RuntimeError(
        "invalid_lease_attempt",
        "lease attempt is invalid",
      );
    if (
      !Number.isFinite(Date.parse(lease.acquiredAt)) ||
      !Number.isFinite(Date.parse(lease.expiresAt)) ||
      lease.acquiredAt >= lease.expiresAt
    )
      throw new E03RuntimeError(
        "invalid_lease_time",
        "lease acquisition/expiry timestamps are invalid",
      );
    const expectedFence = digest({
      taskId: lease.taskId,
      attemptId: lease.attemptId,
      leaseId: lease.leaseId,
      attempt: lease.attempt,
    });
    if (lease.fence !== expectedFence)
      throw new E03RuntimeError(
        "lease_fence_mismatch",
        "lease fence does not bind the task attempt",
      );
    if (lease.state === "active" && lease.releasedAt)
      throw new E03RuntimeError(
        "active_lease_released",
        "active lease has a release timestamp",
      );
    if (lease.state !== "active" && !lease.releasedAt)
      throw new E03RuntimeError(
        "closed_lease_release_missing",
        "closed lease has no release timestamp",
      );
    if (lease.state === "superseded" && !lease.supersededBy)
      throw new E03RuntimeError(
        "superseded_lease_target_missing",
        "superseded lease does not name its replacement",
      );
  }

  private decision(
    accepted: boolean,
    code: string,
    current: TaskLease,
    candidate: { leaseId: string; attempt: number },
  ): LeaseDecision {
    return {
      accepted,
      code,
      current: structuredClone(current),
      candidateLeaseId: candidate.leaseId,
      candidateAttempt: candidate.attempt,
    };
  }
}

function sealLease(value: Omit<TaskLease, "digest"> | TaskLease): TaskLease {
  const { digest: _digest, ...payload } = value as TaskLease;
  return { ...payload, digest: digest(payload) };
}

export function freshLeaseIdentity(
  taskId: string,
  attempt: number,
): Pick<E03Identity, "attemptId" | "leaseId" | "attempt"> {
  if (!taskId.trim() || !Number.isSafeInteger(attempt) || attempt < 1)
    throw new E03RuntimeError(
      "invalid_lease_identity",
      "task id and positive attempt are required",
    );
  return {
    attemptId: createId(`attempt-${taskId}`),
    leaseId: createId(`lease-${taskId}`),
    attempt,
  };
}

export interface TaskIdentityRecord {
  recordId: string;
  runId: string;
  sessionId: string;
  taskId: string;
  parentTaskId: string;
  parentSessionId: string;
  attemptId: string;
  attempt: number;
  leaseId: string;
  lineage: string[];
  idempotencyKey: string;
  identityDigest: string;
  createdAt: string;
  supersededAt: string | null;
  supersededByRecordId: string | null;
  revision: number;
  digest: string;
}

export interface TaskIdentityLookup {
  taskId: string;
  currentRecordId: string;
  currentAttempt: number;
  currentAttemptId: string;
  currentLeaseId: string;
  recordCount: number;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  lineage: string[];
  digest: string;
}

function assertIdentityRecord(record: TaskIdentityRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "identity_record_checksum",
      `identity record ${record.recordId} checksum mismatch`,
    );
  if (
    !record.taskId ||
    !record.sessionId ||
    !record.attemptId ||
    !record.leaseId ||
    !record.idempotencyKey ||
    record.attempt < 1 ||
    record.revision < 1
  )
    throw new E03RuntimeError(
      "identity_record_invalid",
      `identity record ${record.recordId} is invalid`,
    );
  if (record.attemptId !== `${record.taskId}:attempt:${record.attempt}`)
    throw new E03RuntimeError(
      "identity_record_attempt_mismatch",
      `identity record ${record.recordId} attempt id is invalid`,
    );
  if (digest(identityFromRecord(record)) !== record.identityDigest)
    throw new E03RuntimeError(
      "identity_record_identity_digest",
      `identity record ${record.recordId} identity digest mismatch`,
    );
  if (record.supersededAt && !record.supersededByRecordId)
    throw new E03RuntimeError(
      "identity_record_supersession_target",
      `identity record ${record.recordId} supersession target is missing`,
    );
}

function identityFromRecord(record: TaskIdentityRecord): E03Identity {
  return {
    runId: record.runId,
    sessionId: record.sessionId,
    taskId: record.taskId,
    parentTaskId: record.parentTaskId,
    parentSessionId: record.parentSessionId,
    attemptId: record.attemptId,
    attempt: record.attempt,
    leaseId: record.leaseId,
    lineage: [...record.lineage],
  };
}

export class TaskIdentityDirectory {
  private records = new Map<string, TaskIdentityRecord>();
  private byTask = new Map<string, string[]>();
  private byIdempotency = new Map<string, string>();

  register(input: {
    identity: E03Identity;
    idempotencyKey: string;
    now?: string;
  }): TaskIdentityRecord {
    const runtime = new TaskIdentityRuntime();
    runtime.validate(input.identity);
    const idempotencyKey = input.idempotencyKey.trim();
    if (!idempotencyKey)
      throw new E03RuntimeError(
        "identity_record_idempotency_missing",
        "identity record idempotency key is required",
      );
    const bound = this.byIdempotency.get(idempotencyKey);
    if (bound) {
      const record = this.records.get(bound)!;
      if (record.identityDigest !== digest(input.identity))
        throw new E03RuntimeError(
          "identity_record_idempotency_conflict",
          "identity idempotency key identifies another task attempt",
        );
      return structuredClone(record);
    }
    const currentRecords = this.byTask.get(input.identity.taskId) ?? [];
    const current = currentRecords.length
      ? this.records.get(currentRecords.at(-1)!)!
      : null;
    if (current) {
      if (
        input.identity.runId !== current.runId ||
        input.identity.sessionId !== current.sessionId
      )
        throw new E03RuntimeError(
          "identity_record_session_changed",
          "task identity cannot move to another run or session",
        );
      if (input.identity.parentTaskId !== current.parentTaskId)
        throw new E03RuntimeError(
          "identity_record_parent_changed",
          "task identity cannot change parent across attempts",
        );
      if (input.identity.attempt !== current.attempt + 1)
        throw new E03RuntimeError(
          "identity_record_attempt_gap",
          "task identity attempt must advance exactly once",
        );
      if (input.identity.leaseId === current.leaseId)
        throw new E03RuntimeError(
          "identity_record_lease_reused",
          "task identity attempt cannot reuse its prior lease",
        );
    } else if (input.identity.attempt !== 1)
      throw new E03RuntimeError(
        "identity_record_first_attempt",
        "first identity record must be attempt one",
      );
    const createdAt = input.now ?? new Date().toISOString();
    const recordId = `task-identity-record-${digest({
      taskId: input.identity.taskId,
      attempt: input.identity.attempt,
      leaseId: input.identity.leaseId,
    }).slice(0, 32)}`;
    if (current) {
      const { digest: _, ...prior } = current;
      const supersededPayload = {
        ...prior,
        supersededAt: createdAt,
        supersededByRecordId: recordId,
        revision: current.revision + 1,
      };
      const superseded = {
        ...supersededPayload,
        digest: digest(supersededPayload),
      };
      assertIdentityRecord(superseded);
      this.records.set(current.recordId, superseded);
    }
    const payload = {
      recordId,
      runId: input.identity.runId,
      sessionId: input.identity.sessionId,
      taskId: input.identity.taskId,
      parentTaskId: input.identity.parentTaskId,
      parentSessionId: input.identity.parentSessionId,
      attemptId: input.identity.attemptId,
      attempt: input.identity.attempt,
      leaseId: input.identity.leaseId,
      lineage: [...input.identity.lineage],
      idempotencyKey,
      identityDigest: digest(input.identity),
      createdAt,
      supersededAt: null,
      supersededByRecordId: null,
      revision: 1,
    };
    const record = { ...payload, digest: digest(payload) };
    assertIdentityRecord(record);
    this.records.set(record.recordId, record);
    currentRecords.push(record.recordId);
    this.byTask.set(record.taskId, currentRecords);
    this.byIdempotency.set(idempotencyKey, record.recordId);
    return structuredClone(record);
  }

  current(taskId: string): TaskIdentityRecord {
    const recordIds = this.byTask.get(taskId);
    if (!recordIds?.length)
      throw new E03RuntimeError(
        "task_identity_record_missing",
        `task ${taskId} identity is missing`,
      );
    const record = this.records.get(recordIds.at(-1)!)!;
    assertIdentityRecord(record);
    if (record.supersededAt)
      throw new E03RuntimeError(
        "task_identity_current_superseded",
        `task ${taskId} current identity is superseded`,
      );
    return structuredClone(record);
  }

  lookup(taskId: string): TaskIdentityLookup {
    const current = this.current(taskId);
    const recordCount = this.byTask.get(taskId)!.length;
    const payload = {
      taskId,
      currentRecordId: current.recordId,
      currentAttempt: current.attempt,
      currentAttemptId: current.attemptId,
      currentLeaseId: current.leaseId,
      recordCount,
      runId: current.runId,
      sessionId: current.sessionId,
      parentTaskId: current.parentTaskId,
      lineage: [...current.lineage],
    };
    return { ...payload, digest: digest(payload) };
  }

  assertCurrent(identity: E03Identity): TaskIdentityRecord {
    const current = this.current(identity.taskId);
    if (current.identityDigest !== digest(identity))
      throw new E03RuntimeError(
        "task_identity_stale",
        `task ${identity.taskId} identity is not current`,
        {
          currentAttempt: current.attempt,
          candidateAttempt: identity.attempt,
          currentLeaseId: current.leaseId,
          candidateLeaseId: identity.leaseId,
        },
      );
    return current;
  }

  history(taskId: string): TaskIdentityRecord[] {
    return (this.byTask.get(taskId) ?? []).map((recordId) =>
      structuredClone(this.records.get(recordId)!),
    );
  }

  restore(records: readonly TaskIdentityRecord[]): void {
    const next = new Map<string, TaskIdentityRecord>();
    const byTask = new Map<string, string[]>();
    const byIdempotency = new Map<string, string>();
    for (const raw of records) {
      const record = structuredClone(raw);
      assertIdentityRecord(record);
      if (next.has(record.recordId) || byIdempotency.has(record.idempotencyKey))
        throw new E03RuntimeError(
          "duplicate_task_identity_record",
          `identity record ${record.recordId} repeats`,
        );
      next.set(record.recordId, record);
      byIdempotency.set(record.idempotencyKey, record.recordId);
      const taskRecords = byTask.get(record.taskId) ?? [];
      taskRecords.push(record.recordId);
      byTask.set(record.taskId, taskRecords);
    }
    for (const [taskId, recordIds] of byTask) {
      recordIds.sort(
        (leftId, rightId) =>
          next.get(leftId)!.attempt - next.get(rightId)!.attempt,
      );
      for (let index = 0; index < recordIds.length; index += 1) {
        const record = next.get(recordIds[index]!)!;
        if (record.attempt !== index + 1)
          throw new E03RuntimeError(
            "task_identity_restore_attempt_gap",
            `task ${taskId} identity attempts are discontinuous`,
          );
        const nextRecord = recordIds[index + 1]
          ? next.get(recordIds[index + 1]!)!
          : null;
        if (nextRecord && record.supersededByRecordId !== nextRecord.recordId)
          throw new E03RuntimeError(
            "task_identity_restore_supersession",
            `task ${taskId} identity supersession chain is invalid`,
          );
        if (!nextRecord && record.supersededAt)
          throw new E03RuntimeError(
            "task_identity_restore_current_superseded",
            `task ${taskId} current identity is superseded`,
          );
      }
    }
    this.records = next;
    this.byTask = byTask;
    this.byIdempotency = byIdempotency;
  }

  snapshot(): TaskIdentityRecord[] {
    return [...this.records.values()]
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.attempt - right.attempt,
      )
      .map((record) => structuredClone(record));
  }
}

export interface TaskLineageNode {
  taskId: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  parentSessionId: string;
  depth: number;
  lineage: string[];
  childTaskIds: string[];
  registeredAt: string;
  revision: number;
  identityDigest: string;
  digest: string;
}

export interface TaskLineageRelation {
  leftTaskId: string;
  rightTaskId: string;
  relationship:
    | "same"
    | "ancestor"
    | "descendant"
    | "sibling"
    | "cousin"
    | "unrelated";
  commonAncestorTaskId: string | null;
  leftDistance: number;
  rightDistance: number;
  sameRun: boolean;
  sameSession: boolean;
  digest: string;
}

function assertLineageNode(node: TaskLineageNode): void {
  const { digest: checksum, ...payload } = node;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "lineage_node_checksum",
      `lineage node ${node.taskId} checksum mismatch`,
    );
  if (
    !node.taskId ||
    !node.runId ||
    !node.sessionId ||
    node.depth < 1 ||
    node.revision < 1
  )
    throw new E03RuntimeError(
      "lineage_node_identity",
      `lineage node ${node.taskId} identity is invalid`,
    );
  if (node.lineage.at(-1) !== node.parentTaskId)
    throw new E03RuntimeError(
      "lineage_node_parent",
      `lineage node ${node.taskId} does not end at its parent`,
    );
  if (
    node.lineage.includes(node.taskId) ||
    new Set(node.lineage).size !== node.lineage.length
  )
    throw new E03RuntimeError(
      "lineage_node_cycle",
      `lineage node ${node.taskId} contains a cycle`,
    );
}

function resealLineageNode(
  node: TaskLineageNode,
  patch: Partial<Omit<TaskLineageNode, "taskId" | "digest">>,
): TaskLineageNode {
  const { digest: _, ...prior } = node;
  const payload = { ...prior, ...patch, taskId: node.taskId };
  const next = { ...payload, digest: digest(payload) };
  assertLineageNode(next);
  return next;
}

export class TaskLineageGraph {
  private nodes = new Map<string, TaskLineageNode>();

  register(
    identity: E03Identity,
    now = new Date().toISOString(),
  ): TaskLineageNode {
    new TaskIdentityRuntime().validate(identity);
    const existing = this.nodes.get(identity.taskId);
    if (existing) {
      if (
        existing.runId !== identity.runId ||
        existing.sessionId !== identity.sessionId ||
        existing.parentTaskId !== identity.parentTaskId ||
        digest(identity.lineage) !== digest(existing.lineage)
      )
        throw new E03RuntimeError(
          "lineage_node_identity_changed",
          `task ${identity.taskId} lineage changed`,
        );
      const next = resealLineageNode(existing, {
        identityDigest: digest(identity),
        revision: existing.revision + 1,
      });
      this.nodes.set(next.taskId, next);
      return structuredClone(next);
    }
    const parent = this.nodes.get(identity.parentTaskId);
    if (parent) {
      if (identity.runId !== parent.runId)
        throw new E03RuntimeError(
          "lineage_cross_run_parent",
          "task and parent belong to different runs",
        );
      if (
        digest(identity.lineage) !== digest([...parent.lineage, parent.taskId])
      )
        throw new E03RuntimeError(
          "lineage_parent_chain_mismatch",
          "task lineage does not extend registered parent lineage",
        );
    }
    const payload = {
      taskId: identity.taskId,
      runId: identity.runId,
      sessionId: identity.sessionId,
      parentTaskId: identity.parentTaskId,
      parentSessionId: identity.parentSessionId,
      depth: identity.lineage.length,
      lineage: [...identity.lineage],
      childTaskIds: [],
      registeredAt: now,
      revision: 1,
      identityDigest: digest(identity),
    };
    const node = { ...payload, digest: digest(payload) };
    assertLineageNode(node);
    this.nodes.set(node.taskId, node);
    if (parent) {
      const parentNext = resealLineageNode(parent, {
        childTaskIds: [
          ...new Set([...parent.childTaskIds, node.taskId]),
        ].sort(),
        revision: parent.revision + 1,
      });
      this.nodes.set(parentNext.taskId, parentNext);
    }
    return structuredClone(node);
  }

  relation(leftTaskId: string, rightTaskId: string): TaskLineageRelation {
    const left = this.require(leftTaskId);
    const right = this.require(rightTaskId);
    const leftPath = [...left.lineage, left.taskId];
    const rightPath = [...right.lineage, right.taskId];
    const sameRun = left.runId === right.runId;
    const sameSession = left.sessionId === right.sessionId;
    let relationship: TaskLineageRelation["relationship"] = "unrelated";
    if (left.taskId === right.taskId) relationship = "same";
    else if (right.lineage.includes(left.taskId)) relationship = "ancestor";
    else if (left.lineage.includes(right.taskId)) relationship = "descendant";
    else if (left.parentTaskId === right.parentTaskId) relationship = "sibling";
    else if (sameRun) relationship = "cousin";
    let commonAncestorTaskId: string | null = null;
    for (const taskId of leftPath)
      if (rightPath.includes(taskId)) commonAncestorTaskId = taskId;
    const leftIndex = commonAncestorTaskId
      ? leftPath.lastIndexOf(commonAncestorTaskId)
      : -1;
    const rightIndex = commonAncestorTaskId
      ? rightPath.lastIndexOf(commonAncestorTaskId)
      : -1;
    const payload = {
      leftTaskId,
      rightTaskId,
      relationship,
      commonAncestorTaskId,
      leftDistance: leftIndex < 0 ? -1 : leftPath.length - leftIndex - 1,
      rightDistance: rightIndex < 0 ? -1 : rightPath.length - rightIndex - 1,
      sameRun,
      sameSession,
    };
    return { ...payload, digest: digest(payload) };
  }

  ancestors(taskId: string): TaskLineageNode[] {
    const node = this.require(taskId);
    return node.lineage
      .map((ancestorTaskId) => this.nodes.get(ancestorTaskId))
      .filter((value): value is TaskLineageNode => Boolean(value))
      .map((value) => structuredClone(value));
  }

  descendants(taskId: string): TaskLineageNode[] {
    this.require(taskId);
    const descendants: TaskLineageNode[] = [];
    const queue = [...(this.nodes.get(taskId)?.childTaskIds ?? [])];
    const seen = new Set<string>();
    while (queue.length) {
      const childTaskId = queue.shift()!;
      if (seen.has(childTaskId))
        throw new E03RuntimeError(
          "lineage_graph_cycle",
          `lineage graph cycle reached ${childTaskId}`,
        );
      seen.add(childTaskId);
      const child = this.require(childTaskId);
      descendants.push(structuredClone(child));
      queue.push(...child.childTaskIds);
    }
    return descendants.sort(
      (left, right) =>
        left.depth - right.depth || left.taskId.localeCompare(right.taskId),
    );
  }

  assertParent(identity: E03Identity): TaskLineageNode {
    const parent = this.require(identity.parentTaskId);
    if (
      parent.runId !== identity.runId ||
      parent.sessionId !== identity.parentSessionId ||
      digest([...parent.lineage, parent.taskId]) !== digest(identity.lineage)
    )
      throw new E03RuntimeError(
        "lineage_parent_identity_mismatch",
        `task ${identity.taskId} identity does not extend parent`,
      );
    return parent;
  }

  restore(nodes: readonly TaskLineageNode[]): void {
    const next = new Map<string, TaskLineageNode>();
    for (const raw of nodes) {
      const node = structuredClone(raw);
      assertLineageNode(node);
      if (next.has(node.taskId))
        throw new E03RuntimeError(
          "duplicate_lineage_node",
          `lineage node ${node.taskId} repeats`,
        );
      next.set(node.taskId, node);
    }
    for (const node of next.values()) {
      const parent = next.get(node.parentTaskId);
      if (parent) {
        if (!parent.childTaskIds.includes(node.taskId))
          throw new E03RuntimeError(
            "lineage_parent_child_projection",
            `parent ${parent.taskId} does not project child ${node.taskId}`,
          );
        if (digest([...parent.lineage, parent.taskId]) !== digest(node.lineage))
          throw new E03RuntimeError(
            "lineage_restore_parent_chain",
            `lineage node ${node.taskId} does not extend parent chain`,
          );
      }
      for (const childTaskId of node.childTaskIds) {
        const child = next.get(childTaskId);
        if (!child || child.parentTaskId !== node.taskId)
          throw new E03RuntimeError(
            "lineage_restore_child_missing",
            `lineage child ${childTaskId} is missing or points elsewhere`,
          );
      }
    }
    this.nodes = next;
  }

  snapshot(): TaskLineageNode[] {
    return [...this.nodes.values()]
      .sort(
        (left, right) =>
          left.depth - right.depth || left.taskId.localeCompare(right.taskId),
      )
      .map((node) => structuredClone(node));
  }

  private require(taskId: string): TaskLineageNode {
    const node = this.nodes.get(taskId);
    if (!node)
      throw new E03RuntimeError(
        "lineage_node_missing",
        `lineage node ${taskId} is missing`,
      );
    assertLineageNode(node);
    return node;
  }
}

export type TaskAttemptPhase =
  | "allocated"
  | "leased"
  | "started"
  | "waiting"
  | "resumed"
  | "completed"
  | "failed"
  | "cancelled"
  | "killed"
  | "superseded";

export interface TaskAttemptRecord {
  attemptRecordId: string;
  taskId: string;
  sessionId: string;
  attemptId: string;
  attempt: number;
  leaseId: string;
  phase: TaskAttemptPhase;
  expectedTaskRevision: number;
  firstTaskRevision: number;
  lastTaskRevision: number;
  workerId: string | null;
  priorAttemptRecordId: string | null;
  nextAttemptRecordId: string | null;
  reason: string;
  resultDigest: string | null;
  errorDigest: string | null;
  allocatedAt: string;
  startedAt: string | null;
  terminalAt: string | null;
  revision: number;
  digest: string;
}

export interface AttemptFenceDecision {
  accepted: boolean;
  code: string;
  taskId: string;
  candidateAttempt: number;
  currentAttempt: number;
  candidateLeaseId: string;
  currentLeaseId: string;
  candidateTaskRevision: number;
  currentTaskRevision: number;
  candidatePhase: TaskAttemptPhase;
  currentPhase: TaskAttemptPhase;
  decidedAt: string;
  digest: string;
}

function assertAttemptRecord(record: TaskAttemptRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "attempt_record_checksum",
      `attempt record ${record.attemptRecordId} checksum mismatch`,
    );
  if (
    !record.taskId ||
    !record.attemptId ||
    !record.leaseId ||
    record.attempt < 1 ||
    record.firstTaskRevision < 1 ||
    record.lastTaskRevision < record.firstTaskRevision ||
    record.revision < 1
  )
    throw new E03RuntimeError(
      "attempt_record_invalid",
      `attempt record ${record.attemptRecordId} is invalid`,
    );
  if (record.attemptId !== `${record.taskId}:attempt:${record.attempt}`)
    throw new E03RuntimeError(
      "attempt_record_identity",
      `attempt record ${record.attemptRecordId} attempt id is invalid`,
    );
  if (
    ["completed", "failed", "cancelled", "killed", "superseded"].includes(
      record.phase,
    ) &&
    !record.terminalAt
  )
    throw new E03RuntimeError(
      "attempt_record_terminal_time",
      `terminal attempt record ${record.attemptRecordId} has no terminal time`,
    );
}

function resealAttemptRecord(
  record: TaskAttemptRecord,
  patch: Partial<
    Omit<
      TaskAttemptRecord,
      "attemptRecordId" | "taskId" | "attemptId" | "digest"
    >
  >,
): TaskAttemptRecord {
  const { digest: _, ...prior } = record;
  const payload = {
    ...prior,
    ...patch,
    attemptRecordId: record.attemptRecordId,
    taskId: record.taskId,
    attemptId: record.attemptId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertAttemptRecord(next);
  return next;
}

const ATTEMPT_PHASES: Readonly<
  Record<TaskAttemptPhase, readonly TaskAttemptPhase[]>
> = Object.freeze({
  allocated: ["leased", "cancelled", "killed", "superseded"],
  leased: ["started", "cancelled", "killed", "superseded"],
  started: ["waiting", "completed", "failed", "cancelled", "killed"],
  waiting: ["resumed", "completed", "failed", "cancelled", "killed"],
  resumed: ["waiting", "completed", "failed", "cancelled", "killed"],
  completed: [],
  failed: ["superseded"],
  cancelled: [],
  killed: [],
  superseded: [],
});

export class TaskAttemptJournal {
  private attempts = new Map<string, TaskAttemptRecord>();
  private byTask = new Map<string, string[]>();

  allocate(
    task: E03TaskState,
    now = new Date().toISOString(),
  ): TaskAttemptRecord {
    const currentIds = this.byTask.get(task.identity.taskId) ?? [];
    const current = currentIds.length
      ? this.attempts.get(currentIds.at(-1)!)!
      : null;
    if (current) {
      if (task.identity.attempt !== current.attempt + 1)
        throw new E03RuntimeError(
          "attempt_journal_attempt_gap",
          "new task attempt must advance exactly once",
        );
      if (current.phase !== "failed" && current.phase !== "superseded")
        throw new E03RuntimeError(
          "attempt_journal_current_active",
          `cannot allocate attempt while prior attempt is ${current.phase}`,
        );
      if (current.leaseId === task.identity.leaseId)
        throw new E03RuntimeError(
          "attempt_journal_lease_reused",
          "new task attempt cannot reuse prior lease",
        );
    } else if (task.identity.attempt !== 1)
      throw new E03RuntimeError(
        "attempt_journal_first_attempt",
        "first task attempt must be one",
      );
    const attemptRecordId = `task-attempt-record-${digest({
      taskId: task.identity.taskId,
      attemptId: task.identity.attemptId,
      leaseId: task.identity.leaseId,
    }).slice(0, 32)}`;
    if (current && current.phase !== "superseded") {
      const superseded = resealAttemptRecord(current, {
        phase: "superseded",
        nextAttemptRecordId: attemptRecordId,
        reason: current.reason || "retry_superseded",
        terminalAt: current.terminalAt ?? now,
        revision: current.revision + 1,
      });
      this.attempts.set(current.attemptRecordId, superseded);
    }
    const payload = {
      attemptRecordId,
      taskId: task.identity.taskId,
      sessionId: task.identity.sessionId,
      attemptId: task.identity.attemptId,
      attempt: task.identity.attempt,
      leaseId: task.identity.leaseId,
      phase: "allocated" as const,
      expectedTaskRevision: task.revision,
      firstTaskRevision: task.revision,
      lastTaskRevision: task.revision,
      workerId: null,
      priorAttemptRecordId: current?.attemptRecordId ?? null,
      nextAttemptRecordId: null,
      reason: "",
      resultDigest: null,
      errorDigest: null,
      allocatedAt: now,
      startedAt: null,
      terminalAt: null,
      revision: 1,
    };
    const record = { ...payload, digest: digest(payload) };
    assertAttemptRecord(record);
    this.attempts.set(record.attemptRecordId, record);
    currentIds.push(record.attemptRecordId);
    this.byTask.set(record.taskId, currentIds);
    return structuredClone(record);
  }

  transition(input: {
    task: E03TaskState;
    phase: TaskAttemptPhase;
    expectedRecordRevision: number;
    workerId?: string;
    reason?: string;
    result?: unknown;
    error?: string;
    now?: string;
  }): TaskAttemptRecord {
    const current = this.current(input.task.identity.taskId);
    const decision = this.decide({
      task: input.task,
      candidatePhase: input.phase,
      expectedRecordRevision: input.expectedRecordRevision,
      now: input.now,
    });
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `task attempt ${current.attemptRecordId} rejected ${input.phase}`,
      );
    const now = input.now ?? new Date().toISOString();
    const terminal = [
      "completed",
      "failed",
      "cancelled",
      "killed",
      "superseded",
    ].includes(input.phase);
    const next = resealAttemptRecord(current, {
      phase: input.phase,
      expectedTaskRevision: input.task.revision,
      lastTaskRevision: input.task.revision,
      workerId: input.workerId?.trim() || current.workerId,
      reason: input.reason?.trim() || current.reason,
      resultDigest:
        input.phase === "completed"
          ? digest(input.result ?? input.task.result)
          : current.resultDigest,
      errorDigest:
        input.phase === "failed" ||
        input.phase === "cancelled" ||
        input.phase === "killed"
          ? digest(input.error ?? input.task.error)
          : current.errorDigest,
      startedAt:
        input.phase === "started"
          ? (current.startedAt ?? now)
          : current.startedAt,
      terminalAt: terminal ? (current.terminalAt ?? now) : null,
      revision: current.revision + 1,
    });
    this.attempts.set(next.attemptRecordId, next);
    return structuredClone(next);
  }

  decide(input: {
    task: E03TaskState;
    candidatePhase: TaskAttemptPhase;
    expectedRecordRevision: number;
    now?: string;
  }): AttemptFenceDecision {
    const current = this.current(input.task.identity.taskId);
    let code = "attempt_fence_accepted";
    if (current.attempt !== input.task.identity.attempt)
      code = "attempt_fence_stale_attempt";
    else if (current.leaseId !== input.task.identity.leaseId)
      code = "attempt_fence_stale_lease";
    else if (current.revision !== input.expectedRecordRevision)
      code = "attempt_fence_stale_record_revision";
    else if (input.task.revision < current.lastTaskRevision)
      code = "attempt_fence_stale_task_revision";
    else if (!ATTEMPT_PHASES[current.phase].includes(input.candidatePhase))
      code = "attempt_fence_phase_denied";
    const payload = {
      accepted: code === "attempt_fence_accepted",
      code,
      taskId: input.task.identity.taskId,
      candidateAttempt: input.task.identity.attempt,
      currentAttempt: current.attempt,
      candidateLeaseId: input.task.identity.leaseId,
      currentLeaseId: current.leaseId,
      candidateTaskRevision: input.task.revision,
      currentTaskRevision: current.lastTaskRevision,
      candidatePhase: input.candidatePhase,
      currentPhase: current.phase,
      decidedAt: input.now ?? new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  current(taskId: string): TaskAttemptRecord {
    const ids = this.byTask.get(taskId);
    if (!ids?.length)
      throw new E03RuntimeError(
        "task_attempt_record_missing",
        `task ${taskId} attempt record is missing`,
      );
    const record = this.attempts.get(ids.at(-1)!)!;
    assertAttemptRecord(record);
    return structuredClone(record);
  }

  history(taskId: string): TaskAttemptRecord[] {
    return (this.byTask.get(taskId) ?? []).map((recordId) =>
      structuredClone(this.attempts.get(recordId)!),
    );
  }

  restore(records: readonly TaskAttemptRecord[]): void {
    const next = new Map<string, TaskAttemptRecord>();
    const byTask = new Map<string, string[]>();
    for (const raw of records) {
      const record = structuredClone(raw);
      assertAttemptRecord(record);
      if (next.has(record.attemptRecordId))
        throw new E03RuntimeError(
          "duplicate_task_attempt_record",
          `task attempt record ${record.attemptRecordId} repeats`,
        );
      next.set(record.attemptRecordId, record);
      const ids = byTask.get(record.taskId) ?? [];
      ids.push(record.attemptRecordId);
      byTask.set(record.taskId, ids);
    }
    for (const [taskId, ids] of byTask) {
      ids.sort(
        (leftId, rightId) =>
          next.get(leftId)!.attempt - next.get(rightId)!.attempt,
      );
      for (let index = 0; index < ids.length; index += 1) {
        const record = next.get(ids[index]!)!;
        if (record.attempt !== index + 1)
          throw new E03RuntimeError(
            "task_attempt_restore_gap",
            `task ${taskId} attempts are discontinuous`,
          );
        const prior = index ? next.get(ids[index - 1]!)! : null;
        const following = ids[index + 1] ? next.get(ids[index + 1]!)! : null;
        if (record.priorAttemptRecordId !== (prior?.attemptRecordId ?? null))
          throw new E03RuntimeError(
            "task_attempt_restore_prior",
            `task ${taskId} attempt prior link is invalid`,
          );
        if (
          following &&
          record.nextAttemptRecordId !== following.attemptRecordId
        )
          throw new E03RuntimeError(
            "task_attempt_restore_next",
            `task ${taskId} attempt next link is invalid`,
          );
      }
    }
    this.attempts = next;
    this.byTask = byTask;
  }

  snapshot(): TaskAttemptRecord[] {
    return [...this.attempts.values()]
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.attempt - right.attempt,
      )
      .map((record) => structuredClone(record));
  }
}

export type TaskIdentityReservationState =
  | "reserved"
  | "bound"
  | "released"
  | "expired"
  | "conflicted";

export interface TaskIdentityReservation {
  reservationId: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  requestedTaskId: string | null;
  allocatedTaskId: string;
  idempotencyKey: string;
  ownerId: string;
  state: TaskIdentityReservationState;
  identityDigest: string | null;
  reservedAt: string;
  expiresAt: string;
  boundAt: string | null;
  releasedAt: string | null;
  conflictReason: string | null;
  revision: number;
  digest: string;
}

export interface TaskIdentityAlias {
  aliasId: string;
  runId: string;
  sessionId: string;
  taskId: string;
  alias: string;
  namespace: "external" | "display" | "provider" | "legacy";
  active: boolean;
  createdAt: string;
  retiredAt: string | null;
  digest: string;
}

function assertTaskIdentityReservation(
  reservation: TaskIdentityReservation,
): void {
  const { digest: checksum, ...payload } = reservation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_identity_reservation_digest",
      `task identity reservation ${reservation.reservationId} digest is invalid`,
    );
  if (
    !reservation.reservationId ||
    !reservation.runId ||
    !reservation.sessionId ||
    !reservation.parentTaskId ||
    !reservation.allocatedTaskId ||
    !reservation.idempotencyKey ||
    !reservation.ownerId
  )
    throw new E03RuntimeError(
      "task_identity_reservation_identity",
      "task identity reservation identity is incomplete",
    );
  if (Date.parse(reservation.reservedAt) >= Date.parse(reservation.expiresAt))
    throw new E03RuntimeError(
      "task_identity_reservation_expiry",
      "task identity reservation expiry is invalid",
    );
  if (!Number.isSafeInteger(reservation.revision) || reservation.revision < 1)
    throw new E03RuntimeError(
      "task_identity_reservation_revision",
      "task identity reservation revision is invalid",
    );
  if (
    reservation.state === "bound" &&
    (!reservation.identityDigest || !reservation.boundAt)
  )
    throw new E03RuntimeError(
      "task_identity_reservation_state",
      "bound task identity reservation requires identity metadata",
    );
  if (reservation.state === "conflicted" && !reservation.conflictReason)
    throw new E03RuntimeError(
      "task_identity_reservation_state",
      "conflicted task identity reservation requires a reason",
    );
}

function assertTaskIdentityAlias(alias: TaskIdentityAlias): void {
  const { digest: checksum, ...payload } = alias;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_identity_alias_digest",
      `task identity alias ${alias.aliasId} digest is invalid`,
    );
  if (
    !alias.aliasId ||
    !alias.runId ||
    !alias.sessionId ||
    !alias.taskId ||
    !alias.alias
  )
    throw new E03RuntimeError(
      "task_identity_alias_identity",
      "task identity alias identity is incomplete",
    );
  if (!alias.active && alias.retiredAt === null)
    throw new E03RuntimeError(
      "task_identity_alias_state",
      "retired task identity alias requires retiredAt",
    );
}

export class TaskIdentityReservationRuntime {
  private reservations = new Map<string, TaskIdentityReservation>();
  private byIdempotency = new Map<string, string>();
  private byTaskId = new Map<string, string>();
  private aliases = new Map<string, TaskIdentityAlias>();
  private aliasLookup = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  reserve(input: {
    runId: string;
    sessionId: string;
    parentTaskId: string;
    requestedTaskId?: string | null;
    idempotencyKey: string;
    ownerId: string;
    expiresAt: string;
  }): TaskIdentityReservation {
    const priorId = this.byIdempotency.get(input.idempotencyKey);
    if (priorId) return structuredClone(this.requireReservation(priorId));
    const requestedTaskId = input.requestedTaskId?.trim() || null;
    const allocatedTaskId = requestedTaskId ?? createId("task");
    const existingId = this.byTaskId.get(allocatedTaskId);
    if (existingId) {
      const existing = this.requireReservation(existingId);
      if (
        existing.runId !== input.runId ||
        existing.sessionId !== input.sessionId ||
        existing.parentTaskId !== input.parentTaskId
      )
        throw new E03RuntimeError(
          "task_identity_reservation_conflict",
          `task identity ${allocatedTaskId} is reserved by another lineage`,
        );
      return structuredClone(existing);
    }
    const now = this.clock.now();
    const payload = {
      reservationId: createId("task-identity-reservation"),
      runId: input.runId.trim(),
      sessionId: input.sessionId.trim(),
      parentTaskId: input.parentTaskId.trim(),
      requestedTaskId,
      allocatedTaskId,
      idempotencyKey: input.idempotencyKey.trim(),
      ownerId: input.ownerId.trim(),
      state: "reserved" as const,
      identityDigest: null,
      reservedAt: now,
      expiresAt: input.expiresAt,
      boundAt: null,
      releasedAt: null,
      conflictReason: null,
      revision: 1,
    };
    const reservation = { ...payload, digest: digest(payload) };
    assertTaskIdentityReservation(reservation);
    this.reservations.set(reservation.reservationId, reservation);
    this.byIdempotency.set(
      reservation.idempotencyKey,
      reservation.reservationId,
    );
    this.byTaskId.set(reservation.allocatedTaskId, reservation.reservationId);
    return structuredClone(reservation);
  }

  bind(
    reservationId: string,
    identity: E03Identity,
    expectedRevision: number,
  ): TaskIdentityReservation {
    const reservation = this.requireReservation(reservationId);
    this.assertRevision(reservation, expectedRevision);
    if (reservation.state !== "reserved")
      throw new E03RuntimeError(
        "task_identity_reservation_bind_state",
        `task identity reservation ${reservationId} is ${reservation.state}`,
      );
    if (
      identity.runId !== reservation.runId ||
      identity.sessionId !== reservation.sessionId ||
      identity.parentTaskId !== reservation.parentTaskId ||
      identity.taskId !== reservation.allocatedTaskId
    )
      return this.transition(reservation, {
        state: "conflicted",
        conflictReason: "allocated identity does not match reservation custody",
      });
    return this.transition(reservation, {
      state: "bound",
      identityDigest: digest(identity),
      boundAt: this.clock.now(),
    });
  }

  release(
    reservationId: string,
    ownerId: string,
    expectedRevision: number,
  ): TaskIdentityReservation {
    const reservation = this.requireReservation(reservationId);
    this.assertRevision(reservation, expectedRevision);
    if (reservation.ownerId !== ownerId)
      throw new E03RuntimeError(
        "task_identity_reservation_owner",
        `task identity reservation ${reservationId} belongs to another owner`,
      );
    if (reservation.state === "released") return structuredClone(reservation);
    if (reservation.state !== "reserved" && reservation.state !== "bound")
      throw new E03RuntimeError(
        "task_identity_reservation_release_state",
        `task identity reservation ${reservationId} is ${reservation.state}`,
      );
    const next = this.transition(reservation, {
      state: "released",
      releasedAt: this.clock.now(),
    });
    this.byTaskId.delete(reservation.allocatedTaskId);
    return next;
  }

  sweep(now = this.clock.now()): TaskIdentityReservation[] {
    const expired: TaskIdentityReservation[] = [];
    for (const reservation of this.reservations.values()) {
      if (
        reservation.state === "reserved" &&
        Date.parse(now) >= Date.parse(reservation.expiresAt)
      ) {
        expired.push(this.transition(reservation, { state: "expired" }));
        this.byTaskId.delete(reservation.allocatedTaskId);
      }
    }
    return expired;
  }

  addAlias(input: {
    identity: E03Identity;
    alias: string;
    namespace: TaskIdentityAlias["namespace"];
    createdAt?: string;
  }): TaskIdentityAlias {
    const alias = input.alias.trim();
    const key = `${input.identity.runId}\0${input.namespace}\0${alias}`;
    const existingId = this.aliasLookup.get(key);
    if (existingId) {
      const existing = this.aliases.get(existingId)!;
      if (existing.taskId !== input.identity.taskId)
        throw new E03RuntimeError(
          "task_identity_alias_conflict",
          `task identity alias ${alias} belongs to another task`,
        );
      return structuredClone(existing);
    }
    const payload = {
      aliasId: createId("task-identity-alias"),
      runId: input.identity.runId,
      sessionId: input.identity.sessionId,
      taskId: input.identity.taskId,
      alias,
      namespace: input.namespace,
      active: true,
      createdAt: input.createdAt ?? this.clock.now(),
      retiredAt: null,
    };
    const record = { ...payload, digest: digest(payload) };
    assertTaskIdentityAlias(record);
    this.aliases.set(record.aliasId, record);
    this.aliasLookup.set(key, record.aliasId);
    return structuredClone(record);
  }

  resolveAlias(
    runId: string,
    namespace: TaskIdentityAlias["namespace"],
    alias: string,
  ): TaskIdentityAlias | null {
    const aliasId = this.aliasLookup.get(
      `${runId}\0${namespace}\0${alias.trim()}`,
    );
    if (!aliasId) return null;
    const record = this.aliases.get(aliasId);
    if (!record || !record.active) return null;
    assertTaskIdentityAlias(record);
    return structuredClone(record);
  }

  retireAlias(aliasId: string): TaskIdentityAlias {
    const alias = this.aliases.get(aliasId);
    if (!alias)
      throw new E03RuntimeError(
        "task_identity_alias_missing",
        `task identity alias ${aliasId} does not exist`,
      );
    if (!alias.active) return structuredClone(alias);
    const { digest: _, ...prior } = alias;
    const payload = { ...prior, active: false, retiredAt: this.clock.now() };
    const next = { ...payload, digest: digest(payload) };
    assertTaskIdentityAlias(next);
    this.aliases.set(aliasId, next);
    this.aliasLookup.delete(
      `${alias.runId}\0${alias.namespace}\0${alias.alias}`,
    );
    return structuredClone(next);
  }

  snapshot(): {
    reservations: TaskIdentityReservation[];
    aliases: TaskIdentityAlias[];
  } {
    return {
      reservations: [...this.reservations.values()].map((value) =>
        structuredClone(value),
      ),
      aliases: [...this.aliases.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    reservations: readonly TaskIdentityReservation[];
    aliases: readonly TaskIdentityAlias[];
  }): void {
    const reservations = new Map<string, TaskIdentityReservation>();
    const byIdempotency = new Map<string, string>();
    const byTaskId = new Map<string, string>();
    const aliases = new Map<string, TaskIdentityAlias>();
    const aliasLookup = new Map<string, string>();
    for (const reservation of input.reservations) {
      assertTaskIdentityReservation(reservation);
      if (
        reservations.has(reservation.reservationId) ||
        byIdempotency.has(reservation.idempotencyKey)
      )
        throw new E03RuntimeError(
          "task_identity_reservation_restore_duplicate",
          `duplicate task identity reservation ${reservation.reservationId}`,
        );
      reservations.set(reservation.reservationId, structuredClone(reservation));
      byIdempotency.set(reservation.idempotencyKey, reservation.reservationId);
      if (["reserved", "bound"].includes(reservation.state)) {
        if (byTaskId.has(reservation.allocatedTaskId))
          throw new E03RuntimeError(
            "task_identity_reservation_restore_task_duplicate",
            `task identity ${reservation.allocatedTaskId} has multiple active reservations`,
          );
        byTaskId.set(reservation.allocatedTaskId, reservation.reservationId);
      }
    }
    for (const alias of input.aliases) {
      assertTaskIdentityAlias(alias);
      if (aliases.has(alias.aliasId))
        throw new E03RuntimeError(
          "task_identity_alias_restore_duplicate",
          `duplicate task identity alias ${alias.aliasId}`,
        );
      aliases.set(alias.aliasId, structuredClone(alias));
      if (alias.active) {
        const key = `${alias.runId}\0${alias.namespace}\0${alias.alias}`;
        if (aliasLookup.has(key))
          throw new E03RuntimeError(
            "task_identity_alias_restore_conflict",
            `duplicate active task identity alias ${alias.alias}`,
          );
        aliasLookup.set(key, alias.aliasId);
      }
    }
    this.reservations = reservations;
    this.byIdempotency = byIdempotency;
    this.byTaskId = byTaskId;
    this.aliases = aliases;
    this.aliasLookup = aliasLookup;
  }

  private requireReservation(reservationId: string): TaskIdentityReservation {
    const reservation = this.reservations.get(reservationId);
    if (!reservation)
      throw new E03RuntimeError(
        "task_identity_reservation_missing",
        `task identity reservation ${reservationId} does not exist`,
      );
    assertTaskIdentityReservation(reservation);
    return reservation;
  }

  private assertRevision(
    reservation: TaskIdentityReservation,
    expected: number,
  ): void {
    if (reservation.revision !== expected)
      throw new E03RuntimeError(
        "task_identity_reservation_stale_revision",
        `task identity reservation ${reservation.reservationId} revision is stale`,
      );
  }

  private transition(
    reservation: TaskIdentityReservation,
    patch: Partial<
      Omit<TaskIdentityReservation, "reservationId" | "revision" | "digest">
    >,
  ): TaskIdentityReservation {
    const { digest: _, ...prior } = reservation;
    const payload = {
      ...prior,
      ...patch,
      reservationId: reservation.reservationId,
      revision: reservation.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskIdentityReservation(next);
    this.reservations.set(next.reservationId, next);
    return structuredClone(next);
  }
}

export interface TaskIdentityEpoch {
  epochId: string;
  taskId: string;
  runId: string;
  sessionId: string;
  epoch: number;
  state: "proposed" | "active" | "retiring" | "revoked";
  predecessorEpochId: string | null;
  leaseId: string;
  attempt: number;
  identityDigest: string;
  activationNonceDigest: string;
  proposedAt: string;
  activatedAt: string | null;
  retiredAt: string | null;
  revokedAt: string | null;
  revokeReason: string | null;
  revision: number;
  digest: string;
}
export interface TaskIdentityProof {
  proofId: string;
  epochId: string;
  taskId: string;
  purpose: "dispatch" | "commit" | "resume" | "delivery" | "merge";
  nonceDigest: string;
  payloadDigest: string;
  expiresAt: string;
  consumedAt: string | null;
  state: "issued" | "consumed" | "expired" | "revoked";
  issuedAt: string;
  revision: number;
  digest: string;
}
function assertIdentityEpoch(value: TaskIdentityEpoch): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_identity_epoch_digest",
      `task identity epoch ${value.epochId} is corrupt`,
    );
  if (
    !value.epochId ||
    !value.taskId ||
    !value.runId ||
    !value.sessionId ||
    !value.leaseId ||
    !value.identityDigest ||
    !value.activationNonceDigest ||
    !Number.isSafeInteger(value.epoch) ||
    value.epoch < 1 ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "task_identity_epoch",
      `task identity epoch ${value.epochId} is invalid`,
    );
  if (value.state === "active" && value.activatedAt === null)
    throw new E03RuntimeError(
      "task_identity_epoch_activation",
      `active identity epoch ${value.epochId} lacks time`,
    );
}
function assertIdentityProof(value: TaskIdentityProof): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_identity_proof_digest",
      `task identity proof ${value.proofId} is corrupt`,
    );
  if (
    !value.proofId ||
    !value.epochId ||
    !value.taskId ||
    !value.nonceDigest ||
    !value.payloadDigest ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "task_identity_proof",
      `task identity proof ${value.proofId} is invalid`,
    );
  if (value.state === "consumed" && value.consumedAt === null)
    throw new E03RuntimeError(
      "task_identity_proof_consumption",
      `consumed identity proof ${value.proofId} lacks time`,
    );
}
export class TaskIdentityEpochRuntime {
  private epochs = new Map<string, TaskIdentityEpoch>();
  private activeByTask = new Map<string, string>();
  private proofs = new Map<string, TaskIdentityProof>();
  private nonceIndex = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  propose(input: {
    identity: E03TaskState["identity"];
    activationNonce: string;
    predecessorEpochId?: string | null;
  }): TaskIdentityEpoch {
    if (!input.activationNonce)
      throw new E03RuntimeError(
        "task_identity_epoch_nonce",
        "task identity epoch activation nonce is required",
      );
    const activeId = this.activeByTask.get(input.identity.taskId);
    const predecessorEpochId = input.predecessorEpochId ?? activeId ?? null;
    let epoch = 1;
    if (predecessorEpochId) {
      const predecessor = this.requireEpoch(predecessorEpochId);
      if (
        predecessor.taskId !== input.identity.taskId ||
        predecessor.runId !== input.identity.runId ||
        predecessor.sessionId !== input.identity.sessionId
      )
        throw new E03RuntimeError(
          "task_identity_epoch_custody",
          "task identity epoch predecessor has different custody",
        );
      if (predecessor.state !== "active" && predecessor.state !== "retiring")
        throw new E03RuntimeError(
          "task_identity_epoch_predecessor_state",
          `task identity epoch predecessor is ${predecessor.state}`,
        );
      epoch = predecessor.epoch + 1;
    }
    if (
      [...this.epochs.values()].some(
        (value) =>
          value.taskId === input.identity.taskId &&
          value.epoch === epoch &&
          value.state !== "revoked",
      )
    )
      throw new E03RuntimeError(
        "task_identity_epoch_duplicate",
        `task ${input.identity.taskId} already has identity epoch ${epoch}`,
      );
    const payload = {
      epochId: createId("task-identity-epoch"),
      taskId: input.identity.taskId,
      runId: input.identity.runId,
      sessionId: input.identity.sessionId,
      epoch,
      state: "proposed" as const,
      predecessorEpochId,
      leaseId: input.identity.leaseId,
      attempt: input.identity.attempt,
      identityDigest: digest(input.identity),
      activationNonceDigest: digest(input.activationNonce),
      proposedAt: this.clock.now(),
      activatedAt: null,
      retiredAt: null,
      revokedAt: null,
      revokeReason: null,
      revision: 1,
    };
    const identityEpoch = { ...payload, digest: digest(payload) };
    assertIdentityEpoch(identityEpoch);
    this.epochs.set(identityEpoch.epochId, identityEpoch);
    return structuredClone(identityEpoch);
  }
  activate(
    epochId: string,
    expectedRevision: number,
    activationNonce: string,
  ): { epoch: TaskIdentityEpoch; predecessor: TaskIdentityEpoch | null } {
    const epoch = this.requireEpoch(epochId);
    this.assertEpochRevision(epoch, expectedRevision);
    if (epoch.state !== "proposed")
      throw new E03RuntimeError(
        "task_identity_epoch_activate_state",
        `task identity epoch ${epochId} is ${epoch.state}`,
      );
    if (epoch.activationNonceDigest !== digest(activationNonce))
      throw new E03RuntimeError(
        "task_identity_epoch_activation_nonce",
        `task identity epoch ${epochId} activation nonce is invalid`,
      );
    const activeId = this.activeByTask.get(epoch.taskId);
    if (activeId && activeId !== epoch.predecessorEpochId)
      throw new E03RuntimeError(
        "task_identity_epoch_active_changed",
        `task ${epoch.taskId} active identity epoch changed`,
      );
    let predecessor: TaskIdentityEpoch | null = null;
    if (epoch.predecessorEpochId) {
      const prior = this.requireEpoch(epoch.predecessorEpochId);
      if (prior.state === "active")
        predecessor = this.transitionEpoch(prior, {
          state: "retiring",
          retiredAt: this.clock.now(),
        });
      else predecessor = structuredClone(prior);
    }
    const next = this.transitionEpoch(epoch, {
      state: "active",
      activatedAt: this.clock.now(),
    });
    this.activeByTask.set(next.taskId, next.epochId);
    return { epoch: next, predecessor };
  }
  issueProof(input: {
    taskId: string;
    purpose: TaskIdentityProof["purpose"];
    nonce: string;
    payloadDigest: string;
    ttlMs: number;
  }): TaskIdentityProof {
    const activeId = this.activeByTask.get(input.taskId);
    if (!activeId)
      throw new E03RuntimeError(
        "task_identity_epoch_active_missing",
        `task ${input.taskId} has no active identity epoch`,
      );
    const epoch = this.requireEpoch(activeId);
    if (epoch.state !== "active")
      throw new E03RuntimeError(
        "task_identity_epoch_inactive",
        `task identity epoch ${epoch.epochId} is ${epoch.state}`,
      );
    if (
      !input.nonce ||
      !input.payloadDigest ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "task_identity_proof_input",
        "task identity proof input is invalid",
      );
    const nonceDigest = digest({ epochId: epoch.epochId, nonce: input.nonce });
    if (this.nonceIndex.has(nonceDigest))
      throw new E03RuntimeError(
        "task_identity_proof_nonce_reuse",
        "task identity proof nonce was already used",
      );
    const payload = {
      proofId: createId("task-identity-proof"),
      epochId: epoch.epochId,
      taskId: epoch.taskId,
      purpose: input.purpose,
      nonceDigest,
      payloadDigest: input.payloadDigest,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      consumedAt: null,
      state: "issued" as const,
      issuedAt: this.clock.now(),
      revision: 1,
    };
    const proof = { ...payload, digest: digest(payload) };
    assertIdentityProof(proof);
    this.proofs.set(proof.proofId, proof);
    this.nonceIndex.set(proof.nonceDigest, proof.proofId);
    return structuredClone(proof);
  }
  verifyAndConsume(input: {
    proofId: string;
    expectedRevision: number;
    taskId: string;
    purpose: TaskIdentityProof["purpose"];
    payloadDigest: string;
  }): TaskIdentityProof {
    const proof = this.requireProof(input.proofId);
    this.assertProofRevision(proof, input.expectedRevision);
    if (proof.state !== "issued")
      throw new E03RuntimeError(
        "task_identity_proof_state",
        `task identity proof ${proof.proofId} is ${proof.state}`,
      );
    if (Date.parse(proof.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionProof(proof, { state: "expired" });
    if (
      proof.taskId !== input.taskId ||
      proof.purpose !== input.purpose ||
      proof.payloadDigest !== input.payloadDigest
    )
      throw new E03RuntimeError(
        "task_identity_proof_mismatch",
        `task identity proof ${proof.proofId} does not authorize payload`,
      );
    const epoch = this.requireEpoch(proof.epochId);
    if (
      epoch.state !== "active" ||
      this.activeByTask.get(proof.taskId) !== epoch.epochId
    )
      return this.transitionProof(proof, { state: "revoked" });
    return this.transitionProof(proof, {
      state: "consumed",
      consumedAt: this.clock.now(),
    });
  }
  revokeEpoch(
    epochId: string,
    expectedRevision: number,
    reason: string,
  ): { epoch: TaskIdentityEpoch; proofs: TaskIdentityProof[] } {
    const epoch = this.requireEpoch(epochId);
    this.assertEpochRevision(epoch, expectedRevision);
    if (epoch.state === "revoked")
      return { epoch: structuredClone(epoch), proofs: [] };
    if (!reason.trim())
      throw new E03RuntimeError(
        "task_identity_epoch_revoke_reason",
        "task identity epoch revoke reason is required",
      );
    const next = this.transitionEpoch(epoch, {
      state: "revoked",
      revokedAt: this.clock.now(),
      revokeReason: reason.trim(),
    });
    if (this.activeByTask.get(next.taskId) === next.epochId)
      this.activeByTask.delete(next.taskId);
    const proofs: TaskIdentityProof[] = [];
    for (const proof of this.proofs.values())
      if (proof.epochId === epochId && proof.state === "issued")
        proofs.push(this.transitionProof(proof, { state: "revoked" }));
    return { epoch: next, proofs };
  }
  active(taskId: string): TaskIdentityEpoch | null {
    const id = this.activeByTask.get(taskId);
    return id ? structuredClone(this.requireEpoch(id)) : null;
  }
  lineage(epochId: string): TaskIdentityEpoch[] {
    const values: TaskIdentityEpoch[] = [];
    const seen = new Set<string>();
    let cursor: TaskIdentityEpoch | null = this.requireEpoch(epochId);
    while (cursor) {
      if (seen.has(cursor.epochId))
        throw new E03RuntimeError(
          "task_identity_epoch_cycle",
          `task identity epoch cycles at ${cursor.epochId}`,
        );
      seen.add(cursor.epochId);
      values.push(structuredClone(cursor));
      cursor = cursor.predecessorEpochId
        ? this.requireEpoch(cursor.predecessorEpochId)
        : null;
    }
    return values;
  }
  snapshot(): {
    epochs: TaskIdentityEpoch[];
    activeByTask: Array<[string, string]>;
    proofs: TaskIdentityProof[];
  } {
    return {
      epochs: [...this.epochs.values()].map((value) => structuredClone(value)),
      activeByTask: [...this.activeByTask.entries()].map(
        ([taskId, epochId]) => [taskId, epochId],
      ),
      proofs: [...this.proofs.values()].map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    epochs: readonly TaskIdentityEpoch[];
    activeByTask: ReadonlyArray<readonly [string, string]>;
    proofs: readonly TaskIdentityProof[];
  }): void {
    const epochs = new Map<string, TaskIdentityEpoch>();
    const activeByTask = new Map<string, string>();
    const proofs = new Map<string, TaskIdentityProof>();
    const nonceIndex = new Map<string, string>();
    for (const value of snapshot.epochs) {
      assertIdentityEpoch(value);
      if (epochs.has(value.epochId))
        throw new E03RuntimeError(
          "task_identity_epoch_restore_duplicate",
          `duplicate task identity epoch ${value.epochId}`,
        );
      epochs.set(value.epochId, structuredClone(value));
    }
    for (const value of epochs.values())
      if (value.predecessorEpochId && !epochs.has(value.predecessorEpochId))
        throw new E03RuntimeError(
          "task_identity_epoch_restore_predecessor",
          `task identity epoch ${value.epochId} has no predecessor`,
        );
    for (const [taskId, epochId] of snapshot.activeByTask) {
      const epoch = epochs.get(epochId);
      if (
        !epoch ||
        epoch.taskId !== taskId ||
        epoch.state !== "active" ||
        activeByTask.has(taskId)
      )
        throw new E03RuntimeError(
          "task_identity_epoch_restore_active",
          `task identity active index ${taskId} is invalid`,
        );
      activeByTask.set(taskId, epochId);
    }
    for (const value of snapshot.proofs) {
      assertIdentityProof(value);
      if (
        proofs.has(value.proofId) ||
        nonceIndex.has(value.nonceDigest) ||
        !epochs.has(value.epochId)
      )
        throw new E03RuntimeError(
          "task_identity_proof_restore",
          `task identity proof ${value.proofId} is invalid`,
        );
      proofs.set(value.proofId, structuredClone(value));
      nonceIndex.set(value.nonceDigest, value.proofId);
    }
    this.epochs = epochs;
    this.activeByTask = activeByTask;
    this.proofs = proofs;
    this.nonceIndex = nonceIndex;
    for (const value of epochs.values()) this.lineage(value.epochId);
  }
  private requireEpoch(id: string): TaskIdentityEpoch {
    const value = this.epochs.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_identity_epoch_missing",
        `task identity epoch ${id} does not exist`,
      );
    assertIdentityEpoch(value);
    return value;
  }
  private requireProof(id: string): TaskIdentityProof {
    const value = this.proofs.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_identity_proof_missing",
        `task identity proof ${id} does not exist`,
      );
    assertIdentityProof(value);
    return value;
  }
  private assertEpochRevision(
    value: TaskIdentityEpoch,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_identity_epoch_stale_revision",
        `task identity epoch ${value.epochId} revision is stale`,
      );
  }
  private assertProofRevision(
    value: TaskIdentityProof,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_identity_proof_stale_revision",
        `task identity proof ${value.proofId} revision is stale`,
      );
  }
  private transitionEpoch(
    value: TaskIdentityEpoch,
    patch: Partial<Omit<TaskIdentityEpoch, "epochId" | "revision" | "digest">>,
  ): TaskIdentityEpoch {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      epochId: value.epochId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIdentityEpoch(next);
    this.epochs.set(next.epochId, next);
    return structuredClone(next);
  }
  private transitionProof(
    value: TaskIdentityProof,
    patch: Partial<Omit<TaskIdentityProof, "proofId" | "revision" | "digest">>,
  ): TaskIdentityProof {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      proofId: value.proofId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIdentityProof(next);
    this.proofs.set(next.proofId, next);
    return structuredClone(next);
  }
}
