import {
  assertDigest,
  createId,
  digest,
  E03RuntimeError,
  unique,
  type E03CapabilityScope,
  type E03Clock,
  type E03ContextSnapshot,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface ContextForkRequest {
  parent: E03ContextSnapshot;
  childTaskId: string;
  childSessionId: string;
  mode: "isolated" | "fork" | "resume";
  scope: E03CapabilityScope;
  messageRefs?: readonly string[];
  artifactRefs?: readonly string[];
  evidenceRefs?: readonly string[];
  memoryRefs?: readonly string[];
  topologyRevision?: number;
}

export class AgentContextFork {
  readonly ledger = new AgentContextLedger();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  fork(request: ContextForkRequest): E03ContextSnapshot {
    this.validate(request.parent);
    if (!request.childTaskId || !request.childSessionId)
      throw new E03RuntimeError(
        "invalid_context_identity",
        "child task/session identity is required",
      );
    const isolated = request.mode === "isolated";
    const resumed = request.mode === "resume";
    const payload = {
      snapshotId: createId("agent-context"),
      parentSnapshotId: request.parent.snapshotId,
      sessionId: request.childSessionId,
      taskId: request.childTaskId,
      branchId: resumed ? request.parent.branchId : createId("agent-branch"),
      sequence: resumed ? request.parent.sequence + 1 : 0,
      messageRefs: isolated
        ? unique(request.messageRefs ?? [])
        : unique([
            ...request.parent.messageRefs,
            ...(request.messageRefs ?? []),
          ]),
      artifactRefs: isolated
        ? unique(request.artifactRefs ?? [])
        : unique([
            ...request.parent.artifactRefs,
            ...(request.artifactRefs ?? []),
          ]),
      evidenceRefs: isolated
        ? unique(request.evidenceRefs ?? [])
        : unique([
            ...request.parent.evidenceRefs,
            ...(request.evidenceRefs ?? []),
          ]),
      memoryRefs:
        request.mode === "fork" || resumed
          ? unique([
              ...request.parent.memoryRefs,
              ...(request.memoryRefs ?? []),
            ])
          : unique(request.memoryRefs ?? []),
      compactBoundaryIds: resumed ? [...request.parent.compactBoundaryIds] : [],
      permissionDigest: request.scope.permissionCeilingDigest,
      toolCatalogDigest: digest(request.scope.tools),
      topologyRevision:
        request.topologyRevision ?? request.parent.topologyRevision,
      createdAt: this.clock.now(),
    };
    const snapshot = { ...payload, checksum: digest(payload) };
    this.ledger.record(
      snapshot,
      "fork",
      `forked from ${request.parent.snapshotId}`,
    );
    return snapshot;
  }

  restore(
    value: E03ContextSnapshot,
    expected: { sessionId: string; taskId: string; permissionDigest: string },
  ): E03ContextSnapshot {
    this.validate(value);
    if (
      value.sessionId !== expected.sessionId ||
      value.taskId !== expected.taskId
    ) {
      throw new E03RuntimeError(
        "context_identity_mismatch",
        "context snapshot belongs to another task/session",
        {
          expectedSessionId: expected.sessionId,
          expectedTaskId: expected.taskId,
          actualSessionId: value.sessionId,
          actualTaskId: value.taskId,
        },
      );
    }
    if (value.permissionDigest !== expected.permissionDigest)
      throw new E03RuntimeError(
        "context_permission_mismatch",
        "restored context permission ceiling changed",
      );
    this.ledger.record(value, "restore", `restored ${value.snapshotId}`);
    return structuredClone(value);
  }

  append(
    snapshot: E03ContextSnapshot,
    update: {
      messageRefs?: readonly string[];
      artifactRefs?: readonly string[];
      evidenceRefs?: readonly string[];
      memoryRefs?: readonly string[];
      compactBoundaryId?: string;
      topologyRevision?: number;
    },
  ): E03ContextSnapshot {
    this.validate(snapshot);
    const payload = {
      ...snapshot,
      sequence: snapshot.sequence + 1,
      messageRefs: unique([
        ...snapshot.messageRefs,
        ...(update.messageRefs ?? []),
      ]),
      artifactRefs: unique([
        ...snapshot.artifactRefs,
        ...(update.artifactRefs ?? []),
      ]),
      evidenceRefs: unique([
        ...snapshot.evidenceRefs,
        ...(update.evidenceRefs ?? []),
      ]),
      memoryRefs: unique([
        ...snapshot.memoryRefs,
        ...(update.memoryRefs ?? []),
      ]),
      compactBoundaryIds: unique([
        ...snapshot.compactBoundaryIds,
        ...(update.compactBoundaryId ? [update.compactBoundaryId] : []),
      ]),
      topologyRevision: update.topologyRevision ?? snapshot.topologyRevision,
      createdAt: this.clock.now(),
      checksum: "",
    };
    const { checksum: _checksum, ...unsigned } = payload;
    const next = { ...unsigned, checksum: digest(unsigned) };
    this.ledger.record(
      next,
      update.compactBoundaryId ? "compact" : "append",
      update.compactBoundaryId || "context references appended",
    );
    return next;
  }

  fromTask(task: E03TaskState): E03ContextSnapshot {
    this.validate(task.context);
    if (
      task.context.taskId !== task.identity.taskId ||
      task.context.sessionId !== task.identity.sessionId
    )
      throw new E03RuntimeError(
        "task_context_mismatch",
        "task context identity differs from task identity",
      );
    return structuredClone(task.context);
  }

  validate(snapshot: E03ContextSnapshot): void {
    const { checksum, ...payload } = snapshot;
    assertDigest(payload, checksum, `context:${snapshot.snapshotId}`);
    if (
      !snapshot.snapshotId ||
      !snapshot.sessionId ||
      !snapshot.taskId ||
      !snapshot.branchId
    )
      throw new E03RuntimeError(
        "invalid_context_snapshot",
        "context identity fields are required",
      );
    if (!Number.isSafeInteger(snapshot.sequence) || snapshot.sequence < 0)
      throw new E03RuntimeError(
        "invalid_context_snapshot",
        "context sequence must be non-negative",
      );
    if (
      !Number.isSafeInteger(snapshot.topologyRevision) ||
      snapshot.topologyRevision < 0
    )
      throw new E03RuntimeError(
        "invalid_context_snapshot",
        "topology revision must be non-negative",
      );
    for (const [field, values] of Object.entries({
      messageRefs: snapshot.messageRefs,
      artifactRefs: snapshot.artifactRefs,
      evidenceRefs: snapshot.evidenceRefs,
      memoryRefs: snapshot.memoryRefs,
      compactBoundaryIds: snapshot.compactBoundaryIds,
    }))
      if (values.some((value) => !value.trim()))
        throw new E03RuntimeError(
          "invalid_context_snapshot",
          `${field} contains an empty reference`,
        );
  }
}

export function rootContext(input: {
  sessionId: string;
  taskId: string;
  permissionDigest: string;
  toolCatalogDigest: string;
  topologyRevision?: number;
  clock?: E03Clock;
}): E03ContextSnapshot {
  const clock = input.clock ?? new SystemE03Clock();
  const payload = {
    snapshotId: createId("root-context"),
    parentSnapshotId: null,
    sessionId: input.sessionId,
    taskId: input.taskId,
    branchId: createId("root-branch"),
    sequence: 0,
    messageRefs: [],
    artifactRefs: [],
    evidenceRefs: [],
    memoryRefs: [],
    compactBoundaryIds: [],
    permissionDigest: input.permissionDigest,
    toolCatalogDigest: input.toolCatalogDigest,
    topologyRevision: input.topologyRevision ?? 0,
    createdAt: clock.now(),
  };
  return { ...payload, checksum: digest(payload) };
}

export type ContextLedgerAction =
  | "root"
  | "fork"
  | "append"
  | "compact"
  | "restore"
  | "rebind";

export interface ContextLedgerEntry {
  entryId: string;
  snapshotId: string;
  parentSnapshotId: string | null;
  sessionId: string;
  taskId: string;
  branchId: string;
  sequence: number;
  action: ContextLedgerAction;
  snapshotChecksum: string;
  permissionDigest: string;
  toolCatalogDigest: string;
  topologyRevision: number;
  messageCount: number;
  artifactCount: number;
  evidenceCount: number;
  memoryCount: number;
  compactBoundaryCount: number;
  reason: string;
  recordedAt: string;
  previousEntryDigest: string;
  digest: string;
}

export class AgentContextLedger {
  private readonly entries = new Map<string, ContextLedgerEntry[]>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  record(
    snapshot: E03ContextSnapshot,
    action: ContextLedgerAction,
    reason: string,
  ): ContextLedgerEntry {
    this.assertSnapshot(snapshot);
    const branch = this.entries.get(snapshot.branchId) ?? [];
    const prior = branch.at(-1);
    if (prior) {
      if (snapshot.sequence < prior.sequence)
        throw new E03RuntimeError(
          "context_sequence_regression",
          "context ledger sequence regressed",
        );
      if (
        snapshot.sessionId !== prior.sessionId ||
        snapshot.taskId !== prior.taskId
      )
        throw new E03RuntimeError(
          "context_branch_authority_changed",
          "context branch changed task/session authority",
        );
      if (snapshot.permissionDigest !== prior.permissionDigest)
        throw new E03RuntimeError(
          "context_permission_changed",
          "context branch changed permission ceiling",
        );
      if (snapshot.topologyRevision < prior.topologyRevision)
        throw new E03RuntimeError(
          "context_topology_regression",
          "context topology revision regressed",
        );
      if (
        snapshot.sequence === prior.sequence &&
        snapshot.checksum !== prior.snapshotChecksum &&
        action !== "restore"
      )
        throw new E03RuntimeError(
          "context_sequence_conflict",
          "same context sequence has different checksum",
        );
    }
    const payload = {
      entryId: `context-ledger-${digest({ snapshotId: snapshot.snapshotId, action, prior: prior?.digest ?? "" }).slice(0, 32)}`,
      snapshotId: snapshot.snapshotId,
      parentSnapshotId: snapshot.parentSnapshotId,
      sessionId: snapshot.sessionId,
      taskId: snapshot.taskId,
      branchId: snapshot.branchId,
      sequence: snapshot.sequence,
      action,
      snapshotChecksum: snapshot.checksum,
      permissionDigest: snapshot.permissionDigest,
      toolCatalogDigest: snapshot.toolCatalogDigest,
      topologyRevision: snapshot.topologyRevision,
      messageCount: snapshot.messageRefs.length,
      artifactCount: snapshot.artifactRefs.length,
      evidenceCount: snapshot.evidenceRefs.length,
      memoryCount: snapshot.memoryRefs.length,
      compactBoundaryCount: snapshot.compactBoundaryIds.length,
      reason: reason.trim() || action,
      recordedAt: this.clock.now(),
      previousEntryDigest: prior?.digest ?? "",
    };
    const entry = { ...payload, digest: digest(payload) };
    branch.push(entry);
    this.entries.set(snapshot.branchId, branch);
    return structuredClone(entry);
  }

  restore(entries: readonly ContextLedgerEntry[]): void {
    const branches = new Map<string, ContextLedgerEntry[]>();
    for (const entry of entries) {
      const branch = branches.get(entry.branchId) ?? [];
      this.assertEntry(entry, branch.at(-1)?.digest ?? "");
      branch.push(structuredClone(entry));
      branches.set(entry.branchId, branch);
    }
    this.entries.clear();
    for (const [branchId, branch] of branches)
      this.entries.set(branchId, branch);
  }

  snapshot(): ContextLedgerEntry[] {
    return [...this.entries.values()]
      .flat()
      .sort(
        (left, right) =>
          left.recordedAt.localeCompare(right.recordedAt) ||
          left.branchId.localeCompare(right.branchId) ||
          left.sequence - right.sequence,
      )
      .map((entry) => structuredClone(entry));
  }

  branch(branchId: string): ContextLedgerEntry[] {
    return (this.entries.get(branchId) ?? []).map((entry) =>
      structuredClone(entry),
    );
  }

  head(branchId: string): ContextLedgerEntry | null {
    const value = this.entries.get(branchId)?.at(-1);
    return value ? structuredClone(value) : null;
  }

  verifyBranch(branchId: string): {
    valid: true;
    entries: number;
    headDigest: string;
    sequence: number;
  } {
    const branch = this.entries.get(branchId) ?? [];
    let previous = "";
    let sequence = -1;
    for (const entry of branch) {
      this.assertEntry(entry, previous);
      if (entry.sequence < sequence)
        throw new E03RuntimeError(
          "context_sequence_regression",
          `branch ${branchId} sequence regressed`,
        );
      previous = entry.digest;
      sequence = entry.sequence;
    }
    return {
      valid: true,
      entries: branch.length,
      headDigest: previous,
      sequence,
    };
  }

  private assertSnapshot(snapshot: E03ContextSnapshot): void {
    const { checksum, ...payload } = snapshot;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "context_checksum_mismatch",
        `context ${snapshot.snapshotId} checksum is invalid`,
      );
    if (
      !snapshot.snapshotId ||
      !snapshot.sessionId ||
      !snapshot.taskId ||
      !snapshot.branchId ||
      !snapshot.permissionDigest ||
      !snapshot.toolCatalogDigest
    )
      throw new E03RuntimeError(
        "invalid_context_snapshot",
        "context identity or capability binding is incomplete",
      );
    if (snapshot.sequence < 0 || snapshot.topologyRevision < 0)
      throw new E03RuntimeError(
        "invalid_context_revision",
        "context sequence/topology revision is negative",
      );
  }

  private assertEntry(entry: ContextLedgerEntry, previousDigest: string): void {
    const { digest: checksum, ...payload } = entry;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "context_ledger_digest",
        `context ledger ${entry.entryId} digest is invalid`,
      );
    if (entry.previousEntryDigest !== previousDigest)
      throw new E03RuntimeError(
        "context_ledger_chain",
        `context ledger ${entry.entryId} does not extend prior record`,
      );
    if (
      !entry.entryId ||
      !entry.snapshotId ||
      !entry.sessionId ||
      !entry.taskId ||
      !entry.branchId ||
      !entry.snapshotChecksum ||
      !entry.reason
    )
      throw new E03RuntimeError(
        "invalid_context_ledger",
        "context ledger identity is incomplete",
      );
    if (entry.sequence < 0 || entry.topologyRevision < 0)
      throw new E03RuntimeError(
        "invalid_context_ledger_revision",
        "context ledger sequence/topology revision is negative",
      );
  }
}

export type ContextReferenceKind =
  | "message"
  | "artifact"
  | "evidence"
  | "memory"
  | "compact-boundary";

export interface ContextReferenceDelta {
  deltaId: string;
  baseSnapshotId: string;
  branchId: string;
  sequence: number;
  added: Record<ContextReferenceKind, string[]>;
  removed: Record<ContextReferenceKind, string[]>;
  permissionDigest: string;
  toolCatalogDigest: string;
  topologyRevision: number;
  reason: string;
  createdAt: string;
  digest: string;
}

export interface ContextDeltaProjection {
  branchId: string;
  baseSnapshotId: string;
  latestSequence: number;
  pendingDeltaIds: string[];
  addedReferenceCount: number;
  removedReferenceCount: number;
  permissionDigests: string[];
  toolCatalogDigests: string[];
  topologyRevisions: number[];
  digest: string;
}

function emptyReferenceSet(): Record<ContextReferenceKind, string[]> {
  return {
    message: [],
    artifact: [],
    evidence: [],
    memory: [],
    "compact-boundary": [],
  };
}

function assertReferenceDelta(delta: ContextReferenceDelta): void {
  const { digest: checksum, ...payload } = delta;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "context_delta_digest",
      `context delta ${delta.deltaId} digest is invalid`,
    );
  if (
    !delta.deltaId ||
    !delta.baseSnapshotId ||
    !delta.branchId ||
    !delta.reason
  )
    throw new E03RuntimeError(
      "context_delta_identity",
      "context delta identity is incomplete",
    );
  if (!Number.isSafeInteger(delta.sequence) || delta.sequence < 1)
    throw new E03RuntimeError(
      "context_delta_sequence",
      "context delta sequence must be positive",
    );
  if (
    !Number.isSafeInteger(delta.topologyRevision) ||
    delta.topologyRevision < 0
  )
    throw new E03RuntimeError(
      "context_delta_topology",
      "context delta topology revision is invalid",
    );
  if (!delta.permissionDigest || !delta.toolCatalogDigest)
    throw new E03RuntimeError(
      "context_delta_capability",
      "context delta capability bindings are incomplete",
    );
  for (const kind of Object.keys(
    emptyReferenceSet(),
  ) as ContextReferenceKind[]) {
    const added = delta.added[kind];
    const removed = delta.removed[kind];
    if (
      added.length !== unique(added).length ||
      removed.length !== unique(removed).length
    )
      throw new E03RuntimeError(
        "context_delta_duplicate",
        `context delta ${delta.deltaId} contains duplicate ${kind} references`,
      );
    const removedSet = new Set(removed);
    if (added.some((reference) => removedSet.has(reference)))
      throw new E03RuntimeError(
        "context_delta_overlap",
        `context delta ${delta.deltaId} both adds and removes a ${kind} reference`,
      );
  }
}

export class ContextDeltaRuntime {
  private readonly deltas = new Map<string, ContextReferenceDelta>();
  private readonly byBranch = new Map<string, string[]>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  create(input: {
    base: E03ContextSnapshot;
    added?: Partial<Record<ContextReferenceKind, readonly string[]>>;
    removed?: Partial<Record<ContextReferenceKind, readonly string[]>>;
    reason: string;
    topologyRevision?: number;
  }): ContextReferenceDelta {
    if (!input.reason.trim())
      throw new E03RuntimeError(
        "context_delta_reason",
        "context delta reason is required",
      );
    const prior = this.byBranch.get(input.base.branchId) ?? [];
    const last = prior.length
      ? this.deltas.get(prior[prior.length - 1]!)
      : undefined;
    const added = emptyReferenceSet();
    const removed = emptyReferenceSet();
    for (const kind of Object.keys(added) as ContextReferenceKind[]) {
      added[kind] = unique(input.added?.[kind] ?? []).filter(Boolean);
      removed[kind] = unique(input.removed?.[kind] ?? []).filter(Boolean);
    }
    const payload = {
      deltaId: createId("context-delta"),
      baseSnapshotId: input.base.snapshotId,
      branchId: input.base.branchId,
      sequence: (last?.sequence ?? 0) + 1,
      added,
      removed,
      permissionDigest: input.base.permissionDigest,
      toolCatalogDigest: input.base.toolCatalogDigest,
      topologyRevision: input.topologyRevision ?? input.base.topologyRevision,
      reason: input.reason.trim(),
      createdAt: this.clock.now(),
    };
    const delta = { ...payload, digest: digest(payload) };
    assertReferenceDelta(delta);
    this.deltas.set(delta.deltaId, structuredClone(delta));
    this.byBranch.set(delta.branchId, [...prior, delta.deltaId]);
    return structuredClone(delta);
  }

  apply(snapshot: E03ContextSnapshot, deltaId: string): E03ContextSnapshot {
    const delta = this.require(deltaId);
    if (
      delta.baseSnapshotId !== snapshot.snapshotId ||
      delta.branchId !== snapshot.branchId
    )
      throw new E03RuntimeError(
        "context_delta_base",
        `context delta ${delta.deltaId} does not belong to snapshot ${snapshot.snapshotId}`,
      );
    if (
      delta.permissionDigest !== snapshot.permissionDigest ||
      delta.toolCatalogDigest !== snapshot.toolCatalogDigest
    )
      throw new E03RuntimeError(
        "context_delta_capability_drift",
        `context delta ${delta.deltaId} capability binding changed`,
      );
    const update = (
      current: readonly string[],
      added: readonly string[],
      removed: readonly string[],
    ): string[] => {
      const removedSet = new Set(removed);
      return unique([
        ...current.filter((value) => !removedSet.has(value)),
        ...added,
      ]);
    };
    const payload = {
      ...snapshot,
      snapshotId: createId("agent-context"),
      parentSnapshotId: snapshot.snapshotId,
      sequence: snapshot.sequence + 1,
      messageRefs: update(
        snapshot.messageRefs,
        delta.added.message,
        delta.removed.message,
      ),
      artifactRefs: update(
        snapshot.artifactRefs,
        delta.added.artifact,
        delta.removed.artifact,
      ),
      evidenceRefs: update(
        snapshot.evidenceRefs,
        delta.added.evidence,
        delta.removed.evidence,
      ),
      memoryRefs: update(
        snapshot.memoryRefs,
        delta.added.memory,
        delta.removed.memory,
      ),
      compactBoundaryIds: update(
        snapshot.compactBoundaryIds,
        delta.added["compact-boundary"],
        delta.removed["compact-boundary"],
      ),
      topologyRevision: Math.max(
        snapshot.topologyRevision,
        delta.topologyRevision,
      ),
      createdAt: this.clock.now(),
    };
    const { checksum: _, ...withoutChecksum } = payload;
    return { ...withoutChecksum, checksum: digest(withoutChecksum) };
  }

  list(branchId: string, afterSequence = 0): ContextReferenceDelta[] {
    if (!Number.isSafeInteger(afterSequence) || afterSequence < 0)
      throw new E03RuntimeError(
        "context_delta_cursor",
        "context delta cursor is invalid",
      );
    return (this.byBranch.get(branchId) ?? [])
      .map((deltaId) => this.require(deltaId))
      .filter((delta) => delta.sequence > afterSequence)
      .map((delta) => structuredClone(delta));
  }

  project(branchId: string): ContextDeltaProjection {
    const deltas = this.list(branchId);
    if (!deltas.length)
      throw new E03RuntimeError(
        "context_delta_branch_missing",
        `context delta branch ${branchId} does not exist`,
      );
    const payload = {
      branchId,
      baseSnapshotId: deltas[0]!.baseSnapshotId,
      latestSequence: deltas[deltas.length - 1]!.sequence,
      pendingDeltaIds: deltas.map((delta) => delta.deltaId),
      addedReferenceCount: deltas.reduce(
        (count, delta) =>
          count +
          Object.values(delta.added).reduce(
            (sum, values) => sum + values.length,
            0,
          ),
        0,
      ),
      removedReferenceCount: deltas.reduce(
        (count, delta) =>
          count +
          Object.values(delta.removed).reduce(
            (sum, values) => sum + values.length,
            0,
          ),
        0,
      ),
      permissionDigests: unique(deltas.map((delta) => delta.permissionDigest)),
      toolCatalogDigests: unique(
        deltas.map((delta) => delta.toolCatalogDigest),
      ),
      topologyRevisions: unique(
        deltas.map((delta) => String(delta.topologyRevision)),
      ).map(Number),
    };
    return { ...payload, digest: digest(payload) };
  }

  snapshot(): ContextReferenceDelta[] {
    return [...this.deltas.values()]
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .map((delta) => structuredClone(delta));
  }

  restore(rows: readonly ContextReferenceDelta[]): void {
    const next = new Map<string, ContextReferenceDelta>();
    const byBranch = new Map<string, string[]>();
    for (const raw of rows) {
      const delta = structuredClone(raw);
      assertReferenceDelta(delta);
      if (next.has(delta.deltaId))
        throw new E03RuntimeError(
          "context_delta_restore_duplicate",
          `duplicate context delta ${delta.deltaId}`,
        );
      const branch = byBranch.get(delta.branchId) ?? [];
      const previous = branch.length
        ? next.get(branch[branch.length - 1]!)
        : undefined;
      if (delta.sequence !== (previous?.sequence ?? 0) + 1)
        throw new E03RuntimeError(
          "context_delta_restore_gap",
          `context delta branch ${delta.branchId} contains a sequence gap`,
        );
      next.set(delta.deltaId, delta);
      byBranch.set(delta.branchId, [...branch, delta.deltaId]);
    }
    this.deltas.clear();
    this.byBranch.clear();
    for (const [key, value] of next) this.deltas.set(key, value);
    for (const [key, value] of byBranch) this.byBranch.set(key, value);
  }

  private require(deltaId: string): ContextReferenceDelta {
    const delta = this.deltas.get(deltaId);
    if (!delta)
      throw new E03RuntimeError(
        "context_delta_missing",
        `context delta ${deltaId} does not exist`,
      );
    assertReferenceDelta(delta);
    return delta;
  }
}

export type ContextMergeResolution = "source" | "target" | "union" | "reject";

export interface ContextMergeConflict {
  conflictId: string;
  mergeId: string;
  kind: ContextReferenceKind | "permission" | "tool-catalog" | "topology";
  sourceValues: string[];
  targetValues: string[];
  resolution: ContextMergeResolution | null;
  resolvedValues: string[];
  resolvedAt: string | null;
  digest: string;
}

export interface ContextMergeRecord {
  mergeId: string;
  ancestorSnapshotId: string;
  sourceSnapshotId: string;
  targetSnapshotId: string;
  sourceBranchId: string;
  targetBranchId: string;
  state: "planned" | "conflicted" | "resolved" | "committed" | "rejected";
  conflictIds: string[];
  resultSnapshotId: string | null;
  createdAt: string;
  committedAt: string | null;
  digest: string;
}

function snapshotReferences(
  snapshot: E03ContextSnapshot,
): Record<ContextReferenceKind, string[]> {
  return {
    message: [...snapshot.messageRefs],
    artifact: [...snapshot.artifactRefs],
    evidence: [...snapshot.evidenceRefs],
    memory: [...snapshot.memoryRefs],
    "compact-boundary": [...snapshot.compactBoundaryIds],
  };
}

function assertMergeRecord(record: ContextMergeRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "context_merge_digest",
      `context merge ${record.mergeId} digest is invalid`,
    );
  if (
    !record.mergeId ||
    !record.ancestorSnapshotId ||
    !record.sourceSnapshotId ||
    !record.targetSnapshotId ||
    !record.sourceBranchId ||
    !record.targetBranchId
  )
    throw new E03RuntimeError(
      "context_merge_identity",
      "context merge identity is incomplete",
    );
}

function assertMergeConflict(conflict: ContextMergeConflict): void {
  const { digest: checksum, ...payload } = conflict;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "context_merge_conflict_digest",
      `context merge conflict ${conflict.conflictId} digest is invalid`,
    );
  if (!conflict.conflictId || !conflict.mergeId)
    throw new E03RuntimeError(
      "context_merge_conflict_identity",
      "context merge conflict identity is incomplete",
    );
  if (conflict.resolution === null && conflict.resolvedAt !== null)
    throw new E03RuntimeError(
      "context_merge_conflict_state",
      "unresolved context conflict cannot have a resolution timestamp",
    );
}

export class ContextMergeRuntime {
  private readonly merges = new Map<string, ContextMergeRecord>();
  private readonly conflicts = new Map<string, ContextMergeConflict>();
  private readonly snapshots = new Map<string, E03ContextSnapshot>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  plan(input: {
    ancestor: E03ContextSnapshot;
    source: E03ContextSnapshot;
    target: E03ContextSnapshot;
  }): ContextMergeRecord {
    if (
      input.source.sessionId !== input.target.sessionId ||
      input.ancestor.sessionId !== input.target.sessionId
    )
      throw new E03RuntimeError(
        "context_merge_session",
        "context snapshots belong to different sessions",
      );
    if (
      input.source.snapshotId === input.target.snapshotId ||
      input.source.branchId === input.target.branchId
    )
      throw new E03RuntimeError(
        "context_merge_same_branch",
        "context merge requires distinct source and target branches",
      );
    this.remember(input.ancestor);
    this.remember(input.source);
    this.remember(input.target);
    const mergeId = createId("context-merge");
    const sourceReferences = snapshotReferences(input.source);
    const targetReferences = snapshotReferences(input.target);
    const ancestorReferences = snapshotReferences(input.ancestor);
    const conflictIds: string[] = [];
    for (const kind of Object.keys(
      sourceReferences,
    ) as ContextReferenceKind[]) {
      const ancestor = new Set(ancestorReferences[kind]);
      const sourceChanged = sourceReferences[kind].filter(
        (value) => !ancestor.has(value),
      );
      const targetChanged = targetReferences[kind].filter(
        (value) => !ancestor.has(value),
      );
      if (!sourceChanged.length || !targetChanged.length) continue;
      if (digest(sourceChanged) === digest(targetChanged)) continue;
      const conflict = this.makeConflict({
        mergeId,
        kind,
        sourceValues: sourceChanged,
        targetValues: targetChanged,
      });
      this.conflicts.set(conflict.conflictId, conflict);
      conflictIds.push(conflict.conflictId);
    }
    if (input.source.permissionDigest !== input.target.permissionDigest) {
      const conflict = this.makeConflict({
        mergeId,
        kind: "permission",
        sourceValues: [input.source.permissionDigest],
        targetValues: [input.target.permissionDigest],
      });
      this.conflicts.set(conflict.conflictId, conflict);
      conflictIds.push(conflict.conflictId);
    }
    if (input.source.toolCatalogDigest !== input.target.toolCatalogDigest) {
      const conflict = this.makeConflict({
        mergeId,
        kind: "tool-catalog",
        sourceValues: [input.source.toolCatalogDigest],
        targetValues: [input.target.toolCatalogDigest],
      });
      this.conflicts.set(conflict.conflictId, conflict);
      conflictIds.push(conflict.conflictId);
    }
    const payload = {
      mergeId,
      ancestorSnapshotId: input.ancestor.snapshotId,
      sourceSnapshotId: input.source.snapshotId,
      targetSnapshotId: input.target.snapshotId,
      sourceBranchId: input.source.branchId,
      targetBranchId: input.target.branchId,
      state: (conflictIds.length
        ? "conflicted"
        : "planned") as ContextMergeRecord["state"],
      conflictIds,
      resultSnapshotId: null,
      createdAt: this.clock.now(),
      committedAt: null,
    };
    const record = { ...payload, digest: digest(payload) };
    assertMergeRecord(record);
    this.merges.set(record.mergeId, record);
    return structuredClone(record);
  }

  resolve(input: {
    mergeId: string;
    conflictId: string;
    resolution: ContextMergeResolution;
    values?: readonly string[];
  }): ContextMergeConflict {
    const merge = this.requireMerge(input.mergeId);
    if (merge.state !== "conflicted" && merge.state !== "resolved")
      throw new E03RuntimeError(
        "context_merge_resolution_state",
        `context merge ${merge.mergeId} is not accepting resolutions`,
      );
    const conflict = this.requireConflict(input.conflictId);
    if (conflict.mergeId !== merge.mergeId)
      throw new E03RuntimeError(
        "context_merge_resolution_custody",
        `context conflict ${conflict.conflictId} belongs to another merge`,
      );
    let resolvedValues: string[];
    switch (input.resolution) {
      case "source":
        resolvedValues = [...conflict.sourceValues];
        break;
      case "target":
        resolvedValues = [...conflict.targetValues];
        break;
      case "union":
        resolvedValues = unique([
          ...conflict.targetValues,
          ...conflict.sourceValues,
        ]);
        break;
      case "reject":
        resolvedValues = [];
        break;
      default:
        throw new E03RuntimeError(
          "context_merge_resolution_invalid",
          "context merge resolution is invalid",
        );
    }
    if (input.values) {
      const allowed = new Set([
        ...conflict.sourceValues,
        ...conflict.targetValues,
      ]);
      if (input.values.some((value) => !allowed.has(value)))
        throw new E03RuntimeError(
          "context_merge_resolution_value",
          "context merge resolution contains an unknown value",
        );
      resolvedValues = unique(input.values);
    }
    const nextConflict = this.resealConflict(conflict, {
      resolution: input.resolution,
      resolvedValues,
      resolvedAt: this.clock.now(),
    });
    this.conflicts.set(nextConflict.conflictId, nextConflict);
    const pending = merge.conflictIds
      .map((conflictId) => this.requireConflict(conflictId))
      .some((item) => item.resolution === null);
    const nextMerge = this.resealMerge(merge, {
      state: pending ? "conflicted" : "resolved",
    });
    this.merges.set(nextMerge.mergeId, nextMerge);
    return structuredClone(nextConflict);
  }

  commit(mergeId: string): E03ContextSnapshot {
    const merge = this.requireMerge(mergeId);
    if (merge.state !== "planned" && merge.state !== "resolved")
      throw new E03RuntimeError(
        "context_merge_commit_state",
        `context merge ${mergeId} is not ready to commit`,
      );
    const source = this.requireSnapshot(merge.sourceSnapshotId);
    const target = this.requireSnapshot(merge.targetSnapshotId);
    const sourceReferences = snapshotReferences(source);
    const targetReferences = snapshotReferences(target);
    const selected = new Map<
      ContextReferenceKind | "permission" | "tool-catalog" | "topology",
      string[]
    >();
    for (const conflictId of merge.conflictIds) {
      const conflict = this.requireConflict(conflictId);
      if (conflict.resolution === null)
        throw new E03RuntimeError(
          "context_merge_unresolved",
          `context merge ${mergeId} contains unresolved conflicts`,
        );
      if (conflict.resolution === "reject") {
        const rejected = this.resealMerge(merge, { state: "rejected" });
        this.merges.set(rejected.mergeId, rejected);
        throw new E03RuntimeError(
          "context_merge_rejected",
          `context merge ${mergeId} was rejected`,
        );
      }
      selected.set(conflict.kind, conflict.resolvedValues);
    }
    const refs = (kind: ContextReferenceKind): string[] =>
      selected.get(kind) ??
      unique([...targetReferences[kind], ...sourceReferences[kind]]);
    const payload = {
      ...target,
      snapshotId: createId("agent-context"),
      parentSnapshotId: target.snapshotId,
      sequence: target.sequence + 1,
      messageRefs: refs("message"),
      artifactRefs: refs("artifact"),
      evidenceRefs: refs("evidence"),
      memoryRefs: refs("memory"),
      compactBoundaryIds: refs("compact-boundary"),
      permissionDigest:
        selected.get("permission")?.[0] ?? target.permissionDigest,
      toolCatalogDigest:
        selected.get("tool-catalog")?.[0] ?? target.toolCatalogDigest,
      topologyRevision: Math.max(
        source.topologyRevision,
        target.topologyRevision,
      ),
      createdAt: this.clock.now(),
    };
    const { checksum: _, ...withoutChecksum } = payload;
    const result = { ...withoutChecksum, checksum: digest(withoutChecksum) };
    this.remember(result);
    const committed = this.resealMerge(merge, {
      state: "committed",
      resultSnapshotId: result.snapshotId,
      committedAt: this.clock.now(),
    });
    this.merges.set(committed.mergeId, committed);
    return structuredClone(result);
  }

  get(mergeId: string): ContextMergeRecord {
    return structuredClone(this.requireMerge(mergeId));
  }

  listConflicts(mergeId: string): ContextMergeConflict[] {
    const merge = this.requireMerge(mergeId);
    return merge.conflictIds.map((conflictId) =>
      structuredClone(this.requireConflict(conflictId)),
    );
  }

  snapshot(): {
    merges: ContextMergeRecord[];
    conflicts: ContextMergeConflict[];
    snapshots: E03ContextSnapshot[];
  } {
    return {
      merges: [...this.merges.values()].map((value) => structuredClone(value)),
      conflicts: [...this.conflicts.values()].map((value) =>
        structuredClone(value),
      ),
      snapshots: [...this.snapshots.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    merges: readonly ContextMergeRecord[];
    conflicts: readonly ContextMergeConflict[];
    snapshots: readonly E03ContextSnapshot[];
  }): void {
    const nextMerges = new Map<string, ContextMergeRecord>();
    const nextConflicts = new Map<string, ContextMergeConflict>();
    const nextSnapshots = new Map<string, E03ContextSnapshot>();
    for (const record of input.merges) {
      assertMergeRecord(record);
      if (nextMerges.has(record.mergeId))
        throw new E03RuntimeError(
          "context_merge_restore_duplicate",
          `duplicate context merge ${record.mergeId}`,
        );
      nextMerges.set(record.mergeId, structuredClone(record));
    }
    for (const conflict of input.conflicts) {
      assertMergeConflict(conflict);
      if (!nextMerges.has(conflict.mergeId))
        throw new E03RuntimeError(
          "context_merge_restore_orphan",
          `context conflict ${conflict.conflictId} has no merge`,
        );
      if (nextConflicts.has(conflict.conflictId))
        throw new E03RuntimeError(
          "context_merge_restore_conflict_duplicate",
          `duplicate context conflict ${conflict.conflictId}`,
        );
      nextConflicts.set(conflict.conflictId, structuredClone(conflict));
    }
    for (const snapshot of input.snapshots) {
      assertDigest(snapshot, "checksum", `context ${snapshot.snapshotId}`);
      if (nextSnapshots.has(snapshot.snapshotId))
        throw new E03RuntimeError(
          "context_merge_restore_snapshot_duplicate",
          `duplicate context snapshot ${snapshot.snapshotId}`,
        );
      nextSnapshots.set(snapshot.snapshotId, structuredClone(snapshot));
    }
    for (const merge of nextMerges.values()) {
      for (const conflictId of merge.conflictIds)
        if (!nextConflicts.has(conflictId))
          throw new E03RuntimeError(
            "context_merge_restore_conflict_missing",
            `context merge ${merge.mergeId} references missing conflict ${conflictId}`,
          );
      for (const snapshotId of [
        merge.ancestorSnapshotId,
        merge.sourceSnapshotId,
        merge.targetSnapshotId,
      ])
        if (!nextSnapshots.has(snapshotId))
          throw new E03RuntimeError(
            "context_merge_restore_snapshot_missing",
            `context merge ${merge.mergeId} references missing snapshot ${snapshotId}`,
          );
    }
    this.merges.clear();
    this.conflicts.clear();
    this.snapshots.clear();
    for (const [key, value] of nextMerges) this.merges.set(key, value);
    for (const [key, value] of nextConflicts) this.conflicts.set(key, value);
    for (const [key, value] of nextSnapshots) this.snapshots.set(key, value);
  }

  private makeConflict(input: {
    mergeId: string;
    kind: ContextMergeConflict["kind"];
    sourceValues: readonly string[];
    targetValues: readonly string[];
  }): ContextMergeConflict {
    const payload = {
      conflictId: createId("context-conflict"),
      mergeId: input.mergeId,
      kind: input.kind,
      sourceValues: unique(input.sourceValues),
      targetValues: unique(input.targetValues),
      resolution: null,
      resolvedValues: [],
      resolvedAt: null,
    };
    const conflict = { ...payload, digest: digest(payload) };
    assertMergeConflict(conflict);
    return conflict;
  }

  private remember(snapshot: E03ContextSnapshot): void {
    assertDigest(snapshot, "checksum", `context ${snapshot.snapshotId}`);
    this.snapshots.set(snapshot.snapshotId, structuredClone(snapshot));
  }

  private requireMerge(mergeId: string): ContextMergeRecord {
    const merge = this.merges.get(mergeId);
    if (!merge)
      throw new E03RuntimeError(
        "context_merge_missing",
        `context merge ${mergeId} does not exist`,
      );
    assertMergeRecord(merge);
    return merge;
  }

  private requireConflict(conflictId: string): ContextMergeConflict {
    const conflict = this.conflicts.get(conflictId);
    if (!conflict)
      throw new E03RuntimeError(
        "context_merge_conflict_missing",
        `context merge conflict ${conflictId} does not exist`,
      );
    assertMergeConflict(conflict);
    return conflict;
  }

  private requireSnapshot(snapshotId: string): E03ContextSnapshot {
    const snapshot = this.snapshots.get(snapshotId);
    if (!snapshot)
      throw new E03RuntimeError(
        "context_merge_snapshot_missing",
        `context snapshot ${snapshotId} does not exist`,
      );
    assertDigest(snapshot, "checksum", `context ${snapshot.snapshotId}`);
    return snapshot;
  }

  private resealMerge(
    merge: ContextMergeRecord,
    patch: Partial<Omit<ContextMergeRecord, "mergeId" | "digest">>,
  ): ContextMergeRecord {
    const { digest: _, ...prior } = merge;
    const payload = { ...prior, ...patch, mergeId: merge.mergeId };
    const next = { ...payload, digest: digest(payload) };
    assertMergeRecord(next);
    return next;
  }

  private resealConflict(
    conflict: ContextMergeConflict,
    patch: Partial<
      Omit<ContextMergeConflict, "conflictId" | "mergeId" | "digest">
    >,
  ): ContextMergeConflict {
    const { digest: _, ...prior } = conflict;
    const payload = {
      ...prior,
      ...patch,
      conflictId: conflict.conflictId,
      mergeId: conflict.mergeId,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeConflict(next);
    return next;
  }
}

export type ContextCheckpointState =
  | "staged"
  | "committed"
  | "superseded"
  | "expired"
  | "corrupt";

export interface ContextCheckpointRecord {
  checkpointId: string;
  taskId: string;
  sessionId: string;
  branchId: string;
  sequence: number;
  state: ContextCheckpointState;
  snapshot: E03ContextSnapshot;
  parentCheckpointId: string | null;
  writerId: string;
  idempotencyKey: string;
  pinCount: number;
  retainUntil: string | null;
  stagedAt: string;
  committedAt: string | null;
  supersededAt: string | null;
  expiredAt: string | null;
  revision: number;
  digest: string;
}

export interface ContextCheckpointPin {
  pinId: string;
  checkpointId: string;
  ownerId: string;
  reason: string;
  expiresAt: string;
  releasedAt: string | null;
  revision: number;
  digest: string;
}

function assertCheckpoint(record: ContextCheckpointRecord): void {
  assertDigest(record, "digest", `context checkpoint ${record.checkpointId}`);
  assertDigest(
    record.snapshot,
    "checksum",
    `context ${record.snapshot.snapshotId}`,
  );
  if (!record.checkpointId || !record.writerId || !record.idempotencyKey)
    throw new E03RuntimeError(
      "context_checkpoint_identity",
      "context checkpoint identity is required",
    );
  if (
    record.taskId !== record.snapshot.taskId ||
    record.sessionId !== record.snapshot.sessionId ||
    record.branchId !== record.snapshot.branchId ||
    record.sequence !== record.snapshot.sequence
  )
    throw new E03RuntimeError(
      "context_checkpoint_custody",
      `context checkpoint ${record.checkpointId} custody does not match its snapshot`,
    );
  if (
    !Number.isSafeInteger(record.pinCount) ||
    record.pinCount < 0 ||
    !Number.isSafeInteger(record.revision) ||
    record.revision < 1
  )
    throw new E03RuntimeError(
      "context_checkpoint_revision",
      `context checkpoint ${record.checkpointId} counters are invalid`,
    );
  if (record.state === "committed" && record.committedAt === null)
    throw new E03RuntimeError(
      "context_checkpoint_commit_time",
      `context checkpoint ${record.checkpointId} lacks commit time`,
    );
  if (record.state === "superseded" && record.supersededAt === null)
    throw new E03RuntimeError(
      "context_checkpoint_supersede_time",
      `context checkpoint ${record.checkpointId} lacks supersede time`,
    );
  if (record.state === "expired" && record.expiredAt === null)
    throw new E03RuntimeError(
      "context_checkpoint_expire_time",
      `context checkpoint ${record.checkpointId} lacks expiry time`,
    );
}

function assertCheckpointPin(pin: ContextCheckpointPin): void {
  assertDigest(pin, "digest", `context checkpoint pin ${pin.pinId}`);
  if (!pin.pinId || !pin.checkpointId || !pin.ownerId || !pin.reason.trim())
    throw new E03RuntimeError(
      "context_checkpoint_pin_identity",
      "context checkpoint pin identity is required",
    );
  if (
    !Number.isSafeInteger(pin.revision) ||
    pin.revision < 1 ||
    Number.isNaN(Date.parse(pin.expiresAt))
  )
    throw new E03RuntimeError(
      "context_checkpoint_pin_revision",
      `context checkpoint pin ${pin.pinId} is invalid`,
    );
}

export class ContextCheckpointArchive {
  private checkpoints = new Map<string, ContextCheckpointRecord>();
  private idempotency = new Map<string, string>();
  private branchHeads = new Map<string, string>();
  private pins = new Map<string, ContextCheckpointPin>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  stage(input: {
    snapshot: E03ContextSnapshot;
    writerId: string;
    idempotencyKey: string;
    parentCheckpointId?: string | null;
    retainUntil?: string | null;
  }): ContextCheckpointRecord {
    assertDigest(
      input.snapshot,
      "checksum",
      `context ${input.snapshot.snapshotId}`,
    );
    if (!input.writerId.trim() || !input.idempotencyKey.trim())
      throw new E03RuntimeError(
        "context_checkpoint_stage_identity",
        "checkpoint writer and idempotency key are required",
      );
    const priorId = this.idempotency.get(input.idempotencyKey);
    if (priorId) {
      const prior = this.requireCheckpoint(priorId);
      if (
        prior.snapshot.checksum !== input.snapshot.checksum ||
        prior.writerId !== input.writerId
      )
        throw new E03RuntimeError(
          "context_checkpoint_idempotency_conflict",
          `checkpoint idempotency key ${input.idempotencyKey} was reused`,
        );
      return structuredClone(prior);
    }
    const branchHead = this.branchHeads.get(input.snapshot.branchId) ?? null;
    const parentCheckpointId =
      input.parentCheckpointId === undefined
        ? branchHead
        : input.parentCheckpointId;
    if (parentCheckpointId) {
      const parent = this.requireCheckpoint(parentCheckpointId);
      if (
        parent.taskId !== input.snapshot.taskId ||
        parent.sessionId !== input.snapshot.sessionId ||
        parent.branchId !== input.snapshot.branchId
      )
        throw new E03RuntimeError(
          "context_checkpoint_parent_custody",
          `checkpoint parent ${parentCheckpointId} belongs to another branch`,
        );
      if (parent.sequence >= input.snapshot.sequence)
        throw new E03RuntimeError(
          "context_checkpoint_sequence",
          "checkpoint sequence must advance its parent",
        );
    }
    if (
      input.retainUntil !== undefined &&
      input.retainUntil !== null &&
      Number.isNaN(Date.parse(input.retainUntil))
    )
      throw new E03RuntimeError(
        "context_checkpoint_retention",
        "checkpoint retention time is invalid",
      );
    const payload = {
      checkpointId: createId("context-checkpoint"),
      taskId: input.snapshot.taskId,
      sessionId: input.snapshot.sessionId,
      branchId: input.snapshot.branchId,
      sequence: input.snapshot.sequence,
      state: "staged" as const,
      snapshot: structuredClone(input.snapshot),
      parentCheckpointId: parentCheckpointId ?? null,
      writerId: input.writerId.trim(),
      idempotencyKey: input.idempotencyKey.trim(),
      pinCount: 0,
      retainUntil: input.retainUntil ?? null,
      stagedAt: this.clock.now(),
      committedAt: null,
      supersededAt: null,
      expiredAt: null,
      revision: 1,
    };
    const checkpoint = { ...payload, digest: digest(payload) };
    assertCheckpoint(checkpoint);
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    this.idempotency.set(checkpoint.idempotencyKey, checkpoint.checkpointId);
    return structuredClone(checkpoint);
  }

  commit(
    checkpointId: string,
    expectedRevision: number,
  ): ContextCheckpointRecord {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "staged")
      throw new E03RuntimeError(
        "context_checkpoint_commit_state",
        `context checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    const headId = this.branchHeads.get(checkpoint.branchId);
    if (headId) {
      const head = this.requireCheckpoint(headId);
      if (head.sequence >= checkpoint.sequence)
        throw new E03RuntimeError(
          "context_checkpoint_head_conflict",
          `branch ${checkpoint.branchId} already has sequence ${head.sequence}`,
        );
      if (checkpoint.parentCheckpointId !== head.checkpointId)
        throw new E03RuntimeError(
          "context_checkpoint_head_changed",
          `branch ${checkpoint.branchId} head changed before commit`,
        );
      this.transition(head, {
        state: "superseded",
        supersededAt: this.clock.now(),
      });
    }
    const committed = this.transition(checkpoint, {
      state: "committed",
      committedAt: this.clock.now(),
    });
    this.branchHeads.set(committed.branchId, committed.checkpointId);
    return committed;
  }

  pin(input: {
    checkpointId: string;
    ownerId: string;
    reason: string;
    ttlMs: number;
  }): ContextCheckpointPin {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    if (checkpoint.state === "expired" || checkpoint.state === "corrupt")
      throw new E03RuntimeError(
        "context_checkpoint_pin_state",
        `context checkpoint ${checkpoint.checkpointId} cannot be pinned`,
      );
    if (
      !input.ownerId.trim() ||
      !input.reason.trim() ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "context_checkpoint_pin_input",
        "checkpoint pin input is invalid",
      );
    const existing = [...this.pins.values()].find(
      (pin) =>
        pin.checkpointId === input.checkpointId &&
        pin.ownerId === input.ownerId &&
        pin.releasedAt === null &&
        Date.parse(pin.expiresAt) > Date.parse(this.clock.now()),
    );
    if (existing) return structuredClone(existing);
    const payload = {
      pinId: createId("context-checkpoint-pin"),
      checkpointId: checkpoint.checkpointId,
      ownerId: input.ownerId.trim(),
      reason: input.reason.trim(),
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      releasedAt: null,
      revision: 1,
    };
    const pin = { ...payload, digest: digest(payload) };
    assertCheckpointPin(pin);
    this.pins.set(pin.pinId, pin);
    this.transition(checkpoint, { pinCount: checkpoint.pinCount + 1 });
    return structuredClone(pin);
  }

  releasePin(pinId: string, expectedRevision: number): ContextCheckpointPin {
    const pin = this.requirePin(pinId);
    if (pin.revision !== expectedRevision)
      throw new E03RuntimeError(
        "context_checkpoint_pin_stale_revision",
        `context checkpoint pin ${pinId} revision is stale`,
      );
    if (pin.releasedAt !== null) return structuredClone(pin);
    const next = this.transitionPin(pin, { releasedAt: this.clock.now() });
    const checkpoint = this.requireCheckpoint(pin.checkpointId);
    this.transition(checkpoint, {
      pinCount: Math.max(0, checkpoint.pinCount - 1),
    });
    return next;
  }

  collect(now = this.clock.now()): ContextCheckpointRecord[] {
    const timestamp = Date.parse(now);
    if (Number.isNaN(timestamp))
      throw new E03RuntimeError(
        "context_checkpoint_collect_time",
        "checkpoint collection time is invalid",
      );
    for (const pin of this.pins.values())
      if (pin.releasedAt === null && Date.parse(pin.expiresAt) <= timestamp)
        this.releasePin(pin.pinId, pin.revision);
    const expired: ContextCheckpointRecord[] = [];
    for (const checkpoint of this.checkpoints.values()) {
      if (
        checkpoint.pinCount > 0 ||
        checkpoint.state === "expired" ||
        checkpoint.state === "staged"
      )
        continue;
      if (this.branchHeads.get(checkpoint.branchId) === checkpoint.checkpointId)
        continue;
      if (
        checkpoint.retainUntil !== null &&
        Date.parse(checkpoint.retainUntil) > timestamp
      )
        continue;
      expired.push(
        this.transition(checkpoint, { state: "expired", expiredAt: now }),
      );
    }
    return expired;
  }

  recover(branchId: string, atSequence?: number): E03ContextSnapshot {
    const candidates = [...this.checkpoints.values()]
      .filter(
        (checkpoint) =>
          checkpoint.branchId === branchId &&
          (checkpoint.state === "committed" ||
            checkpoint.state === "superseded") &&
          (atSequence === undefined || checkpoint.sequence <= atSequence),
      )
      .sort((left, right) => right.sequence - left.sequence);
    const checkpoint = candidates[0];
    if (!checkpoint)
      throw new E03RuntimeError(
        "context_checkpoint_recovery_missing",
        `branch ${branchId} has no recoverable checkpoint`,
      );
    try {
      assertCheckpoint(checkpoint);
    } catch (error) {
      this.transition(checkpoint, { state: "corrupt" });
      throw error;
    }
    return structuredClone(checkpoint.snapshot);
  }

  lineage(checkpointId: string): ContextCheckpointRecord[] {
    const values: ContextCheckpointRecord[] = [];
    const seen = new Set<string>();
    let cursor: ContextCheckpointRecord | null =
      this.requireCheckpoint(checkpointId);
    while (cursor) {
      if (seen.has(cursor.checkpointId))
        throw new E03RuntimeError(
          "context_checkpoint_lineage_cycle",
          `checkpoint lineage cycles at ${cursor.checkpointId}`,
        );
      seen.add(cursor.checkpointId);
      values.push(structuredClone(cursor));
      cursor = cursor.parentCheckpointId
        ? this.requireCheckpoint(cursor.parentCheckpointId)
        : null;
    }
    return values;
  }

  snapshot(): {
    checkpoints: ContextCheckpointRecord[];
    pins: ContextCheckpointPin[];
    branchHeads: Array<[string, string]>;
  } {
    return {
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      pins: [...this.pins.values()].map((value) => structuredClone(value)),
      branchHeads: [...this.branchHeads.entries()].map(
        ([branchId, checkpointId]) => [branchId, checkpointId],
      ),
    };
  }

  restore(snapshot: {
    checkpoints: readonly ContextCheckpointRecord[];
    pins: readonly ContextCheckpointPin[];
    branchHeads: ReadonlyArray<readonly [string, string]>;
  }): void {
    const checkpoints = new Map<string, ContextCheckpointRecord>();
    const idempotency = new Map<string, string>();
    const pins = new Map<string, ContextCheckpointPin>();
    const branchHeads = new Map<string, string>();
    for (const checkpoint of snapshot.checkpoints) {
      assertCheckpoint(checkpoint);
      if (
        checkpoints.has(checkpoint.checkpointId) ||
        idempotency.has(checkpoint.idempotencyKey)
      )
        throw new E03RuntimeError(
          "context_checkpoint_restore_duplicate",
          `duplicate context checkpoint ${checkpoint.checkpointId}`,
        );
      checkpoints.set(checkpoint.checkpointId, structuredClone(checkpoint));
      idempotency.set(checkpoint.idempotencyKey, checkpoint.checkpointId);
    }
    for (const checkpoint of checkpoints.values())
      if (
        checkpoint.parentCheckpointId &&
        !checkpoints.has(checkpoint.parentCheckpointId)
      )
        throw new E03RuntimeError(
          "context_checkpoint_restore_parent",
          `checkpoint ${checkpoint.checkpointId} has no parent`,
        );
    for (const pin of snapshot.pins) {
      assertCheckpointPin(pin);
      if (pins.has(pin.pinId) || !checkpoints.has(pin.checkpointId))
        throw new E03RuntimeError(
          "context_checkpoint_restore_pin",
          `checkpoint pin ${pin.pinId} is invalid`,
        );
      pins.set(pin.pinId, structuredClone(pin));
    }
    for (const [branchId, checkpointId] of snapshot.branchHeads) {
      const checkpoint = checkpoints.get(checkpointId);
      if (
        !checkpoint ||
        checkpoint.branchId !== branchId ||
        checkpoint.state !== "committed"
      )
        throw new E03RuntimeError(
          "context_checkpoint_restore_head",
          `branch head ${branchId} is invalid`,
        );
      if (branchHeads.has(branchId))
        throw new E03RuntimeError(
          "context_checkpoint_restore_duplicate_head",
          `branch ${branchId} has duplicate heads`,
        );
      branchHeads.set(branchId, checkpointId);
    }
    this.checkpoints = checkpoints;
    this.idempotency = idempotency;
    this.pins = pins;
    this.branchHeads = branchHeads;
  }

  private requireCheckpoint(checkpointId: string): ContextCheckpointRecord {
    const checkpoint = this.checkpoints.get(checkpointId);
    if (!checkpoint)
      throw new E03RuntimeError(
        "context_checkpoint_missing",
        `context checkpoint ${checkpointId} does not exist`,
      );
    assertCheckpoint(checkpoint);
    return checkpoint;
  }
  private requirePin(pinId: string): ContextCheckpointPin {
    const pin = this.pins.get(pinId);
    if (!pin)
      throw new E03RuntimeError(
        "context_checkpoint_pin_missing",
        `context checkpoint pin ${pinId} does not exist`,
      );
    assertCheckpointPin(pin);
    return pin;
  }
  private assertRevision(
    checkpoint: ContextCheckpointRecord,
    expected: number,
  ): void {
    if (checkpoint.revision !== expected)
      throw new E03RuntimeError(
        "context_checkpoint_stale_revision",
        `context checkpoint ${checkpoint.checkpointId} revision is stale`,
      );
  }
  private transition(
    checkpoint: ContextCheckpointRecord,
    patch: Partial<
      Omit<ContextCheckpointRecord, "checkpointId" | "revision" | "digest">
    >,
  ): ContextCheckpointRecord {
    const { digest: _, ...prior } = checkpoint;
    const payload = {
      ...prior,
      ...patch,
      checkpointId: checkpoint.checkpointId,
      revision: checkpoint.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCheckpoint(next);
    this.checkpoints.set(next.checkpointId, next);
    return structuredClone(next);
  }
  private transitionPin(
    pin: ContextCheckpointPin,
    patch: Partial<Omit<ContextCheckpointPin, "pinId" | "revision" | "digest">>,
  ): ContextCheckpointPin {
    const { digest: _, ...prior } = pin;
    const payload = {
      ...prior,
      ...patch,
      pinId: pin.pinId,
      revision: pin.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCheckpointPin(next);
    this.pins.set(next.pinId, next);
    return structuredClone(next);
  }
}

export interface ContextBranchRecord {
  branchRecordId: string;
  taskId: string;
  sessionId: string;
  branchId: string;
  parentBranchId: string | null;
  forkSnapshotId: string;
  headSnapshotId: string;
  state: "active" | "frozen" | "merged" | "abandoned";
  ownerId: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

function assertBranchRecord(record: ContextBranchRecord): void {
  assertDigest(record, "digest", `context branch ${record.branchRecordId}`);
  if (
    !record.branchRecordId ||
    !record.taskId ||
    !record.sessionId ||
    !record.branchId ||
    !record.forkSnapshotId ||
    !record.headSnapshotId ||
    !record.ownerId
  )
    throw new E03RuntimeError(
      "context_branch_identity",
      "context branch identity is required",
    );
  if (!Number.isSafeInteger(record.revision) || record.revision < 1)
    throw new E03RuntimeError(
      "context_branch_revision",
      `context branch ${record.branchId} revision is invalid`,
    );
  if (record.parentBranchId === record.branchId)
    throw new E03RuntimeError(
      "context_branch_self_parent",
      `context branch ${record.branchId} cannot parent itself`,
    );
}

export class ContextBranchLineageRuntime {
  private branches = new Map<string, ContextBranchRecord>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  create(input: {
    snapshot: E03ContextSnapshot;
    parentBranchId?: string | null;
    ownerId: string;
  }): ContextBranchRecord {
    assertDigest(
      input.snapshot,
      "checksum",
      `context ${input.snapshot.snapshotId}`,
    );
    if (this.branches.has(input.snapshot.branchId))
      throw new E03RuntimeError(
        "context_branch_duplicate",
        `context branch ${input.snapshot.branchId} already exists`,
      );
    if (input.parentBranchId) {
      const parent = this.require(input.parentBranchId);
      if (
        parent.taskId !== input.snapshot.taskId ||
        parent.sessionId !== input.snapshot.sessionId
      )
        throw new E03RuntimeError(
          "context_branch_parent_custody",
          "context branch parent has different custody",
        );
      if (parent.state !== "active" && parent.state !== "frozen")
        throw new E03RuntimeError(
          "context_branch_parent_state",
          `context branch parent is ${parent.state}`,
        );
    }
    const payload = {
      branchRecordId: createId("context-branch-record"),
      taskId: input.snapshot.taskId,
      sessionId: input.snapshot.sessionId,
      branchId: input.snapshot.branchId,
      parentBranchId: input.parentBranchId ?? null,
      forkSnapshotId: input.snapshot.snapshotId,
      headSnapshotId: input.snapshot.snapshotId,
      state: "active" as const,
      ownerId: input.ownerId.trim(),
      createdAt: this.clock.now(),
      updatedAt: this.clock.now(),
      revision: 1,
    };
    const record = { ...payload, digest: digest(payload) };
    assertBranchRecord(record);
    this.branches.set(record.branchId, record);
    return structuredClone(record);
  }
  advance(
    branchId: string,
    expectedRevision: number,
    snapshot: E03ContextSnapshot,
  ): ContextBranchRecord {
    const branch = this.require(branchId);
    this.assertRevision(branch, expectedRevision);
    if (branch.state !== "active")
      throw new E03RuntimeError(
        "context_branch_advance_state",
        `context branch ${branchId} is ${branch.state}`,
      );
    assertDigest(snapshot, "checksum", `context ${snapshot.snapshotId}`);
    if (
      snapshot.taskId !== branch.taskId ||
      snapshot.sessionId !== branch.sessionId ||
      snapshot.branchId !== branch.branchId
    )
      throw new E03RuntimeError(
        "context_branch_advance_custody",
        "context snapshot belongs to another branch",
      );
    return this.transition(branch, { headSnapshotId: snapshot.snapshotId });
  }
  freeze(branchId: string, expectedRevision: number): ContextBranchRecord {
    const branch = this.require(branchId);
    this.assertRevision(branch, expectedRevision);
    if (branch.state !== "active")
      throw new E03RuntimeError(
        "context_branch_freeze_state",
        `context branch ${branchId} is ${branch.state}`,
      );
    return this.transition(branch, { state: "frozen" });
  }
  reopen(branchId: string, expectedRevision: number): ContextBranchRecord {
    const branch = this.require(branchId);
    this.assertRevision(branch, expectedRevision);
    if (branch.state !== "frozen")
      throw new E03RuntimeError(
        "context_branch_reopen_state",
        `context branch ${branchId} is ${branch.state}`,
      );
    return this.transition(branch, { state: "active" });
  }
  merge(
    branchId: string,
    expectedRevision: number,
    targetBranchId: string,
  ): { source: ContextBranchRecord; target: ContextBranchRecord } {
    const source = this.require(branchId);
    const target = this.require(targetBranchId);
    this.assertRevision(source, expectedRevision);
    if (source.state !== "frozen" || target.state !== "active")
      throw new E03RuntimeError(
        "context_branch_merge_state",
        "context branch merge requires frozen source and active target",
      );
    if (
      source.taskId !== target.taskId ||
      source.sessionId !== target.sessionId
    )
      throw new E03RuntimeError(
        "context_branch_merge_custody",
        "context branches have different custody",
      );
    if (
      !this.ancestors(source.branchId).some(
        (candidate) => candidate.branchId === target.branchId,
      ) &&
      !this.ancestors(target.branchId).some(
        (candidate) => candidate.branchId === source.branchId,
      )
    )
      throw new E03RuntimeError(
        "context_branch_merge_lineage",
        "context branches do not share direct lineage",
      );
    const nextSource = this.transition(source, { state: "merged" });
    const nextTarget = this.transition(target, {
      headSnapshotId: source.headSnapshotId,
    });
    return { source: nextSource, target: nextTarget };
  }
  abandon(branchId: string, expectedRevision: number): ContextBranchRecord {
    const branch = this.require(branchId);
    this.assertRevision(branch, expectedRevision);
    if (branch.state === "merged" || branch.state === "abandoned")
      throw new E03RuntimeError(
        "context_branch_abandon_state",
        `context branch ${branchId} is ${branch.state}`,
      );
    if (
      [...this.branches.values()].some(
        (candidate) =>
          candidate.parentBranchId === branchId && candidate.state === "active",
      )
    )
      throw new E03RuntimeError(
        "context_branch_live_child",
        `context branch ${branchId} has an active child`,
      );
    return this.transition(branch, { state: "abandoned" });
  }
  ancestors(branchId: string): ContextBranchRecord[] {
    const values: ContextBranchRecord[] = [];
    const seen = new Set<string>();
    let cursor: ContextBranchRecord | null = this.require(branchId);
    while (cursor) {
      if (seen.has(cursor.branchId))
        throw new E03RuntimeError(
          "context_branch_lineage_cycle",
          `context branch lineage cycles at ${cursor.branchId}`,
        );
      seen.add(cursor.branchId);
      values.push(structuredClone(cursor));
      cursor = cursor.parentBranchId
        ? this.require(cursor.parentBranchId)
        : null;
    }
    return values;
  }
  snapshot(): ContextBranchRecord[] {
    return [...this.branches.values()]
      .sort((left, right) => left.branchId.localeCompare(right.branchId))
      .map((value) => structuredClone(value));
  }
  restore(records: readonly ContextBranchRecord[]): void {
    const next = new Map<string, ContextBranchRecord>();
    for (const record of records) {
      assertBranchRecord(record);
      if (next.has(record.branchId))
        throw new E03RuntimeError(
          "context_branch_restore_duplicate",
          `duplicate context branch ${record.branchId}`,
        );
      next.set(record.branchId, structuredClone(record));
    }
    for (const record of next.values())
      if (record.parentBranchId && !next.has(record.parentBranchId))
        throw new E03RuntimeError(
          "context_branch_restore_parent",
          `context branch ${record.branchId} has no parent`,
        );
    this.branches = next;
    for (const record of next.values()) this.ancestors(record.branchId);
  }
  private require(branchId: string): ContextBranchRecord {
    const branch = this.branches.get(branchId);
    if (!branch)
      throw new E03RuntimeError(
        "context_branch_missing",
        `context branch ${branchId} does not exist`,
      );
    assertBranchRecord(branch);
    return branch;
  }
  private assertRevision(branch: ContextBranchRecord, expected: number): void {
    if (branch.revision !== expected)
      throw new E03RuntimeError(
        "context_branch_stale_revision",
        `context branch ${branch.branchId} revision is stale`,
      );
  }
  private transition(
    branch: ContextBranchRecord,
    patch: Partial<
      Omit<
        ContextBranchRecord,
        "branchRecordId" | "branchId" | "revision" | "digest"
      >
    >,
  ): ContextBranchRecord {
    const { digest: _, ...prior } = branch;
    const payload = {
      ...prior,
      ...patch,
      branchRecordId: branch.branchRecordId,
      branchId: branch.branchId,
      updatedAt: this.clock.now(),
      revision: branch.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertBranchRecord(next);
    this.branches.set(next.branchId, next);
    return structuredClone(next);
  }
}

export interface ContextAccessGrant {
  grantId: string;
  snapshotId: string;
  ownerTaskId: string;
  subjectTaskId: string;
  actions: Array<"read" | "fork" | "merge" | "restore" | "pin">;
  state: "issued" | "active" | "revoked" | "expired" | "exhausted";
  maximumUses: number;
  consumedUses: number;
  issuedAt: string;
  activatedAt: string | null;
  expiresAt: string;
  revokedAt: string | null;
  revokeReason: string | null;
  revision: number;
  digest: string;
}
export interface ContextAccessDecision {
  decisionId: string;
  grantId: string;
  snapshotId: string;
  subjectTaskId: string;
  action: ContextAccessGrant["actions"][number];
  outcome: "allowed" | "denied";
  reason: string;
  sequence: number;
  decidedAt: string;
  previousDigest: string;
  digest: string;
}
function assertContextAccessGrant(value: ContextAccessGrant): void {
  assertDigest(value, "digest", `context access grant ${value.grantId}`);
  if (
    !value.grantId ||
    !value.snapshotId ||
    !value.ownerTaskId ||
    !value.subjectTaskId ||
    !value.actions.length ||
    !Number.isSafeInteger(value.maximumUses) ||
    value.maximumUses < 1 ||
    !Number.isSafeInteger(value.consumedUses) ||
    value.consumedUses < 0 ||
    value.consumedUses > value.maximumUses ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "context_access_grant",
      `context access grant ${value.grantId} is invalid`,
    );
}
function assertContextAccessDecision(value: ContextAccessDecision): void {
  assertDigest(value, "digest", `context access decision ${value.decisionId}`);
  if (
    !value.decisionId ||
    !value.grantId ||
    !value.snapshotId ||
    !value.subjectTaskId ||
    !value.reason ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "context_access_decision",
      `context access decision ${value.decisionId} is invalid`,
    );
}
export class ContextAccessRuntime {
  private grants = new Map<string, ContextAccessGrant>();
  private decisions = new Map<string, ContextAccessDecision[]>();
  private snapshots = new Map<string, E03ContextSnapshot>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  index(snapshots: readonly E03ContextSnapshot[]): void {
    const next = new Map<string, E03ContextSnapshot>();
    for (const snapshot of snapshots) {
      assertDigest(snapshot, "checksum", `context ${snapshot.snapshotId}`);
      if (next.has(snapshot.snapshotId))
        throw new E03RuntimeError(
          "context_access_snapshot_duplicate",
          `duplicate context snapshot ${snapshot.snapshotId}`,
        );
      next.set(snapshot.snapshotId, structuredClone(snapshot));
    }
    this.snapshots = next;
  }
  issue(input: {
    snapshotId: string;
    ownerTaskId: string;
    subjectTaskId: string;
    actions: readonly ContextAccessGrant["actions"][number][];
    maximumUses: number;
    ttlMs: number;
  }): ContextAccessGrant {
    const snapshot = this.requireSnapshot(input.snapshotId);
    if (snapshot.taskId !== input.ownerTaskId || !input.subjectTaskId.trim())
      throw new E03RuntimeError(
        "context_access_grant_custody",
        "context access grant custody is invalid",
      );
    const actions = [...new Set(input.actions)].sort();
    if (
      !actions.length ||
      !Number.isSafeInteger(input.maximumUses) ||
      input.maximumUses < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "context_access_grant_input",
        "context access grant input is invalid",
      );
    const existing = [...this.grants.values()].find(
      (value) =>
        value.snapshotId === input.snapshotId &&
        value.subjectTaskId === input.subjectTaskId &&
        value.state !== "revoked" &&
        value.state !== "expired" &&
        value.state !== "exhausted",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      grantId: createId("context-access-grant"),
      snapshotId: snapshot.snapshotId,
      ownerTaskId: input.ownerTaskId,
      subjectTaskId: input.subjectTaskId.trim(),
      actions,
      state: "issued" as const,
      maximumUses: input.maximumUses,
      consumedUses: 0,
      issuedAt: this.clock.now(),
      activatedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      revokedAt: null,
      revokeReason: null,
      revision: 1,
    };
    const grant = { ...payload, digest: digest(payload) };
    assertContextAccessGrant(grant);
    this.grants.set(grant.grantId, grant);
    return structuredClone(grant);
  }
  activate(
    grantId: string,
    expectedRevision: number,
    subjectTaskId: string,
  ): ContextAccessGrant {
    const grant = this.requireGrant(grantId);
    this.assertGrantRevision(grant, expectedRevision);
    if (grant.subjectTaskId !== subjectTaskId || grant.state !== "issued")
      throw new E03RuntimeError(
        "context_access_activate",
        `context access grant ${grantId} cannot be activated`,
      );
    if (Date.parse(grant.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionGrant(grant, { state: "expired" });
    return this.transitionGrant(grant, {
      state: "active",
      activatedAt: this.clock.now(),
    });
  }
  authorize(input: {
    grantId: string;
    subjectTaskId: string;
    snapshotId: string;
    action: ContextAccessGrant["actions"][number];
  }): { grant: ContextAccessGrant; decision: ContextAccessDecision } {
    const grant = this.requireGrant(input.grantId);
    let allowed = true;
    let reason = "context access grant allowed action";
    if (
      grant.subjectTaskId !== input.subjectTaskId ||
      grant.snapshotId !== input.snapshotId
    ) {
      allowed = false;
      reason = "context access grant custody mismatch";
    } else if (grant.state !== "active") {
      allowed = false;
      reason = `context access grant is ${grant.state}`;
    } else if (Date.parse(grant.expiresAt) <= Date.parse(this.clock.now())) {
      allowed = false;
      reason = "context access grant expired";
    } else if (!grant.actions.includes(input.action)) {
      allowed = false;
      reason = `context access grant denies ${input.action}`;
    } else if (grant.consumedUses >= grant.maximumUses) {
      allowed = false;
      reason = "context access grant exhausted";
    }
    let nextGrant = grant;
    if (
      Date.parse(grant.expiresAt) <= Date.parse(this.clock.now()) &&
      grant.state === "active"
    )
      nextGrant = this.transitionGrant(grant, { state: "expired" });
    else if (allowed)
      nextGrant = this.transitionGrant(grant, {
        consumedUses: grant.consumedUses + 1,
        state:
          grant.consumedUses + 1 >= grant.maximumUses
            ? "exhausted"
            : grant.state,
      });
    const decision = this.appendDecision(nextGrant, input, allowed, reason);
    return { grant: structuredClone(nextGrant), decision };
  }
  revoke(
    grantId: string,
    expectedRevision: number,
    ownerTaskId: string,
    reason: string,
  ): ContextAccessGrant {
    const grant = this.requireGrant(grantId);
    this.assertGrantRevision(grant, expectedRevision);
    if (grant.ownerTaskId !== ownerTaskId || !reason.trim())
      throw new E03RuntimeError(
        "context_access_revoke",
        `context access grant ${grantId} cannot be revoked`,
      );
    if (grant.state === "revoked") return structuredClone(grant);
    return this.transitionGrant(grant, {
      state: "revoked",
      revokedAt: this.clock.now(),
      revokeReason: reason.trim(),
    });
  }
  history(grantId: string): ContextAccessDecision[] {
    const entries = this.decisions.get(grantId) ?? [];
    let previousDigest = "root";
    let sequence = 1;
    for (const decision of entries) {
      assertContextAccessDecision(decision);
      if (
        decision.sequence !== sequence ||
        decision.previousDigest !== previousDigest
      )
        throw new E03RuntimeError(
          "context_access_decision_chain",
          `context access decision ${decision.decisionId} breaks chain`,
        );
      previousDigest = decision.digest;
      sequence += 1;
    }
    return entries.map((value) => structuredClone(value));
  }
  snapshot(): {
    grants: ContextAccessGrant[];
    decisions: ContextAccessDecision[];
    snapshots: E03ContextSnapshot[];
  } {
    for (const id of this.grants.keys()) this.history(id);
    return {
      grants: [...this.grants.values()].map((value) => structuredClone(value)),
      decisions: [...this.decisions.values()]
        .flat()
        .map((value) => structuredClone(value)),
      snapshots: [...this.snapshots.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    grants: readonly ContextAccessGrant[];
    decisions: readonly ContextAccessDecision[];
    snapshots: readonly E03ContextSnapshot[];
  }): void {
    this.index(snapshot.snapshots);
    const grants = new Map<string, ContextAccessGrant>();
    const decisions = new Map<string, ContextAccessDecision[]>();
    for (const value of snapshot.grants) {
      assertContextAccessGrant(value);
      if (grants.has(value.grantId) || !this.snapshots.has(value.snapshotId))
        throw new E03RuntimeError(
          "context_access_grant_restore",
          `context access grant ${value.grantId} is invalid`,
        );
      grants.set(value.grantId, structuredClone(value));
    }
    for (const value of snapshot.decisions) {
      assertContextAccessDecision(value);
      if (!grants.has(value.grantId))
        throw new E03RuntimeError(
          "context_access_decision_restore",
          `context access decision ${value.decisionId} has no grant`,
        );
      const entries = decisions.get(value.grantId) ?? [];
      entries.push(structuredClone(value));
      decisions.set(value.grantId, entries);
    }
    for (const entries of decisions.values())
      entries.sort((left, right) => left.sequence - right.sequence);
    this.grants = grants;
    this.decisions = decisions;
    for (const id of grants.keys()) this.history(id);
  }
  private appendDecision(
    grant: ContextAccessGrant,
    input: {
      subjectTaskId: string;
      snapshotId: string;
      action: ContextAccessGrant["actions"][number];
    },
    allowed: boolean,
    reason: string,
  ): ContextAccessDecision {
    const entries = this.decisions.get(grant.grantId) ?? [];
    const payload = {
      decisionId: createId("context-access-decision"),
      grantId: grant.grantId,
      snapshotId: input.snapshotId,
      subjectTaskId: input.subjectTaskId,
      action: input.action,
      outcome: allowed ? ("allowed" as const) : ("denied" as const),
      reason,
      sequence: entries.length + 1,
      decidedAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const decision = { ...payload, digest: digest(payload) };
    assertContextAccessDecision(decision);
    entries.push(decision);
    this.decisions.set(grant.grantId, entries);
    return structuredClone(decision);
  }
  private requireSnapshot(id: string): E03ContextSnapshot {
    const value = this.snapshots.get(id);
    if (!value)
      throw new E03RuntimeError(
        "context_access_snapshot_missing",
        `context snapshot ${id} does not exist`,
      );
    assertDigest(value, "checksum", `context ${id}`);
    return value;
  }
  private requireGrant(id: string): ContextAccessGrant {
    const value = this.grants.get(id);
    if (!value)
      throw new E03RuntimeError(
        "context_access_grant_missing",
        `context access grant ${id} does not exist`,
      );
    assertContextAccessGrant(value);
    return value;
  }
  private assertGrantRevision(
    value: ContextAccessGrant,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "context_access_grant_stale_revision",
        `context access grant ${value.grantId} revision is stale`,
      );
  }
  private transitionGrant(
    value: ContextAccessGrant,
    patch: Partial<Omit<ContextAccessGrant, "grantId" | "revision" | "digest">>,
  ): ContextAccessGrant {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      grantId: value.grantId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertContextAccessGrant(next);
    this.grants.set(next.grantId, next);
    return structuredClone(next);
  }
}
