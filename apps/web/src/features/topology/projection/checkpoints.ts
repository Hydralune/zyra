import type { JsonObject, JsonValue } from "../../../events/ingress/index.ts"
import type {
  BranchConflictView,
  BranchOutcome,
  BranchView,
  CheckpointView,
  CheckpointWriteView,
  ProjectionRecord,
  TopologyProjectionContext,
} from "./contracts.ts"
import {
  emptyEvidence,
  evidenceForEntity,
  evidenceWithGraph,
  mergeEvidence,
} from "./evidence.ts"
import {
  compactAttributes,
  findRecords,
  firstBoolean,
  firstInteger,
  firstString,
  firstStringArray,
  hashKey,
  isJsonObject,
  namedRecords,
  normalizeEntityState,
  normalizeToken,
  objectForAliases,
  recordCandidates,
  recordLooksLike,
  stableKey,
  uniqueStrings,
  valueForAliases,
} from "./record-reader.ts"

const CHECKPOINT_NAMES = [
  "checkpoint",
  "recovery_checkpoint",
  "workflow_checkpoint",
  "checkpoint_payload",
  "resume_checkpoint",
]

const BRANCH_NAMES = [
  "branch",
  "branch_delta",
  "branchDelta",
  "delta",
  "commit_result",
  "commitResult",
  "branch_result",
  "branchResult",
  "merge_result",
  "mergeResult",
]

const CONFLICT_NAMES = [
  "conflict",
  "conflicts",
  "delta_conflict",
  "commit_conflict",
  "branch_conflict",
]

interface CheckpointSource {
  record: ProjectionRecord
  source: JsonObject
  checkpointId: string
  graphId?: string
  graphRevision: number
  commitRevision: number
}

interface BranchSource {
  record: ProjectionRecord
  source: JsonObject
  branchId: string
  graphId?: string
  graphRevision: number
  commitRevision: number
}

export function projectCheckpoints(context: TopologyProjectionContext): {
  checkpoints: CheckpointView[]
  branches: BranchView[]
  conflicts: BranchConflictView[]
} {
  const checkpointSources: CheckpointSource[] = []
  const branchSources: BranchSource[] = []
  for (const record of context.records) {
    checkpointSources.push(...checkpointSourcesForRecord(record))
    branchSources.push(...branchSourcesForRecord(record))
  }
  checkpointSources.sort(compareCheckpointSource)
  branchSources.sort(compareBranchSource)
  const checkpoints = mergeCheckpoints(
    checkpointSources.map((source) => checkpointView(source, context)),
  )
  const branches = mergeBranches(
    branchSources.map((source) => branchView(source, context)),
  )
  const checkpointIds = new Set(checkpoints.map((checkpoint) => checkpoint.checkpointId))
  validateCheckpointLineage(checkpoints, checkpointIds)
  normalizeBranchVisibility(branches)
  const conflicts = branches
    .flatMap((branch) => branch.conflicts)
    .sort(compareConflict)
  return {
    checkpoints: checkpoints.sort(compareCheckpoint),
    branches: branches.sort(compareBranch),
    conflicts,
  }
}

function checkpointSourcesForRecord(record: ProjectionRecord): CheckpointSource[] {
  const output: CheckpointSource[] = []
  for (const source of namedRecords(record, CHECKPOINT_NAMES)) {
    const candidate = checkpointSource(record, source)
    if (candidate) output.push(candidate)
  }
  if (
    ["recovery", "session", "task"].includes(record.domain) &&
    hasCheckpointSignal(record.attributes)
  ) {
    const candidate = checkpointSource(record, record.attributes)
    if (candidate) output.push(candidate)
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) =>
      recordLooksLike(candidate, [
        "checkpoint_id",
        "pending_writes",
        "committed_writes",
        "pending_request_ids",
        "processed_response_ids",
        "next_task_ids",
      ]) &&
      path.some((part) => /checkpoint|recovery|resume|interrupt|write/i.test(part)),
    4_000,
  )
  for (const source of nested) {
    const candidate = checkpointSource(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeCheckpointSources(output)
}

function checkpointSource(
  record: ProjectionRecord,
  source: JsonObject,
): CheckpointSource | undefined {
  const all = [source, ...recordCandidates(record)]
  const checkpointId =
    firstString(all, "checkpoint_id", "checkpointId", "id") ??
    record.checkpointId
  if (!checkpointId) return undefined
  return {
    record,
    source,
    checkpointId,
    graphId: firstString(all, "graph_id", "graphId"),
    graphRevision:
      firstInteger(
        all,
        "graph_revision",
        "topology_revision",
        "committed_revision",
      ) ?? 0,
    commitRevision:
      firstInteger(all, "commit_revision", "committed_revision", "revision") ??
      record.revision,
  }
}

function checkpointView(
  source: CheckpointSource,
  context: TopologyProjectionContext,
): CheckpointView {
  const { record } = source
  const all = [source.source, ...recordCandidates(record)]
  const pendingWrites = checkpointWrites(source, context, "pending")
  const committedWrites = checkpointWrites(source, context, "committed")
  const parentCheckpointId = firstString(
    all,
    "parent_checkpoint_id",
    "parent_id",
    "previous_checkpoint_id",
  )
  const ancestry = uniqueStrings([
    ...firstStringArray(
      all,
      "ancestry",
      "checkpoint_ancestry",
      "parent_checkpoint_ids",
    ),
    parentCheckpointId,
  ])
  const state = normalizeEntityState(
    firstString(all, "phase", "status", "state"),
    record.lifecycle,
  )
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "checkpoint",
      source.checkpointId,
      source.graphId,
      source.graphRevision,
    ),
    source.graphId,
    source.graphRevision,
    [
      `checkpoint:${source.checkpointId}`,
      parentCheckpointId ? `checkpoint:${parentCheckpointId}` : "",
    ],
  )
  const interruptIds = uniqueStrings([
    ...firstStringArray(all, "interrupt_ids", "interrupts"),
    firstString(all, "interrupt_id"),
  ])
  const resumeIds = uniqueStrings([
    ...firstStringArray(all, "resume_ids", "resumes"),
    firstString(all, "resume_id"),
  ])
  return {
    id: source.checkpointId,
    checkpointId: source.checkpointId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "checkpoint",
    sequence: record.sequence,
    revision:
      firstInteger(all, "revision", "checkpoint_revision", "version") ??
      record.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    state,
    title:
      firstString(all, "title", "name") ??
      `Checkpoint ${source.checkpointId}`,
    summary:
      firstString(all, "summary", "reason", "description") ?? record.summary,
    namespace:
      firstString(all, "checkpoint_ns", "namespace") ?? "checkpoint",
    subgraphId: firstString(all, "subgraph_id", "graph_id"),
    branchId: firstString(all, "branch_id"),
    effective: record.effective,
    terminal:
      record.terminal ||
      ["completed", "failed", "cancelled", "rejected"].includes(state),
    pending:
      pendingWrites.length > 0 ||
      ["planned", "waiting", "interrupted"].includes(state),
    removed: record.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes([source.source], context.sensitiveFieldDrops),
    ),
    parentCheckpointId,
    ancestry: Object.freeze(ancestry),
    phase: firstString(all, "phase", "workflow_phase") ?? state,
    iteration: firstInteger(all, "iteration", "step", "epoch") ?? 0,
    workflowSignature: firstString(
      all,
      "workflow_signature",
      "workflow_digest",
    ),
    graphSignature: firstString(all, "graph_signature", "graph_digest"),
    topologySignature: firstString(
      all,
      "topology_signature",
      "topology_digest",
    ),
    contentDigest: firstString(
      all,
      "content_digest",
      "checkpoint_digest",
      "digest",
    ),
    pendingWrites: Object.freeze(pendingWrites),
    committedWrites: Object.freeze(committedWrites),
    pendingRequestIds: Object.freeze(
      firstStringArray(
        all,
        "pending_request_ids",
        "pending_requests",
        "pending_request_id",
      ),
    ),
    inflightMessageIds: Object.freeze(
      firstStringArray(
        all,
        "inflight_message_ids",
        "in_flight_message_ids",
        "inflight_messages",
      ),
    ),
    completedStepIds: Object.freeze(
      firstStringArray(all, "completed_step_ids", "completed_steps"),
    ),
    processedResponseIds: Object.freeze(
      firstStringArray(
        all,
        "processed_response_ids",
        "processed_responses",
      ),
    ),
    sideEffectFenceKeys: Object.freeze(
      firstStringArray(
        all,
        "side_effect_fence_keys",
        "side_effect_fences",
        "idempotency_keys",
      ),
    ),
    interruptIds: Object.freeze(interruptIds),
    resumeIds: Object.freeze(resumeIds),
    nextTaskIds: Object.freeze(
      firstStringArray(all, "next_task_ids", "next_tasks", "next"),
    ),
    lineageValid: parentCheckpointId === undefined || ancestry.includes(parentCheckpointId),
    visibilityValid: pendingWrites.every(
      (write) =>
        !committedWrites.some(
          (committed) =>
            committed.taskKey === write.taskKey &&
            committed.channel === write.channel &&
            committed.sequence < write.sequence,
        ),
    ),
  }
}

function checkpointWrites(
  source: CheckpointSource,
  context: TopologyProjectionContext,
  state: CheckpointWriteView["state"],
): CheckpointWriteView[] {
  const aliases =
    state === "pending"
      ? ["pending_writes", "pendingWrites", "writes_pending"]
      : ["committed_writes", "committedWrites", "writes_committed"]
  const values: JsonValue[] = []
  for (const candidate of [source.source, ...recordCandidates(source.record)]) {
    const value = valueForAliases(candidate, aliases)
    if (Array.isArray(value)) values.push(...value)
    else if (isJsonObject(value)) {
      for (const [key, nested] of Object.entries(value)) {
        if (isJsonObject(nested)) values.push({ task_key: key, ...nested })
        else values.push({ task_key: key, value: nested })
      }
    }
  }
  const output: CheckpointWriteView[] = []
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    const write = isJsonObject(value) ? value : { value }
    const taskKey =
      firstString([write], "task_key", "task_id", "key") ??
      `${source.record.taskId}:${index}`
    const channel =
      firstString([write], "channel", "channel_name", "field") ?? "state"
    const sequence =
      firstInteger([write], "sequence", "write_sequence", "index") ??
      source.record.sequence
    const id =
      firstString([write], "id", "write_id") ??
      `${source.checkpointId}:${state}:${taskKey}:${channel}:${sequence}`
    output.push({
      id,
      checkpointId: source.checkpointId,
      taskKey,
      channel,
      state:
        normalizeWriteState(firstString([write], "state", "status")) ?? state,
      sequence,
      writerId: firstString([write], "writer_id", "writer", "node_id"),
      idempotencyKey: firstString(
        [write],
        "idempotency_key",
        "side_effect_fence_key",
      ),
      valueDigest:
        firstString([write], "value_digest", "digest", "hash") ??
        hashKey(value),
      branchId: firstString([write], "branch_id"),
      evidence: evidenceForEntity(
        context.evidence,
        source.record,
        "checkpoint_write",
        id,
        source.graphId,
        source.graphRevision,
      ),
    })
  }
  return dedupeWrites(output)
}

function normalizeWriteState(
  value: string | undefined,
): CheckpointWriteView["state"] | undefined {
  const token = normalizeToken(value)
  if (["pending", "staged", "uncommitted"].includes(token)) return "pending"
  if (["committed", "visible", "applied"].includes(token)) return "committed"
  if (["discarded", "rejected", "rolled_back"].includes(token)) return "discarded"
  if (token) return "unknown"
  return undefined
}

function branchSourcesForRecord(record: ProjectionRecord): BranchSource[] {
  const output: BranchSource[] = []
  for (const source of namedRecords(record, BRANCH_NAMES)) {
    const candidate = branchSource(record, source)
    if (candidate) output.push(candidate)
  }
  if (
    ["recovery", "scheduler", "task"].includes(record.domain) &&
    hasBranchSignal(record.attributes)
  ) {
    const candidate = branchSource(record, record.attributes)
    if (candidate) output.push(candidate)
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) =>
      recordLooksLike(candidate, [
        "branch_id",
        "branchId",
        "delta_id",
        "deltaId",
        "base_revision",
        "baseRevision",
        "read_set",
        "readSet",
        "write_set",
        "writeSet",
        "conflicts",
        "rebased_from_revision",
        "rebasedFromRevision",
      ]) &&
      path.some((part) => /branch|delta|commit|conflict|rebase|merge/i.test(part)),
    4_000,
  )
  for (const source of nested) {
    const candidate = branchSource(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeBranchSources(output)
}

function branchSource(
  record: ProjectionRecord,
  source: JsonObject,
): BranchSource | undefined {
  const all = [source, ...recordCandidates(record)]
  const branchId = firstString(
    all,
    "branch_id",
    "branchId",
    "branch",
    "owner_id",
  )
  const deltaId = firstString(all, "delta_id", "deltaId")
  if (!branchId && !deltaId) return undefined
  return {
    record,
    source,
    branchId: branchId ?? `delta:${deltaId}`,
    graphId: firstString(all, "graph_id", "graphId"),
    graphRevision:
      firstInteger(
        all,
        "graph_revision",
        "graphRevision",
        "committed_revision",
        "committedRevision",
        "current_revision",
      ) ?? 0,
    commitRevision:
      firstInteger(
        all,
        "commit_revision",
        "commitRevision",
        "committed_revision",
        "committedRevision",
        "current_revision",
      ) ?? 0,
  }
}

function branchView(
  source: BranchSource,
  context: TopologyProjectionContext,
): BranchView {
  const { record } = source
  const all = [source.source, ...recordCandidates(record)]
  const baseRevision =
    firstInteger(
      all,
      "base_revision",
      "baseRevision",
      "expected_revision",
      "expectedRevision",
      "from_revision",
    ) ?? 0
  const outcome = branchOutcome(all)
  const deltaId = firstString(all, "delta_id", "deltaId")
  const entryIds = extractEntryIds(source.source)
  const mutationIds = uniqueStrings([
    ...firstStringArray(all, "mutation_ids", "mutations"),
    ...entryIds,
  ])
  const conflicts = conflictViews(source, context, baseRevision)
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "branch",
      source.branchId,
      source.graphId,
      source.graphRevision,
    ),
    source.graphId,
    source.graphRevision,
    [
      `branch:${source.branchId}`,
      deltaId ? `delta:${deltaId}` : "",
      ...conflicts.map((conflict) => `conflict:${conflict.id}`),
    ],
  )
  const state =
    outcome === "conflicted"
      ? "conflicted"
      : outcome === "rejected"
        ? "rejected"
        : outcome === "pending"
          ? "waiting"
          : outcome === "rebased"
            ? "recovering"
            : "completed"
  const deterministicOrderKey =
    firstString(
      all,
      "deterministic_order_key",
      "deterministicOrderKey",
      "order_key",
      "commit_order_key",
    ) ??
    [
      String(baseRevision).padStart(12, "0"),
      String(record.sequence).padStart(12, "0"),
      source.branchId,
      deltaId ?? "",
    ].join(":")
  return {
    id: source.branchId,
    branchId: source.branchId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "branch",
    sequence: record.sequence,
    revision:
      firstInteger(all, "revision", "branch_revision", "version") ??
      record.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    state,
    title:
      firstString(all, "title", "name") ?? `Branch ${source.branchId}`,
    summary:
      firstString(all, "summary", "reason", "message") ?? record.summary,
    namespace:
      firstString(all, "namespace", "checkpoint_ns") ?? "branches",
    subgraphId: firstString(all, "subgraph_id", "graph_id"),
    checkpointId:
      firstString(all, "checkpoint_id") ?? record.checkpointId,
    effective:
      record.effective &&
      !["rejected", "discarded", "conflicted"].includes(outcome),
    terminal: !["pending", "unknown", "replan_required"].includes(outcome),
    pending: ["pending", "unknown", "replan_required"].includes(outcome),
    removed: record.removed || outcome === "discarded",
    evidence,
    attributes: Object.freeze(
      compactAttributes([source.source], context.sensitiveFieldDrops),
    ),
    deltaId,
    owner: firstString(all, "owner", "owner_id", "writer_id"),
    baseRevision,
    outcome,
    strategy:
      firstString(all, "strategy", "conflict_strategy", "commit_strategy") ??
      defaultBranchStrategy(outcome),
    readSet: Object.freeze(
      firstStringArray(all, "read_set", "readSet", "reads", "read_keys"),
    ),
    writeSet: Object.freeze(
      firstStringArray(all, "write_set", "writeSet", "writes", "write_keys"),
    ),
    entryIds: Object.freeze(entryIds),
    mutationIds: Object.freeze(mutationIds),
    conflicts: Object.freeze(conflicts),
    rebasedFromRevision: firstInteger(
      all,
      "rebased_from_revision",
      "rebasedFromRevision",
      "rebase_from_revision",
    ),
    deterministicOrderKey,
    visibleInCanonicalState:
      outcome === "committed" ||
      outcome === "rebased" ||
      outcome === "replayed",
  }
}

function branchOutcome(sources: readonly JsonObject[]): BranchOutcome {
  const token = normalizeToken(
    firstString(sources, "outcome", "status", "state", "result"),
  )
  if (["pending", "staged", "open"].includes(token)) return "pending"
  if (["committed", "accepted", "applied", "success"].includes(token)) {
    return "committed"
  }
  if (["rebased", "rebase_succeeded"].includes(token)) return "rebased"
  if (["replayed", "replay_succeeded"].includes(token)) return "replayed"
  if (["conflicted", "conflict", "collision"].includes(token)) return "conflicted"
  if (["replan_required", "needs_replan"].includes(token)) {
    return "replan_required"
  }
  if (["rejected", "denied", "failed"].includes(token)) return "rejected"
  if (["discarded", "rolled_back", "aborted"].includes(token)) {
    return "discarded"
  }
  if (
    firstBoolean(sources, "committed", "accepted", "applied") === true
  ) {
    return "committed"
  }
  const conflicts = valueForAliases(sources[0] ?? {}, CONFLICT_NAMES)
  if (Array.isArray(conflicts) && conflicts.length > 0) return "conflicted"
  return "unknown"
}

function conflictViews(
  source: BranchSource,
  context: TopologyProjectionContext,
  baseRevision: number,
): BranchConflictView[] {
  const values: JsonValue[] = []
  for (const candidate of [source.source, ...recordCandidates(source.record)]) {
    for (const alias of CONFLICT_NAMES) {
      const value = valueForAliases(candidate, [alias])
      if (Array.isArray(value)) values.push(...value)
      else if (isJsonObject(value)) values.push(value)
    }
  }
  const output: BranchConflictView[] = []
  for (let index = 0; index < values.length; index += 1) {
    const raw = values[index]
    const conflict = isJsonObject(raw) ? raw : { reason: raw }
    const key =
      firstString(
        [conflict],
        "key",
        "entity_key",
        "resource_key",
        "write_key",
      ) ?? `unknown:${index}`
    const kind =
      firstString([conflict], "kind", "conflict_type", "type") ??
      "revision_conflict"
    const currentRevision =
      firstInteger(
        [conflict],
        "current_revision",
        "currentRevision",
        "actual_revision",
        "observed_revision",
      ) ?? source.graphRevision
    const id =
      firstString([conflict], "id", "conflict_id") ??
      `${source.branchId}:${kind}:${key}:${currentRevision}`
    output.push({
      id,
      kind,
      key,
      branchId: source.branchId,
      conflictingBranchId: firstString(
        [conflict],
        "conflicting_branch_id",
        "conflictingBranchId",
        "other_branch_id",
      ),
      baseRevision:
        firstInteger(
          [conflict],
          "base_revision",
          "baseRevision",
          "expected_revision",
        ) ??
        baseRevision,
      currentRevision,
      reason:
        firstString([conflict], "reason", "message", "detail") ??
        `${kind} on ${key}`,
      recoverable:
        firstBoolean([conflict], "recoverable", "can_rebase") ??
        ["revision_conflict", "write_conflict", "read_write_conflict"].includes(
          normalizeToken(kind),
        ),
      evidence: evidenceForEntity(
        context.evidence,
        source.record,
        "branch_conflict",
        id,
        source.graphId,
        source.graphRevision,
      ),
    })
  }
  return dedupeConflicts(output)
}

function extractEntryIds(source: JsonObject): string[] {
  const values = valueForAliases(source, [
    "entries",
    "delta_entries",
    "operations",
    "mutations",
  ])
  if (!Array.isArray(values)) return []
  const output: string[] = []
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (typeof value === "string") output.push(value)
    else if (isJsonObject(value)) {
      output.push(
        firstString([value], "id", "entry_id", "mutation_id", "entity_id") ??
          `entry:${index}:${hashKey(value)}`,
      )
    }
  }
  return uniqueStrings(output)
}

function validateCheckpointLineage(
  checkpoints: CheckpointView[],
  ids: ReadonlySet<string>,
): void {
  const byId = new Map(checkpoints.map((checkpoint) => [checkpoint.checkpointId, checkpoint]))
  for (let index = 0; index < checkpoints.length; index += 1) {
    const checkpoint = checkpoints[index]!
    const lineageValid =
      checkpoint.parentCheckpointId === undefined ||
      (ids.has(checkpoint.parentCheckpointId) &&
        !createsCheckpointCycle(checkpoint, byId))
    const pendingKeys = new Set(
      checkpoint.pendingWrites.map(
        (write) => `${write.taskKey}\0${write.channel}\0${write.idempotencyKey ?? ""}`,
      ),
    )
    const committedKeys = new Set(
      checkpoint.committedWrites.map(
        (write) => `${write.taskKey}\0${write.channel}\0${write.idempotencyKey ?? ""}`,
      ),
    )
    let visibilityValid = checkpoint.visibilityValid
    for (const key of pendingKeys) {
      if (committedKeys.has(key)) {
        visibilityValid = false
        break
      }
    }
    if (
      lineageValid !== checkpoint.lineageValid ||
      visibilityValid !== checkpoint.visibilityValid
    ) {
      checkpoints[index] = { ...checkpoint, lineageValid, visibilityValid }
    }
  }
}

function createsCheckpointCycle(
  checkpoint: CheckpointView,
  byId: ReadonlyMap<string, CheckpointView>,
): boolean {
  const seen = new Set([checkpoint.checkpointId])
  let current = checkpoint.parentCheckpointId
  while (current) {
    if (seen.has(current)) return true
    seen.add(current)
    current = byId.get(current)?.parentCheckpointId
  }
  return false
}

function normalizeBranchVisibility(branches: BranchView[]): void {
  branches.sort(
    (left, right) =>
      left.deterministicOrderKey.localeCompare(right.deterministicOrderKey) ||
      left.branchId.localeCompare(right.branchId),
  )
  const committedWrites = new Map<string, string>()
  for (let index = 0; index < branches.length; index += 1) {
    const branch = branches[index]!
    let visible = branch.visibleInCanonicalState
    if (visible) {
      for (const key of branch.writeSet) {
        const owner = committedWrites.get(key)
        if (owner && owner !== branch.branchId && branch.outcome !== "rebased") {
          visible = false
          break
        }
      }
    }
    if (visible) {
      for (const key of branch.writeSet) committedWrites.set(key, branch.branchId)
    }
    if (visible !== branch.visibleInCanonicalState) {
      branches[index] = { ...branch, visibleInCanonicalState: visible }
    }
  }
}

function mergeCheckpoints(values: readonly CheckpointView[]): CheckpointView[] {
  const byId = new Map<string, CheckpointView>()
  for (const value of values) {
    const existing = byId.get(value.checkpointId)
    if (!existing) {
      byId.set(value.checkpointId, value)
      continue
    }
    const newer =
      compareCheckpointVersion(existing, value) <= 0 ? value : existing
    const older = newer === value ? existing : value
    byId.set(value.checkpointId, {
      ...newer,
      ancestry: Object.freeze(uniqueStrings([...older.ancestry, ...newer.ancestry])),
      pendingWrites: Object.freeze(
        dedupeWrites([...older.pendingWrites, ...newer.pendingWrites]),
      ),
      committedWrites: Object.freeze(
        dedupeWrites([...older.committedWrites, ...newer.committedWrites]),
      ),
      pendingRequestIds: Object.freeze(
        uniqueStrings([...older.pendingRequestIds, ...newer.pendingRequestIds]),
      ),
      inflightMessageIds: Object.freeze(
        uniqueStrings([...older.inflightMessageIds, ...newer.inflightMessageIds]),
      ),
      completedStepIds: Object.freeze(
        uniqueStrings([...older.completedStepIds, ...newer.completedStepIds]),
      ),
      processedResponseIds: Object.freeze(
        uniqueStrings([
          ...older.processedResponseIds,
          ...newer.processedResponseIds,
        ]),
      ),
      sideEffectFenceKeys: Object.freeze(
        uniqueStrings([
          ...older.sideEffectFenceKeys,
          ...newer.sideEffectFenceKeys,
        ]),
      ),
      interruptIds: Object.freeze(
        uniqueStrings([...older.interruptIds, ...newer.interruptIds]),
      ),
      resumeIds: Object.freeze(
        uniqueStrings([...older.resumeIds, ...newer.resumeIds]),
      ),
      nextTaskIds: Object.freeze(
        uniqueStrings([...older.nextTaskIds, ...newer.nextTaskIds]),
      ),
      evidence: mergeEvidence(older.evidence, newer.evidence),
    })
  }
  return [...byId.values()]
}

function mergeBranches(values: readonly BranchView[]): BranchView[] {
  const byId = new Map<string, BranchView>()
  for (const value of values) {
    const existing = byId.get(value.branchId)
    if (!existing) {
      byId.set(value.branchId, value)
      continue
    }
    const newer = compareBranchVersion(existing, value) <= 0 ? value : existing
    const older = newer === value ? existing : value
    byId.set(value.branchId, {
      ...newer,
      readSet: Object.freeze(uniqueStrings([...older.readSet, ...newer.readSet])),
      writeSet: Object.freeze(uniqueStrings([...older.writeSet, ...newer.writeSet])),
      entryIds: Object.freeze(uniqueStrings([...older.entryIds, ...newer.entryIds])),
      mutationIds: Object.freeze(
        uniqueStrings([...older.mutationIds, ...newer.mutationIds]),
      ),
      conflicts: Object.freeze(
        dedupeConflicts([...older.conflicts, ...newer.conflicts]),
      ),
      evidence: mergeEvidence(older.evidence, newer.evidence),
    })
  }
  return [...byId.values()]
}

function dedupeWrites(values: readonly CheckpointWriteView[]): CheckpointWriteView[] {
  const byId = new Map<string, CheckpointWriteView>()
  for (const value of values) {
    const existing = byId.get(value.id)
    if (!existing || existing.sequence <= value.sequence) byId.set(value.id, value)
  }
  return [...byId.values()].sort(
    (left, right) =>
      left.sequence - right.sequence ||
      left.taskKey.localeCompare(right.taskKey) ||
      left.channel.localeCompare(right.channel) ||
      left.id.localeCompare(right.id),
  )
}

function dedupeConflicts(
  values: readonly BranchConflictView[],
): BranchConflictView[] {
  const byKey = new Map<string, BranchConflictView>()
  for (const value of values) {
    const key = `${value.branchId}\0${value.kind}\0${value.key}\0${value.currentRevision}`
    const existing = byKey.get(key)
    byKey.set(
      key,
      existing
        ? { ...value, evidence: mergeEvidence(existing.evidence, value.evidence) }
        : value,
    )
  }
  return [...byKey.values()].sort(compareConflict)
}

function dedupeCheckpointSources(
  values: readonly CheckpointSource[],
): CheckpointSource[] {
  return dedupeSources(values, (value) =>
    [
      value.record.eventId,
      value.checkpointId,
      value.graphRevision,
      value.commitRevision,
      stableKey(value.source),
    ].join("\0"),
  )
}

function dedupeBranchSources(values: readonly BranchSource[]): BranchSource[] {
  return dedupeSources(values, (value) =>
    [
      value.record.eventId,
      value.branchId,
      value.graphRevision,
      value.commitRevision,
      stableKey(value.source),
    ].join("\0"),
  )
}

function dedupeSources<T>(
  values: readonly T[],
  keyOf: (value: T) => string,
): T[] {
  const output: T[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = keyOf(value)
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function hasCheckpointSignal(source: JsonObject): boolean {
  return [
    "checkpoint_id",
    "pending_writes",
    "committed_writes",
    "pending_request_ids",
    "processed_response_ids",
    "interrupt_id",
    "resume_id",
  ].some((key) => valueForAliases(source, [key]) !== undefined)
}

function hasBranchSignal(source: JsonObject): boolean {
  return [
    "branch_id",
    "delta_id",
    "base_revision",
    "read_set",
    "write_set",
    "conflicts",
    "rebased_from_revision",
  ].some((key) => valueForAliases(source, [key]) !== undefined)
}

function defaultBranchStrategy(outcome: BranchOutcome): string {
  if (outcome === "rebased") return "deterministic_rebase"
  if (outcome === "replayed") return "idempotent_replay"
  if (outcome === "conflicted") return "reject_on_conflict"
  if (outcome === "replan_required") return "local_replan"
  return "optimistic_commit"
}

function compareCheckpointSource(
  left: CheckpointSource,
  right: CheckpointSource,
): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.checkpointId.localeCompare(right.checkpointId)
  )
}

function compareBranchSource(left: BranchSource, right: BranchSource): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.branchId.localeCompare(right.branchId)
  )
}

function compareCheckpointVersion(
  left: CheckpointView,
  right: CheckpointView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareBranchVersion(left: BranchView, right: BranchView): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareCheckpoint(left: CheckpointView, right: CheckpointView): number {
  return (
    left.sequence - right.sequence ||
    left.iteration - right.iteration ||
    left.checkpointId.localeCompare(right.checkpointId)
  )
}

function compareBranch(left: BranchView, right: BranchView): number {
  return (
    left.deterministicOrderKey.localeCompare(right.deterministicOrderKey) ||
    left.sequence - right.sequence ||
    left.branchId.localeCompare(right.branchId)
  )
}

function compareConflict(
  left: BranchConflictView,
  right: BranchConflictView,
): number {
  return (
    left.baseRevision - right.baseRevision ||
    left.currentRevision - right.currentRevision ||
    left.branchId.localeCompare(right.branchId) ||
    left.key.localeCompare(right.key) ||
    left.id.localeCompare(right.id)
  )
}
