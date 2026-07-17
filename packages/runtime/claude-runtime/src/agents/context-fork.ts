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
