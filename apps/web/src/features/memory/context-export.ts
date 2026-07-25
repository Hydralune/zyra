import type { MemoryRow } from "./projection.ts"
import type { RetrievalPlan } from "./retrieval-session.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  sum,
  text,
  unique,
} from "../session/value.ts"

export interface MemoryContextBlock {
  id: string
  memoryId: string
  layer: string
  namespace: string
  title: string
  content: string
  tokenCount: number
  utility: number
  veracity: string
  veracityScore: number
  provenanceScore: number
  freshness: number
  compactEpoch?: number
  sourceEventIds: readonly string[]
  sourceArtifactIds: readonly string[]
  sourceMemoryIds: readonly string[]
  tags: readonly string[]
  required: boolean
  restorable: boolean
  checksum: string
}

export interface MemoryContextExport {
  id: string
  planId: string
  taskId: string
  sessionId?: string
  compactEpoch: number
  contextRevision: number
  blocks: readonly MemoryContextBlock[]
  memoryIds: readonly string[]
  sourceEventIds: readonly string[]
  sourceArtifactIds: readonly string[]
  sourceMemoryIds: readonly string[]
  tokenCount: number
  tokenBudget: number
  truncated: boolean
  warnings: readonly string[]
  checksum: string
  createdAt: string
}

export interface MemoryContextRestore {
  id: string
  exportId: string
  restoredBlocks: readonly MemoryContextBlock[]
  skippedBlocks: readonly MemoryContextBlock[]
  missingMemoryIds: readonly string[]
  staleMemoryIds: readonly string[]
  conflictedMemoryIds: readonly string[]
  restoredTokenCount: number
  sourceEpoch: number
  targetEpoch: number
  exact: boolean
  warnings: readonly string[]
  checksum: string
}

export interface MemoryContextDifference {
  leftExportId: string
  rightExportId: string
  addedMemoryIds: readonly string[]
  removedMemoryIds: readonly string[]
  changedMemoryIds: readonly string[]
  retainedMemoryIds: readonly string[]
  addedEventIds: readonly string[]
  removedEventIds: readonly string[]
  addedArtifactIds: readonly string[]
  removedArtifactIds: readonly string[]
  tokenDelta: number
  epochDelta: number
  revisionDelta: number
  checksumChanged: boolean
}

export class MemoryContextExporter {
  readonly #exports = new Map<string, MemoryContextExport>()
  readonly #restores = new Map<string, MemoryContextRestore>()
  #enabled = true
  #disabledReason = "Memory context exporter is disabled."

  export(input: {
    plan: RetrievalPlan
    taskId: string
    sessionId?: string
    compactEpoch: number
    contextRevision: number
    createdAt?: string
  }): MemoryContextExport {
    this.#assertEnabled()
    const blocks = input.plan.selected.map((candidate) =>
      blockFromMemory(
        candidate.memory,
        candidate.utility,
        input.plan.constraint.minimumVeracity !== "uncertain",
      ))
    const tokenCount = sum(blocks.map((block) => block.tokenCount))
    const warnings: string[] = []
    if (!blocks.length) warnings.push("memory context export is empty")
    if (tokenCount > input.plan.tokenBudget) warnings.push("memory context exceeds retrieval token budget")
    if (blocks.some((block) => !block.sourceEventIds.length)) warnings.push("memory context contains block without event provenance")
    if (blocks.some((block) => block.provenanceScore === 0)) warnings.push("memory context contains block without provenance confidence")
    if (blocks.some((block) => block.veracity === "contested")) warnings.push("memory context contains contested block")
    if (blocks.some((block) => block.veracity === "rejected")) warnings.push("memory context contains rejected block")
    const memoryIds = blocks.map((block) => block.memoryId)
    const sourceEventIds = unique(blocks.flatMap((block) => block.sourceEventIds))
    const sourceArtifactIds = unique(blocks.flatMap((block) => block.sourceArtifactIds))
    const sourceMemoryIds = unique(blocks.flatMap((block) => block.sourceMemoryIds))
    const checksum = fingerprint([
      input.plan.contextFingerprint,
      input.taskId,
      input.sessionId,
      input.compactEpoch,
      input.contextRevision,
      blocks.map((block) => block.checksum),
    ])
    const value: MemoryContextExport = Object.freeze({
      id: fingerprint([
        input.plan.id,
        input.taskId,
        input.sessionId,
        input.compactEpoch,
        input.contextRevision,
        checksum,
      ]),
      planId: input.plan.id,
      taskId: input.taskId,
      sessionId: input.sessionId,
      compactEpoch: Math.max(0, Math.floor(input.compactEpoch)),
      contextRevision: Math.max(0, Math.floor(input.contextRevision)),
      blocks: Object.freeze(blocks),
      memoryIds: Object.freeze(memoryIds),
      sourceEventIds: Object.freeze(sourceEventIds),
      sourceArtifactIds: Object.freeze(sourceArtifactIds),
      sourceMemoryIds: Object.freeze(sourceMemoryIds),
      tokenCount,
      tokenBudget: input.plan.tokenBudget,
      truncated:
        input.plan.selected.length < input.plan.candidates.filter((candidate) =>
          candidate.eligible).length ||
        tokenCount >= input.plan.tokenBudget,
      warnings: Object.freeze(warnings),
      checksum,
      createdAt: input.createdAt ?? new Date().toISOString(),
    })
    const existing = this.#exports.get(value.id)
    if (existing) {
      if (existing.checksum !== value.checksum) {
        throw new Error("Memory context export identity conflicts with another checksum.")
      }
      return existing
    }
    this.#exports.set(value.id, value)
    return value
  }

  restore(input: {
    exportId: string
    rows: readonly MemoryRow[]
    targetEpoch: number
    targetRevision: number
    allowStale?: boolean
    allowRevisionAdvance?: boolean
  }): MemoryContextRestore {
    this.#assertEnabled()
    const source = this.#exports.get(input.exportId)
    if (!source) throw new Error("Memory context export does not exist.")
    const byId = new Map(input.rows.map((row) => [row.id, row]))
    const restoredBlocks: MemoryContextBlock[] = []
    const skippedBlocks: MemoryContextBlock[] = []
    const missingMemoryIds: string[] = []
    const staleMemoryIds: string[] = []
    const conflictedMemoryIds: string[] = []
    for (const block of source.blocks) {
      const row = byId.get(block.memoryId)
      if (!row) {
        missingMemoryIds.push(block.memoryId)
        skippedBlocks.push(block)
        continue
      }
      const current = blockFromMemory(row, block.utility, block.required)
      if (current.checksum !== block.checksum) {
        conflictedMemoryIds.push(block.memoryId)
        skippedBlocks.push(block)
        continue
      }
      if ((row.status === "stale" || row.staleReason) && !input.allowStale) {
        staleMemoryIds.push(block.memoryId)
        skippedBlocks.push(block)
        continue
      }
      if (row.veracity === "rejected") {
        conflictedMemoryIds.push(block.memoryId)
        skippedBlocks.push(block)
        continue
      }
      restoredBlocks.push(current)
    }
    const warnings: string[] = []
    if (missingMemoryIds.length) warnings.push(`${missingMemoryIds.length} memory blocks are missing`)
    if (staleMemoryIds.length) warnings.push(`${staleMemoryIds.length} memory blocks are stale`)
    if (conflictedMemoryIds.length) warnings.push(`${conflictedMemoryIds.length} memory blocks conflict`)
    if (
      input.targetRevision < source.contextRevision ||
      (!input.allowRevisionAdvance && input.targetRevision !== source.contextRevision)
    ) warnings.push("target context revision differs from export revision")
    if (input.targetEpoch < source.compactEpoch) warnings.push("target compact epoch precedes export epoch")
    const exact =
      missingMemoryIds.length === 0 &&
      staleMemoryIds.length === 0 &&
      conflictedMemoryIds.length === 0 &&
      restoredBlocks.length === source.blocks.length &&
      input.targetEpoch >= source.compactEpoch &&
      (
        input.targetRevision === source.contextRevision ||
        input.allowRevisionAdvance === true
      )
    const checksum = fingerprint([
      source.checksum,
      input.targetEpoch,
      input.targetRevision,
      restoredBlocks.map((block) => block.checksum),
      missingMemoryIds,
      staleMemoryIds,
      conflictedMemoryIds,
    ])
    const restore: MemoryContextRestore = Object.freeze({
      id: fingerprint([source.id, input.targetEpoch, input.targetRevision, checksum]),
      exportId: source.id,
      restoredBlocks: Object.freeze(restoredBlocks),
      skippedBlocks: Object.freeze(skippedBlocks),
      missingMemoryIds: Object.freeze(missingMemoryIds),
      staleMemoryIds: Object.freeze(staleMemoryIds),
      conflictedMemoryIds: Object.freeze(conflictedMemoryIds),
      restoredTokenCount: sum(restoredBlocks.map((block) => block.tokenCount)),
      sourceEpoch: source.compactEpoch,
      targetEpoch: Math.max(0, Math.floor(input.targetEpoch)),
      exact,
      warnings: Object.freeze(warnings),
      checksum,
    })
    this.#restores.set(restore.id, restore)
    return restore
  }

  difference(leftId: string, rightId: string): MemoryContextDifference {
    this.#assertEnabled()
    const left = this.#exports.get(leftId)
    const right = this.#exports.get(rightId)
    if (!left || !right) throw new Error("Both memory context exports must exist.")
    const leftBlocks = new Map(left.blocks.map((block) => [block.memoryId, block]))
    const rightBlocks = new Map(right.blocks.map((block) => [block.memoryId, block]))
    const addedMemoryIds = [...rightBlocks.keys()].filter((id) => !leftBlocks.has(id))
    const removedMemoryIds = [...leftBlocks.keys()].filter((id) => !rightBlocks.has(id))
    const retainedMemoryIds = [...rightBlocks.keys()].filter((id) => leftBlocks.has(id))
    const changedMemoryIds = retainedMemoryIds.filter((id) =>
      leftBlocks.get(id)?.checksum !== rightBlocks.get(id)?.checksum)
    return Object.freeze({
      leftExportId: left.id,
      rightExportId: right.id,
      addedMemoryIds: Object.freeze(addedMemoryIds),
      removedMemoryIds: Object.freeze(removedMemoryIds),
      changedMemoryIds: Object.freeze(changedMemoryIds),
      retainedMemoryIds: Object.freeze(retainedMemoryIds),
      addedEventIds: Object.freeze(right.sourceEventIds.filter((id) =>
        !left.sourceEventIds.includes(id))),
      removedEventIds: Object.freeze(left.sourceEventIds.filter((id) =>
        !right.sourceEventIds.includes(id))),
      addedArtifactIds: Object.freeze(right.sourceArtifactIds.filter((id) =>
        !left.sourceArtifactIds.includes(id))),
      removedArtifactIds: Object.freeze(left.sourceArtifactIds.filter((id) =>
        !right.sourceArtifactIds.includes(id))),
      tokenDelta: right.tokenCount - left.tokenCount,
      epochDelta: right.compactEpoch - left.compactEpoch,
      revisionDelta: right.contextRevision - left.contextRevision,
      checksumChanged: left.checksum !== right.checksum,
    })
  }

  get(exportId: string): MemoryContextExport | undefined {
    this.#assertEnabled()
    return this.#exports.get(exportId)
  }

  list(): readonly MemoryContextExport[] {
    this.#assertEnabled()
    return Object.freeze(sortStable([...this.#exports.values()], (left, right) =>
      right.createdAt.localeCompare(left.createdAt) ||
      compareNumber(right.contextRevision, left.contextRevision) ||
      compareText(left.id, right.id)))
  }

  restoreRecord(restoreId: string): MemoryContextRestore | undefined {
    this.#assertEnabled()
    return this.#restores.get(restoreId)
  }

  restores(): readonly MemoryContextRestore[] {
    this.#assertEnabled()
    return Object.freeze([...this.#restores.values()])
  }

  buildBackendEnvelope(exportId: string): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const value = this.#exports.get(exportId)
    if (!value) throw new Error("Memory context export does not exist.")
    return Object.freeze({
      schema: "zyra.memory-context-export/v1",
      export_id: value.id,
      plan_id: value.planId,
      task_id: value.taskId,
      session_id: value.sessionId,
      compact_epoch: value.compactEpoch,
      context_revision: value.contextRevision,
      checksum: value.checksum,
      token_count: value.tokenCount,
      token_budget: value.tokenBudget,
      truncated: value.truncated,
      memory_ids: value.memoryIds,
      source_event_ids: value.sourceEventIds,
      source_artifact_ids: value.sourceArtifactIds,
      source_memory_ids: value.sourceMemoryIds,
      blocks: Object.freeze(value.blocks.map((block) => Object.freeze({
        id: block.id,
        memory_id: block.memoryId,
        layer: block.layer,
        namespace: block.namespace,
        title: block.title,
        content: block.content,
        token_count: block.tokenCount,
        utility: block.utility,
        veracity: block.veracity,
        veracity_score: block.veracityScore,
        provenance_score: block.provenanceScore,
        freshness: block.freshness,
        compact_epoch: block.compactEpoch,
        source_event_ids: block.sourceEventIds,
        source_artifact_ids: block.sourceArtifactIds,
        source_memory_ids: block.sourceMemoryIds,
        tags: block.tags,
        required: block.required,
        restorable: block.restorable,
        checksum: block.checksum,
      }))),
    })
  }

  disable(reason = "Memory context exporter is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Memory context exporter is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#exports.clear()
    this.#restores.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function blockFromMemory(
  row: MemoryRow,
  utility: number,
  required: boolean,
): MemoryContextBlock {
  const content = text(row.summary, row.title)
  const tokenCount = Math.max(
    1,
    row.tokenCount || Math.ceil(new TextEncoder().encode(content).byteLength / 3.5),
  )
  const checksum = fingerprint([
    row.id,
    row.revision,
    row.layer,
    row.namespace,
    row.key,
    row.title,
    content,
    tokenCount,
    row.veracity,
    row.veracityScore,
    row.provenanceScore,
    row.sourceEventIds,
    row.sourceArtifactIds,
    row.sourceMemoryIds,
  ])
  return Object.freeze({
    id: fingerprint(["memory-context-block", row.id, row.revision, checksum]),
    memoryId: row.id,
    layer: row.layer,
    namespace: row.namespace,
    title: row.title,
    content,
    tokenCount,
    utility: bounded(utility, 0, 1),
    veracity: row.veracity,
    veracityScore: row.veracityScore,
    provenanceScore: row.provenanceScore,
    freshness: row.freshness,
    compactEpoch: row.compactEpoch,
    sourceEventIds: Object.freeze([...row.sourceEventIds]),
    sourceArtifactIds: Object.freeze([...row.sourceArtifactIds]),
    sourceMemoryIds: Object.freeze([...row.sourceMemoryIds]),
    tags: Object.freeze([...row.tags]),
    required,
    restorable:
      row.veracity !== "rejected" &&
      Boolean(row.sourceEventIds.length || row.sourceArtifactIds.length),
    checksum,
  })
}
