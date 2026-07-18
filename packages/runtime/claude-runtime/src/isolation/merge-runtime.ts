import {
  createId,
  digest,
  E03RuntimeError,
  type E03Clock,
  type E03IsolationReceipt,
  type E03IsolationRequest,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface MergeReceipt {
  mergeId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  expectedBaseRevision: string;
  sourceRevision: string;
  targetRevision: string;
  accepted: boolean;
  conflictedPaths: string[];
  resultingRevision: string;
  error: string;
  completedAt: string;
  digest: string;
}

export interface CleanupReceipt {
  cleanupId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  removed: boolean;
  retainedArtifacts: string[];
  error: string;
  completedAt: string;
  digest: string;
}

export class IsolationMergeRuntime {
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  merge(
    task: E03TaskState,
    request: E03IsolationRequest,
    isolation: E03IsolationReceipt,
    receipt: MergeReceipt,
  ): E03TaskState {
    this.validateMerge(task, request, isolation, receipt);
    if (!receipt.accepted) {
      if (receipt.conflictedPaths.length)
        return this.recordConflict(task, receipt);
      throw new E03RuntimeError(
        "worktree_merge_rejected",
        receipt.error || "worktree merge was rejected",
      );
    }
    if (receipt.conflictedPaths.length)
      throw new E03RuntimeError(
        "merge_receipt_conflict",
        "accepted merge receipt contains conflicts",
      );
    const event = delivery(
      task,
      "artifact",
      "worktree merged",
      {
        merge_id: receipt.mergeId,
        resulting_revision: receipt.resultingRevision,
      },
      `merge:${receipt.mergeId}`,
      this.clock.now(),
    );
    return resealTask({
      ...task,
      deliveries: [...task.deliveries, event],
      sequence: task.sequence + 1,
      updatedAt: this.clock.now(),
    });
  }

  recordConflict(task: E03TaskState, receipt: MergeReceipt): E03TaskState {
    if (!receipt.conflictedPaths.length)
      throw new E03RuntimeError(
        "empty_merge_conflict",
        "merge conflict receipt contains no paths",
      );
    const event = delivery(
      task,
      "error",
      "worktree merge conflict",
      {
        merge_id: receipt.mergeId,
        conflicted_paths: receipt.conflictedPaths,
        error: receipt.error,
      },
      `merge-conflict:${receipt.mergeId}`,
      this.clock.now(),
    );
    return resealTask({
      ...task,
      deliveries: [...task.deliveries, event],
      sequence: task.sequence + 1,
      updatedAt: this.clock.now(),
      error: receipt.error || "worktree_merge_conflict",
    });
  }

  cleanup(
    task: E03TaskState,
    request: E03IsolationRequest,
    receipt: CleanupReceipt,
  ): E03TaskState {
    this.validateCleanup(task, request, receipt);
    const kind = receipt.removed ? "artifact" : "error";
    const event = delivery(
      task,
      kind,
      receipt.removed ? "worktree cleaned" : "worktree cleanup failed",
      {
        cleanup_id: receipt.cleanupId,
        retained_artifacts: receipt.retainedArtifacts,
        error: receipt.error,
      },
      `cleanup:${receipt.cleanupId}`,
      this.clock.now(),
    );
    return resealTask({
      ...task,
      deliveries: [...task.deliveries, event],
      sequence: task.sequence + 1,
      updatedAt: this.clock.now(),
      error: receipt.removed
        ? task.error
        : receipt.error || "worktree_cleanup_failed",
    });
  }

  private validateMerge(
    task: E03TaskState,
    request: E03IsolationRequest,
    isolation: E03IsolationReceipt,
    receipt: MergeReceipt,
  ): void {
    const { digest: receiptDigest, ...payload } = receipt;
    if (digest(payload) !== receiptDigest)
      throw new E03RuntimeError(
        "merge_receipt_checksum",
        "merge receipt checksum mismatch",
      );
    if (
      receipt.taskId !== task.identity.taskId ||
      receipt.requestId !== request.requestId ||
      receipt.requestId !== isolation.requestId ||
      receipt.leaseId !== task.identity.leaseId
    )
      throw new E03RuntimeError(
        "merge_receipt_identity",
        "merge receipt identity differs from task/isolation request",
      );
    if (
      receipt.expectedBaseRevision !== request.baseRevision ||
      receipt.sourceRevision !== isolation.resultingRevision
    )
      throw new E03RuntimeError(
        "merge_revision_mismatch",
        "merge receipt revision differs from isolation custody",
      );
    if (
      new Set(receipt.conflictedPaths).size !== receipt.conflictedPaths.length
    )
      throw new E03RuntimeError(
        "duplicate_conflicted_path",
        "merge receipt repeats a conflicted path",
      );
  }

  private validateCleanup(
    task: E03TaskState,
    request: E03IsolationRequest,
    receipt: CleanupReceipt,
  ): void {
    const { digest: receiptDigest, ...payload } = receipt;
    if (digest(payload) !== receiptDigest)
      throw new E03RuntimeError(
        "cleanup_receipt_checksum",
        "cleanup receipt checksum mismatch",
      );
    if (
      receipt.taskId !== task.identity.taskId ||
      receipt.requestId !== request.requestId ||
      receipt.leaseId !== task.identity.leaseId
    )
      throw new E03RuntimeError(
        "cleanup_receipt_identity",
        "cleanup receipt identity differs from task/isolation request",
      );
  }
}

function delivery(
  task: E03TaskState,
  kind: "artifact" | "error",
  summary: string,
  payload: import("../contracts.ts").JsonObject,
  idempotencyKey: string,
  now: string,
) {
  const unsigned = {
    deliveryId: `isolation-delivery-${digest(idempotencyKey).slice(0, 24)}`,
    taskId: task.identity.taskId,
    sequence: task.deliveries.length + 1,
    kind,
    summary,
    payload,
    artifactIds: [],
    idempotencyKey,
    createdAt: now,
    acknowledgedAt: null,
  };
  return { ...unsigned, digest: digest(unsigned) };
}

function resealTask(task: E03TaskState): E03TaskState {
  const { checksum: _checksum, ...payload } = task;
  return { ...payload, checksum: digest(payload) };
}

export type WorktreePhase =
  | "requested"
  | "ready"
  | "conflicted"
  | "merged"
  | "cleanup_pending"
  | "cleaned"
  | "cleanup_failed";

export interface WorktreeLedgerEntry {
  ledgerId: string;
  taskId: string;
  leaseId: string;
  requestId: string;
  mode: E03IsolationRequest["mode"];
  phase: WorktreePhase;
  workspaceRoot: string;
  workspacePath: string;
  baseRevision: string;
  isolatedRevision: string;
  resultingRevision: string;
  branchName: string;
  expectedArtifacts: string[];
  observedArtifacts: string[];
  conflictedPaths: string[];
  dirtyBaseline: boolean;
  nestedRepository: boolean;
  cleanupError: string;
  createdAt: string;
  updatedAt: string;
  digest: string;
}

export class WorktreeCustodyLedger {
  private readonly entries = new Map<string, WorktreeLedgerEntry>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  prepare(request: E03IsolationRequest): WorktreeLedgerEntry {
    this.assertRequest(request);
    const prior = this.entries.get(request.requestId);
    if (prior) {
      if (
        prior.taskId !== request.taskId ||
        prior.leaseId !== request.leaseId ||
        prior.baseRevision !== request.baseRevision
      )
        throw new E03RuntimeError(
          "worktree_idempotency_conflict",
          "worktree request id was reused with different custody",
        );
      return structuredClone(prior);
    }
    const now = this.clock.now();
    const entry = sealEntry({
      ledgerId: `worktree-ledger-${digest(request.requestId).slice(0, 32)}`,
      taskId: request.taskId,
      leaseId: request.leaseId,
      requestId: request.requestId,
      mode: request.mode,
      phase: "requested",
      workspaceRoot: request.workspaceRoot,
      workspacePath: "",
      baseRevision: request.baseRevision,
      isolatedRevision: "",
      resultingRevision: "",
      branchName: request.branchName,
      expectedArtifacts: [...request.expectedArtifacts],
      observedArtifacts: [],
      conflictedPaths: [],
      dirtyBaseline: false,
      nestedRepository: false,
      cleanupError: "",
      createdAt: now,
      updatedAt: now,
    });
    this.entries.set(request.requestId, entry);
    return structuredClone(entry);
  }

  ready(
    request: E03IsolationRequest,
    receipt: E03IsolationReceipt,
  ): WorktreeLedgerEntry {
    const current =
      this.entries.get(request.requestId) ?? this.prepare(request);
    this.assertIsolationReceipt(request, receipt);
    if (!receipt.accepted)
      throw new E03RuntimeError(
        "isolation_rejected",
        receipt.error || "physical isolation was rejected",
      );
    if (current.phase !== "requested" && current.phase !== "ready")
      throw new E03RuntimeError(
        "invalid_worktree_phase",
        `cannot ready worktree in ${current.phase}`,
      );
    const next = sealEntry({
      ...current,
      phase: "ready",
      workspacePath: receipt.workspacePath,
      isolatedRevision: receipt.resultingRevision,
      resultingRevision: receipt.resultingRevision,
      observedArtifacts: receipt.artifacts.map(
        (artifact) => artifact.artifact_id,
      ),
      dirtyBaseline: receipt.dirtyBaseline,
      nestedRepository: receipt.nestedRepository,
      updatedAt: this.clock.now(),
    });
    this.entries.set(request.requestId, next);
    return structuredClone(next);
  }

  merge(
    request: E03IsolationRequest,
    receipt: MergeReceipt,
  ): WorktreeLedgerEntry {
    const current = this.require(request.requestId);
    this.assertMergeReceipt(current, request, receipt);
    if (
      current.phase === "merged" &&
      current.resultingRevision === receipt.resultingRevision
    )
      return structuredClone(current);
    if (current.phase !== "ready" && current.phase !== "conflicted")
      throw new E03RuntimeError(
        "invalid_worktree_phase",
        `cannot merge worktree in ${current.phase}`,
      );
    const phase: WorktreePhase =
      receipt.accepted && !receipt.conflictedPaths.length
        ? "merged"
        : "conflicted";
    const next = sealEntry({
      ...current,
      phase,
      resultingRevision: receipt.resultingRevision || current.resultingRevision,
      conflictedPaths: uniquePaths(receipt.conflictedPaths),
      updatedAt: this.clock.now(),
    });
    this.entries.set(request.requestId, next);
    return structuredClone(next);
  }

  cleanupPending(requestId: string): WorktreeLedgerEntry {
    const current = this.require(requestId);
    if (current.phase === "cleaned" || current.phase === "cleanup_failed")
      return structuredClone(current);
    if (
      current.phase !== "ready" &&
      current.phase !== "merged" &&
      current.phase !== "conflicted"
    )
      throw new E03RuntimeError(
        "invalid_worktree_phase",
        `cannot clean worktree in ${current.phase}`,
      );
    const next = sealEntry({
      ...current,
      phase: "cleanup_pending",
      updatedAt: this.clock.now(),
    });
    this.entries.set(requestId, next);
    return structuredClone(next);
  }

  cleanup(
    request: E03IsolationRequest,
    receipt: CleanupReceipt,
  ): WorktreeLedgerEntry {
    const current = this.require(request.requestId);
    this.assertCleanupReceipt(current, request, receipt);
    if (current.phase === "cleaned" && receipt.removed)
      return structuredClone(current);
    if (
      current.phase !== "cleanup_pending" &&
      current.phase !== "ready" &&
      current.phase !== "merged" &&
      current.phase !== "conflicted"
    )
      throw new E03RuntimeError(
        "invalid_worktree_phase",
        `cannot record cleanup in ${current.phase}`,
      );
    const next = sealEntry({
      ...current,
      phase: receipt.removed ? "cleaned" : "cleanup_failed",
      observedArtifacts: uniquePaths([
        ...current.observedArtifacts,
        ...receipt.retainedArtifacts,
      ]),
      cleanupError: receipt.removed
        ? ""
        : receipt.error || "worktree_cleanup_failed",
      updatedAt: this.clock.now(),
    });
    this.entries.set(request.requestId, next);
    return structuredClone(next);
  }

  restore(entries: readonly WorktreeLedgerEntry[]): void {
    const next = new Map<string, WorktreeLedgerEntry>();
    for (const entry of entries) {
      assertEntry(entry);
      if (next.has(entry.requestId))
        throw new E03RuntimeError(
          "duplicate_worktree_ledger",
          `duplicate request ${entry.requestId}`,
        );
      next.set(entry.requestId, structuredClone(entry));
    }
    this.entries.clear();
    for (const [key, value] of next) this.entries.set(key, value);
  }

  snapshot(): WorktreeLedgerEntry[] {
    return [...this.entries.values()]
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.requestId.localeCompare(right.requestId),
      )
      .map((entry) => structuredClone(entry));
  }

  unresolved(taskId?: string): WorktreeLedgerEntry[] {
    return this.snapshot().filter(
      (entry) =>
        (!taskId || entry.taskId === taskId) && entry.phase !== "cleaned",
    );
  }

  private require(requestId: string): WorktreeLedgerEntry {
    const entry = this.entries.get(requestId);
    if (!entry)
      throw new E03RuntimeError(
        "unknown_worktree_request",
        `unknown worktree request ${requestId}`,
      );
    assertEntry(entry);
    return entry;
  }

  private assertRequest(request: E03IsolationRequest): void {
    const { digest: checksum, ...payload } = request;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "isolation_request_digest_mismatch",
        "isolation request digest is invalid",
      );
    if (
      !request.requestId ||
      !request.taskId ||
      !request.leaseId ||
      !request.workspaceRoot ||
      !request.idempotencyKey
    )
      throw new E03RuntimeError(
        "invalid_isolation_request",
        "isolation request identity is incomplete",
      );
  }

  private assertIsolationReceipt(
    request: E03IsolationRequest,
    receipt: E03IsolationReceipt,
  ): void {
    const { digest: checksum, ...payload } = receipt;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "isolation_receipt_digest_mismatch",
        "isolation receipt digest is invalid",
      );
    if (
      receipt.requestId !== request.requestId ||
      receipt.taskId !== request.taskId ||
      receipt.leaseId !== request.leaseId
    )
      throw new E03RuntimeError(
        "isolation_receipt_custody_mismatch",
        "isolation receipt differs from request custody",
      );
  }

  private assertMergeReceipt(
    current: WorktreeLedgerEntry,
    request: E03IsolationRequest,
    receipt: MergeReceipt,
  ): void {
    const { digest: checksum, ...payload } = receipt;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "merge_receipt_digest_mismatch",
        "merge receipt digest is invalid",
      );
    if (
      receipt.requestId !== request.requestId ||
      receipt.taskId !== current.taskId ||
      receipt.leaseId !== current.leaseId
    )
      throw new E03RuntimeError(
        "merge_receipt_custody_mismatch",
        "merge receipt differs from worktree custody",
      );
    if (
      receipt.expectedBaseRevision !== current.baseRevision ||
      receipt.sourceRevision !== current.isolatedRevision
    )
      throw new E03RuntimeError(
        "merge_revision_mismatch",
        "merge receipt uses a different base/source revision",
      );
  }

  private assertCleanupReceipt(
    current: WorktreeLedgerEntry,
    request: E03IsolationRequest,
    receipt: CleanupReceipt,
  ): void {
    const { digest: checksum, ...payload } = receipt;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "cleanup_receipt_digest_mismatch",
        "cleanup receipt digest is invalid",
      );
    if (
      receipt.requestId !== request.requestId ||
      receipt.taskId !== current.taskId ||
      receipt.leaseId !== current.leaseId
    )
      throw new E03RuntimeError(
        "cleanup_receipt_custody_mismatch",
        "cleanup receipt differs from worktree custody",
      );
  }
}

function sealEntry(
  value: Omit<WorktreeLedgerEntry, "digest"> | WorktreeLedgerEntry,
): WorktreeLedgerEntry {
  const { digest: _digest, ...payload } = value as WorktreeLedgerEntry;
  return { ...payload, digest: digest(payload) };
}

function assertEntry(entry: WorktreeLedgerEntry): void {
  const { digest: checksum, ...payload } = entry;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "worktree_ledger_digest_mismatch",
      `worktree ledger ${entry.ledgerId} digest is invalid`,
    );
  if (
    !entry.ledgerId ||
    !entry.taskId ||
    !entry.leaseId ||
    !entry.requestId ||
    !entry.workspaceRoot ||
    !entry.branchName
  )
    throw new E03RuntimeError(
      "invalid_worktree_ledger",
      "worktree ledger identity is incomplete",
    );
  if (
    entry.phase === "ready" &&
    (!entry.workspacePath || !entry.isolatedRevision)
  )
    throw new E03RuntimeError(
      "invalid_worktree_ready_state",
      "ready worktree lacks path or isolated revision",
    );
  if (entry.phase === "conflicted" && !entry.conflictedPaths.length)
    throw new E03RuntimeError(
      "invalid_worktree_conflict",
      "conflicted worktree has no paths",
    );
  if (entry.phase === "cleanup_failed" && !entry.cleanupError)
    throw new E03RuntimeError(
      "invalid_worktree_cleanup",
      "failed cleanup has no error",
    );
}

function uniquePaths(values: readonly string[]): string[] {
  return [
    ...new Set(
      values.map((value) => value.trim().replaceAll("\\", "/")).filter(Boolean),
    ),
  ].sort();
}

export type WorktreeChangeKind =
  | "add"
  | "modify"
  | "delete"
  | "rename"
  | "copy"
  | "mode";

export interface WorktreePathChange {
  changeId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  path: string;
  priorPath: string | null;
  kind: WorktreeChangeKind;
  baseBlob: string | null;
  isolatedBlob: string | null;
  baseMode: string | null;
  isolatedMode: string | null;
  binary: boolean;
  bytesAdded: number;
  bytesRemoved: number;
  linesAdded: number;
  linesRemoved: number;
  observedAt: string;
  digest: string;
}

export interface WorktreeChangeSet {
  changeSetId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  baseRevision: string;
  isolatedRevision: string;
  changes: WorktreePathChange[];
  addedPaths: string[];
  modifiedPaths: string[];
  deletedPaths: string[];
  renamedPaths: string[];
  binaryPaths: string[];
  totalBytesAdded: number;
  totalBytesRemoved: number;
  totalLinesAdded: number;
  totalLinesRemoved: number;
  createdAt: string;
  digest: string;
}

function assertWorktreeChange(change: WorktreePathChange): void {
  const { digest: checksum, ...payload } = change;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "worktree_change_checksum",
      `worktree change ${change.changeId} checksum mismatch`,
    );
  if (!change.path || change.path.startsWith("../") || change.path === "..")
    throw new E03RuntimeError(
      "worktree_change_path",
      `worktree change ${change.changeId} path is invalid`,
    );
  if (
    change.bytesAdded < 0 ||
    change.bytesRemoved < 0 ||
    change.linesAdded < 0 ||
    change.linesRemoved < 0
  )
    throw new E03RuntimeError(
      "worktree_change_counter",
      `worktree change ${change.changeId} counters are invalid`,
    );
  if ((change.kind === "rename" || change.kind === "copy") && !change.priorPath)
    throw new E03RuntimeError(
      "worktree_change_prior_path",
      `${change.kind} change requires a prior path`,
    );
}

export class WorktreeChangeSetRuntime {
  private sets = new Map<string, WorktreeChangeSet>();

  create(input: {
    task: E03TaskState;
    request: E03IsolationRequest;
    isolation: E03IsolationReceipt;
    baseRevision: string;
    isolatedRevision: string;
    changes: readonly Omit<
      WorktreePathChange,
      "changeId" | "taskId" | "requestId" | "leaseId" | "observedAt" | "digest"
    >[];
    now?: string;
  }): WorktreeChangeSet {
    if (
      input.request.taskId !== input.task.identity.taskId ||
      input.request.leaseId !== input.task.identity.leaseId ||
      input.isolation.taskId !== input.task.identity.taskId ||
      input.isolation.leaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "worktree_changeset_custody",
        "worktree change set identity differs from task",
      );
    if (!input.isolation.accepted)
      throw new E03RuntimeError(
        "worktree_changeset_isolation_rejected",
        "cannot create change set for rejected isolation",
      );
    if (
      input.request.baseRevision !== input.baseRevision ||
      input.isolation.resultingRevision !== input.isolatedRevision
    )
      throw new E03RuntimeError(
        "worktree_changeset_revision",
        "worktree change set revision differs from isolation receipt",
      );
    const observedAt = input.now ?? new Date().toISOString();
    const paths = new Set<string>();
    const changes = input.changes.map((raw) => {
      const path = raw.path.trim().replaceAll("\\", "/");
      const priorPath = raw.priorPath?.trim().replaceAll("\\", "/") || null;
      if (paths.has(path))
        throw new E03RuntimeError(
          "duplicate_worktree_change_path",
          `worktree path ${path} appears more than once`,
        );
      paths.add(path);
      const payload = {
        changeId: `worktree-change-${digest({
          requestId: input.request.requestId,
          path,
          priorPath,
          kind: raw.kind,
          isolatedBlob: raw.isolatedBlob,
        }).slice(0, 32)}`,
        taskId: input.task.identity.taskId,
        requestId: input.request.requestId,
        leaseId: input.task.identity.leaseId,
        path,
        priorPath,
        kind: raw.kind,
        baseBlob: raw.baseBlob,
        isolatedBlob: raw.isolatedBlob,
        baseMode: raw.baseMode,
        isolatedMode: raw.isolatedMode,
        binary: raw.binary,
        bytesAdded: raw.bytesAdded,
        bytesRemoved: raw.bytesRemoved,
        linesAdded: raw.linesAdded,
        linesRemoved: raw.linesRemoved,
        observedAt,
      };
      const change = { ...payload, digest: digest(payload) };
      assertWorktreeChange(change);
      return change;
    });
    const payload = {
      changeSetId: `worktree-changeset-${digest({
        requestId: input.request.requestId,
        baseRevision: input.baseRevision,
        isolatedRevision: input.isolatedRevision,
        changes: changes.map((change) => change.digest),
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      requestId: input.request.requestId,
      leaseId: input.task.identity.leaseId,
      baseRevision: input.baseRevision,
      isolatedRevision: input.isolatedRevision,
      changes,
      addedPaths: changes
        .filter((change) => change.kind === "add")
        .map((change) => change.path)
        .sort(),
      modifiedPaths: changes
        .filter((change) => change.kind === "modify" || change.kind === "mode")
        .map((change) => change.path)
        .sort(),
      deletedPaths: changes
        .filter((change) => change.kind === "delete")
        .map((change) => change.path)
        .sort(),
      renamedPaths: changes
        .filter((change) => change.kind === "rename" || change.kind === "copy")
        .map((change) => change.path)
        .sort(),
      binaryPaths: changes
        .filter((change) => change.binary)
        .map((change) => change.path)
        .sort(),
      totalBytesAdded: changes.reduce(
        (sum, change) => sum + change.bytesAdded,
        0,
      ),
      totalBytesRemoved: changes.reduce(
        (sum, change) => sum + change.bytesRemoved,
        0,
      ),
      totalLinesAdded: changes.reduce(
        (sum, change) => sum + change.linesAdded,
        0,
      ),
      totalLinesRemoved: changes.reduce(
        (sum, change) => sum + change.linesRemoved,
        0,
      ),
      createdAt: observedAt,
    };
    const changeSet = { ...payload, digest: digest(payload) };
    this.verify(changeSet);
    const existing = this.sets.get(changeSet.changeSetId);
    if (existing) return structuredClone(existing);
    this.sets.set(changeSet.changeSetId, changeSet);
    return structuredClone(changeSet);
  }

  compare(
    leftId: string,
    rightId: string,
  ): {
    unchanged: boolean;
    added: string[];
    removed: string[];
    modified: string[];
    digest: string;
  } {
    const left = this.require(leftId);
    const right = this.require(rightId);
    if (left.taskId !== right.taskId || left.requestId !== right.requestId)
      throw new E03RuntimeError(
        "worktree_changeset_compare_custody",
        "cannot compare change sets from different worktrees",
      );
    const leftByPath = new Map(
      left.changes.map((change) => [change.path, change]),
    );
    const rightByPath = new Map(
      right.changes.map((change) => [change.path, change]),
    );
    const added = [...rightByPath.keys()]
      .filter((path) => !leftByPath.has(path))
      .sort();
    const removed = [...leftByPath.keys()]
      .filter((path) => !rightByPath.has(path))
      .sort();
    const modified = [...rightByPath.keys()]
      .filter(
        (path) =>
          leftByPath.has(path) &&
          leftByPath.get(path)!.digest !== rightByPath.get(path)!.digest,
      )
      .sort();
    const payload = {
      unchanged: !added.length && !removed.length && !modified.length,
      added,
      removed,
      modified,
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(changeSets: readonly WorktreeChangeSet[]): void {
    const next = new Map<string, WorktreeChangeSet>();
    for (const raw of changeSets) {
      const changeSet = structuredClone(raw);
      this.verify(changeSet);
      if (next.has(changeSet.changeSetId))
        throw new E03RuntimeError(
          "duplicate_worktree_changeset",
          `worktree change set ${changeSet.changeSetId} repeats`,
        );
      next.set(changeSet.changeSetId, changeSet);
    }
    this.sets = next;
  }

  snapshot(): WorktreeChangeSet[] {
    return [...this.sets.values()]
      .sort((left, right) => left.changeSetId.localeCompare(right.changeSetId))
      .map((changeSet) => structuredClone(changeSet));
  }

  private require(changeSetId: string): WorktreeChangeSet {
    const changeSet = this.sets.get(changeSetId);
    if (!changeSet)
      throw new E03RuntimeError(
        "worktree_changeset_missing",
        `worktree change set ${changeSetId} is missing`,
      );
    this.verify(changeSet);
    return changeSet;
  }

  private verify(changeSet: WorktreeChangeSet): void {
    const { digest: checksum, ...payload } = changeSet;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "worktree_changeset_checksum",
        `worktree change set ${changeSet.changeSetId} checksum mismatch`,
      );
    const paths = new Set<string>();
    for (const change of changeSet.changes) {
      assertWorktreeChange(change);
      if (
        change.taskId !== changeSet.taskId ||
        change.requestId !== changeSet.requestId
      )
        throw new E03RuntimeError(
          "worktree_changeset_change_custody",
          `worktree change ${change.changeId} belongs elsewhere`,
        );
      if (paths.has(change.path))
        throw new E03RuntimeError(
          "duplicate_worktree_change_path",
          `worktree path ${change.path} appears more than once`,
        );
      paths.add(change.path);
    }
  }
}

export type MergePathAction =
  | "apply"
  | "delete"
  | "rename"
  | "keep-target"
  | "manual"
  | "skip";

export interface MergePathInstruction {
  instructionId: string;
  mergePlanId: string;
  taskId: string;
  path: string;
  priorPath: string | null;
  changeKind: WorktreeChangeKind;
  action: MergePathAction;
  expectedTargetBlob: string | null;
  sourceBlob: string | null;
  expectedTargetMode: string | null;
  sourceMode: string | null;
  binary: boolean;
  conflictRisk: "none" | "low" | "high" | "certain";
  reason: string;
  order: number;
  digest: string;
}

export interface WorktreeMergePlan {
  mergePlanId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  changeSetId: string;
  expectedBaseRevision: string;
  sourceRevision: string;
  targetRevision: string;
  strategy: "fast-forward" | "three-way" | "squash" | "cherry-pick";
  instructions: MergePathInstruction[];
  certainConflictPaths: string[];
  highRiskPaths: string[];
  binaryPaths: string[];
  expectedArtifacts: string[];
  allowPartial: boolean;
  requireCleanTarget: boolean;
  plannedAt: string;
  digest: string;
}

export interface TargetPathObservation {
  path: string;
  blob: string | null;
  mode: string | null;
  exists: boolean;
  modifiedSinceBase: boolean;
  untracked: boolean;
  ignored: boolean;
}

function assertMergeInstruction(instruction: MergePathInstruction): void {
  const { digest: checksum, ...payload } = instruction;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_instruction_checksum",
      `merge instruction ${instruction.instructionId} checksum mismatch`,
    );
  if (!instruction.path || instruction.path.startsWith("../"))
    throw new E03RuntimeError(
      "merge_instruction_path",
      `merge instruction ${instruction.instructionId} path is invalid`,
    );
  if (instruction.order < 0)
    throw new E03RuntimeError(
      "merge_instruction_order",
      `merge instruction ${instruction.instructionId} order is invalid`,
    );
}

function assertMergePlan(plan: WorktreeMergePlan): void {
  const { digest: checksum, ...payload } = plan;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "worktree_merge_plan_checksum",
      `merge plan ${plan.mergePlanId} checksum mismatch`,
    );
  const paths = new Set<string>();
  const orders = new Set<number>();
  for (const instruction of plan.instructions) {
    assertMergeInstruction(instruction);
    if (instruction.mergePlanId !== plan.mergePlanId)
      throw new E03RuntimeError(
        "merge_instruction_plan",
        `merge instruction ${instruction.instructionId} belongs elsewhere`,
      );
    if (paths.has(instruction.path) || orders.has(instruction.order))
      throw new E03RuntimeError(
        "duplicate_merge_instruction",
        `merge instruction for ${instruction.path} repeats`,
      );
    paths.add(instruction.path);
    orders.add(instruction.order);
  }
}

export class WorktreeMergePlanner {
  private plans = new Map<string, WorktreeMergePlan>();

  plan(input: {
    task: E03TaskState;
    request: E03IsolationRequest;
    changeSet: WorktreeChangeSet;
    targetRevision: string;
    targetObservations: readonly TargetPathObservation[];
    strategy?: WorktreeMergePlan["strategy"];
    allowPartial?: boolean;
    requireCleanTarget?: boolean;
    now?: string;
  }): WorktreeMergePlan {
    if (
      input.changeSet.taskId !== input.task.identity.taskId ||
      input.changeSet.requestId !== input.request.requestId ||
      input.changeSet.leaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "merge_plan_custody",
        "merge plan inputs belong to different worktrees",
      );
    if (!input.targetRevision.trim())
      throw new E03RuntimeError(
        "merge_plan_target_revision",
        "merge plan target revision is required",
      );
    const observations = new Map<string, TargetPathObservation>();
    for (const raw of input.targetObservations) {
      const path = raw.path.trim().replaceAll("\\", "/");
      if (!path || path.startsWith("../") || observations.has(path))
        throw new E03RuntimeError(
          "merge_target_observation_path",
          `target path observation ${path} is invalid or duplicate`,
        );
      observations.set(path, { ...structuredClone(raw), path });
    }
    const mergePlanId = `worktree-merge-plan-${digest({
      taskId: input.task.identity.taskId,
      requestId: input.request.requestId,
      changeSetId: input.changeSet.changeSetId,
      targetRevision: input.targetRevision,
    }).slice(0, 32)}`;
    const instructions = input.changeSet.changes.map((change, order) => {
      const observation = observations.get(change.path);
      let conflictRisk: MergePathInstruction["conflictRisk"] = "none";
      let action: MergePathAction =
        change.kind === "delete"
          ? "delete"
          : change.kind === "rename"
            ? "rename"
            : "apply";
      let reason = "source_change_can_apply";
      if (observation?.untracked && change.kind !== "delete") {
        conflictRisk = "certain";
        action = "manual";
        reason = "target_untracked_path_collision";
      } else if (
        observation?.modifiedSinceBase &&
        observation.blob !== change.baseBlob
      ) {
        conflictRisk = change.binary ? "certain" : "high";
        action = change.binary ? "manual" : "apply";
        reason = change.binary
          ? "binary_target_modified"
          : "text_target_modified_three_way_required";
      } else if (
        change.kind === "add" &&
        observation?.exists &&
        observation.blob !== change.isolatedBlob
      ) {
        conflictRisk = "certain";
        action = "manual";
        reason = "added_path_already_exists";
      } else if (change.kind === "delete" && observation?.modifiedSinceBase) {
        conflictRisk = "high";
        action = "manual";
        reason = "deleting_modified_target";
      } else if (change.binary) {
        conflictRisk = "low";
        reason = "binary_source_change";
      }
      const payload = {
        instructionId: `merge-instruction-${digest({
          mergePlanId,
          path: change.path,
          action,
          targetBlob: observation?.blob ?? null,
          sourceBlob: change.isolatedBlob,
        }).slice(0, 32)}`,
        mergePlanId,
        taskId: input.task.identity.taskId,
        path: change.path,
        priorPath: change.priorPath,
        changeKind: change.kind,
        action,
        expectedTargetBlob: observation?.blob ?? null,
        sourceBlob: change.isolatedBlob,
        expectedTargetMode: observation?.mode ?? null,
        sourceMode: change.isolatedMode,
        binary: change.binary,
        conflictRisk,
        reason,
        order,
      };
      const instruction = { ...payload, digest: digest(payload) };
      assertMergeInstruction(instruction);
      return instruction;
    });
    const certainConflictPaths = instructions
      .filter((instruction) => instruction.conflictRisk === "certain")
      .map((instruction) => instruction.path)
      .sort();
    const highRiskPaths = instructions
      .filter((instruction) => instruction.conflictRisk === "high")
      .map((instruction) => instruction.path)
      .sort();
    if (certainConflictPaths.length && !input.allowPartial)
      throw new E03RuntimeError(
        "merge_plan_certain_conflict",
        "merge plan contains certain conflicts and partial merge is disabled",
        { certainConflictPaths },
      );
    const payload = {
      mergePlanId,
      taskId: input.task.identity.taskId,
      requestId: input.request.requestId,
      leaseId: input.task.identity.leaseId,
      changeSetId: input.changeSet.changeSetId,
      expectedBaseRevision: input.changeSet.baseRevision,
      sourceRevision: input.changeSet.isolatedRevision,
      targetRevision: input.targetRevision.trim(),
      strategy: input.strategy ?? "three-way",
      instructions,
      certainConflictPaths,
      highRiskPaths,
      binaryPaths: instructions
        .filter((instruction) => instruction.binary)
        .map((instruction) => instruction.path)
        .sort(),
      expectedArtifacts: [...input.request.expectedArtifacts].sort(),
      allowPartial: input.allowPartial ?? false,
      requireCleanTarget: input.requireCleanTarget ?? true,
      plannedAt: input.now ?? new Date().toISOString(),
    };
    const plan = { ...payload, digest: digest(payload) };
    assertMergePlan(plan);
    const existing = this.plans.get(plan.mergePlanId);
    if (existing) return structuredClone(existing);
    this.plans.set(plan.mergePlanId, plan);
    return structuredClone(plan);
  }

  verifyTarget(
    plan: WorktreeMergePlan,
    observations: readonly TargetPathObservation[],
  ): void {
    assertMergePlan(plan);
    const byPath = new Map(
      observations.map((observation) => [
        observation.path.trim().replaceAll("\\", "/"),
        observation,
      ]),
    );
    for (const instruction of plan.instructions) {
      const observation = byPath.get(instruction.path);
      const actualBlob = observation?.blob ?? null;
      const actualMode = observation?.mode ?? null;
      if (actualBlob !== instruction.expectedTargetBlob)
        throw new E03RuntimeError(
          "merge_target_blob_changed",
          `target blob for ${instruction.path} changed after planning`,
        );
      if (actualMode !== instruction.expectedTargetMode)
        throw new E03RuntimeError(
          "merge_target_mode_changed",
          `target mode for ${instruction.path} changed after planning`,
        );
    }
  }

  restore(plans: readonly WorktreeMergePlan[]): void {
    const next = new Map<string, WorktreeMergePlan>();
    for (const raw of plans) {
      const plan = structuredClone(raw);
      assertMergePlan(plan);
      if (next.has(plan.mergePlanId))
        throw new E03RuntimeError(
          "duplicate_merge_plan",
          `merge plan ${plan.mergePlanId} repeats`,
        );
      next.set(plan.mergePlanId, plan);
    }
    this.plans = next;
  }

  snapshot(): WorktreeMergePlan[] {
    return [...this.plans.values()]
      .sort((left, right) => left.mergePlanId.localeCompare(right.mergePlanId))
      .map((plan) => structuredClone(plan));
  }
}

export type MergeConflictResolutionKind =
  | "take-source"
  | "take-target"
  | "manual-content"
  | "delete"
  | "rename";

export interface MergeConflictRecord {
  conflictId: string;
  mergePlanId: string;
  taskId: string;
  leaseId: string;
  path: string;
  baseBlob: string | null;
  sourceBlob: string | null;
  targetBlob: string | null;
  binary: boolean;
  reason: string;
  status: "open" | "resolved" | "abandoned";
  resolution: MergeConflictResolutionKind | null;
  resolutionBlob: string | null;
  renamedPath: string | null;
  resolvedBy: string | null;
  openedAt: string;
  resolvedAt: string | null;
  revision: number;
  digest: string;
}

function assertMergeConflict(conflict: MergeConflictRecord): void {
  const { digest: checksum, ...payload } = conflict;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_conflict_checksum",
      `merge conflict ${conflict.conflictId} checksum mismatch`,
    );
  if (
    !conflict.path ||
    conflict.path.startsWith("../") ||
    conflict.revision < 1
  )
    throw new E03RuntimeError(
      "merge_conflict_identity",
      `merge conflict ${conflict.conflictId} identity is invalid`,
    );
  if (
    conflict.status === "resolved" &&
    (!conflict.resolution || !conflict.resolvedAt)
  )
    throw new E03RuntimeError(
      "merge_conflict_resolution_missing",
      `resolved merge conflict ${conflict.conflictId} lacks a resolution`,
    );
}

export class MergeConflictRuntime {
  private conflicts = new Map<string, MergeConflictRecord>();

  open(input: {
    plan: WorktreeMergePlan;
    path: string;
    baseBlob?: string | null;
    sourceBlob?: string | null;
    targetBlob?: string | null;
    binary?: boolean;
    reason: string;
    now?: string;
  }): MergeConflictRecord {
    assertMergePlan(input.plan);
    const instruction = input.plan.instructions.find(
      (candidate) => candidate.path === input.path,
    );
    if (!instruction)
      throw new E03RuntimeError(
        "merge_conflict_instruction_missing",
        `merge path ${input.path} is not in plan`,
      );
    const payload = {
      conflictId: `merge-conflict-${digest({
        mergePlanId: input.plan.mergePlanId,
        path: input.path,
        sourceBlob: input.sourceBlob ?? instruction.sourceBlob,
        targetBlob: input.targetBlob ?? instruction.expectedTargetBlob,
      }).slice(0, 32)}`,
      mergePlanId: input.plan.mergePlanId,
      taskId: input.plan.taskId,
      leaseId: input.plan.leaseId,
      path: input.path,
      baseBlob: input.baseBlob ?? null,
      sourceBlob: input.sourceBlob ?? instruction.sourceBlob,
      targetBlob: input.targetBlob ?? instruction.expectedTargetBlob,
      binary: input.binary ?? instruction.binary,
      reason: input.reason.trim() || instruction.reason,
      status: "open" as const,
      resolution: null,
      resolutionBlob: null,
      renamedPath: null,
      resolvedBy: null,
      openedAt: input.now ?? new Date().toISOString(),
      resolvedAt: null,
      revision: 1,
    };
    const conflict = { ...payload, digest: digest(payload) };
    assertMergeConflict(conflict);
    const existing = this.conflicts.get(conflict.conflictId);
    if (existing) return structuredClone(existing);
    this.conflicts.set(conflict.conflictId, conflict);
    return structuredClone(conflict);
  }

  resolve(input: {
    conflictId: string;
    expectedRevision: number;
    leaseId: string;
    resolution: MergeConflictResolutionKind;
    resolutionBlob?: string;
    renamedPath?: string;
    resolvedBy: string;
    now?: string;
  }): MergeConflictRecord {
    const current = this.require(input.conflictId);
    if (current.status === "resolved") return structuredClone(current);
    if (current.status !== "open")
      throw new E03RuntimeError(
        "merge_conflict_not_open",
        `merge conflict ${current.conflictId} is ${current.status}`,
      );
    if (current.revision !== input.expectedRevision)
      throw new E03RuntimeError(
        "merge_conflict_stale_revision",
        `merge conflict ${current.conflictId} revision changed`,
      );
    if (current.leaseId !== input.leaseId)
      throw new E03RuntimeError(
        "merge_conflict_stale_lease",
        `merge conflict ${current.conflictId} lease changed`,
      );
    if (input.resolution === "manual-content" && !input.resolutionBlob?.trim())
      throw new E03RuntimeError(
        "merge_manual_blob_missing",
        "manual merge resolution requires a result blob",
      );
    if (input.resolution === "rename" && !input.renamedPath?.trim())
      throw new E03RuntimeError(
        "merge_rename_path_missing",
        "rename merge resolution requires a path",
      );
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      status: "resolved" as const,
      resolution: input.resolution,
      resolutionBlob:
        input.resolution === "take-source"
          ? current.sourceBlob
          : input.resolution === "take-target"
            ? current.targetBlob
            : input.resolution === "delete"
              ? null
              : input.resolutionBlob?.trim() || null,
      renamedPath: input.renamedPath?.trim().replaceAll("\\", "/") || null,
      resolvedBy: input.resolvedBy.trim(),
      resolvedAt: input.now ?? new Date().toISOString(),
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeConflict(next);
    this.conflicts.set(next.conflictId, next);
    return structuredClone(next);
  }

  abandon(
    conflictId: string,
    expectedRevision: number,
    reason: string,
    now = new Date().toISOString(),
  ): MergeConflictRecord {
    const current = this.require(conflictId);
    if (current.revision !== expectedRevision)
      throw new E03RuntimeError(
        "merge_conflict_stale_revision",
        `merge conflict ${conflictId} revision changed`,
      );
    if (current.status !== "open") return structuredClone(current);
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      status: "abandoned" as const,
      reason: `${current.reason};abandoned:${reason.trim() || "unspecified"}`,
      resolvedAt: now,
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeConflict(next);
    this.conflicts.set(next.conflictId, next);
    return structuredClone(next);
  }

  assertResolved(plan: WorktreeMergePlan): MergeConflictRecord[] {
    assertMergePlan(plan);
    const conflicts = this.forPlan(plan.mergePlanId);
    const open = conflicts.filter((conflict) => conflict.status === "open");
    if (open.length)
      throw new E03RuntimeError(
        "merge_conflicts_unresolved",
        `merge plan ${plan.mergePlanId} has unresolved conflicts`,
        { conflictIds: open.map((conflict) => conflict.conflictId) },
      );
    return conflicts;
  }

  restore(conflicts: readonly MergeConflictRecord[]): void {
    const next = new Map<string, MergeConflictRecord>();
    for (const raw of conflicts) {
      const conflict = structuredClone(raw);
      assertMergeConflict(conflict);
      if (next.has(conflict.conflictId))
        throw new E03RuntimeError(
          "duplicate_merge_conflict",
          `merge conflict ${conflict.conflictId} repeats`,
        );
      next.set(conflict.conflictId, conflict);
    }
    this.conflicts = next;
  }

  forPlan(mergePlanId: string): MergeConflictRecord[] {
    return [...this.conflicts.values()]
      .filter((conflict) => conflict.mergePlanId === mergePlanId)
      .sort((left, right) => left.path.localeCompare(right.path))
      .map((conflict) => structuredClone(conflict));
  }

  snapshot(): MergeConflictRecord[] {
    return [...this.conflicts.values()]
      .sort((left, right) => left.conflictId.localeCompare(right.conflictId))
      .map((conflict) => structuredClone(conflict));
  }

  private require(conflictId: string): MergeConflictRecord {
    const conflict = this.conflicts.get(conflictId);
    if (!conflict)
      throw new E03RuntimeError(
        "merge_conflict_missing",
        `merge conflict ${conflictId} is missing`,
      );
    assertMergeConflict(conflict);
    return conflict;
  }
}

export type CleanupActionKind =
  | "stop-process"
  | "flush-artifact"
  | "remove-worktree"
  | "delete-branch"
  | "release-lease"
  | "revoke-token"
  | "prune-metadata";

export interface CleanupAction {
  actionId: string;
  cleanupPlanId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  kind: CleanupActionKind;
  target: string;
  dependencies: string[];
  required: boolean;
  maximumAttempts: number;
  timeoutMs: number;
  idempotencyKey: string;
  order: number;
  digest: string;
}

export interface WorktreeCleanupPlan {
  cleanupPlanId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  workspacePath: string;
  branchName: string;
  retainArtifacts: string[];
  actions: CleanupAction[];
  force: boolean;
  plannedAt: string;
  digest: string;
}

export interface CleanupActionAttempt {
  attemptId: string;
  cleanupPlanId: string;
  actionId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  status: "started" | "succeeded" | "failed" | "timed-out" | "skipped";
  effectId: string;
  receiptId: string | null;
  errorCode: string;
  errorDigest: string;
  startedAt: string;
  completedAt: string | null;
  previousDigest: string;
  digest: string;
}

export interface CleanupProjection {
  cleanupPlanId: string;
  taskId: string;
  status: "pending" | "running" | "succeeded" | "failed";
  readyActionIds: string[];
  runningActionIds: string[];
  succeededActionIds: string[];
  failedActionIds: string[];
  skippedActionIds: string[];
  exhaustedActionIds: string[];
  evaluatedAt: string;
  digest: string;
}

function assertCleanupAction(action: CleanupAction): void {
  const { digest: checksum, ...payload } = action;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "cleanup_action_checksum",
      `cleanup action ${action.actionId} checksum mismatch`,
    );
  if (
    !action.target ||
    action.maximumAttempts < 1 ||
    action.timeoutMs < 1 ||
    action.order < 0
  )
    throw new E03RuntimeError(
      "cleanup_action_invalid",
      `cleanup action ${action.actionId} is invalid`,
    );
}

function assertCleanupPlan(plan: WorktreeCleanupPlan): void {
  const { digest: checksum, ...payload } = plan;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "cleanup_plan_checksum",
      `cleanup plan ${plan.cleanupPlanId} checksum mismatch`,
    );
  const actionIds = new Set<string>();
  for (const action of plan.actions) {
    assertCleanupAction(action);
    if (action.cleanupPlanId !== plan.cleanupPlanId)
      throw new E03RuntimeError(
        "cleanup_action_plan_mismatch",
        `cleanup action ${action.actionId} belongs elsewhere`,
      );
    if (actionIds.has(action.actionId))
      throw new E03RuntimeError(
        "duplicate_cleanup_action",
        `cleanup action ${action.actionId} repeats`,
      );
    actionIds.add(action.actionId);
  }
  for (const action of plan.actions)
    for (const dependency of action.dependencies)
      if (!actionIds.has(dependency))
        throw new E03RuntimeError(
          "cleanup_action_dependency_missing",
          `cleanup action ${action.actionId} dependency ${dependency} is missing`,
        );
}

function assertCleanupAttempt(attempt: CleanupActionAttempt): void {
  const { digest: checksum, ...payload } = attempt;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "cleanup_attempt_checksum",
      `cleanup attempt ${attempt.attemptId} checksum mismatch`,
    );
  if (attempt.attempt < 1 || !attempt.effectId)
    throw new E03RuntimeError(
      "cleanup_attempt_invalid",
      `cleanup attempt ${attempt.attemptId} is invalid`,
    );
  if (
    ["succeeded", "failed", "timed-out", "skipped"].includes(attempt.status) &&
    !attempt.completedAt
  )
    throw new E03RuntimeError(
      "cleanup_attempt_terminal_time",
      `cleanup attempt ${attempt.attemptId} is terminal without a completion time`,
    );
}

export class WorktreeCleanupSupervisor {
  private plans = new Map<string, WorktreeCleanupPlan>();
  private attempts = new Map<string, CleanupActionAttempt[]>();

  plan(input: {
    task: E03TaskState;
    request: E03IsolationRequest;
    isolation: E03IsolationReceipt;
    processIds?: readonly string[];
    artifactPaths?: readonly string[];
    tokenIds?: readonly string[];
    retainArtifacts?: readonly string[];
    force?: boolean;
    maximumAttempts?: number;
    timeoutMs?: number;
    now?: string;
  }): WorktreeCleanupPlan {
    if (
      input.request.taskId !== input.task.identity.taskId ||
      input.request.leaseId !== input.task.identity.leaseId ||
      input.isolation.taskId !== input.task.identity.taskId ||
      input.isolation.leaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "cleanup_plan_custody",
        "cleanup plan inputs belong to different tasks",
      );
    const maximumAttempts = input.maximumAttempts ?? 3;
    const timeoutMs = input.timeoutMs ?? 30_000;
    if (!Number.isSafeInteger(maximumAttempts) || maximumAttempts < 1)
      throw new E03RuntimeError(
        "cleanup_plan_attempt_limit",
        "cleanup plan attempt limit is invalid",
      );
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1)
      throw new E03RuntimeError(
        "cleanup_plan_timeout",
        "cleanup plan timeout is invalid",
      );
    const cleanupPlanId = `worktree-cleanup-plan-${digest({
      taskId: input.task.identity.taskId,
      requestId: input.request.requestId,
      leaseId: input.task.identity.leaseId,
    }).slice(0, 32)}`;
    const actionSeeds: Array<{
      key: string;
      kind: CleanupActionKind;
      target: string;
      dependencies: string[];
      required: boolean;
    }> = [];
    for (const processId of uniquePaths(input.processIds ?? []))
      actionSeeds.push({
        key: `process:${processId}`,
        kind: "stop-process",
        target: processId,
        dependencies: [],
        required: true,
      });
    for (const artifactPath of uniquePaths(input.artifactPaths ?? []))
      actionSeeds.push({
        key: `artifact:${artifactPath}`,
        kind: "flush-artifact",
        target: artifactPath,
        dependencies: [],
        required: input.request.expectedArtifacts.includes(artifactPath),
      });
    for (const tokenId of uniquePaths(input.tokenIds ?? []))
      actionSeeds.push({
        key: `token:${tokenId}`,
        kind: "revoke-token",
        target: tokenId,
        dependencies: [],
        required: true,
      });
    const processKeys = actionSeeds
      .filter((seed) => seed.kind === "stop-process")
      .map((seed) => seed.key);
    const artifactKeys = actionSeeds
      .filter((seed) => seed.kind === "flush-artifact")
      .map((seed) => seed.key);
    actionSeeds.push({
      key: "worktree",
      kind: "remove-worktree",
      target: input.isolation.workspacePath,
      dependencies: [...processKeys, ...artifactKeys],
      required: true,
    });
    actionSeeds.push({
      key: "branch",
      kind: "delete-branch",
      target: input.request.branchName,
      dependencies: ["worktree"],
      required: !input.force,
    });
    actionSeeds.push({
      key: "lease",
      kind: "release-lease",
      target: input.task.identity.leaseId,
      dependencies: [
        "worktree",
        ...actionSeeds
          .filter((seed) => seed.kind === "revoke-token")
          .map((seed) => seed.key),
      ],
      required: true,
    });
    actionSeeds.push({
      key: "metadata",
      kind: "prune-metadata",
      target: input.request.requestId,
      dependencies: ["lease", "branch"],
      required: false,
    });
    const actionIdByKey = new Map(
      actionSeeds.map((seed) => [
        seed.key,
        `cleanup-action-${digest({ cleanupPlanId, key: seed.key, target: seed.target }).slice(0, 32)}`,
      ]),
    );
    const actions = actionSeeds.map((seed, order) => {
      const payload = {
        actionId: actionIdByKey.get(seed.key)!,
        cleanupPlanId,
        taskId: input.task.identity.taskId,
        requestId: input.request.requestId,
        leaseId: input.task.identity.leaseId,
        kind: seed.kind,
        target: seed.target,
        dependencies: seed.dependencies.map((key) => actionIdByKey.get(key)!),
        required: seed.required,
        maximumAttempts,
        timeoutMs,
        idempotencyKey: `cleanup:${input.request.requestId}:${seed.kind}:${digest(seed.target).slice(0, 16)}`,
        order,
      };
      const action = { ...payload, digest: digest(payload) };
      assertCleanupAction(action);
      return action;
    });
    const payload = {
      cleanupPlanId,
      taskId: input.task.identity.taskId,
      requestId: input.request.requestId,
      leaseId: input.task.identity.leaseId,
      workspacePath: input.isolation.workspacePath,
      branchName: input.request.branchName,
      retainArtifacts: uniquePaths(input.retainArtifacts ?? []),
      actions,
      force: input.force ?? false,
      plannedAt: input.now ?? new Date().toISOString(),
    };
    const plan = { ...payload, digest: digest(payload) };
    assertCleanupPlan(plan);
    const existing = this.plans.get(cleanupPlanId);
    if (existing) return structuredClone(existing);
    this.plans.set(cleanupPlanId, plan);
    return structuredClone(plan);
  }

  acquire(
    cleanupPlanId: string,
    maximum: number,
    now = new Date().toISOString(),
  ): CleanupActionAttempt[] {
    const plan = this.requirePlan(cleanupPlanId);
    if (!Number.isSafeInteger(maximum) || maximum < 1)
      throw new E03RuntimeError(
        "cleanup_acquire_limit",
        "cleanup acquire maximum is invalid",
      );
    const projection = this.project(plan, now);
    const selected = projection.readyActionIds.slice(0, maximum);
    const attempts = selected.map((actionId) => {
      const action = plan.actions.find(
        (candidate) => candidate.actionId === actionId,
      )!;
      const prior = this.attempts.get(actionId) ?? [];
      const previousDigest = prior.at(-1)?.digest ?? "";
      const payload = {
        attemptId: `cleanup-attempt-${digest({
          actionId,
          attempt: prior.length + 1,
          previousDigest,
        }).slice(0, 32)}`,
        cleanupPlanId,
        actionId,
        taskId: plan.taskId,
        leaseId: plan.leaseId,
        attempt: prior.length + 1,
        status: "started" as const,
        effectId: `cleanup-effect-${digest({ actionId, attempt: prior.length + 1 }).slice(0, 32)}`,
        receiptId: null,
        errorCode: "",
        errorDigest: "",
        startedAt: now,
        completedAt: null,
        previousDigest,
      };
      const attempt = { ...payload, digest: digest(payload) };
      assertCleanupAttempt(attempt);
      prior.push(attempt);
      this.attempts.set(actionId, prior);
      return attempt;
    });
    return attempts.map((attempt) => structuredClone(attempt));
  }

  settle(input: {
    cleanupPlanId: string;
    actionId: string;
    attemptId: string;
    effectId: string;
    succeeded: boolean;
    timedOut?: boolean;
    receiptId?: string;
    errorCode?: string;
    error?: string;
    now?: string;
  }): CleanupActionAttempt {
    const plan = this.requirePlan(input.cleanupPlanId);
    const action = plan.actions.find(
      (candidate) => candidate.actionId === input.actionId,
    );
    if (!action)
      throw new E03RuntimeError(
        "cleanup_action_missing",
        `cleanup action ${input.actionId} is missing`,
      );
    const attempts = this.attempts.get(action.actionId) ?? [];
    const current = attempts.find(
      (attempt) => attempt.attemptId === input.attemptId,
    );
    if (!current)
      throw new E03RuntimeError(
        "cleanup_attempt_missing",
        `cleanup attempt ${input.attemptId} is missing`,
      );
    if (current.status !== "started") return structuredClone(current);
    if (current.effectId !== input.effectId)
      throw new E03RuntimeError(
        "cleanup_effect_mismatch",
        "cleanup effect id differs from acquired attempt",
      );
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      status: input.succeeded
        ? ("succeeded" as const)
        : input.timedOut
          ? ("timed-out" as const)
          : ("failed" as const),
      receiptId: input.receiptId?.trim() || null,
      errorCode: input.succeeded
        ? ""
        : input.errorCode?.trim() ||
          (input.timedOut ? "cleanup_timeout" : "cleanup_failed"),
      errorDigest: input.succeeded ? "" : digest(input.error ?? ""),
      completedAt: input.now ?? new Date().toISOString(),
    };
    const next = { ...payload, digest: digest(payload) };
    assertCleanupAttempt(next);
    attempts[attempts.indexOf(current)] = next;
    this.attempts.set(action.actionId, attempts);
    return structuredClone(next);
  }

  skip(
    cleanupPlanId: string,
    actionId: string,
    reason: string,
    now = new Date().toISOString(),
  ): CleanupActionAttempt {
    const plan = this.requirePlan(cleanupPlanId);
    const action = plan.actions.find(
      (candidate) => candidate.actionId === actionId,
    );
    if (!action)
      throw new E03RuntimeError(
        "cleanup_action_missing",
        `cleanup action ${actionId} is missing`,
      );
    if (action.required)
      throw new E03RuntimeError(
        "cleanup_required_action_skip",
        `required cleanup action ${actionId} cannot be skipped`,
      );
    const attempts = this.attempts.get(actionId) ?? [];
    const previousDigest = attempts.at(-1)?.digest ?? "";
    const payload = {
      attemptId: `cleanup-attempt-${digest({ actionId, status: "skipped", previousDigest }).slice(0, 32)}`,
      cleanupPlanId,
      actionId,
      taskId: plan.taskId,
      leaseId: plan.leaseId,
      attempt: attempts.length + 1,
      status: "skipped" as const,
      effectId: `cleanup-skip-${digest({ actionId, reason }).slice(0, 32)}`,
      receiptId: null,
      errorCode: reason.trim() || "optional_cleanup_skipped",
      errorDigest: digest(reason),
      startedAt: now,
      completedAt: now,
      previousDigest,
    };
    const attempt = { ...payload, digest: digest(payload) };
    assertCleanupAttempt(attempt);
    attempts.push(attempt);
    this.attempts.set(actionId, attempts);
    return structuredClone(attempt);
  }

  project(
    planOrId: WorktreeCleanupPlan | string,
    now = new Date().toISOString(),
  ): CleanupProjection {
    const plan =
      typeof planOrId === "string" ? this.requirePlan(planOrId) : planOrId;
    assertCleanupPlan(plan);
    const readyActionIds: string[] = [];
    const runningActionIds: string[] = [];
    const succeededActionIds: string[] = [];
    const failedActionIds: string[] = [];
    const skippedActionIds: string[] = [];
    const exhaustedActionIds: string[] = [];
    const finalByAction = new Map<string, CleanupActionAttempt | undefined>();
    for (const action of plan.actions)
      finalByAction.set(
        action.actionId,
        (this.attempts.get(action.actionId) ?? []).at(-1),
      );
    for (const action of plan.actions) {
      const final = finalByAction.get(action.actionId);
      if (final?.status === "started") {
        if (Date.parse(now) - Date.parse(final.startedAt) >= action.timeoutMs)
          failedActionIds.push(action.actionId);
        else runningActionIds.push(action.actionId);
        continue;
      }
      if (final?.status === "succeeded") {
        succeededActionIds.push(action.actionId);
        continue;
      }
      if (final?.status === "skipped") {
        skippedActionIds.push(action.actionId);
        continue;
      }
      const attempts = this.attempts.get(action.actionId) ?? [];
      if (attempts.length >= action.maximumAttempts) {
        exhaustedActionIds.push(action.actionId);
        failedActionIds.push(action.actionId);
        continue;
      }
      const dependenciesSatisfied = action.dependencies.every((dependency) => {
        const dependencyAttempt = finalByAction.get(dependency);
        return (
          dependencyAttempt?.status === "succeeded" ||
          dependencyAttempt?.status === "skipped"
        );
      });
      if (dependenciesSatisfied) readyActionIds.push(action.actionId);
    }
    const requiredFailures = failedActionIds.filter(
      (actionId) =>
        plan.actions.find((action) => action.actionId === actionId)!.required,
    );
    const terminalCount = succeededActionIds.length + skippedActionIds.length;
    const status: CleanupProjection["status"] = requiredFailures.length
      ? "failed"
      : terminalCount === plan.actions.length
        ? "succeeded"
        : runningActionIds.length
          ? "running"
          : "pending";
    const payload = {
      cleanupPlanId: plan.cleanupPlanId,
      taskId: plan.taskId,
      status,
      readyActionIds,
      runningActionIds,
      succeededActionIds,
      failedActionIds,
      skippedActionIds,
      exhaustedActionIds,
      evaluatedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(input: {
    plans: readonly WorktreeCleanupPlan[];
    attempts: readonly CleanupActionAttempt[];
  }): void {
    const plans = new Map<string, WorktreeCleanupPlan>();
    const actionToPlan = new Map<string, WorktreeCleanupPlan>();
    for (const raw of input.plans) {
      const plan = structuredClone(raw);
      assertCleanupPlan(plan);
      if (plans.has(plan.cleanupPlanId))
        throw new E03RuntimeError(
          "duplicate_cleanup_plan",
          `cleanup plan ${plan.cleanupPlanId} repeats`,
        );
      plans.set(plan.cleanupPlanId, plan);
      for (const action of plan.actions)
        actionToPlan.set(action.actionId, plan);
    }
    const attempts = new Map<string, CleanupActionAttempt[]>();
    for (const raw of input.attempts) {
      const attempt = structuredClone(raw);
      assertCleanupAttempt(attempt);
      const plan = actionToPlan.get(attempt.actionId);
      if (!plan || plan.cleanupPlanId !== attempt.cleanupPlanId)
        throw new E03RuntimeError(
          "cleanup_attempt_action_missing",
          `cleanup attempt ${attempt.attemptId} action is missing`,
        );
      const current = attempts.get(attempt.actionId) ?? [];
      if (
        attempt.attempt !== current.length + 1 ||
        attempt.previousDigest !== (current.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "cleanup_attempt_chain",
          `cleanup attempt ${attempt.attemptId} chain is invalid`,
        );
      current.push(attempt);
      attempts.set(attempt.actionId, current);
    }
    this.plans = plans;
    this.attempts = attempts;
  }

  snapshot(): {
    plans: WorktreeCleanupPlan[];
    attempts: CleanupActionAttempt[];
  } {
    return {
      plans: [...this.plans.values()]
        .sort((left, right) =>
          left.cleanupPlanId.localeCompare(right.cleanupPlanId),
        )
        .map((plan) => structuredClone(plan)),
      attempts: [...this.attempts.values()]
        .flat()
        .sort(
          (left, right) =>
            left.cleanupPlanId.localeCompare(right.cleanupPlanId) ||
            left.actionId.localeCompare(right.actionId) ||
            left.attempt - right.attempt,
        )
        .map((attempt) => structuredClone(attempt)),
    };
  }

  private requirePlan(cleanupPlanId: string): WorktreeCleanupPlan {
    const plan = this.plans.get(cleanupPlanId);
    if (!plan)
      throw new E03RuntimeError(
        "cleanup_plan_missing",
        `cleanup plan ${cleanupPlanId} is missing`,
      );
    assertCleanupPlan(plan);
    return plan;
  }
}

export type MergeApprovalState =
  | "requested"
  | "reviewing"
  | "approved"
  | "rejected"
  | "expired"
  | "consumed";

export interface MergeApprovalRequest {
  approvalId: string;
  mergePlanId: string;
  changeSetId: string;
  taskId: string;
  leaseId: string;
  requesterId: string;
  reviewerIds: string[];
  minimumApprovals: number;
  state: MergeApprovalState;
  riskScore: number;
  changedPathCount: number;
  conflictCount: number;
  requiredChecks: string[];
  passedChecks: string[];
  failedChecks: string[];
  requestedAt: string;
  expiresAt: string;
  decidedAt: string | null;
  consumedAt: string | null;
  revision: number;
  digest: string;
}

export interface MergeApprovalVote {
  voteId: string;
  approvalId: string;
  reviewerId: string;
  decision: "approve" | "reject" | "abstain";
  reason: string;
  evidenceRefs: string[];
  approvalRevision: number;
  votedAt: string;
  digest: string;
}

function assertMergeApprovalRequest(approval: MergeApprovalRequest): void {
  const { digest: checksum, ...payload } = approval;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_approval_digest",
      `merge approval ${approval.approvalId} digest is invalid`,
    );
  if (
    !approval.approvalId ||
    !approval.mergePlanId ||
    !approval.changeSetId ||
    !approval.taskId ||
    !approval.leaseId ||
    !approval.requesterId
  )
    throw new E03RuntimeError(
      "merge_approval_identity",
      "merge approval identity is incomplete",
    );
  if (
    !Number.isSafeInteger(approval.minimumApprovals) ||
    approval.minimumApprovals < 1 ||
    approval.minimumApprovals > approval.reviewerIds.length
  )
    throw new E03RuntimeError(
      "merge_approval_threshold",
      "merge approval threshold is invalid",
    );
  if (
    !Number.isFinite(approval.riskScore) ||
    approval.riskScore < 0 ||
    approval.riskScore > 1
  )
    throw new E03RuntimeError(
      "merge_approval_risk",
      "merge approval risk score is invalid",
    );
  if (Date.parse(approval.requestedAt) >= Date.parse(approval.expiresAt))
    throw new E03RuntimeError(
      "merge_approval_expiry",
      "merge approval expiry is invalid",
    );
  if (!Number.isSafeInteger(approval.revision) || approval.revision < 1)
    throw new E03RuntimeError(
      "merge_approval_revision",
      "merge approval revision is invalid",
    );
  if (
    ["approved", "rejected", "expired"].includes(approval.state) &&
    !approval.decidedAt
  )
    throw new E03RuntimeError(
      "merge_approval_state",
      "decided merge approval requires decidedAt",
    );
}

function assertMergeApprovalVote(vote: MergeApprovalVote): void {
  const { digest: checksum, ...payload } = vote;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_approval_vote_digest",
      `merge approval vote ${vote.voteId} digest is invalid`,
    );
  if (!vote.voteId || !vote.approvalId || !vote.reviewerId || !vote.reason)
    throw new E03RuntimeError(
      "merge_approval_vote_identity",
      "merge approval vote identity is incomplete",
    );
  if (!Number.isSafeInteger(vote.approvalRevision) || vote.approvalRevision < 1)
    throw new E03RuntimeError(
      "merge_approval_vote_revision",
      "merge approval vote revision is invalid",
    );
}

export class WorktreeMergeApprovalRuntime {
  private approvals = new Map<string, MergeApprovalRequest>();
  private votes = new Map<string, MergeApprovalVote>();
  private voteByReviewer = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  request(input: {
    plan: WorktreeMergePlan;
    requesterId: string;
    reviewerIds: readonly string[];
    minimumApprovals: number;
    requiredChecks?: readonly string[];
    riskScore: number;
    expiresAt: string;
  }): MergeApprovalRequest {
    assertMergePlan(input.plan);
    const reviewerIds = [...new Set(input.reviewerIds)].filter(
      (reviewerId) => reviewerId !== input.requesterId,
    );
    const now = this.clock.now();
    const payload = {
      approvalId: createId("merge-approval"),
      mergePlanId: input.plan.mergePlanId,
      changeSetId: input.plan.changeSetId,
      taskId: input.plan.taskId,
      leaseId: input.plan.leaseId,
      requesterId: input.requesterId.trim(),
      reviewerIds,
      minimumApprovals: input.minimumApprovals,
      state: "requested" as const,
      riskScore: input.riskScore,
      changedPathCount: input.plan.instructions.length,
      conflictCount: input.plan.certainConflictPaths.length,
      requiredChecks: [...new Set(input.requiredChecks ?? [])],
      passedChecks: [],
      failedChecks: [],
      requestedAt: now,
      expiresAt: input.expiresAt,
      decidedAt: null,
      consumedAt: null,
      revision: 1,
    };
    const approval = { ...payload, digest: digest(payload) };
    assertMergeApprovalRequest(approval);
    this.approvals.set(approval.approvalId, approval);
    return structuredClone(approval);
  }

  check(input: {
    approvalId: string;
    expectedRevision: number;
    checkId: string;
    passed: boolean;
  }): MergeApprovalRequest {
    const approval = this.requireApproval(input.approvalId);
    this.assertRevision(approval, input.expectedRevision);
    if (approval.state !== "requested" && approval.state !== "reviewing")
      throw new E03RuntimeError(
        "merge_approval_check_state",
        `merge approval ${approval.approvalId} is ${approval.state}`,
      );
    if (!approval.requiredChecks.includes(input.checkId))
      throw new E03RuntimeError(
        "merge_approval_check_unknown",
        `merge approval check ${input.checkId} is not required`,
      );
    const passedChecks = approval.passedChecks.filter(
      (value) => value !== input.checkId,
    );
    const failedChecks = approval.failedChecks.filter(
      (value) => value !== input.checkId,
    );
    if (input.passed) passedChecks.push(input.checkId);
    else failedChecks.push(input.checkId);
    return this.transition(approval, {
      state: "reviewing",
      passedChecks: [...new Set(passedChecks)].sort(),
      failedChecks: [...new Set(failedChecks)].sort(),
    });
  }

  vote(input: {
    approvalId: string;
    expectedRevision: number;
    reviewerId: string;
    decision: MergeApprovalVote["decision"];
    reason: string;
    evidenceRefs?: readonly string[];
  }): { approval: MergeApprovalRequest; vote: MergeApprovalVote } {
    let approval = this.requireApproval(input.approvalId);
    this.assertRevision(approval, input.expectedRevision);
    if (approval.state !== "requested" && approval.state !== "reviewing")
      throw new E03RuntimeError(
        "merge_approval_vote_state",
        `merge approval ${approval.approvalId} is ${approval.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(approval.expiresAt)) {
      approval = this.transition(approval, {
        state: "expired",
        decidedAt: now,
      });
      throw new E03RuntimeError(
        "merge_approval_expired",
        `merge approval ${approval.approvalId} expired`,
      );
    }
    if (!approval.reviewerIds.includes(input.reviewerId))
      throw new E03RuntimeError(
        "merge_approval_reviewer",
        `reviewer ${input.reviewerId} is not assigned`,
      );
    const reviewerKey = `${approval.approvalId}\0${input.reviewerId}`;
    if (this.voteByReviewer.has(reviewerKey))
      throw new E03RuntimeError(
        "merge_approval_vote_duplicate",
        `reviewer ${input.reviewerId} already voted`,
      );
    const votePayload = {
      voteId: createId("merge-approval-vote"),
      approvalId: approval.approvalId,
      reviewerId: input.reviewerId,
      decision: input.decision,
      reason: input.reason.trim(),
      evidenceRefs: [...new Set(input.evidenceRefs ?? [])],
      approvalRevision: approval.revision,
      votedAt: now,
    };
    const vote = { ...votePayload, digest: digest(votePayload) };
    assertMergeApprovalVote(vote);
    this.votes.set(vote.voteId, vote);
    this.voteByReviewer.set(reviewerKey, vote.voteId);
    const votes = this.votesFor(approval.approvalId);
    const rejected = votes.some((value) => value.decision === "reject");
    const approvedCount = votes.filter(
      (value) => value.decision === "approve",
    ).length;
    const checksComplete = approval.requiredChecks.every((checkId) =>
      approval.passedChecks.includes(checkId),
    );
    if (rejected || approval.failedChecks.length)
      approval = this.transition(approval, {
        state: "rejected",
        decidedAt: now,
      });
    else if (approvedCount >= approval.minimumApprovals && checksComplete)
      approval = this.transition(approval, {
        state: "approved",
        decidedAt: now,
      });
    else approval = this.transition(approval, { state: "reviewing" });
    return { approval, vote: structuredClone(vote) };
  }

  consume(
    approvalId: string,
    plan: WorktreeMergePlan,
    expectedRevision: number,
  ): MergeApprovalRequest {
    const approval = this.requireApproval(approvalId);
    this.assertRevision(approval, expectedRevision);
    if (approval.state !== "approved")
      throw new E03RuntimeError(
        "merge_approval_consume_state",
        `merge approval ${approvalId} is ${approval.state}`,
      );
    assertMergePlan(plan);
    if (
      plan.mergePlanId !== approval.mergePlanId ||
      plan.changeSetId !== approval.changeSetId ||
      plan.taskId !== approval.taskId ||
      plan.leaseId !== approval.leaseId
    )
      throw new E03RuntimeError(
        "merge_approval_consume_custody",
        `merge approval ${approvalId} belongs to another plan`,
      );
    return this.transition(approval, {
      state: "consumed",
      consumedAt: this.clock.now(),
    });
  }

  votesFor(approvalId: string): MergeApprovalVote[] {
    this.requireApproval(approvalId);
    return [...this.votes.values()]
      .filter((vote) => vote.approvalId === approvalId)
      .sort((left, right) => left.votedAt.localeCompare(right.votedAt))
      .map((vote) => structuredClone(vote));
  }

  snapshot(): {
    approvals: MergeApprovalRequest[];
    votes: MergeApprovalVote[];
  } {
    return {
      approvals: [...this.approvals.values()].map((value) =>
        structuredClone(value),
      ),
      votes: [...this.votes.values()].map((value) => structuredClone(value)),
    };
  }

  restore(input: {
    approvals: readonly MergeApprovalRequest[];
    votes: readonly MergeApprovalVote[];
  }): void {
    const approvals = new Map<string, MergeApprovalRequest>();
    const votes = new Map<string, MergeApprovalVote>();
    const voteByReviewer = new Map<string, string>();
    for (const approval of input.approvals) {
      assertMergeApprovalRequest(approval);
      if (approvals.has(approval.approvalId))
        throw new E03RuntimeError(
          "merge_approval_restore_duplicate",
          `duplicate merge approval ${approval.approvalId}`,
        );
      approvals.set(approval.approvalId, structuredClone(approval));
    }
    for (const vote of input.votes) {
      assertMergeApprovalVote(vote);
      const approval = approvals.get(vote.approvalId);
      if (!approval || !approval.reviewerIds.includes(vote.reviewerId))
        throw new E03RuntimeError(
          "merge_approval_vote_restore_approval",
          `merge approval vote ${vote.voteId} has invalid approval custody`,
        );
      const key = `${vote.approvalId}\0${vote.reviewerId}`;
      if (votes.has(vote.voteId) || voteByReviewer.has(key))
        throw new E03RuntimeError(
          "merge_approval_vote_restore_duplicate",
          `duplicate merge approval vote ${vote.voteId}`,
        );
      votes.set(vote.voteId, structuredClone(vote));
      voteByReviewer.set(key, vote.voteId);
    }
    this.approvals = approvals;
    this.votes = votes;
    this.voteByReviewer = voteByReviewer;
  }

  private requireApproval(approvalId: string): MergeApprovalRequest {
    const approval = this.approvals.get(approvalId);
    if (!approval)
      throw new E03RuntimeError(
        "merge_approval_missing",
        `merge approval ${approvalId} does not exist`,
      );
    assertMergeApprovalRequest(approval);
    return approval;
  }

  private assertRevision(
    approval: MergeApprovalRequest,
    expected: number,
  ): void {
    if (approval.revision !== expected)
      throw new E03RuntimeError(
        "merge_approval_stale_revision",
        `merge approval ${approval.approvalId} revision is stale`,
      );
  }

  private transition(
    approval: MergeApprovalRequest,
    patch: Partial<
      Omit<MergeApprovalRequest, "approvalId" | "revision" | "digest">
    >,
  ): MergeApprovalRequest {
    const { digest: _, ...prior } = approval;
    const payload = {
      ...prior,
      ...patch,
      approvalId: approval.approvalId,
      revision: approval.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeApprovalRequest(next);
    this.approvals.set(next.approvalId, next);
    return structuredClone(next);
  }
}

export interface MergeRollbackRecord {
  rollbackId: string;
  mergeId: string;
  taskId: string;
  leaseId: string;
  fromRevision: string;
  targetRevision: string;
  state: "planned" | "running" | "completed" | "failed";
  affectedPaths: string[];
  restoredPaths: string[];
  failedPaths: string[];
  requestedAt: string;
  startedAt: string | null;
  completedAt: string | null;
  failure: string | null;
  revision: number;
  digest: string;
}

export class WorktreeMergeRollbackRuntime {
  private records = new Map<string, MergeRollbackRecord>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  plan(input: {
    receipt: MergeReceipt;
    targetRevision: string;
    affectedPaths: readonly string[];
  }): MergeRollbackRecord {
    if (!input.receipt.accepted || !input.receipt.resultingRevision)
      throw new E03RuntimeError(
        "merge_rollback_receipt",
        "merge rollback requires an accepted merge receipt",
      );
    const payload = {
      rollbackId: createId("merge-rollback"),
      mergeId: input.receipt.mergeId,
      taskId: input.receipt.taskId,
      leaseId: input.receipt.leaseId,
      fromRevision: input.receipt.resultingRevision,
      targetRevision: input.targetRevision.trim(),
      state: "planned" as const,
      affectedPaths: uniquePaths(input.affectedPaths),
      restoredPaths: [],
      failedPaths: [],
      requestedAt: this.clock.now(),
      startedAt: null,
      completedAt: null,
      failure: null,
      revision: 1,
    };
    const record = { ...payload, digest: digest(payload) };
    this.assertRecord(record);
    this.records.set(record.rollbackId, record);
    return structuredClone(record);
  }

  start(rollbackId: string, expectedRevision: number): MergeRollbackRecord {
    const record = this.requireRecord(rollbackId);
    this.assertRevision(record, expectedRevision);
    if (record.state !== "planned")
      throw new E03RuntimeError(
        "merge_rollback_start_state",
        `merge rollback ${rollbackId} is ${record.state}`,
      );
    return this.transition(record, {
      state: "running",
      startedAt: this.clock.now(),
    });
  }

  recordPath(input: {
    rollbackId: string;
    expectedRevision: number;
    path: string;
    restored: boolean;
  }): MergeRollbackRecord {
    const record = this.requireRecord(input.rollbackId);
    this.assertRevision(record, input.expectedRevision);
    if (record.state !== "running")
      throw new E03RuntimeError(
        "merge_rollback_path_state",
        `merge rollback ${record.rollbackId} is ${record.state}`,
      );
    const path = input.path.replaceAll("\\", "/");
    if (!record.affectedPaths.includes(path))
      throw new E03RuntimeError(
        "merge_rollback_path_unknown",
        `merge rollback path ${path} is not affected`,
      );
    return this.transition(record, {
      restoredPaths: input.restored
        ? uniquePaths([...record.restoredPaths, path])
        : record.restoredPaths,
      failedPaths: input.restored
        ? record.failedPaths.filter((value) => value !== path)
        : uniquePaths([...record.failedPaths, path]),
    });
  }

  complete(rollbackId: string, expectedRevision: number): MergeRollbackRecord {
    const record = this.requireRecord(rollbackId);
    this.assertRevision(record, expectedRevision);
    if (record.state !== "running")
      throw new E03RuntimeError(
        "merge_rollback_complete_state",
        `merge rollback ${rollbackId} is ${record.state}`,
      );
    const pending = record.affectedPaths.filter(
      (path) =>
        !record.restoredPaths.includes(path) &&
        !record.failedPaths.includes(path),
    );
    if (pending.length)
      throw new E03RuntimeError(
        "merge_rollback_incomplete",
        `merge rollback ${rollbackId} has pending paths`,
      );
    return this.transition(record, {
      state: record.failedPaths.length ? "failed" : "completed",
      completedAt: this.clock.now(),
      failure: record.failedPaths.length
        ? `failed paths: ${record.failedPaths.join(", ")}`
        : null,
    });
  }

  snapshot(): MergeRollbackRecord[] {
    return [...this.records.values()].map((value) => structuredClone(value));
  }

  restore(values: readonly MergeRollbackRecord[]): void {
    const records = new Map<string, MergeRollbackRecord>();
    for (const record of values) {
      this.assertRecord(record);
      if (records.has(record.rollbackId))
        throw new E03RuntimeError(
          "merge_rollback_restore_duplicate",
          `duplicate merge rollback ${record.rollbackId}`,
        );
      records.set(record.rollbackId, structuredClone(record));
    }
    this.records = records;
  }

  private requireRecord(rollbackId: string): MergeRollbackRecord {
    const record = this.records.get(rollbackId);
    if (!record)
      throw new E03RuntimeError(
        "merge_rollback_missing",
        `merge rollback ${rollbackId} does not exist`,
      );
    this.assertRecord(record);
    return record;
  }

  private assertRecord(record: MergeRollbackRecord): void {
    const { digest: checksum, ...payload } = record;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "merge_rollback_digest",
        `merge rollback ${record.rollbackId} digest is invalid`,
      );
    if (
      !record.rollbackId ||
      !record.mergeId ||
      !record.taskId ||
      !record.leaseId ||
      !record.fromRevision ||
      !record.targetRevision
    )
      throw new E03RuntimeError(
        "merge_rollback_identity",
        "merge rollback identity is incomplete",
      );
    if (!Number.isSafeInteger(record.revision) || record.revision < 1)
      throw new E03RuntimeError(
        "merge_rollback_revision",
        "merge rollback revision is invalid",
      );
    if (["completed", "failed"].includes(record.state) && !record.completedAt)
      throw new E03RuntimeError(
        "merge_rollback_state",
        "terminal merge rollback requires completion time",
      );
  }

  private assertRevision(record: MergeRollbackRecord, expected: number): void {
    if (record.revision !== expected)
      throw new E03RuntimeError(
        "merge_rollback_stale_revision",
        `merge rollback ${record.rollbackId} revision is stale`,
      );
  }

  private transition(
    record: MergeRollbackRecord,
    patch: Partial<
      Omit<MergeRollbackRecord, "rollbackId" | "revision" | "digest">
    >,
  ): MergeRollbackRecord {
    const { digest: _, ...prior } = record;
    const payload = {
      ...prior,
      ...patch,
      rollbackId: record.rollbackId,
      revision: record.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    this.assertRecord(next);
    this.records.set(next.rollbackId, next);
    return structuredClone(next);
  }
}

export interface MergeVerificationPolicy {
  policyId: string;
  name: string;
  requiredChecks: string[];
  optionalChecks: string[];
  minimumOptionalPasses: number;
  rejectOnConflict: boolean;
  requireCleanTarget: boolean;
  requireSourceAttestation: boolean;
  state: "active" | "disabled" | "retired";
  revision: number;
  digest: string;
}
export interface MergeVerificationEvidence {
  evidenceId: string;
  attestationId: string;
  check: string;
  required: boolean;
  outcome: "passed" | "failed" | "warning" | "skipped";
  observedDigest: string;
  expectedDigest: string | null;
  summary: string;
  collectedAt: string;
  digest: string;
}
export interface MergeAttestation {
  attestationId: string;
  mergePlanId: string;
  taskId: string;
  requestId: string;
  policyId: string;
  state: "collecting" | "verified" | "rejected" | "revoked";
  baseRevision: string;
  sourceRevision: string;
  targetRevision: string;
  resultingRevision: string | null;
  evidenceIds: string[];
  requiredPassed: number;
  requiredFailed: number;
  optionalPassed: number;
  warningCount: number;
  openedAt: string;
  completedAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
function assertMergeVerificationPolicy(value: MergeVerificationPolicy): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_verification_policy_digest",
      `merge verification policy ${value.policyId} is corrupt`,
    );
  if (
    !value.policyId ||
    !value.name ||
    !value.requiredChecks.length ||
    !Number.isSafeInteger(value.minimumOptionalPasses) ||
    value.minimumOptionalPasses < 0 ||
    value.minimumOptionalPasses > value.optionalChecks.length ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "merge_verification_policy",
      `merge verification policy ${value.policyId} is invalid`,
    );
}
function assertMergeVerificationEvidence(
  value: MergeVerificationEvidence,
): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_verification_evidence_digest",
      `merge verification evidence ${value.evidenceId} is corrupt`,
    );
  if (
    !value.evidenceId ||
    !value.attestationId ||
    !value.check ||
    !value.observedDigest ||
    !value.summary
  )
    throw new E03RuntimeError(
      "merge_verification_evidence",
      `merge verification evidence ${value.evidenceId} is invalid`,
    );
}
function assertMergeAttestation(value: MergeAttestation): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "merge_attestation_digest",
      `merge attestation ${value.attestationId} is corrupt`,
    );
  if (
    !value.attestationId ||
    !value.mergePlanId ||
    !value.taskId ||
    !value.requestId ||
    !value.policyId ||
    !value.baseRevision ||
    !value.sourceRevision ||
    !value.targetRevision ||
    [
      value.requiredPassed,
      value.requiredFailed,
      value.optionalPassed,
      value.warningCount,
    ].some((number) => !Number.isSafeInteger(number) || number < 0) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "merge_attestation",
      `merge attestation ${value.attestationId} is invalid`,
    );
  if (
    (value.state === "verified" ||
      value.state === "rejected" ||
      value.state === "revoked") &&
    value.completedAt === null
  )
    throw new E03RuntimeError(
      "merge_attestation_completion",
      `completed merge attestation ${value.attestationId} lacks time`,
    );
}
export type MergePromotionState =
  | "requested"
  | "fenced"
  | "applying"
  | "verifying"
  | "promoted"
  | "rejected"
  | "rolled_back";

export interface MergePromotion {
  promotionId: string;
  mergePlanId: string;
  taskId: string;
  sourceRevision: string;
  expectedTargetRevision: string;
  resultingRevision: string;
  idempotencyKey: string;
  ownerId: string;
  state: MergePromotionState;
  fencingToken: number;
  requiredChecks: string[];
  passedChecks: string[];
  failedChecks: string[];
  requestedAt: string;
  updatedAt: string;
  promotedAt: string;
  terminalReason: string;
  revision: number;
  digest: string;
}

export interface MergePromotionEffect {
  effectId: string;
  promotionId: string;
  phase: "prepare" | "apply" | "verify" | "rollback";
  requestDigest: string;
  receiptDigest: string;
  workerId: string;
  fencingToken: number;
  accepted: boolean;
  errorCode: string;
  startedAt: string;
  completedAt: string;
  previousDigest: string;
  revision: number;
  digest: string;
}

export interface MergePromotionCheck {
  checkId: string;
  promotionId: string;
  check: string;
  attempt: number;
  accepted: boolean;
  evidenceDigest: string;
  outputArtifactIds: string[];
  errorCode: string;
  completedAt: string;
  previousDigest: string;
  digest: string;
}

export interface MergePromotionSnapshot {
  promotions: MergePromotion[];
  effects: MergePromotionEffect[];
  checks: MergePromotionCheck[];
  promotionByIdempotencyKey: [string, string][];
  activePromotionByPlan: [string, string][];
  nextFenceByPlan: [string, number][];
}

function assertMergePromotion(value: MergePromotion): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.promotionId ||
    !value.mergePlanId ||
    !value.taskId ||
    !value.sourceRevision ||
    !value.expectedTargetRevision ||
    !value.idempotencyKey ||
    !value.ownerId ||
    value.fencingToken < 0 ||
    value.revision < 1 ||
    new Set(value.requiredChecks).size !== value.requiredChecks.length ||
    new Set(value.passedChecks).size !== value.passedChecks.length ||
    new Set(value.failedChecks).size !== value.failedChecks.length ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_promotion_corrupt",
      `merge promotion ${value.promotionId || "<empty>"} is corrupt`,
    );
}

function assertMergePromotionEffect(value: MergePromotionEffect): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.effectId ||
    !value.promotionId ||
    !value.requestDigest ||
    !value.workerId ||
    value.fencingToken < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_promotion_effect_corrupt",
      `merge promotion effect ${value.effectId || "<empty>"} is corrupt`,
    );
}

function assertMergePromotionCheck(value: MergePromotionCheck): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.checkId ||
    !value.promotionId ||
    !value.check ||
    value.attempt < 1 ||
    !value.evidenceDigest ||
    new Set(value.outputArtifactIds).size !== value.outputArtifactIds.length ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_promotion_check_corrupt",
      `merge promotion check ${value.checkId || "<empty>"} is corrupt`,
    );
}

export class WorktreeMergePromotionRuntime {
  private promotions = new Map<string, MergePromotion>();
  private effects = new Map<string, MergePromotionEffect[]>();
  private checks = new Map<string, MergePromotionCheck[]>();
  private promotionByIdempotencyKey = new Map<string, string>();
  private activePromotionByPlan = new Map<string, string>();
  private nextFenceByPlan = new Map<string, number>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  request(input: {
    promotionId?: string;
    mergePlanId: string;
    taskId: string;
    sourceRevision: string;
    expectedTargetRevision: string;
    idempotencyKey: string;
    ownerId: string;
    requiredChecks: readonly string[];
  }): MergePromotion {
    const duplicate = this.promotionByIdempotencyKey.get(input.idempotencyKey);
    if (duplicate) return structuredClone(this.requirePromotion(duplicate));
    if (this.activePromotionByPlan.has(input.mergePlanId))
      throw new E03RuntimeError(
        "merge_promotion_active",
        `merge plan ${input.mergePlanId} already has an active promotion`,
      );
    const requiredChecks = [...new Set(input.requiredChecks)].sort();
    if (!requiredChecks.length)
      throw new E03RuntimeError(
        "merge_promotion_checks_empty",
        "merge promotion requires verification checks",
      );
    const promotionId = input.promotionId ?? createId("merge-promotion");
    if (this.promotions.has(promotionId))
      throw new E03RuntimeError(
        "merge_promotion_duplicate",
        `merge promotion ${promotionId} already exists`,
      );
    const now = this.clock.now();
    const payload = {
      promotionId,
      mergePlanId: input.mergePlanId,
      taskId: input.taskId,
      sourceRevision: input.sourceRevision,
      expectedTargetRevision: input.expectedTargetRevision,
      resultingRevision: "",
      idempotencyKey: input.idempotencyKey,
      ownerId: input.ownerId,
      state: "requested" as const,
      fencingToken: 0,
      requiredChecks,
      passedChecks: [] as string[],
      failedChecks: [] as string[],
      requestedAt: now,
      updatedAt: now,
      promotedAt: "",
      terminalReason: "",
      revision: 1,
    };
    const promotion = { ...payload, digest: digest(payload) };
    assertMergePromotion(promotion);
    this.promotions.set(promotionId, promotion);
    this.effects.set(promotionId, []);
    this.checks.set(promotionId, []);
    this.promotionByIdempotencyKey.set(input.idempotencyKey, promotionId);
    this.activePromotionByPlan.set(input.mergePlanId, promotionId);
    return structuredClone(promotion);
  }

  fence(promotionId: string, expectedRevision: number): MergePromotion {
    const promotion = this.requirePromotion(promotionId);
    this.assertPromotionRevision(promotion, expectedRevision);
    if (promotion.state !== "requested")
      throw new E03RuntimeError(
        "merge_promotion_fence_state",
        `merge promotion ${promotionId} is ${promotion.state}`,
      );
    const fencingToken =
      (this.nextFenceByPlan.get(promotion.mergePlanId) ?? 0) + 1;
    this.nextFenceByPlan.set(promotion.mergePlanId, fencingToken);
    return this.transitionPromotion(promotion, {
      state: "fenced",
      fencingToken,
    });
  }

  recordEffect(input: {
    effectId?: string;
    promotionId: string;
    expectedRevision: number;
    phase: MergePromotionEffect["phase"];
    requestDigest: string;
    receiptDigest: string;
    workerId: string;
    fencingToken: number;
    accepted: boolean;
    errorCode?: string;
    startedAt: string;
  }): MergePromotionEffect {
    const promotion = this.requirePromotion(input.promotionId);
    this.assertPromotionRevision(promotion, input.expectedRevision);
    if (promotion.fencingToken !== input.fencingToken)
      throw new E03RuntimeError(
        "merge_promotion_effect_fence",
        `merge promotion ${promotion.promotionId} fence is stale`,
      );
    const permitted: Record<
      MergePromotionEffect["phase"],
      MergePromotionState[]
    > = {
      prepare: ["fenced"],
      apply: ["fenced", "applying"],
      verify: ["verifying"],
      rollback: ["applying", "verifying", "rejected", "promoted"],
    };
    if (!permitted[input.phase].includes(promotion.state))
      throw new E03RuntimeError(
        "merge_promotion_effect_state",
        `merge promotion ${promotion.promotionId} cannot ${input.phase}`,
      );
    const entries = this.effectEntries(promotion.promotionId);
    const duplicate = entries.find(
      (value) =>
        value.phase === input.phase &&
        value.requestDigest === input.requestDigest,
    );
    if (duplicate) {
      if (
        duplicate.receiptDigest !== input.receiptDigest ||
        duplicate.accepted !== input.accepted
      )
        throw new E03RuntimeError(
          "merge_promotion_effect_idempotency_conflict",
          `merge promotion ${promotion.promotionId} effect conflicts`,
        );
      return structuredClone(duplicate);
    }
    const effectId = input.effectId ?? createId("merge-promotion-effect");
    const payload = {
      effectId,
      promotionId: promotion.promotionId,
      phase: input.phase,
      requestDigest: input.requestDigest,
      receiptDigest: input.receiptDigest,
      workerId: input.workerId,
      fencingToken: input.fencingToken,
      accepted: input.accepted,
      errorCode: input.errorCode ?? "",
      startedAt: input.startedAt,
      completedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
      revision: 1,
    };
    const effect = { ...payload, digest: digest(payload) };
    assertMergePromotionEffect(effect);
    entries.push(effect);
    this.effects.set(promotion.promotionId, entries);
    if (!effect.accepted)
      this.transitionPromotion(promotion, {
        state: "rejected",
        terminalReason: effect.errorCode || `${effect.phase}_failed`,
      });
    else if (effect.phase === "prepare" || effect.phase === "apply")
      this.transitionPromotion(promotion, { state: "applying" });
    else if (effect.phase === "rollback") {
      this.transitionPromotion(promotion, {
        state: "rolled_back",
        terminalReason: effect.errorCode || "rollback_complete",
      });
      this.activePromotionByPlan.delete(promotion.mergePlanId);
    }
    return structuredClone(effect);
  }

  beginVerification(
    promotionId: string,
    expectedRevision: number,
    resultingRevision: string,
  ): MergePromotion {
    const promotion = this.requirePromotion(promotionId);
    this.assertPromotionRevision(promotion, expectedRevision);
    if (promotion.state !== "applying")
      throw new E03RuntimeError(
        "merge_promotion_verify_state",
        `merge promotion ${promotionId} is ${promotion.state}`,
      );
    const apply = this.effectEntries(promotionId).find(
      (value) => value.phase === "apply" && value.accepted,
    );
    if (!apply || !resultingRevision)
      throw new E03RuntimeError(
        "merge_promotion_apply_receipt_missing",
        `merge promotion ${promotionId} has no apply receipt`,
      );
    return this.transitionPromotion(promotion, {
      state: "verifying",
      resultingRevision,
    });
  }

  recordCheck(input: {
    checkId?: string;
    promotionId: string;
    expectedRevision: number;
    check: string;
    accepted: boolean;
    evidenceDigest: string;
    outputArtifactIds?: readonly string[];
    errorCode?: string;
  }): MergePromotionCheck {
    const promotion = this.requirePromotion(input.promotionId);
    this.assertPromotionRevision(promotion, input.expectedRevision);
    if (promotion.state !== "verifying")
      throw new E03RuntimeError(
        "merge_promotion_check_state",
        `merge promotion ${promotion.promotionId} is ${promotion.state}`,
      );
    if (!promotion.requiredChecks.includes(input.check))
      throw new E03RuntimeError(
        "merge_promotion_check_unknown",
        `merge promotion check ${input.check} is not required`,
      );
    const entries = this.checkEntries(promotion.promotionId);
    const attempts = entries.filter((value) => value.check === input.check);
    const payload = {
      checkId: input.checkId ?? createId("merge-promotion-check"),
      promotionId: promotion.promotionId,
      check: input.check,
      attempt: attempts.length + 1,
      accepted: input.accepted,
      evidenceDigest: input.evidenceDigest,
      outputArtifactIds: [...new Set(input.outputArtifactIds ?? [])].sort(),
      errorCode: input.errorCode ?? "",
      completedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const check = { ...payload, digest: digest(payload) };
    assertMergePromotionCheck(check);
    entries.push(check);
    this.checks.set(promotion.promotionId, entries);
    const latestByName = new Map<string, MergePromotionCheck>();
    for (const value of entries) latestByName.set(value.check, value);
    const passedChecks = [...latestByName.values()]
      .filter((value) => value.accepted)
      .map((value) => value.check)
      .sort();
    const failedChecks = [...latestByName.values()]
      .filter((value) => !value.accepted)
      .map((value) => value.check)
      .sort();
    this.transitionPromotion(promotion, { passedChecks, failedChecks });
    return structuredClone(check);
  }

  promote(promotionId: string, expectedRevision: number): MergePromotion {
    const promotion = this.requirePromotion(promotionId);
    this.assertPromotionRevision(promotion, expectedRevision);
    if (promotion.state !== "verifying")
      throw new E03RuntimeError(
        "merge_promotion_promote_state",
        `merge promotion ${promotionId} is ${promotion.state}`,
      );
    if (
      promotion.failedChecks.length ||
      promotion.requiredChecks.some(
        (value) => !promotion.passedChecks.includes(value),
      )
    )
      throw new E03RuntimeError(
        "merge_promotion_checks_incomplete",
        `merge promotion ${promotionId} checks are incomplete`,
      );
    const next = this.transitionPromotion(promotion, {
      state: "promoted",
      promotedAt: this.clock.now(),
      terminalReason: "promotion_complete",
    });
    this.activePromotionByPlan.delete(promotion.mergePlanId);
    return next;
  }

  snapshot(): MergePromotionSnapshot {
    return {
      promotions: [...this.promotions.values()].map((value) =>
        structuredClone(value),
      ),
      effects: [...this.effects.values()]
        .flat()
        .map((value) => structuredClone(value)),
      checks: [...this.checks.values()]
        .flat()
        .map((value) => structuredClone(value)),
      promotionByIdempotencyKey: [...this.promotionByIdempotencyKey.entries()],
      activePromotionByPlan: [...this.activePromotionByPlan.entries()],
      nextFenceByPlan: [...this.nextFenceByPlan.entries()],
    };
  }

  restore(snapshot: MergePromotionSnapshot): void {
    const promotions = new Map<string, MergePromotion>();
    const effects = new Map<string, MergePromotionEffect[]>();
    const checks = new Map<string, MergePromotionCheck[]>();
    for (const value of snapshot.promotions) {
      assertMergePromotion(value);
      if (promotions.has(value.promotionId))
        throw new E03RuntimeError(
          "merge_promotion_restore_duplicate",
          `duplicate merge promotion ${value.promotionId}`,
        );
      promotions.set(value.promotionId, structuredClone(value));
      effects.set(value.promotionId, []);
      checks.set(value.promotionId, []);
    }
    for (const value of snapshot.effects) {
      assertMergePromotionEffect(value);
      const entries = effects.get(value.promotionId);
      if (
        !entries ||
        value.previousDigest !== (entries.at(-1)?.digest ?? "") ||
        entries.some((entry) => entry.effectId === value.effectId)
      )
        throw new E03RuntimeError(
          "merge_promotion_effect_restore_chain",
          `merge promotion effect ${value.effectId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.checks) {
      assertMergePromotionCheck(value);
      const entries = checks.get(value.promotionId);
      if (
        !entries ||
        value.previousDigest !== (entries.at(-1)?.digest ?? "") ||
        entries.some((entry) => entry.checkId === value.checkId)
      )
        throw new E03RuntimeError(
          "merge_promotion_check_restore_chain",
          `merge promotion check ${value.checkId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const promotionByIdempotencyKey = new Map(
      snapshot.promotionByIdempotencyKey,
    );
    const activePromotionByPlan = new Map(snapshot.activePromotionByPlan);
    const nextFenceByPlan = new Map(snapshot.nextFenceByPlan);
    if (
      promotionByIdempotencyKey.size !==
        snapshot.promotionByIdempotencyKey.length ||
      activePromotionByPlan.size !== snapshot.activePromotionByPlan.length
    )
      throw new E03RuntimeError(
        "merge_promotion_restore_index_duplicate",
        "merge promotion restore indexes contain duplicates",
      );
    for (const [key, promotionId] of promotionByIdempotencyKey) {
      const promotion = promotions.get(promotionId);
      if (!promotion || promotion.idempotencyKey !== key)
        throw new E03RuntimeError(
          "merge_promotion_restore_idempotency",
          `merge promotion idempotency index ${key} is invalid`,
        );
    }
    for (const [planId, promotionId] of activePromotionByPlan) {
      const promotion = promotions.get(promotionId);
      if (
        !promotion ||
        promotion.mergePlanId !== planId ||
        ["promoted", "rolled_back"].includes(promotion.state)
      )
        throw new E03RuntimeError(
          "merge_promotion_restore_active",
          `merge promotion active index ${planId} is invalid`,
        );
    }
    for (const [planId, fence] of nextFenceByPlan) {
      const maximum = Math.max(
        0,
        ...[...promotions.values()]
          .filter((value) => value.mergePlanId === planId)
          .map((value) => value.fencingToken),
      );
      if (!Number.isSafeInteger(fence) || fence < maximum)
        throw new E03RuntimeError(
          "merge_promotion_restore_fence",
          `merge promotion fence ${planId} is invalid`,
        );
    }
    this.promotions = promotions;
    this.effects = effects;
    this.checks = checks;
    this.promotionByIdempotencyKey = promotionByIdempotencyKey;
    this.activePromotionByPlan = activePromotionByPlan;
    this.nextFenceByPlan = nextFenceByPlan;
  }

  private effectEntries(promotionId: string): MergePromotionEffect[] {
    return this.effects.get(promotionId) ?? [];
  }

  private checkEntries(promotionId: string): MergePromotionCheck[] {
    return this.checks.get(promotionId) ?? [];
  }

  private requirePromotion(id: string): MergePromotion {
    const value = this.promotions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "merge_promotion_missing",
        `merge promotion ${id} does not exist`,
      );
    assertMergePromotion(value);
    return value;
  }

  private assertPromotionRevision(
    value: MergePromotion,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "merge_promotion_stale_revision",
        `merge promotion ${value.promotionId} revision is stale`,
      );
  }

  private transitionPromotion(
    value: MergePromotion,
    patch: Partial<Omit<MergePromotion, "promotionId" | "revision" | "digest">>,
  ): MergePromotion {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      promotionId: value.promotionId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergePromotion(next);
    this.promotions.set(next.promotionId, next);
    return structuredClone(next);
  }
}

export interface MergeArtifactSettlement {
  settlementId: string;
  mergePlanId: string;
  taskId: string;
  targetRevision: string;
  idempotencyKey: string;
  expectedArtifactIds: string[];
  acceptedArtifactIds: string[];
  rejectedArtifactIds: string[];
  state:
    | "open"
    | "collecting"
    | "verifying"
    | "committed"
    | "rejected"
    | "rolled_back";
  createdAt: string;
  updatedAt: string;
  committedAt: string;
  terminalReason: string;
  revision: number;
  digest: string;
}

export interface MergeArtifactReceipt {
  receiptId: string;
  settlementId: string;
  artifactId: string;
  logicalPath: string;
  contentDigest: string;
  byteLength: number;
  sourceRevision: string;
  targetRevision: string;
  workerId: string;
  accepted: boolean;
  errorCode: string;
  receivedAt: string;
  previousDigest: string;
  digest: string;
}

export interface MergeArtifactVerification {
  verificationId: string;
  settlementId: string;
  artifactId: string;
  check: "content" | "path" | "size" | "provenance" | "executable";
  accepted: boolean;
  evidenceDigest: string;
  verifierId: string;
  errorCode: string;
  verifiedAt: string;
  previousDigest: string;
  digest: string;
}

export interface MergeArtifactSettlementSnapshot {
  settlements: MergeArtifactSettlement[];
  receipts: MergeArtifactReceipt[];
  verifications: MergeArtifactVerification[];
  settlementByIdempotencyKey: [string, string][];
  activeSettlementByPlan: [string, string][];
}

function assertMergeArtifactSettlement(value: MergeArtifactSettlement): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.settlementId ||
    !value.mergePlanId ||
    !value.taskId ||
    !value.targetRevision ||
    !value.idempotencyKey ||
    !value.expectedArtifactIds.length ||
    new Set(value.expectedArtifactIds).size !==
      value.expectedArtifactIds.length ||
    new Set(value.acceptedArtifactIds).size !==
      value.acceptedArtifactIds.length ||
    new Set(value.rejectedArtifactIds).size !==
      value.rejectedArtifactIds.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_artifact_settlement_corrupt",
      `merge artifact settlement ${value.settlementId || "<empty>"} is corrupt`,
    );
}

function assertMergeArtifactReceipt(value: MergeArtifactReceipt): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.settlementId ||
    !value.artifactId ||
    !value.logicalPath ||
    value.logicalPath.startsWith("/") ||
    value.logicalPath.includes("..") ||
    !value.contentDigest ||
    value.byteLength < 0 ||
    !value.sourceRevision ||
    !value.targetRevision ||
    !value.workerId ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_artifact_receipt_corrupt",
      `merge artifact receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

function assertMergeArtifactVerification(
  value: MergeArtifactVerification,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.verificationId ||
    !value.settlementId ||
    !value.artifactId ||
    !value.evidenceDigest ||
    !value.verifierId ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "merge_artifact_verification_corrupt",
      `merge artifact verification ${value.verificationId || "<empty>"} is corrupt`,
    );
}

export class WorktreeMergeArtifactSettlementRuntime {
  private settlements = new Map<string, MergeArtifactSettlement>();
  private receipts = new Map<string, MergeArtifactReceipt[]>();
  private verifications = new Map<string, MergeArtifactVerification[]>();
  private settlementByIdempotencyKey = new Map<string, string>();
  private activeSettlementByPlan = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    settlementId?: string;
    mergePlanId: string;
    taskId: string;
    targetRevision: string;
    idempotencyKey: string;
    expectedArtifactIds: readonly string[];
  }): MergeArtifactSettlement {
    const duplicateId = this.settlementByIdempotencyKey.get(
      input.idempotencyKey,
    );
    if (duplicateId)
      return structuredClone(this.requireSettlement(duplicateId));
    if (this.activeSettlementByPlan.has(input.mergePlanId))
      throw new E03RuntimeError(
        "merge_artifact_settlement_active",
        `merge plan ${input.mergePlanId} already has artifact settlement`,
      );
    const settlementId =
      input.settlementId ?? createId("merge-artifact-settlement");
    const now = this.clock.now();
    const payload = {
      settlementId,
      mergePlanId: input.mergePlanId,
      taskId: input.taskId,
      targetRevision: input.targetRevision,
      idempotencyKey: input.idempotencyKey,
      expectedArtifactIds: [...new Set(input.expectedArtifactIds)].sort(),
      acceptedArtifactIds: [] as string[],
      rejectedArtifactIds: [] as string[],
      state: "collecting" as const,
      createdAt: now,
      updatedAt: now,
      committedAt: "",
      terminalReason: "",
      revision: 1,
    };
    const settlement = { ...payload, digest: digest(payload) };
    assertMergeArtifactSettlement(settlement);
    this.settlements.set(settlementId, settlement);
    this.receipts.set(settlementId, []);
    this.verifications.set(settlementId, []);
    this.settlementByIdempotencyKey.set(input.idempotencyKey, settlementId);
    this.activeSettlementByPlan.set(input.mergePlanId, settlementId);
    return structuredClone(settlement);
  }

  receive(input: {
    receiptId?: string;
    settlementId: string;
    expectedRevision: number;
    artifactId: string;
    logicalPath: string;
    contentDigest: string;
    byteLength: number;
    sourceRevision: string;
    targetRevision: string;
    workerId: string;
  }): MergeArtifactReceipt {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "collecting")
      throw new E03RuntimeError(
        "merge_artifact_receive_state",
        `merge artifact settlement ${settlement.settlementId} is ${settlement.state}`,
      );
    if (
      !settlement.expectedArtifactIds.includes(input.artifactId) ||
      input.targetRevision !== settlement.targetRevision
    )
      throw new E03RuntimeError(
        "merge_artifact_receipt_binding",
        `merge artifact ${input.artifactId} does not match settlement`,
      );
    const entries = this.receiptEntries(settlement.settlementId);
    const duplicate = entries.find(
      (value) => value.artifactId === input.artifactId,
    );
    if (duplicate) {
      if (
        duplicate.contentDigest !== input.contentDigest ||
        duplicate.logicalPath !== input.logicalPath
      )
        throw new E03RuntimeError(
          "merge_artifact_receipt_idempotency_conflict",
          `merge artifact ${input.artifactId} receipt conflicts`,
        );
      return structuredClone(duplicate);
    }
    const payload = {
      receiptId: input.receiptId ?? createId("merge-artifact-receipt"),
      settlementId: settlement.settlementId,
      artifactId: input.artifactId,
      logicalPath: input.logicalPath.replaceAll("\\", "/"),
      contentDigest: input.contentDigest,
      byteLength: input.byteLength,
      sourceRevision: input.sourceRevision,
      targetRevision: input.targetRevision,
      workerId: input.workerId,
      accepted: true,
      errorCode: "",
      receivedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertMergeArtifactReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(settlement.settlementId, entries);
    return structuredClone(receipt);
  }

  beginVerification(
    settlementId: string,
    expectedRevision: number,
  ): MergeArtifactSettlement {
    const settlement = this.requireSettlement(settlementId);
    this.assertSettlementRevision(settlement, expectedRevision);
    if (settlement.state !== "collecting")
      throw new E03RuntimeError(
        "merge_artifact_verify_state",
        `merge artifact settlement ${settlementId} is ${settlement.state}`,
      );
    const receipts = this.receiptEntries(settlementId);
    if (
      settlement.expectedArtifactIds.some(
        (artifactId) =>
          !receipts.some((value) => value.artifactId === artifactId),
      )
    )
      throw new E03RuntimeError(
        "merge_artifact_receipts_incomplete",
        `merge artifact settlement ${settlementId} lacks receipts`,
      );
    return this.transitionSettlement(settlement, { state: "verifying" });
  }

  verify(input: {
    verificationId?: string;
    settlementId: string;
    expectedRevision: number;
    artifactId: string;
    check: MergeArtifactVerification["check"];
    accepted: boolean;
    evidenceDigest: string;
    verifierId: string;
    errorCode?: string;
  }): MergeArtifactVerification {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "verifying")
      throw new E03RuntimeError(
        "merge_artifact_verification_state",
        `merge artifact settlement ${settlement.settlementId} is ${settlement.state}`,
      );
    if (
      !this.receiptEntries(settlement.settlementId).some(
        (value) => value.artifactId === input.artifactId,
      )
    )
      throw new E03RuntimeError(
        "merge_artifact_verification_receipt_missing",
        `merge artifact ${input.artifactId} has no receipt`,
      );
    const entries = this.verificationEntries(settlement.settlementId);
    if (
      entries.some(
        (value) =>
          value.artifactId === input.artifactId && value.check === input.check,
      )
    )
      throw new E03RuntimeError(
        "merge_artifact_verification_duplicate",
        `merge artifact ${input.artifactId} check ${input.check} exists`,
      );
    const payload = {
      verificationId:
        input.verificationId ?? createId("merge-artifact-verification"),
      settlementId: settlement.settlementId,
      artifactId: input.artifactId,
      check: input.check,
      accepted: input.accepted,
      evidenceDigest: input.evidenceDigest,
      verifierId: input.verifierId,
      errorCode: input.errorCode ?? "",
      verifiedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const verification = { ...payload, digest: digest(payload) };
    assertMergeArtifactVerification(verification);
    entries.push(verification);
    this.verifications.set(settlement.settlementId, entries);
    const requiredChecks: MergeArtifactVerification["check"][] = [
      "content",
      "path",
      "size",
      "provenance",
    ];
    const acceptedArtifactIds = settlement.expectedArtifactIds.filter(
      (artifactId) =>
        requiredChecks.every((check) =>
          entries.some(
            (value) =>
              value.artifactId === artifactId &&
              value.check === check &&
              value.accepted,
          ),
        ),
    );
    const rejectedArtifactIds = settlement.expectedArtifactIds.filter(
      (artifactId) =>
        entries.some(
          (value) => value.artifactId === artifactId && !value.accepted,
        ),
    );
    this.transitionSettlement(settlement, {
      acceptedArtifactIds,
      rejectedArtifactIds,
    });
    return structuredClone(verification);
  }

  commit(
    settlementId: string,
    expectedRevision: number,
  ): MergeArtifactSettlement {
    const settlement = this.requireSettlement(settlementId);
    this.assertSettlementRevision(settlement, expectedRevision);
    if (
      settlement.state !== "verifying" ||
      settlement.rejectedArtifactIds.length ||
      settlement.acceptedArtifactIds.length !==
        settlement.expectedArtifactIds.length
    )
      throw new E03RuntimeError(
        "merge_artifact_settlement_incomplete",
        `merge artifact settlement ${settlementId} is incomplete`,
      );
    const next = this.transitionSettlement(settlement, {
      state: "committed",
      committedAt: this.clock.now(),
      terminalReason: "all_artifacts_verified",
    });
    this.activeSettlementByPlan.delete(settlement.mergePlanId);
    return next;
  }

  reject(
    settlementId: string,
    expectedRevision: number,
    reason: string,
  ): MergeArtifactSettlement {
    const settlement = this.requireSettlement(settlementId);
    this.assertSettlementRevision(settlement, expectedRevision);
    if (!["collecting", "verifying"].includes(settlement.state))
      throw new E03RuntimeError(
        "merge_artifact_settlement_reject_state",
        `merge artifact settlement ${settlementId} is ${settlement.state}`,
      );
    const next = this.transitionSettlement(settlement, {
      state: "rejected",
      terminalReason: reason,
    });
    this.activeSettlementByPlan.delete(settlement.mergePlanId);
    return next;
  }

  snapshot(): MergeArtifactSettlementSnapshot {
    return {
      settlements: [...this.settlements.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      verifications: [...this.verifications.values()]
        .flat()
        .map((value) => structuredClone(value)),
      settlementByIdempotencyKey: [
        ...this.settlementByIdempotencyKey.entries(),
      ],
      activeSettlementByPlan: [...this.activeSettlementByPlan.entries()],
    };
  }

  restore(snapshot: MergeArtifactSettlementSnapshot): void {
    const settlements = new Map<string, MergeArtifactSettlement>();
    const receipts = new Map<string, MergeArtifactReceipt[]>();
    const verifications = new Map<string, MergeArtifactVerification[]>();
    for (const value of snapshot.settlements) {
      assertMergeArtifactSettlement(value);
      settlements.set(value.settlementId, structuredClone(value));
      receipts.set(value.settlementId, []);
      verifications.set(value.settlementId, []);
    }
    for (const value of snapshot.receipts) {
      assertMergeArtifactReceipt(value);
      const entries = receipts.get(value.settlementId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "merge_artifact_restore_receipt_chain",
          `receipt ${value.receiptId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.verifications) {
      assertMergeArtifactVerification(value);
      const entries = verifications.get(value.settlementId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "merge_artifact_restore_verification_chain",
          `verification ${value.verificationId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const settlementByIdempotencyKey = new Map(
      snapshot.settlementByIdempotencyKey,
    );
    const activeSettlementByPlan = new Map(snapshot.activeSettlementByPlan);
    if (
      settlementByIdempotencyKey.size !==
        snapshot.settlementByIdempotencyKey.length ||
      activeSettlementByPlan.size !== snapshot.activeSettlementByPlan.length
    )
      throw new E03RuntimeError(
        "merge_artifact_restore_index_duplicate",
        "merge artifact indexes duplicate",
      );
    for (const [key, settlementId] of settlementByIdempotencyKey) {
      const value = settlements.get(settlementId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "merge_artifact_restore_idempotency",
          `settlement index ${key} invalid`,
        );
    }
    for (const [planId, settlementId] of activeSettlementByPlan) {
      const value = settlements.get(settlementId);
      if (
        !value ||
        value.mergePlanId !== planId ||
        ["committed", "rejected", "rolled_back"].includes(value.state)
      )
        throw new E03RuntimeError(
          "merge_artifact_restore_active",
          `settlement index ${planId} invalid`,
        );
    }
    this.settlements = settlements;
    this.receipts = receipts;
    this.verifications = verifications;
    this.settlementByIdempotencyKey = settlementByIdempotencyKey;
    this.activeSettlementByPlan = activeSettlementByPlan;
  }

  private receiptEntries(settlementId: string): MergeArtifactReceipt[] {
    return this.receipts.get(settlementId) ?? [];
  }

  private verificationEntries(
    settlementId: string,
  ): MergeArtifactVerification[] {
    return this.verifications.get(settlementId) ?? [];
  }

  private requireSettlement(id: string): MergeArtifactSettlement {
    const value = this.settlements.get(id);
    if (!value)
      throw new E03RuntimeError(
        "merge_artifact_settlement_missing",
        `settlement ${id} missing`,
      );
    assertMergeArtifactSettlement(value);
    return value;
  }

  private assertSettlementRevision(
    value: MergeArtifactSettlement,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "merge_artifact_settlement_stale_revision",
        `settlement ${value.settlementId} stale`,
      );
  }

  private transitionSettlement(
    value: MergeArtifactSettlement,
    patch: Partial<
      Omit<MergeArtifactSettlement, "settlementId" | "revision" | "digest">
    >,
  ): MergeArtifactSettlement {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      settlementId: value.settlementId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeArtifactSettlement(next);
    this.settlements.set(next.settlementId, next);
    return structuredClone(next);
  }
}

export class WorktreeMergeVerificationRuntime {
  private policies = new Map<string, MergeVerificationPolicy>();
  private attestations = new Map<string, MergeAttestation>();
  private evidence = new Map<string, MergeVerificationEvidence[]>();
  private activeByPlan = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  registerPolicy(
    input: Omit<MergeVerificationPolicy, "policyId" | "revision" | "digest">,
  ): MergeVerificationPolicy {
    if (
      [...this.policies.values()].some(
        (value) => value.name === input.name && value.state !== "retired",
      )
    )
      throw new E03RuntimeError(
        "merge_verification_policy_duplicate",
        `merge verification policy ${input.name} already exists`,
      );
    const requiredChecks = [...new Set(input.requiredChecks)].sort();
    const optionalChecks = [...new Set(input.optionalChecks)].sort();
    if (requiredChecks.some((check) => optionalChecks.includes(check)))
      throw new E03RuntimeError(
        "merge_verification_policy_overlap",
        "merge verification checks overlap",
      );
    const payload = {
      ...structuredClone(input),
      policyId: createId("merge-verification-policy"),
      requiredChecks,
      optionalChecks,
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertMergeVerificationPolicy(policy);
    this.policies.set(policy.policyId, policy);
    return structuredClone(policy);
  }
  updatePolicy(
    policyId: string,
    expectedRevision: number,
    patch: Partial<
      Pick<
        MergeVerificationPolicy,
        | "requiredChecks"
        | "optionalChecks"
        | "minimumOptionalPasses"
        | "rejectOnConflict"
        | "requireCleanTarget"
        | "requireSourceAttestation"
        | "state"
      >
    >,
  ): MergeVerificationPolicy {
    const policy = this.requirePolicy(policyId);
    if (policy.revision !== expectedRevision)
      throw new E03RuntimeError(
        "merge_verification_policy_stale_revision",
        `merge verification policy ${policyId} revision is stale`,
      );
    if (policy.state === "retired")
      throw new E03RuntimeError(
        "merge_verification_policy_update_state",
        `merge verification policy ${policyId} is retired`,
      );
    const { digest: _, ...prior } = policy;
    const payload = {
      ...prior,
      ...structuredClone(patch),
      policyId: policy.policyId,
      requiredChecks: patch.requiredChecks
        ? [...new Set(patch.requiredChecks)].sort()
        : policy.requiredChecks,
      optionalChecks: patch.optionalChecks
        ? [...new Set(patch.optionalChecks)].sort()
        : policy.optionalChecks,
      revision: policy.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeVerificationPolicy(next);
    if (
      next.requiredChecks.some((check) => next.optionalChecks.includes(check))
    )
      throw new E03RuntimeError(
        "merge_verification_policy_overlap",
        "merge verification checks overlap",
      );
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }
  begin(input: {
    plan: WorktreeMergePlan;
    policyId: string;
  }): MergeAttestation {
    const policy = this.requirePolicy(input.policyId);
    if (policy.state !== "active")
      throw new E03RuntimeError(
        "merge_verification_policy_inactive",
        `merge verification policy ${policy.policyId} is ${policy.state}`,
      );
    const existingId = this.activeByPlan.get(input.plan.mergePlanId);
    if (existingId) return structuredClone(this.requireAttestation(existingId));
    const payload = {
      attestationId: createId("merge-attestation"),
      mergePlanId: input.plan.mergePlanId,
      taskId: input.plan.taskId,
      requestId: input.plan.requestId,
      policyId: policy.policyId,
      state: "collecting" as const,
      baseRevision: input.plan.expectedBaseRevision,
      sourceRevision: input.plan.sourceRevision,
      targetRevision: input.plan.targetRevision,
      resultingRevision: null,
      evidenceIds: [],
      requiredPassed: 0,
      requiredFailed: 0,
      optionalPassed: 0,
      warningCount: 0,
      openedAt: this.clock.now(),
      completedAt: null,
      rejectionReason: null,
      revision: 1,
    };
    const attestation = { ...payload, digest: digest(payload) };
    assertMergeAttestation(attestation);
    this.attestations.set(attestation.attestationId, attestation);
    this.activeByPlan.set(attestation.mergePlanId, attestation.attestationId);
    return structuredClone(attestation);
  }
  record(input: {
    attestationId: string;
    expectedRevision: number;
    check: string;
    outcome: MergeVerificationEvidence["outcome"];
    observedDigest: string;
    expectedDigest?: string | null;
    summary: string;
  }): { attestation: MergeAttestation; evidence: MergeVerificationEvidence } {
    const attestation = this.requireAttestation(input.attestationId);
    this.assertAttestationRevision(attestation, input.expectedRevision);
    if (attestation.state !== "collecting")
      throw new E03RuntimeError(
        "merge_verification_record_state",
        `merge attestation ${attestation.attestationId} is ${attestation.state}`,
      );
    const policy = this.requirePolicy(attestation.policyId);
    const required = policy.requiredChecks.includes(input.check);
    if (!required && !policy.optionalChecks.includes(input.check))
      throw new E03RuntimeError(
        "merge_verification_check_unknown",
        `merge verification check ${input.check} is not in policy`,
      );
    if (!input.observedDigest || !input.summary.trim())
      throw new E03RuntimeError(
        "merge_verification_evidence_input",
        "merge verification evidence input is invalid",
      );
    const entries = this.evidence.get(attestation.attestationId) ?? [];
    const prior = entries.find((value) => value.check === input.check);
    if (prior) {
      if (
        prior.outcome !== input.outcome ||
        prior.observedDigest !== input.observedDigest
      )
        throw new E03RuntimeError(
          "merge_verification_evidence_conflict",
          `merge verification check ${input.check} already has evidence`,
        );
      return {
        attestation: structuredClone(attestation),
        evidence: structuredClone(prior),
      };
    }
    const payload = {
      evidenceId: createId("merge-verification-evidence"),
      attestationId: attestation.attestationId,
      check: input.check,
      required,
      outcome: input.outcome,
      observedDigest: input.observedDigest,
      expectedDigest: input.expectedDigest ?? null,
      summary: input.summary.trim(),
      collectedAt: this.clock.now(),
    };
    const evidence = { ...payload, digest: digest(payload) };
    assertMergeVerificationEvidence(evidence);
    entries.push(evidence);
    this.evidence.set(attestation.attestationId, entries);
    const next = this.transitionAttestation(attestation, {
      evidenceIds: [...attestation.evidenceIds, evidence.evidenceId],
      requiredPassed:
        attestation.requiredPassed +
        (required && input.outcome === "passed" ? 1 : 0),
      requiredFailed:
        attestation.requiredFailed +
        (required && (input.outcome === "failed" || input.outcome === "skipped")
          ? 1
          : 0),
      optionalPassed:
        attestation.optionalPassed +
        (!required && input.outcome === "passed" ? 1 : 0),
      warningCount:
        attestation.warningCount + (input.outcome === "warning" ? 1 : 0),
    });
    return { attestation: next, evidence: structuredClone(evidence) };
  }
  finalize(input: {
    attestationId: string;
    expectedRevision: number;
    receipt: MergeReceipt;
    conflictedPaths?: readonly string[];
    targetWasClean?: boolean;
    sourceAttestationDigest?: string | null;
  }): MergeAttestation {
    const attestation = this.requireAttestation(input.attestationId);
    this.assertAttestationRevision(attestation, input.expectedRevision);
    if (attestation.state !== "collecting")
      throw new E03RuntimeError(
        "merge_verification_finalize_state",
        `merge attestation ${attestation.attestationId} is ${attestation.state}`,
      );
    if (
      input.receipt.taskId !== attestation.taskId ||
      input.receipt.requestId !== attestation.requestId ||
      input.receipt.expectedBaseRevision !== attestation.baseRevision ||
      input.receipt.sourceRevision !== attestation.sourceRevision ||
      input.receipt.targetRevision !== attestation.targetRevision
    )
      throw new E03RuntimeError(
        "merge_verification_receipt_custody",
        "merge receipt does not match attestation",
      );
    const policy = this.requirePolicy(attestation.policyId);
    const entries = this.evidence.get(attestation.attestationId) ?? [];
    const missing = policy.requiredChecks.filter(
      (check) => !entries.some((value) => value.check === check),
    );
    const reasons: string[] = [];
    if (missing.length)
      reasons.push(`missing required checks: ${missing.join(",")}`);
    if (attestation.requiredFailed)
      reasons.push(`${attestation.requiredFailed} required checks failed`);
    if (attestation.optionalPassed < policy.minimumOptionalPasses)
      reasons.push("optional verification threshold not met");
    if (!input.receipt.accepted || !input.receipt.resultingRevision)
      reasons.push("merge receipt was not accepted");
    if (
      policy.rejectOnConflict &&
      (input.conflictedPaths?.length || input.receipt.conflictedPaths.length)
    )
      reasons.push("merge conflicts rejected by policy");
    if (policy.requireCleanTarget && !input.targetWasClean)
      reasons.push("target was not clean");
    if (policy.requireSourceAttestation && !input.sourceAttestationDigest)
      reasons.push("source attestation is missing");
    const rejected = reasons.length > 0;
    const next = this.transitionAttestation(attestation, {
      state: rejected ? "rejected" : "verified",
      resultingRevision: input.receipt.resultingRevision || null,
      completedAt: this.clock.now(),
      rejectionReason: rejected ? reasons.join("; ") : null,
    });
    this.activeByPlan.delete(next.mergePlanId);
    return next;
  }
  revoke(
    attestationId: string,
    expectedRevision: number,
    reason: string,
  ): MergeAttestation {
    const attestation = this.requireAttestation(attestationId);
    this.assertAttestationRevision(attestation, expectedRevision);
    if (attestation.state === "revoked") return structuredClone(attestation);
    if (!reason.trim())
      throw new E03RuntimeError(
        "merge_attestation_revoke_reason",
        "merge attestation revoke reason is required",
      );
    const next = this.transitionAttestation(attestation, {
      state: "revoked",
      completedAt: this.clock.now(),
      rejectionReason: reason.trim(),
    });
    this.activeByPlan.delete(next.mergePlanId);
    return next;
  }
  snapshot(): {
    policies: MergeVerificationPolicy[];
    attestations: MergeAttestation[];
    evidence: MergeVerificationEvidence[];
    activeByPlan: Array<[string, string]>;
  } {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      attestations: [...this.attestations.values()].map((value) =>
        structuredClone(value),
      ),
      evidence: [...this.evidence.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeByPlan: [...this.activeByPlan.entries()].map(
        ([planId, attestationId]) => [planId, attestationId],
      ),
    };
  }
  restore(snapshot: {
    policies: readonly MergeVerificationPolicy[];
    attestations: readonly MergeAttestation[];
    evidence: readonly MergeVerificationEvidence[];
    activeByPlan: ReadonlyArray<readonly [string, string]>;
  }): void {
    const policies = new Map<string, MergeVerificationPolicy>();
    const attestations = new Map<string, MergeAttestation>();
    const evidence = new Map<string, MergeVerificationEvidence[]>();
    const activeByPlan = new Map<string, string>();
    for (const value of snapshot.policies) {
      assertMergeVerificationPolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "merge_verification_policy_restore_duplicate",
          `duplicate merge verification policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    for (const value of snapshot.attestations) {
      assertMergeAttestation(value);
      if (
        attestations.has(value.attestationId) ||
        !policies.has(value.policyId)
      )
        throw new E03RuntimeError(
          "merge_attestation_restore",
          `merge attestation ${value.attestationId} is invalid`,
        );
      attestations.set(value.attestationId, structuredClone(value));
    }
    for (const value of snapshot.evidence) {
      assertMergeVerificationEvidence(value);
      if (!attestations.has(value.attestationId))
        throw new E03RuntimeError(
          "merge_verification_evidence_restore",
          `merge verification evidence ${value.evidenceId} has no attestation`,
        );
      const entries = evidence.get(value.attestationId) ?? [];
      if (
        entries.some(
          (entry) =>
            entry.check === value.check ||
            entry.evidenceId === value.evidenceId,
        )
      )
        throw new E03RuntimeError(
          "merge_verification_evidence_restore_duplicate",
          `duplicate merge verification evidence ${value.evidenceId}`,
        );
      entries.push(structuredClone(value));
      evidence.set(value.attestationId, entries);
    }
    for (const [planId, attestationId] of snapshot.activeByPlan) {
      const attestation = attestations.get(attestationId);
      if (
        !attestation ||
        attestation.mergePlanId !== planId ||
        attestation.state !== "collecting" ||
        activeByPlan.has(planId)
      )
        throw new E03RuntimeError(
          "merge_attestation_active_restore",
          `merge attestation active index ${planId} is invalid`,
        );
      activeByPlan.set(planId, attestationId);
    }
    this.policies = policies;
    this.attestations = attestations;
    this.evidence = evidence;
    this.activeByPlan = activeByPlan;
  }
  private requirePolicy(id: string): MergeVerificationPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "merge_verification_policy_missing",
        `merge verification policy ${id} does not exist`,
      );
    assertMergeVerificationPolicy(value);
    return value;
  }
  private requireAttestation(id: string): MergeAttestation {
    const value = this.attestations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "merge_attestation_missing",
        `merge attestation ${id} does not exist`,
      );
    assertMergeAttestation(value);
    return value;
  }
  private assertAttestationRevision(
    value: MergeAttestation,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "merge_attestation_stale_revision",
        `merge attestation ${value.attestationId} revision is stale`,
      );
  }
  private transitionAttestation(
    value: MergeAttestation,
    patch: Partial<
      Omit<MergeAttestation, "attestationId" | "revision" | "digest">
    >,
  ): MergeAttestation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      attestationId: value.attestationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMergeAttestation(next);
    this.attestations.set(next.attestationId, next);
    return structuredClone(next);
  }
}

export type CleanupSettlementState =
  | "planned"
  | "collecting"
  | "verified"
  | "incomplete"
  | "released";

export interface CleanupResourceClaim {
  claimId: string;
  cleanupId: string;
  taskId: string;
  requestId: string;
  resourceKind: "worktree" | "process" | "mount" | "credential" | "artifact";
  resourceId: string;
  expectedPresent: boolean;
  observedPresent: boolean | null;
  observationDigest: string;
  observedAt: string;
  revision: number;
  digest: string;
}

export interface CleanupSettlement {
  settlementId: string;
  cleanupId: string;
  taskId: string;
  requestId: string;
  leaseId: string;
  state: CleanupSettlementState;
  requiredClaimIds: string[];
  satisfiedClaimIds: string[];
  retainedArtifactIds: string[];
  blockingClaimIds: string[];
  cleanupReceiptDigest: string;
  releaseFence: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface CleanupSettlementEvidence {
  evidenceId: string;
  settlementId: string;
  claimId: string;
  observerId: string;
  observedPresent: boolean;
  observationDigest: string;
  priorClaimDigest: string;
  recordedAt: string;
  revision: number;
  digest: string;
}

export interface CleanupSettlementSnapshot {
  settlements: CleanupSettlement[];
  claims: CleanupResourceClaim[];
  evidence: CleanupSettlementEvidence[];
  digest: string;
}

function assertCleanupResourceClaim(value: CleanupResourceClaim): void {
  if (
    !value.claimId ||
    !value.cleanupId ||
    !value.taskId ||
    !value.requestId ||
    !value.resourceId
  )
    throw new E03RuntimeError(
      "cleanup_resource_claim_invalid",
      "cleanup resource claim requires custody identity",
    );
  if (!Number.isInteger(value.revision) || value.revision < 1)
    throw new E03RuntimeError(
      "cleanup_resource_claim_revision_invalid",
      "cleanup resource claim revision must be positive",
    );
  if (value.observedPresent !== null && !value.observationDigest)
    throw new E03RuntimeError(
      "cleanup_resource_observation_digest_missing",
      "observed cleanup resource requires evidence digest",
    );
}

function assertCleanupSettlement(value: CleanupSettlement): void {
  if (
    !value.settlementId ||
    !value.cleanupId ||
    !value.taskId ||
    !value.requestId ||
    !value.leaseId
  )
    throw new E03RuntimeError(
      "cleanup_settlement_invalid",
      "cleanup settlement requires task and lease custody",
    );
  if (new Set(value.requiredClaimIds).size !== value.requiredClaimIds.length)
    throw new E03RuntimeError(
      "cleanup_settlement_claim_duplicate",
      "cleanup settlement claims must be unique",
    );
  if (
    value.satisfiedClaimIds.some(
      (claimId) => !value.requiredClaimIds.includes(claimId),
    ) ||
    value.blockingClaimIds.some(
      (claimId) => !value.requiredClaimIds.includes(claimId),
    )
  )
    throw new E03RuntimeError(
      "cleanup_settlement_claim_outside_plan",
      "cleanup settlement references claim outside plan",
    );
  if (value.state === "released" && !value.releaseFence)
    throw new E03RuntimeError(
      "cleanup_settlement_release_fence_missing",
      "released cleanup settlement requires fence",
    );
}

function assertCleanupSettlementEvidence(
  value: CleanupSettlementEvidence,
): void {
  if (
    !value.evidenceId ||
    !value.settlementId ||
    !value.claimId ||
    !value.observerId ||
    !value.observationDigest ||
    !value.priorClaimDigest
  )
    throw new E03RuntimeError(
      "cleanup_settlement_evidence_invalid",
      "cleanup settlement evidence requires observer and digest chain",
    );
}

export class WorktreeCleanupSettlementRuntime {
  private settlements = new Map<string, CleanupSettlement>();
  private claims = new Map<string, CleanupResourceClaim>();
  private evidence = new Map<string, CleanupSettlementEvidence>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  plan(input: {
    cleanup: CleanupReceipt;
    resources: Array<{
      resourceKind: CleanupResourceClaim["resourceKind"];
      resourceId: string;
      expectedPresent?: boolean;
    }>;
  }): CleanupSettlement {
    if (!input.cleanup.cleanupId || !input.cleanup.digest)
      throw new E03RuntimeError(
        "cleanup_settlement_receipt_invalid",
        "cleanup settlement requires a sealed cleanup receipt",
      );
    const existing = [...this.settlements.values()].find(
      (value) => value.cleanupId === input.cleanup.cleanupId,
    );
    if (existing) {
      if (existing.cleanupReceiptDigest !== input.cleanup.digest)
        throw new E03RuntimeError(
          "cleanup_settlement_receipt_conflict",
          "cleanup settlement already exists for another receipt digest",
        );
      return structuredClone(existing);
    }
    const uniqueResources = new Set<string>();
    const claims = input.resources.map((resource) => {
      const resourceKey = `${resource.resourceKind}:${resource.resourceId}`;
      if (!resource.resourceId || uniqueResources.has(resourceKey))
        throw new E03RuntimeError(
          "cleanup_settlement_resource_duplicate",
          `cleanup settlement resource ${resourceKey} is invalid or duplicate`,
        );
      uniqueResources.add(resourceKey);
      const claimPayload = {
        claimId: createId("cleanup-resource-claim"),
        cleanupId: input.cleanup.cleanupId,
        taskId: input.cleanup.taskId,
        requestId: input.cleanup.requestId,
        resourceKind: resource.resourceKind,
        resourceId: resource.resourceId,
        expectedPresent: resource.expectedPresent ?? false,
        observedPresent: null,
        observationDigest: "",
        observedAt: "",
        revision: 1,
      };
      const claim = { ...claimPayload, digest: digest(claimPayload) };
      assertCleanupResourceClaim(claim);
      this.claims.set(claim.claimId, claim);
      return claim;
    });
    const now = this.clock.now();
    const payload = {
      settlementId: createId("cleanup-settlement"),
      cleanupId: input.cleanup.cleanupId,
      taskId: input.cleanup.taskId,
      requestId: input.cleanup.requestId,
      leaseId: input.cleanup.leaseId,
      state: "planned" as const,
      requiredClaimIds: claims.map((value) => value.claimId),
      satisfiedClaimIds: [] as string[],
      retainedArtifactIds: [...input.cleanup.retainedArtifacts].sort(),
      blockingClaimIds: [] as string[],
      cleanupReceiptDigest: input.cleanup.digest,
      releaseFence: "",
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const settlement = { ...payload, digest: digest(payload) };
    assertCleanupSettlement(settlement);
    this.settlements.set(settlement.settlementId, settlement);
    return structuredClone(settlement);
  }

  begin(input: {
    settlementId: string;
    expectedRevision: number;
    cleanupReceiptDigest: string;
  }): CleanupSettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.cleanupReceiptDigest !== input.cleanupReceiptDigest)
      throw new E03RuntimeError(
        "cleanup_settlement_receipt_digest_mismatch",
        "cleanup settlement receipt digest changed",
      );
    if (settlement.state === "released") return structuredClone(settlement);
    if (settlement.state !== "planned" && settlement.state !== "incomplete")
      throw new E03RuntimeError(
        "cleanup_settlement_begin_invalid_state",
        `cannot begin cleanup settlement from ${settlement.state}`,
      );
    return this.transitionSettlement(settlement, {
      state: "collecting",
      updatedAt: this.clock.now(),
      blockingClaimIds: [],
    });
  }

  observe(input: {
    settlementId: string;
    claimId: string;
    expectedSettlementRevision: number;
    expectedClaimRevision: number;
    observerId: string;
    observedPresent: boolean;
    observationDigest: string;
  }): CleanupSettlementEvidence {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedSettlementRevision);
    if (settlement.state !== "collecting")
      throw new E03RuntimeError(
        "cleanup_settlement_not_collecting",
        "cleanup evidence may only be recorded while collecting",
      );
    const claim = this.requireClaim(input.claimId);
    this.assertClaimRevision(claim, input.expectedClaimRevision);
    if (!settlement.requiredClaimIds.includes(claim.claimId))
      throw new E03RuntimeError(
        "cleanup_settlement_claim_mismatch",
        "cleanup claim does not belong to settlement",
      );
    if (!input.observerId || !input.observationDigest)
      throw new E03RuntimeError(
        "cleanup_settlement_observation_invalid",
        "cleanup observation requires observer and digest",
      );
    const nextClaim = this.transitionClaim(claim, {
      observedPresent: input.observedPresent,
      observationDigest: input.observationDigest,
      observedAt: this.clock.now(),
    });
    const evidencePayload = {
      evidenceId: createId("cleanup-settlement-evidence"),
      settlementId: settlement.settlementId,
      claimId: claim.claimId,
      observerId: input.observerId,
      observedPresent: input.observedPresent,
      observationDigest: input.observationDigest,
      priorClaimDigest: claim.digest,
      recordedAt: this.clock.now(),
      revision: 1,
    };
    const evidence = {
      ...evidencePayload,
      digest: digest({ ...evidencePayload, nextClaimDigest: nextClaim.digest }),
    };
    assertCleanupSettlementEvidence(evidence);
    this.evidence.set(evidence.evidenceId, evidence);
    return structuredClone(evidence);
  }

  reconcile(input: {
    settlementId: string;
    expectedRevision: number;
  }): CleanupSettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "collecting")
      throw new E03RuntimeError(
        "cleanup_settlement_reconcile_invalid_state",
        "cleanup settlement must be collecting before reconciliation",
      );
    const claims = settlement.requiredClaimIds.map((claimId) =>
      this.requireClaim(claimId),
    );
    const unobserved = claims.filter((claim) => claim.observedPresent === null);
    if (unobserved.length > 0)
      throw new E03RuntimeError(
        "cleanup_settlement_observation_incomplete",
        `cleanup settlement is missing ${unobserved.length} observations`,
      );
    const blocking = claims.filter(
      (claim) => claim.observedPresent !== claim.expectedPresent,
    );
    const satisfied = claims.filter(
      (claim) => claim.observedPresent === claim.expectedPresent,
    );
    return this.transitionSettlement(settlement, {
      state: blocking.length === 0 ? "verified" : "incomplete",
      satisfiedClaimIds: satisfied.map((value) => value.claimId),
      blockingClaimIds: blocking.map((value) => value.claimId),
      updatedAt: this.clock.now(),
    });
  }

  release(input: {
    settlementId: string;
    expectedRevision: number;
    leaseId: string;
    releaseFence: string;
  }): CleanupSettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state === "released") {
      if (settlement.releaseFence !== input.releaseFence)
        throw new E03RuntimeError(
          "cleanup_settlement_release_conflict",
          "cleanup settlement was released by another fence",
        );
      return structuredClone(settlement);
    }
    if (settlement.state !== "verified")
      throw new E03RuntimeError(
        "cleanup_settlement_not_verified",
        "cleanup settlement must be verified before release",
      );
    if (settlement.leaseId !== input.leaseId || !input.releaseFence)
      throw new E03RuntimeError(
        "cleanup_settlement_release_fence_invalid",
        "cleanup settlement release lease or fence is invalid",
      );
    return this.transitionSettlement(settlement, {
      state: "released",
      releaseFence: input.releaseFence,
      updatedAt: this.clock.now(),
    });
  }

  reopen(input: {
    settlementId: string;
    expectedRevision: number;
    resolvedClaimIds: string[];
  }): CleanupSettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "incomplete")
      throw new E03RuntimeError(
        "cleanup_settlement_reopen_invalid_state",
        "only incomplete cleanup settlement may reopen",
      );
    for (const claimId of input.resolvedClaimIds) {
      if (!settlement.blockingClaimIds.includes(claimId))
        throw new E03RuntimeError(
          "cleanup_settlement_resolution_mismatch",
          `cleanup claim ${claimId} is not blocking`,
        );
      const claim = this.requireClaim(claimId);
      this.transitionClaim(claim, {
        observedPresent: null,
        observationDigest: "",
        observedAt: "",
      });
    }
    return this.transitionSettlement(settlement, {
      state: "collecting",
      blockingClaimIds: settlement.blockingClaimIds.filter(
        (claimId) => !input.resolvedClaimIds.includes(claimId),
      ),
      satisfiedClaimIds: settlement.satisfiedClaimIds.filter(
        (claimId) => !input.resolvedClaimIds.includes(claimId),
      ),
      updatedAt: this.clock.now(),
    });
  }

  snapshot(): CleanupSettlementSnapshot {
    const payload = {
      settlements: [...this.settlements.values()].map((value) =>
        structuredClone(value),
      ),
      claims: [...this.claims.values()].map((value) => structuredClone(value)),
      evidence: [...this.evidence.values()].map((value) =>
        structuredClone(value),
      ),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: CleanupSettlementSnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "cleanup_settlement_snapshot_corrupt",
        "cleanup settlement snapshot digest mismatch",
      );
    const settlements = new Map<string, CleanupSettlement>();
    const claims = new Map<string, CleanupResourceClaim>();
    const evidence = new Map<string, CleanupSettlementEvidence>();
    for (const value of payload.claims) {
      assertCleanupResourceClaim(value);
      if (claims.has(value.claimId))
        throw new E03RuntimeError(
          "cleanup_settlement_snapshot_claim_duplicate",
          `duplicate cleanup claim ${value.claimId}`,
        );
      claims.set(value.claimId, structuredClone(value));
    }
    for (const value of payload.settlements) {
      assertCleanupSettlement(value);
      if (value.requiredClaimIds.some((claimId) => !claims.has(claimId)))
        throw new E03RuntimeError(
          "cleanup_settlement_snapshot_claim_missing",
          `cleanup settlement ${value.settlementId} references missing claim`,
        );
      settlements.set(value.settlementId, structuredClone(value));
    }
    for (const value of payload.evidence) {
      assertCleanupSettlementEvidence(value);
      if (!settlements.has(value.settlementId) || !claims.has(value.claimId))
        throw new E03RuntimeError(
          "cleanup_settlement_snapshot_evidence_orphaned",
          `cleanup evidence ${value.evidenceId} is orphaned`,
        );
      evidence.set(value.evidenceId, structuredClone(value));
    }
    this.settlements = settlements;
    this.claims = claims;
    this.evidence = evidence;
  }

  private requireSettlement(id: string): CleanupSettlement {
    const value = this.settlements.get(id);
    if (!value)
      throw new E03RuntimeError(
        "cleanup_settlement_missing",
        `cleanup settlement ${id} does not exist`,
      );
    assertCleanupSettlement(value);
    return value;
  }

  private requireClaim(id: string): CleanupResourceClaim {
    const value = this.claims.get(id);
    if (!value)
      throw new E03RuntimeError(
        "cleanup_resource_claim_missing",
        `cleanup resource claim ${id} does not exist`,
      );
    assertCleanupResourceClaim(value);
    return value;
  }

  private assertSettlementRevision(
    value: CleanupSettlement,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "cleanup_settlement_stale_revision",
        `cleanup settlement ${value.settlementId} revision is stale`,
      );
  }

  private assertClaimRevision(
    value: CleanupResourceClaim,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "cleanup_resource_claim_stale_revision",
        `cleanup resource claim ${value.claimId} revision is stale`,
      );
  }

  private transitionSettlement(
    value: CleanupSettlement,
    patch: Partial<
      Omit<CleanupSettlement, "settlementId" | "revision" | "digest">
    >,
  ): CleanupSettlement {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      settlementId: value.settlementId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCleanupSettlement(next);
    this.settlements.set(next.settlementId, next);
    return structuredClone(next);
  }

  private transitionClaim(
    value: CleanupResourceClaim,
    patch: Partial<
      Omit<CleanupResourceClaim, "claimId" | "revision" | "digest">
    >,
  ): CleanupResourceClaim {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      claimId: value.claimId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCleanupResourceClaim(next);
    this.claims.set(next.claimId, next);
    return structuredClone(next);
  }
}
