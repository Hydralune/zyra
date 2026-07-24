import {
  TraceCompleteness,
  type CausalTraceProjection,
  type TraceNode,
  type TraceReportReference,
} from "../contracts.ts"

export interface TracePinStoreAudit {
  closed: boolean
  taskId?: string
  pinCount: number
  staleCount: number
  addCount: number
  removeCount: number
  rebaseCount: number
  exportCount: number
  listenerCount: number
  lastRevision?: number
}

export interface TraceReportExport {
  schema: "zyra.causal-trace-report-refs/v1"
  taskId: string
  projectionRevision: number
  generatedAt: string
  references: readonly TraceReportReference[]
  markdown: string
  unresolvedReferenceIds: readonly string[]
}

function stablePinId(node: TraceNode): string {
  return `trace-ref:${node.taskId}:${node.runId}:${node.key}`
}

function normalizedNote(note: string | undefined): string | undefined {
  const value = note?.trim()
  if (!value) return undefined
  return value.slice(0, 4_096)
}

function mutationIds(node: TraceNode): readonly string[] {
  return Object.freeze(node.refs.mutationId ? [node.refs.mutationId] : [])
}

function reportReference(
  node: TraceNode,
  projection: CausalTraceProjection,
  note?: string,
): TraceReportReference {
  return Object.freeze({
    id: stablePinId(node),
    taskId: node.taskId,
    runId: node.runId,
    nodeKey: node.key,
    eventIds: Object.freeze([...node.eventIds]),
    artifactIds: Object.freeze([...node.refs.artifactIds]),
    mutationIds: mutationIds(node),
    spanId: node.refs.spanId,
    workerId: node.refs.workerId,
    providerId: node.refs.providerId,
    label: node.title,
    note: normalizedNote(note),
    createdAt: new Date(projection.committedAtMs || Date.now()).toISOString(),
    revision: projection.projectionRevision,
    stale: false,
  })
}

function referenceChanged(
  reference: TraceReportReference,
  node: TraceNode,
  projection: CausalTraceProjection,
): boolean {
  if (reference.revision !== projection.projectionRevision) return true
  if (reference.eventIds.join("|") !== node.eventIds.join("|")) return true
  if (reference.artifactIds.join("|") !== node.refs.artifactIds.join("|")) return true
  if (reference.mutationIds.join("|") !== mutationIds(node).join("|")) return true
  return false
}

function rebaseReference(
  reference: TraceReportReference,
  projection: CausalTraceProjection,
): TraceReportReference {
  const node = projection.nodesByKey[reference.nodeKey]
  if (!node) return Object.freeze({ ...reference, revision: projection.projectionRevision, stale: true })
  const missingEvent = reference.eventIds.some((eventId) => !projection.admissionByEvent[eventId])
  const quarantined = node.completeness === TraceCompleteness.QUARANTINED
  return Object.freeze({
    ...reference,
    runId: node.runId,
    eventIds: Object.freeze([...node.eventIds]),
    artifactIds: Object.freeze([...node.refs.artifactIds]),
    mutationIds: mutationIds(node),
    spanId: node.refs.spanId,
    workerId: node.refs.workerId,
    providerId: node.refs.providerId,
    label: node.title,
    revision: projection.projectionRevision,
    stale: missingEvent || quarantined,
  })
}

function markdownEscape(value: string): string {
  return value.replace(/([\\`*_[\]<>|])/g, "\\$1").replace(/\r?\n/g, " ")
}

function referenceMarkdown(reference: TraceReportReference, ordinal: number): string {
  const facts: string[] = []
  if (reference.spanId) facts.push(`span \`${markdownEscape(reference.spanId)}\``)
  if (reference.workerId) facts.push(`worker \`${markdownEscape(reference.workerId)}\``)
  if (reference.providerId) facts.push(`provider \`${markdownEscape(reference.providerId)}\``)
  if (reference.eventIds.length) facts.push(`${reference.eventIds.length} event(s)`)
  if (reference.artifactIds.length) facts.push(`${reference.artifactIds.length} artifact(s)`)
  if (reference.mutationIds.length) facts.push(`${reference.mutationIds.length} mutation(s)`)
  const lines = [
    `${ordinal + 1}. **${markdownEscape(reference.label)}**${reference.stale ? " _(stale)_" : ""}`,
    `   - Trace node: \`${markdownEscape(reference.nodeKey)}\``,
    `   - Run: \`${markdownEscape(reference.runId)}\``,
    `   - Evidence: ${facts.length ? facts.join(", ") : "typed node reference"}`,
  ]
  if (reference.eventIds.length) {
    lines.push(`   - Event IDs: ${reference.eventIds.map((id) => `\`${markdownEscape(id)}\``).join(", ")}`)
  }
  if (reference.artifactIds.length) {
    lines.push(`   - Artifact IDs: ${reference.artifactIds.map((id) => `\`${markdownEscape(id)}\``).join(", ")}`)
  }
  if (reference.mutationIds.length) {
    lines.push(`   - Mutation IDs: ${reference.mutationIds.map((id) => `\`${markdownEscape(id)}\``).join(", ")}`)
  }
  if (reference.note) lines.push(`   - Note: ${markdownEscape(reference.note)}`)
  return lines.join("\n")
}

function exportMarkdown(
  taskId: string,
  revision: number,
  references: readonly TraceReportReference[],
): string {
  const header = [
    "# Causal trace references",
    "",
    `Task: \`${markdownEscape(taskId)}\`  `,
    `Projection revision: \`${revision}\`  `,
    "",
  ]
  if (!references.length) return [...header, "No trace references were pinned."].join("\n")
  return [...header, ...references.map(referenceMarkdown)].join("\n\n")
}

export class TraceReportReferenceStore {
  readonly #maximumPins: number
  readonly #now: () => number
  readonly #listeners = new Set<() => void>()
  #references = new Map<string, TraceReportReference>()
  #closed = false
  #taskId?: string
  #lastRevision?: number
  #addCount = 0
  #removeCount = 0
  #rebaseCount = 0
  #exportCount = 0

  constructor(options: { maximumPins?: number; now?: () => number } = {}) {
    this.#maximumPins = Math.max(1, Math.min(1_000, options.maximumPins ?? 128))
    this.#now = options.now ?? (() => Date.now())
  }

  list(): readonly TraceReportReference[] {
    return Object.freeze([...this.#references.values()])
  }

  has(nodeKey: string): boolean {
    return [...this.#references.values()].some((reference) => reference.nodeKey === nodeKey)
  }

  subscribe(listener: () => void): () => void {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  pin(
    projection: CausalTraceProjection,
    nodeKey: string,
    note?: string,
  ): TraceReportReference {
    this.#assertOpen()
    const node = projection.nodesByKey[nodeKey]
    if (!node) throw new Error(`Cannot pin unknown trace node ${nodeKey}.`)
    this.#adoptTask(projection.taskId)
    const reference = reportReference(node, projection, note)
    const existing = this.#references.get(reference.id)
    this.#references.delete(reference.id)
    this.#references.set(reference.id, existing
      ? Object.freeze({ ...reference, createdAt: existing.createdAt, note: normalizedNote(note) ?? existing.note })
      : reference)
    while (this.#references.size > this.#maximumPins) {
      const oldest = this.#references.keys().next().value
      if (!oldest) break
      this.#references.delete(oldest)
    }
    this.#lastRevision = projection.projectionRevision
    this.#addCount += 1
    this.#emit()
    return this.#references.get(reference.id)!
  }

  unpin(referenceId: string): boolean {
    this.#assertOpen()
    const removed = this.#references.delete(referenceId)
    if (removed) {
      this.#removeCount += 1
      this.#emit()
    }
    return removed
  }

  unpinNode(nodeKey: string): boolean {
    const reference = [...this.#references.values()].find((value) => value.nodeKey === nodeKey)
    return reference ? this.unpin(reference.id) : false
  }

  updateNote(referenceId: string, note?: string): TraceReportReference | undefined {
    this.#assertOpen()
    const existing = this.#references.get(referenceId)
    if (!existing) return undefined
    const updated = Object.freeze({ ...existing, note: normalizedNote(note) })
    this.#references.set(referenceId, updated)
    this.#emit()
    return updated
  }

  rebase(projection: CausalTraceProjection): readonly TraceReportReference[] {
    this.#assertOpen()
    this.#adoptTask(projection.taskId)
    let changed = false
    for (const [id, reference] of this.#references) {
      const node = projection.nodesByKey[reference.nodeKey]
      if (!node || referenceChanged(reference, node, projection) || reference.stale) {
        const next = rebaseReference(reference, projection)
        this.#references.set(id, next)
        changed = true
      }
    }
    this.#lastRevision = projection.projectionRevision
    this.#rebaseCount += 1
    if (changed) this.#emit()
    return this.list()
  }

  reorder(referenceId: string, beforeReferenceId?: string): boolean {
    this.#assertOpen()
    const value = this.#references.get(referenceId)
    if (!value) return false
    const entries = [...this.#references.entries()].filter(([id]) => id !== referenceId)
    const index = beforeReferenceId
      ? entries.findIndex(([id]) => id === beforeReferenceId)
      : entries.length
    entries.splice(index < 0 ? entries.length : index, 0, [referenceId, value])
    this.#references = new Map(entries)
    this.#emit()
    return true
  }

  clear(): void {
    this.#assertOpen()
    if (!this.#references.size) return
    this.#removeCount += this.#references.size
    this.#references.clear()
    this.#emit()
  }

  export(projection: CausalTraceProjection): TraceReportExport {
    this.#assertOpen()
    this.rebase(projection)
    const references = this.list()
    const generatedAt = new Date(this.#now()).toISOString()
    this.#exportCount += 1
    return Object.freeze({
      schema: "zyra.causal-trace-report-refs/v1",
      taskId: projection.taskId,
      projectionRevision: projection.projectionRevision,
      generatedAt,
      references,
      markdown: exportMarkdown(projection.taskId, projection.projectionRevision, references),
      unresolvedReferenceIds: Object.freeze(references.filter((reference) => reference.stale).map((reference) => reference.id)),
    })
  }

  close(): void {
    this.#closed = true
    this.#references.clear()
    this.#listeners.clear()
    this.#taskId = undefined
    this.#lastRevision = undefined
  }

  audit(): TracePinStoreAudit {
    return Object.freeze({
      closed: this.#closed,
      taskId: this.#taskId,
      pinCount: this.#references.size,
      staleCount: [...this.#references.values()].filter((reference) => reference.stale).length,
      addCount: this.#addCount,
      removeCount: this.#removeCount,
      rebaseCount: this.#rebaseCount,
      exportCount: this.#exportCount,
      listenerCount: this.#listeners.size,
      lastRevision: this.#lastRevision,
    })
  }

  #assertOpen(): void {
    if (this.#closed) throw new Error("Trace report reference store is closed.")
  }

  #adoptTask(taskId: string): void {
    if (this.#taskId && this.#taskId !== taskId) {
      throw new Error(`Trace references belong to ${this.#taskId}, not ${taskId}.`)
    }
    this.#taskId = taskId
  }

  #emit(): void {
    for (const listener of this.#listeners) listener()
  }
}
