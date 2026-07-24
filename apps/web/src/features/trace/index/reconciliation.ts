import {
  TraceCompleteness,
  TraceSemanticKind,
  type CausalTraceProjection,
  type TraceEventAdmission,
  type TraceNode,
} from "../contracts.ts"

export const TraceReconciliationKind = {
  EVENT_ADDED: "event-added",
  EVENT_FINALIZED: "event-finalized",
  EVENT_LATE: "event-late",
  EVENT_REPLACED: "event-replaced",
  ARTIFACT_RESOLVED: "artifact-resolved",
  SPAN_RESOLVED: "span-resolved",
  ORPHAN_RESOLVED: "orphan-resolved",
  RECONNECT: "reconnect",
  QUARANTINE_ADDED: "quarantine-added",
  QUARANTINE_CLEARED: "quarantine-cleared",
} as const

export type TraceReconciliationKindValue =
  (typeof TraceReconciliationKind)[keyof typeof TraceReconciliationKind]

export interface TraceReconciliationChange {
  id: string
  kind: TraceReconciliationKindValue
  eventId?: string
  nodeKey?: string
  artifactId?: string
  previousRevision: number
  currentRevision: number
  sequence: number
  late: boolean
  terminal: boolean
  message: string
}

export interface TraceReconciliationResult {
  taskId: string
  previousRevision: number
  currentRevision: number
  changes: readonly TraceReconciliationChange[]
  addedEventIds: readonly string[]
  finalizedEventIds: readonly string[]
  lateEventIds: readonly string[]
  resolvedArtifactIds: readonly string[]
  resolvedNodeKeys: readonly string[]
  reconnectEventIds: readonly string[]
  staleInput: boolean
}

interface EventFingerprint {
  eventId: string
  sequence: number
  terminal: boolean
  completeness: string
  artifactIds: readonly string[]
  spanId?: string
  causationId?: string
  correlationId: string
  content: string
}

interface ProjectionFingerprint {
  taskId: string
  revision: number
  committedSequence: number
  eventById: ReadonlyMap<string, EventFingerprint>
  quarantineIds: ReadonlySet<string>
}

export interface TraceReconciliationAudit {
  closed: boolean
  disabled: boolean
  taskId?: string
  revision?: number
  retainedEventFingerprints: number
  reconcileCount: number
  staleInputCount: number
  lateEventCount: number
  finalizedEventCount: number
  resolvedArtifactCount: number
  lastChangeCount: number
}

function admissionFingerprint(admission: TraceEventAdmission): EventFingerprint {
  const event = admission.event
  const artifactIds = Object.freeze([...admission.refs.artifactIds].sort())
  return Object.freeze({
    eventId: event.eventId,
    sequence: event.sequence,
    terminal: event.terminal,
    completeness: admission.completeness,
    artifactIds,
    spanId: admission.refs.spanId,
    causationId: event.causationId,
    correlationId: event.correlationId,
    content: [
      event.sequence,
      event.aggregateSequence,
      event.terminal ? 1 : 0,
      event.effective ? 1 : 0,
      admission.completeness,
      admission.refs.spanId ?? "",
      event.causationId ?? "",
      event.correlationId,
      artifactIds.join(","),
    ].join("|"),
  })
}

function projectionFingerprint(projection: CausalTraceProjection): ProjectionFingerprint {
  return Object.freeze({
    taskId: projection.taskId,
    revision: projection.projectionRevision,
    committedSequence: projection.diagnostics.committedSequence,
    eventById: new Map(
      Object.values(projection.admissionByEvent).map((admission) => [
        admission.event.eventId,
        admissionFingerprint(admission),
      ]),
    ),
    quarantineIds: new Set(projection.quarantine.map((record) => record.id)),
  })
}

function changeId(
  kind: TraceReconciliationKindValue,
  identity: string,
  revision: number,
): string {
  return `trace-reconcile:${revision}:${kind}:${identity}`
}

function change(
  kind: TraceReconciliationKindValue,
  identity: string,
  previousRevision: number,
  currentRevision: number,
  input: Omit<TraceReconciliationChange, "id" | "kind" | "previousRevision" | "currentRevision">,
): TraceReconciliationChange {
  return Object.freeze({
    id: changeId(kind, identity, currentRevision),
    kind,
    previousRevision,
    currentRevision,
    ...input,
  })
}

function nodeForEvent(
  projection: CausalTraceProjection,
  eventId: string,
): TraceNode | undefined {
  const keys = projection.indexes.nodeKeysByEvent[eventId] ?? []
  return keys
    .map((key) => projection.nodesByKey[key])
    .find((node) => node?.identity.kind === "event")
}

function artifactDifference(
  previous: readonly string[],
  current: readonly string[],
): readonly string[] {
  const retained = new Set(previous)
  return current.filter((artifactId) => !retained.has(artifactId))
}

function isReconnect(admission: TraceEventAdmission): boolean {
  return admission.semantics.includes(TraceSemanticKind.MCP_RECONNECT) ||
    (admission.semantics.includes(TraceSemanticKind.TERMINAL) &&
      /(^|[._-])reconnect(ed|ing)?($|[._-])/i.test(admission.event.eventType)) ||
    (admission.semantics.includes(TraceSemanticKind.BROWSER) &&
      /(^|[._-])reconnect(ed|ing)?($|[._-])/i.test(admission.event.eventType))
}

function compareChanges(
  left: TraceReconciliationChange,
  right: TraceReconciliationChange,
): number {
  return left.sequence - right.sequence || left.kind.localeCompare(right.kind) || left.id.localeCompare(right.id)
}

function reconcileFingerprints(
  previous: ProjectionFingerprint,
  current: ProjectionFingerprint,
  projection: CausalTraceProjection,
): TraceReconciliationResult {
  if (current.taskId !== previous.taskId) {
    throw new TypeError(
      `Cannot reconcile causal traces across tasks: ${previous.taskId} -> ${current.taskId}.`,
    )
  }
  const staleInput = current.revision < previous.revision
  if (staleInput) {
    return Object.freeze({
      taskId: current.taskId,
      previousRevision: previous.revision,
      currentRevision: current.revision,
      changes: Object.freeze([]),
      addedEventIds: Object.freeze([]),
      finalizedEventIds: Object.freeze([]),
      lateEventIds: Object.freeze([]),
      resolvedArtifactIds: Object.freeze([]),
      resolvedNodeKeys: Object.freeze([]),
      reconnectEventIds: Object.freeze([]),
      staleInput: true,
    })
  }
  const changes: TraceReconciliationChange[] = []
  const added: string[] = []
  const finalized: string[] = []
  const late: string[] = []
  const resolvedArtifacts: string[] = []
  const resolvedNodes: string[] = []
  const reconnects: string[] = []
  for (const [eventId, currentEvent] of current.eventById) {
    const previousEvent = previous.eventById.get(eventId)
    const admission = projection.admissionByEvent[eventId]
    const node = nodeForEvent(projection, eventId)
    if (!previousEvent) {
      const isLate = currentEvent.sequence < previous.committedSequence
      added.push(eventId)
      if (isLate) late.push(eventId)
      changes.push(change(
        isLate ? TraceReconciliationKind.EVENT_LATE : TraceReconciliationKind.EVENT_ADDED,
        eventId,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          sequence: currentEvent.sequence,
          late: isLate,
          terminal: currentEvent.terminal,
          message: isLate
            ? `Late canonical event ${eventId} joined at sequence ${currentEvent.sequence}.`
            : `Canonical event ${eventId} joined the trace.`,
        },
      ))
      if (admission && isReconnect(admission)) {
        reconnects.push(eventId)
        changes.push(change(
          TraceReconciliationKind.RECONNECT,
          eventId,
          previous.revision,
          current.revision,
          {
            eventId,
            nodeKey: node?.key,
            sequence: currentEvent.sequence,
            late: isLate,
            terminal: currentEvent.terminal,
            message: `Typed reconnect event ${eventId} opened a new transport epoch.`,
          },
        ))
      }
      continue
    }
    if (previousEvent.content === currentEvent.content) continue
    if (!previousEvent.terminal && currentEvent.terminal) {
      finalized.push(eventId)
      changes.push(change(
        TraceReconciliationKind.EVENT_FINALIZED,
        eventId,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          sequence: currentEvent.sequence,
          late: currentEvent.sequence < previous.committedSequence,
          terminal: true,
          message: `Partial event ${eventId} settled with its canonical final state.`,
        },
      ))
    } else {
      changes.push(change(
        TraceReconciliationKind.EVENT_REPLACED,
        eventId,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          sequence: currentEvent.sequence,
          late: currentEvent.sequence < previous.committedSequence,
          terminal: currentEvent.terminal,
          message: `Canonical event ${eventId} changed at projection revision ${current.revision}.`,
        },
      ))
    }
    const artifacts = artifactDifference(previousEvent.artifactIds, currentEvent.artifactIds)
    for (const artifactId of artifacts) {
      resolvedArtifacts.push(artifactId)
      changes.push(change(
        TraceReconciliationKind.ARTIFACT_RESOLVED,
        `${eventId}:${artifactId}`,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          artifactId,
          sequence: currentEvent.sequence,
          late: currentEvent.sequence < previous.committedSequence,
          terminal: currentEvent.terminal,
          message: `Artifact ${artifactId} was attached to ${eventId} by typed identity.`,
        },
      ))
    }
    if (!previousEvent.spanId && currentEvent.spanId) {
      if (node) resolvedNodes.push(node.key)
      changes.push(change(
        TraceReconciliationKind.SPAN_RESOLVED,
        eventId,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          sequence: currentEvent.sequence,
          late: currentEvent.sequence < previous.committedSequence,
          terminal: currentEvent.terminal,
          message: `Missing span for ${eventId} resolved as ${currentEvent.spanId}.`,
        },
      ))
    }
    if (
      previousEvent.completeness === TraceCompleteness.ORPHAN &&
      currentEvent.completeness !== TraceCompleteness.ORPHAN
    ) {
      if (node) resolvedNodes.push(node.key)
      changes.push(change(
        TraceReconciliationKind.ORPHAN_RESOLVED,
        eventId,
        previous.revision,
        current.revision,
        {
          eventId,
          nodeKey: node?.key,
          sequence: currentEvent.sequence,
          late: currentEvent.sequence < previous.committedSequence,
          terminal: currentEvent.terminal,
          message: `Missing cause for ${eventId} entered the retained canonical window.`,
        },
      ))
    }
  }
  for (const id of current.quarantineIds) {
    if (previous.quarantineIds.has(id)) continue
    const record = projection.quarantine.find((candidate) => candidate.id === id)
    changes.push(change(
      TraceReconciliationKind.QUARANTINE_ADDED,
      id,
      previous.revision,
      current.revision,
      {
        eventId: record?.eventId,
        nodeKey: record?.nodeKey,
        sequence: record?.sequence ?? 0,
        late: false,
        terminal: false,
        message: record?.message ?? `Trace record ${id} was quarantined.`,
      },
    ))
  }
  for (const id of previous.quarantineIds) {
    if (current.quarantineIds.has(id)) continue
    changes.push(change(
      TraceReconciliationKind.QUARANTINE_CLEARED,
      id,
      previous.revision,
      current.revision,
      {
        sequence: current.committedSequence,
        late: false,
        terminal: true,
        message: `Quarantine record ${id} cleared after canonical reconciliation.`,
      },
    ))
  }
  changes.sort(compareChanges)
  return Object.freeze({
    taskId: current.taskId,
    previousRevision: previous.revision,
    currentRevision: current.revision,
    changes: Object.freeze(changes),
    addedEventIds: Object.freeze(added),
    finalizedEventIds: Object.freeze(finalized),
    lateEventIds: Object.freeze(late),
    resolvedArtifactIds: Object.freeze([...new Set(resolvedArtifacts)]),
    resolvedNodeKeys: Object.freeze([...new Set(resolvedNodes)]),
    reconnectEventIds: Object.freeze(reconnects),
    staleInput,
  })
}

function initialResult(projection: CausalTraceProjection): TraceReconciliationResult {
  return Object.freeze({
    taskId: projection.taskId,
    previousRevision: projection.projectionRevision,
    currentRevision: projection.projectionRevision,
    changes: Object.freeze([]),
    addedEventIds: Object.freeze([]),
    finalizedEventIds: Object.freeze([]),
    lateEventIds: Object.freeze([]),
    resolvedArtifactIds: Object.freeze([]),
    resolvedNodeKeys: Object.freeze([]),
    reconnectEventIds: Object.freeze([]),
    staleInput: false,
  })
}

export class TraceReconciliationRuntime {
  readonly #disabled: boolean
  #closed = false
  #previous?: ProjectionFingerprint
  #reconcileCount = 0
  #staleInputCount = 0
  #lateEventCount = 0
  #finalizedEventCount = 0
  #resolvedArtifactCount = 0
  #lastChangeCount = 0

  constructor(options: { disabled?: boolean } = {}) {
    this.#disabled = options.disabled === true
  }

  reconcile(projection: CausalTraceProjection): TraceReconciliationResult {
    if (this.#closed) throw new Error("Trace reconciliation runtime is closed.")
    if (this.#disabled) throw new Error("Trace reconciliation runtime is disabled.")
    const current = projectionFingerprint(projection)
    if (!this.#previous || this.#previous.taskId !== projection.taskId) {
      this.#previous = current
      this.#reconcileCount += 1
      this.#lastChangeCount = 0
      return initialResult(projection)
    }
    const result = reconcileFingerprints(this.#previous, current, projection)
    this.#reconcileCount += 1
    this.#lastChangeCount = result.changes.length
    if (result.staleInput) {
      this.#staleInputCount += 1
      return result
    }
    this.#previous = current
    this.#lateEventCount += result.lateEventIds.length
    this.#finalizedEventCount += result.finalizedEventIds.length
    this.#resolvedArtifactCount += result.resolvedArtifactIds.length
    return result
  }

  reset(taskId?: string): void {
    if (!taskId || this.#previous?.taskId === taskId) this.#previous = undefined
  }

  close(): void {
    this.#closed = true
    this.#previous = undefined
  }

  audit(): TraceReconciliationAudit {
    return Object.freeze({
      closed: this.#closed,
      disabled: this.#disabled,
      taskId: this.#previous?.taskId,
      revision: this.#previous?.revision,
      retainedEventFingerprints: this.#previous?.eventById.size ?? 0,
      reconcileCount: this.#reconcileCount,
      staleInputCount: this.#staleInputCount,
      lateEventCount: this.#lateEventCount,
      finalizedEventCount: this.#finalizedEventCount,
      resolvedArtifactCount: this.#resolvedArtifactCount,
      lastChangeCount: this.#lastChangeCount,
    })
  }
}
