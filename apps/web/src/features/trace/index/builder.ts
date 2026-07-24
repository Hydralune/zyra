import type {
  ArtifactProjection,
  CanonicalProjectionState,
  DomainProjection,
  ProjectionEntity,
} from "../../../state/contracts.ts"
import {
  buildWorkerCausalTimeline,
  type TimelineEventFacts,
  type WorkerCausalTimelineProjection,
} from "../../timeline/projection/index.ts"
import {
  TraceCompleteness,
  TraceEdgeKind,
  TraceNodeKind,
  TraceSemanticKind,
  type TraceDiagnostics,
  type TraceEdge,
  type TraceEdgeKindValue,
  type TraceEventAdmission,
  type TraceHierarchy,
  type TraceIdentity,
  type TraceIndexes,
  type TraceNode,
  type TraceNodeKindValue,
  type TraceProjectionOptions,
  type TraceQuarantineRecord,
  type TraceSemanticKindValue,
  type TraceTypedRefs,
} from "../contracts.ts"
import {
  admitTraceEvent,
  compareTraceAdmissions,
  eventNodeIdentity,
  traceIdentity,
  traceIdentityKey,
  traceRefsToIdentities,
} from "../identity.ts"

interface MutableTraceNode {
  identity: TraceIdentity
  taskId: string
  runId: string
  eventIds: Set<string>
  primaryEventId?: string
  parentKey?: string
  childKeys: Set<string>
  incomingEdgeIds: Set<string>
  outgoingEdgeIds: Set<string>
  sequence: number
  endSequence: number
  startedAt?: string
  endedAt?: string
  title: string
  summary: string
  semantics: Set<TraceSemanticKindValue>
  completeness: TraceNode["completeness"]
  terminal: boolean
  effective: boolean
  retryCount: number
  contributionScore: number
  latencyScore: number
  costScore: number
  refs: TraceTypedRefs
  tags: Set<string>
  diagnostics: Set<string>
  safeAttributes: Record<string, string | number | boolean>
}

interface MutableTraceEdge {
  id: string
  kind: TraceEdgeKindValue
  sourceKey: string
  targetKey: string
  sourceEventId?: string
  targetEventId?: string
  explicit: boolean
  weight: number
  late: boolean
  missingSource: boolean
  missingTarget: boolean
  label: string
  evidenceEventIds: Set<string>
  metadata: Record<string, string | number | boolean>
}

export interface TraceBaseProjection {
  state: CanonicalProjectionState
  taskId: string
  timeline: WorkerCausalTimelineProjection
  nodes: readonly TraceNode[]
  edges: readonly TraceEdge[]
  nodesByKey: Readonly<Record<string, TraceNode>>
  edgesById: Readonly<Record<string, TraceEdge>>
  admissionByEvent: Readonly<Record<string, TraceEventAdmission>>
  hierarchy: TraceHierarchy
  indexes: TraceIndexes
  quarantine: readonly TraceQuarantineRecord[]
  diagnostics: TraceDiagnostics
}

const EMPTY_REFS = Object.freeze({
  taskId: "",
  runId: "",
  artifactIds: Object.freeze([]),
}) satisfies TraceTypedRefs

const COMPLETENESS_RANK: Readonly<Record<TraceNode["completeness"], number>> =
  Object.freeze({
    [TraceCompleteness.COMPLETE]: 0,
    [TraceCompleteness.LATE]: 1,
    [TraceCompleteness.PARTIAL]: 2,
    [TraceCompleteness.ORPHAN]: 3,
    [TraceCompleteness.QUARANTINED]: 4,
  })

const IDENTITY_EDGE_KIND: Partial<
  Readonly<Record<TraceNodeKindValue, TraceEdgeKindValue>>
> = Object.freeze({
  [TraceNodeKind.TASK]: TraceEdgeKind.TASK_MEMBER,
  [TraceNodeKind.RUN]: TraceEdgeKind.RUN_MEMBER,
  [TraceNodeKind.SESSION]: TraceEdgeKind.SESSION_MEMBER,
  [TraceNodeKind.NODE]: TraceEdgeKind.NODE_MEMBER,
  [TraceNodeKind.WORKER]: TraceEdgeKind.WORKER_MEMBER,
  [TraceNodeKind.SPAN]: TraceEdgeKind.SPAN_EVENT,
  [TraceNodeKind.TOOL]: TraceEdgeKind.TOOL_CALL,
  [TraceNodeKind.PERMISSION]: TraceEdgeKind.PERMISSION_GUARD,
  [TraceNodeKind.ARTIFACT]: TraceEdgeKind.ARTIFACT_PRODUCED,
  [TraceNodeKind.MUTATION]: TraceEdgeKind.MUTATION_APPLIED,
  [TraceNodeKind.CHECKPOINT]: TraceEdgeKind.CHECKPOINT_STATE,
  [TraceNodeKind.COMMAND]: TraceEdgeKind.COMMAND_CONTROL,
  [TraceNodeKind.ROUTE]: TraceEdgeKind.ROUTE_SELECTED,
  [TraceNodeKind.PLACEMENT]: TraceEdgeKind.PLACEMENT_SELECTED,
  [TraceNodeKind.PROVIDER]: TraceEdgeKind.PROVIDER_DISPATCH,
  [TraceNodeKind.FAILURE]: TraceEdgeKind.FAILURE_TRIGGER,
  [TraceNodeKind.RECOVERY]: TraceEdgeKind.RECOVERY_ATTEMPT,
  [TraceNodeKind.MCP]: TraceEdgeKind.MCP_CALL,
  [TraceNodeKind.SKILL]: TraceEdgeKind.SKILL_EXECUTION,
  [TraceNodeKind.SUBAGENT]: TraceEdgeKind.SUBAGENT_PARENT,
  [TraceNodeKind.TERMINAL]: TraceEdgeKind.TERMINAL_IO,
  [TraceNodeKind.BROWSER]: TraceEdgeKind.BROWSER_ACTION,
  [TraceNodeKind.BACKGROUND]: TraceEdgeKind.BACKGROUND_LIFECYCLE,
})

const EVENT_NODE_WEIGHT: Readonly<Record<TraceSemanticKindValue, number>> =
  Object.freeze({
    [TraceSemanticKind.TASK]: 1,
    [TraceSemanticKind.WORKER]: 2,
    [TraceSemanticKind.TOOL]: 4,
    [TraceSemanticKind.PERMISSION]: 5,
    [TraceSemanticKind.COMPACT]: 6,
    [TraceSemanticKind.CHECKPOINT]: 3,
    [TraceSemanticKind.RESTORE]: 6,
    [TraceSemanticKind.PLACEMENT]: 4,
    [TraceSemanticKind.PROVIDER]: 5,
    [TraceSemanticKind.PROVIDER_RETRY]: 8,
    [TraceSemanticKind.FAULT]: 10,
    [TraceSemanticKind.RECOVERY]: 9,
    [TraceSemanticKind.MCP]: 5,
    [TraceSemanticKind.MCP_RECONNECT]: 8,
    [TraceSemanticKind.SKILL]: 5,
    [TraceSemanticKind.SUBAGENT]: 6,
    [TraceSemanticKind.SUBAGENT_YIELD]: 4,
    [TraceSemanticKind.TERMINAL]: 4,
    [TraceSemanticKind.PTY]: 4,
    [TraceSemanticKind.BROWSER]: 5,
    [TraceSemanticKind.ARTIFACT]: 4,
    [TraceSemanticKind.MUTATION]: 3,
    [TraceSemanticKind.TOPOLOGY]: 3,
    [TraceSemanticKind.COMMAND]: 3,
    [TraceSemanticKind.BACKGROUND]: 3,
    [TraceSemanticKind.OTHER]: 1,
  })

function unique<T>(values: readonly T[]): T[] {
  return [...new Set(values)]
}

function compareEventIds(
  admissionByEvent: Readonly<Record<string, TraceEventAdmission>>,
  left: string,
  right: string,
): number {
  const a = admissionByEvent[left]
  const b = admissionByEvent[right]
  if (!a && !b) return left.localeCompare(right)
  if (!a) return 1
  if (!b) return -1
  return compareTraceAdmissions(a, b)
}

function eventDuration(admission: TraceEventAdmission): number {
  const created = Date.parse(admission.event.createdAt)
  const committed = Date.parse(admission.event.committedAt)
  if (!Number.isFinite(created) || !Number.isFinite(committed)) return 0
  return Math.max(0, committed - created)
}

function semanticWeight(semantics: readonly TraceSemanticKindValue[]): number {
  let weight = 1
  for (const semantic of semantics) {
    weight = Math.max(weight, EVENT_NODE_WEIGHT[semantic])
  }
  return weight
}

function emptyRefs(taskId: string, runId: string): TraceTypedRefs {
  return Object.freeze({ ...EMPTY_REFS, taskId, runId })
}

function mergeRefs(left: TraceTypedRefs, right: TraceTypedRefs): TraceTypedRefs {
  const take = (a: string | undefined, b: string | undefined) => a ?? b
  return Object.freeze({
    taskId: left.taskId || right.taskId,
    runId: left.runId || right.runId,
    sessionId: take(left.sessionId, right.sessionId),
    nodeId: take(left.nodeId, right.nodeId),
    workerId: take(left.workerId, right.workerId),
    spanId: take(left.spanId, right.spanId),
    parentSpanId: take(left.parentSpanId, right.parentSpanId),
    toolCallId: take(left.toolCallId, right.toolCallId),
    permissionId: take(left.permissionId, right.permissionId),
    artifactIds: Object.freeze(unique([...left.artifactIds, ...right.artifactIds])),
    mutationId: take(left.mutationId, right.mutationId),
    checkpointId: take(left.checkpointId, right.checkpointId),
    commandId: take(left.commandId, right.commandId),
    routeId: take(left.routeId, right.routeId),
    placementId: take(left.placementId, right.placementId),
    providerId: take(left.providerId, right.providerId),
    failureId: take(left.failureId, right.failureId),
    recoveryId: take(left.recoveryId, right.recoveryId),
    mcpCallId: take(left.mcpCallId, right.mcpCallId),
    mcpServerId: take(left.mcpServerId, right.mcpServerId),
    skillId: take(left.skillId, right.skillId),
    subagentId: take(left.subagentId, right.subagentId),
    parentToolCallId: take(left.parentToolCallId, right.parentToolCallId),
    yieldId: take(left.yieldId, right.yieldId),
    terminalSessionId: take(left.terminalSessionId, right.terminalSessionId),
    terminalFrameId: take(left.terminalFrameId, right.terminalFrameId),
    browserSessionId: take(left.browserSessionId, right.browserSessionId),
    browserStepId: take(left.browserStepId, right.browserStepId),
    browserActionId: take(left.browserActionId, right.browserActionId),
    backgroundJobId: take(left.backgroundJobId, right.backgroundJobId),
  })
}

function tableEntity(
  state: CanonicalProjectionState,
  identity: TraceIdentity,
): DomainProjection | undefined {
  switch (identity.kind) {
    case TraceNodeKind.TASK:
      return state.tasks[identity.id]
    case TraceNodeKind.NODE:
      return state.nodes[identity.id]
    case TraceNodeKind.WORKER:
      return state.workers[identity.id]
    case TraceNodeKind.TOOL:
      return state.tools[identity.id]
    case TraceNodeKind.PERMISSION:
      return state.permissions[identity.id]
    case TraceNodeKind.ARTIFACT:
      return state.artifacts[identity.id]
    case TraceNodeKind.PLACEMENT:
    case TraceNodeKind.ROUTE:
    case TraceNodeKind.PROVIDER:
      return Object.values(state.schedulers).find((scheduler) =>
        scheduler.id === identity.id ||
        scheduler.placementId === identity.id ||
        scheduler.routeId === identity.id ||
        scheduler.providerId === identity.id,
      )
    case TraceNodeKind.RECOVERY:
      return state.recoveries[identity.id]
    case TraceNodeKind.COMMAND:
      return state.commands[identity.id]
    case TraceNodeKind.SESSION:
      return state.sessions[identity.id]
    default:
      return undefined
  }
}

function entityTitle(identity: TraceIdentity, entity?: ProjectionEntity): string {
  if (entity?.title) return entity.title
  const label = identity.kind.replace(/(^|-)([a-z])/g, (_, prefix: string, value: string) =>
    `${prefix ? " " : ""}${value.toUpperCase()}`,
  )
  return `${label} ${identity.id}`
}

function entitySummary(identity: TraceIdentity, entity?: ProjectionEntity): string {
  if (entity?.summary) return entity.summary
  return `Canonical ${identity.kind} identity ${identity.id}.`
}

function nodeFromIdentity(
  state: CanonicalProjectionState,
  identity: TraceIdentity,
  taskId: string,
  runId: string,
): MutableTraceNode {
  const entity = tableEntity(state, identity)
  const sequence = entity?.sequence ?? Number.MAX_SAFE_INTEGER
  return {
    identity,
    taskId,
    runId,
    eventIds: new Set(),
    childKeys: new Set(),
    incomingEdgeIds: new Set(),
    outgoingEdgeIds: new Set(),
    sequence,
    endSequence: entity?.sequence ?? 0,
    startedAt: entity?.createdAt,
    endedAt: entity?.updatedAt,
    title: entityTitle(identity, entity),
    summary: entitySummary(identity, entity),
    semantics: new Set(),
    completeness: entity ? TraceCompleteness.COMPLETE : TraceCompleteness.PARTIAL,
    terminal: entity?.terminal ?? false,
    effective: entity?.effective ?? true,
    retryCount: 0,
    contributionScore: 0,
    latencyScore: 0,
    costScore: 0,
    refs: emptyRefs(taskId, runId),
    tags: new Set([identity.kind]),
    diagnostics: new Set(entity ? [] : ["canonical-entity-not-retained"]),
    safeAttributes: {},
  }
}

function eventNode(admission: TraceEventAdmission): MutableTraceNode {
  const event = admission.event
  const duration = eventDuration(admission)
  const weight = semanticWeight(admission.semantics)
  return {
    identity: eventNodeIdentity(event.eventId),
    taskId: event.taskId,
    runId: event.runId,
    eventIds: new Set([event.eventId]),
    primaryEventId: event.eventId,
    childKeys: new Set(),
    incomingEdgeIds: new Set(),
    outgoingEdgeIds: new Set(),
    sequence: event.sequence,
    endSequence: event.sequence,
    startedAt: event.createdAt,
    endedAt: event.committedAt,
    title: event.eventType,
    summary: event.summary,
    semantics: new Set(admission.semantics),
    completeness: admission.completeness,
    terminal: event.terminal,
    effective: event.effective,
    retryCount: admission.semantics.some((value) =>
      value === TraceSemanticKind.PROVIDER_RETRY || value === TraceSemanticKind.MCP_RECONNECT,
    ) ? 1 : 0,
    contributionScore: weight,
    latencyScore: duration,
    costScore: 0,
    refs: admission.refs,
    tags: new Set([
      ...admission.semantics,
      admission.completeness,
      event.terminal ? "terminal" : "open",
      event.effective ? "effective" : "non-effective",
    ]),
    diagnostics: new Set([...admission.partialReasons, ...admission.identityConflicts]),
    safeAttributes: { ...admission.safeAttributes },
  }
}

function issueIdentity(code: string, eventId: string, ordinal: number): TraceIdentity {
  const id = `${code}:${eventId}:${ordinal}`.replace(/[^A-Za-z0-9._:/@+-]/g, "-")
  return {
    kind: TraceNodeKind.ISSUE,
    id,
    key: `${TraceNodeKind.ISSUE}:${id}`,
  }
}

function issueNode(
  admission: TraceEventAdmission,
  code: string,
  ordinal: number,
): MutableTraceNode {
  const identity = issueIdentity(code, admission.event.eventId, ordinal)
  return {
    identity,
    taskId: admission.event.taskId,
    runId: admission.event.runId,
    eventIds: new Set([admission.event.eventId]),
    primaryEventId: admission.event.eventId,
    childKeys: new Set(),
    incomingEdgeIds: new Set(),
    outgoingEdgeIds: new Set(),
    sequence: admission.event.sequence,
    endSequence: admission.event.sequence,
    startedAt: admission.event.committedAt,
    endedAt: admission.event.committedAt,
    title: code,
    summary: `Trace integrity issue for ${admission.event.eventId}: ${code}.`,
    semantics: new Set([TraceSemanticKind.OTHER]),
    completeness: code.startsWith("conflict")
      ? TraceCompleteness.QUARANTINED
      : code.startsWith("missing-cause")
        ? TraceCompleteness.ORPHAN
        : TraceCompleteness.PARTIAL,
    terminal: true,
    effective: false,
    retryCount: 0,
    contributionScore: 0,
    latencyScore: 0,
    costScore: 0,
    refs: admission.refs,
    tags: new Set(["issue", code]),
    diagnostics: new Set([code]),
    safeAttributes: {},
  }
}

function edgeId(
  kind: TraceEdgeKindValue,
  sourceKey: string,
  targetKey: string,
  discriminator = "",
): string {
  const raw = `${kind}|${sourceKey}|${targetKey}|${discriminator}`
  let hash = 2166136261
  for (let index = 0; index < raw.length; index += 1) {
    hash ^= raw.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `trace-edge:${kind}:${(hash >>> 0).toString(36)}`
}

function defaultEdgeLabel(kind: TraceEdgeKindValue): string {
  return kind.replaceAll("-", " ")
}

class TraceGraphBuilder {
  readonly #state: CanonicalProjectionState
  readonly #taskId: string
  readonly #options: Required<TraceProjectionOptions>
  readonly #nodes = new Map<string, MutableTraceNode>()
  readonly #edges = new Map<string, MutableTraceEdge>()
  readonly #admissions = new Map<string, TraceEventAdmission>()
  readonly #factsByEvent = new Map<string, TimelineEventFacts>()
  readonly #quarantine: TraceQuarantineRecord[] = []
  readonly #eventsByCorrelation = new Map<string, TraceEventAdmission[]>()
  readonly #eventsByWorker = new Map<string, TraceEventAdmission[]>()
  readonly #eventsBySession = new Map<string, TraceEventAdmission[]>()
  readonly #eventsBySpan = new Map<string, TraceEventAdmission[]>()
  readonly #identityOwners = new Map<string, { taskId: string; runId: string }>()
  #timeline?: WorkerCausalTimelineProjection

  constructor(
    state: CanonicalProjectionState,
    taskId: string,
    options: Required<TraceProjectionOptions>,
  ) {
    this.#state = state
    this.#taskId = taskId
    this.#options = options
  }

  build(): TraceBaseProjection {
    this.#timeline = buildWorkerCausalTimeline(this.#state, this.#taskId, {
      includeNonEffective: this.#options.includeNonEffective,
      includePartial: this.#options.includePartial,
      maximumEvents: this.#options.maximumEvents,
      maximumClosureDepth: this.#options.maximumClosureDepth,
      maximumAlternativePaths: this.#options.maximumAlternativePaths,
    })
    this.#admitEvents(this.#timeline.events)
    this.#materializeEvents()
    this.#connectExplicitCausation()
    this.#connectCorrelations()
    this.#connectSpanHierarchy()
    this.#connectSubagentAndYield()
    this.#connectReplacementAndRetry()
    this.#connectStableSequence(this.#eventsByWorker, "worker")
    this.#connectStableSequence(this.#eventsBySession, "session")
    this.#connectArtifactProducers()
    this.#assignHierarchy()
    return this.#finalize(this.#timeline)
  }

  #admitEvents(factsList: readonly TimelineEventFacts[]): void {
    for (const facts of factsList) {
      const admission = admitTraceEvent(this.#state, facts, {
        maximumAttributesPerEvent: this.#options.maximumAttributesPerEvent,
        lateSequenceTolerance: this.#options.lateSequenceTolerance,
      })
      this.#admissions.set(admission.event.eventId, admission)
      this.#factsByEvent.set(admission.event.eventId, facts)
      this.#appendGroup(this.#eventsByCorrelation, admission.event.correlationId, admission)
      this.#appendGroup(this.#eventsByWorker, admission.refs.workerId, admission)
      this.#appendGroup(this.#eventsBySession, admission.refs.sessionId, admission)
      this.#appendGroup(this.#eventsBySpan, admission.refs.spanId, admission)
      for (const conflict of admission.identityConflicts) {
        this.#quarantine.push(Object.freeze({
          id: `quarantine:${admission.event.eventId}:${this.#quarantine.length}`,
          eventId: admission.event.eventId,
          code: "identity-conflict",
          message: conflict,
          expectedTaskId: this.#taskId,
          observedTaskId: admission.event.taskId,
          sequence: admission.event.sequence,
          recoverable: false,
        }))
      }
    }
    for (const group of [
      this.#eventsByCorrelation,
      this.#eventsByWorker,
      this.#eventsBySession,
      this.#eventsBySpan,
    ]) {
      for (const admissions of group.values()) admissions.sort(compareTraceAdmissions)
    }
  }

  #appendGroup(
    map: Map<string, TraceEventAdmission[]>,
    key: string | undefined,
    admission: TraceEventAdmission,
  ): void {
    if (!key) return
    const values = map.get(key)
    if (values) values.push(admission)
    else map.set(key, [admission])
  }

  #materializeEvents(): void {
    const admissions = [...this.#admissions.values()].sort(compareTraceAdmissions)
    for (const admission of admissions) {
      const event = admission.event
      const eventMutable = eventNode(admission)
      this.#nodes.set(eventMutable.identity.key, eventMutable)
      const identities = traceRefsToIdentities(admission.refs)
      for (const identity of identities) {
        const aggregate = this.#ensureAggregate(identity, admission)
        if (aggregate.identity.key === eventMutable.identity.key) continue
        this.#mergeAdmission(aggregate, admission)
        const kind = IDENTITY_EDGE_KIND[identity.kind]
        if (kind) {
          this.#addEdge({
            kind,
            sourceKey: identity.key,
            targetKey: eventMutable.identity.key,
            sourceEventId: identity.kind === TraceNodeKind.ARTIFACT
              ? this.#state.artifacts[identity.id]?.producerEventId
              : undefined,
            targetEventId: event.eventId,
            explicit: true,
            late: admission.completeness === TraceCompleteness.LATE,
            label: defaultEdgeLabel(kind),
            evidenceEventIds: [event.eventId],
            metadata: { identityKind: identity.kind },
          })
        }
      }
      let ordinal = 0
      for (const reason of admission.partialReasons) {
        const issue = issueNode(admission, reason, ordinal++)
        this.#nodes.set(issue.identity.key, issue)
        this.#addEdge({
          kind: TraceEdgeKind.CORRELATION,
          sourceKey: eventMutable.identity.key,
          targetKey: issue.identity.key,
          sourceEventId: event.eventId,
          targetEventId: event.eventId,
          explicit: true,
          late: false,
          label: "diagnostic",
          evidenceEventIds: [event.eventId],
          metadata: { issue: reason },
        })
      }
      for (const conflict of admission.identityConflicts) {
        const issue = issueNode(admission, `conflict:${conflict}`, ordinal++)
        this.#nodes.set(issue.identity.key, issue)
        this.#addEdge({
          kind: TraceEdgeKind.CORRELATION,
          sourceKey: eventMutable.identity.key,
          targetKey: issue.identity.key,
          sourceEventId: event.eventId,
          targetEventId: event.eventId,
          explicit: true,
          late: false,
          label: "quarantined identity conflict",
          evidenceEventIds: [event.eventId],
          metadata: { conflict },
        })
      }
    }
  }

  #ensureAggregate(
    identity: TraceIdentity,
    admission: TraceEventAdmission,
  ): MutableTraceNode {
    const existing = this.#nodes.get(identity.key)
    if (existing) return existing
    const owner = this.#identityOwners.get(identity.key)
    if (owner && (owner.taskId !== admission.event.taskId || owner.runId !== admission.event.runId)) {
      this.#quarantine.push(Object.freeze({
        id: `quarantine:${identity.key}:${this.#quarantine.length}`,
        eventId: admission.event.eventId,
        nodeKey: identity.key,
        code: "identity-owner-conflict",
        message: `${identity.key} already belongs to ${owner.taskId}/${owner.runId}.`,
        expectedTaskId: owner.taskId,
        observedTaskId: admission.event.taskId,
        identityKind: identity.kind,
        identityId: identity.id,
        sequence: admission.event.sequence,
        recoverable: false,
      }))
    } else {
      this.#identityOwners.set(identity.key, {
        taskId: admission.event.taskId,
        runId: admission.event.runId,
      })
    }
    const node = nodeFromIdentity(
      this.#state,
      identity,
      admission.event.taskId,
      admission.event.runId,
    )
    this.#nodes.set(identity.key, node)
    return node
  }

  #mergeAdmission(node: MutableTraceNode, admission: TraceEventAdmission): void {
    const event = admission.event
    node.eventIds.add(event.eventId)
    if (!node.primaryEventId || event.sequence < node.sequence) node.primaryEventId = event.eventId
    node.sequence = Math.min(node.sequence, event.sequence)
    node.endSequence = Math.max(node.endSequence, event.sequence)
    if (!node.startedAt || event.createdAt < node.startedAt) node.startedAt = event.createdAt
    if (!node.endedAt || event.committedAt > node.endedAt) node.endedAt = event.committedAt
    for (const semantic of admission.semantics) node.semantics.add(semantic)
    for (const reason of admission.partialReasons) node.diagnostics.add(reason)
    for (const conflict of admission.identityConflicts) node.diagnostics.add(conflict)
    for (const [key, value] of Object.entries(admission.safeAttributes)) {
      if (!(key in node.safeAttributes)) node.safeAttributes[key] = value
    }
    node.refs = mergeRefs(node.refs, admission.refs)
    node.terminal ||= event.terminal
    node.effective ||= event.effective
    node.retryCount += admission.semantics.some((value) =>
      value === TraceSemanticKind.PROVIDER_RETRY || value === TraceSemanticKind.MCP_RECONNECT,
    ) ? 1 : 0
    node.contributionScore += semanticWeight(admission.semantics)
    node.latencyScore += eventDuration(admission)
    if (COMPLETENESS_RANK[admission.completeness] > COMPLETENESS_RANK[node.completeness]) {
      node.completeness = admission.completeness
    }
    admission.semantics.forEach((value) => node.tags.add(value))
    node.tags.add(admission.completeness)
  }

  #addEdge(input: {
    id?: string
    kind: TraceEdgeKindValue
    sourceKey: string
    targetKey: string
    sourceEventId?: string
    targetEventId?: string
    explicit: boolean
    weight?: number
    late: boolean
    missingSource?: boolean
    missingTarget?: boolean
    label: string
    evidenceEventIds: readonly string[]
    metadata: Record<string, string | number | boolean>
  }): MutableTraceEdge | undefined {
    if (input.sourceKey === input.targetKey) return undefined
    const id = input.id ?? edgeId(
      input.kind,
      input.sourceKey,
      input.targetKey,
      `${input.sourceEventId ?? ""}:${input.targetEventId ?? ""}`,
    )
    const existing = this.#edges.get(id)
    if (existing) {
      input.evidenceEventIds.forEach((eventId) => existing.evidenceEventIds.add(eventId))
      existing.weight = Math.max(existing.weight, input.weight ?? 1)
      existing.explicit ||= input.explicit
      existing.late ||= input.late
      return existing
    }
    const source = this.#nodes.get(input.sourceKey)
    const target = this.#nodes.get(input.targetKey)
    const edge: MutableTraceEdge = {
      ...input,
      id,
      weight: input.weight ?? 1,
      missingSource: input.missingSource === true || !source,
      missingTarget: input.missingTarget === true || !target,
      evidenceEventIds: new Set(input.evidenceEventIds),
      metadata: { ...input.metadata },
    }
    this.#edges.set(id, edge)
    source?.outgoingEdgeIds.add(id)
    target?.incomingEdgeIds.add(id)
    if (!source || !target) {
      const eventId = input.targetEventId ?? input.sourceEventId
      this.#quarantine.push(Object.freeze({
        id: `quarantine:${id}`,
        eventId,
        edgeId: id,
        code: !source ? "missing-edge-source" : "missing-edge-target",
        message: `${input.kind} could not resolve ${!source ? input.sourceKey : input.targetKey}.`,
        expectedTaskId: this.#taskId,
        sequence: eventId ? this.#admissions.get(eventId)?.event.sequence ?? 0 : 0,
        recoverable: true,
      }))
    }
    return edge
  }

  #connectExplicitCausation(): void {
    for (const admission of this.#admissions.values()) {
      const event = admission.event
      if (!event.causationId) continue
      const sourceKey = traceIdentityKey(TraceNodeKind.EVENT, event.causationId)
      const targetKey = traceIdentityKey(TraceNodeKind.EVENT, event.eventId)
      if (!sourceKey || !targetKey) continue
      this.#addEdge({
        kind: TraceEdgeKind.CAUSATION,
        sourceKey,
        targetKey,
        sourceEventId: event.causationId,
        targetEventId: event.eventId,
        explicit: true,
        weight: Math.max(1, eventDuration(admission)),
        late: admission.completeness === TraceCompleteness.LATE,
        missingSource: !this.#admissions.has(event.causationId),
        missingTarget: false,
        label: "caused",
        evidenceEventIds: [event.causationId, event.eventId],
        metadata: { correlationId: event.correlationId },
      })
    }
  }

  #connectCorrelations(): void {
    for (const [correlationId, admissions] of this.#eventsByCorrelation) {
      for (let index = 1; index < admissions.length; index += 1) {
        const previous = admissions[index - 1]
        const current = admissions[index]
        if (!previous || !current) continue
        if (current.event.causationId === previous.event.eventId) continue
        const sourceKey = traceIdentityKey(TraceNodeKind.EVENT, previous.event.eventId)
        const targetKey = traceIdentityKey(TraceNodeKind.EVENT, current.event.eventId)
        if (!sourceKey || !targetKey) continue
        this.#addEdge({
          kind: TraceEdgeKind.CORRELATION,
          sourceKey,
          targetKey,
          sourceEventId: previous.event.eventId,
          targetEventId: current.event.eventId,
          explicit: true,
          weight: 1,
          late: current.completeness === TraceCompleteness.LATE,
          missingSource: false,
          missingTarget: false,
          label: "same typed correlation",
          evidenceEventIds: [previous.event.eventId, current.event.eventId],
          metadata: { correlationId },
        })
      }
    }
  }

  #connectSpanHierarchy(): void {
    for (const admission of this.#admissions.values()) {
      const refs = admission.refs
      if (!refs.spanId || !refs.parentSpanId || refs.spanId === refs.parentSpanId) continue
      const sourceKey = traceIdentityKey(TraceNodeKind.SPAN, refs.parentSpanId)
      const targetKey = traceIdentityKey(TraceNodeKind.SPAN, refs.spanId)
      if (!sourceKey || !targetKey) continue
      this.#addEdge({
        kind: TraceEdgeKind.SPAN_PARENT,
        sourceKey,
        targetKey,
        sourceEventId: undefined,
        targetEventId: admission.event.eventId,
        explicit: true,
        weight: 1,
        late: admission.completeness === TraceCompleteness.LATE,
        missingSource: !this.#nodes.has(sourceKey),
        missingTarget: !this.#nodes.has(targetKey),
        label: "parent span",
        evidenceEventIds: [admission.event.eventId],
        metadata: { parentSpanId: refs.parentSpanId, spanId: refs.spanId },
      })
    }
  }

  #connectSubagentAndYield(): void {
    for (const admission of this.#admissions.values()) {
      const refs = admission.refs
      const eventKey = traceIdentityKey(TraceNodeKind.EVENT, admission.event.eventId)
      if (!eventKey) continue
      if (refs.subagentId && refs.parentToolCallId) {
        const sourceKey = traceIdentityKey(TraceNodeKind.TOOL, refs.parentToolCallId)
        const targetKey = traceIdentityKey(TraceNodeKind.SUBAGENT, refs.subagentId)
        if (sourceKey && targetKey) {
          this.#addEdge({
            kind: TraceEdgeKind.SUBAGENT_PARENT,
            sourceKey,
            targetKey,
            sourceEventId: undefined,
            targetEventId: admission.event.eventId,
            explicit: true,
            weight: 4,
            late: admission.completeness === TraceCompleteness.LATE,
            missingSource: !this.#nodes.has(sourceKey),
            missingTarget: !this.#nodes.has(targetKey),
            label: "parent tool spawned subagent",
            evidenceEventIds: [admission.event.eventId],
            metadata: {
              parentToolCallId: refs.parentToolCallId,
              subagentId: refs.subagentId,
            },
          })
        }
      }
      if (refs.subagentId && refs.yieldId) {
        const sourceKey = traceIdentityKey(TraceNodeKind.SUBAGENT, refs.subagentId)
        if (sourceKey) {
          this.#addEdge({
            kind: TraceEdgeKind.SUBAGENT_YIELD,
            sourceKey,
            targetKey: eventKey,
            sourceEventId: undefined,
            targetEventId: admission.event.eventId,
            explicit: true,
            weight: 3,
            late: admission.completeness === TraceCompleteness.LATE,
            missingSource: !this.#nodes.has(sourceKey),
            missingTarget: false,
            label: admission.event.terminal ? "final subagent yield" : "partial subagent yield",
            evidenceEventIds: [admission.event.eventId],
            metadata: { yieldId: refs.yieldId, terminal: admission.event.terminal },
          })
        }
      }
    }
  }

  #connectReplacementAndRetry(): void {
    const recoveryByFailure = new Map<string, TraceEventAdmission[]>()
    const providerRetryByProvider = new Map<string, TraceEventAdmission[]>()
    for (const admission of this.#admissions.values()) {
      if (admission.refs.failureId) {
        this.#appendGroup(recoveryByFailure, admission.refs.failureId, admission)
      }
      if (
        admission.refs.providerId &&
        admission.semantics.includes(TraceSemanticKind.PROVIDER_RETRY)
      ) {
        this.#appendGroup(providerRetryByProvider, admission.refs.providerId, admission)
      }
    }
    for (const [failureId, admissions] of recoveryByFailure) {
      admissions.sort(compareTraceAdmissions)
      const failureKey = traceIdentityKey(TraceNodeKind.FAILURE, failureId)
      if (!failureKey) continue
      for (const admission of admissions) {
        if (!admission.refs.recoveryId) continue
        const recoveryKey = traceIdentityKey(TraceNodeKind.RECOVERY, admission.refs.recoveryId)
        if (!recoveryKey) continue
        this.#addEdge({
          kind: TraceEdgeKind.RECOVERY_ATTEMPT,
          sourceKey: failureKey,
          targetKey: recoveryKey,
          sourceEventId: undefined,
          targetEventId: admission.event.eventId,
          explicit: true,
          weight: 7,
          late: admission.completeness === TraceCompleteness.LATE,
          missingSource: !this.#nodes.has(failureKey),
          missingTarget: !this.#nodes.has(recoveryKey),
          label: "recovery for failure",
          evidenceEventIds: [admission.event.eventId],
          metadata: { failureId, recoveryId: admission.refs.recoveryId },
        })
      }
    }
    for (const [providerId, admissions] of providerRetryByProvider) {
      admissions.sort(compareTraceAdmissions)
      for (let index = 1; index < admissions.length; index += 1) {
        const previous = admissions[index - 1]
        const current = admissions[index]
        if (!previous || !current) continue
        const sourceKey = traceIdentityKey(TraceNodeKind.EVENT, previous.event.eventId)
        const targetKey = traceIdentityKey(TraceNodeKind.EVENT, current.event.eventId)
        if (!sourceKey || !targetKey) continue
        this.#addEdge({
          kind: TraceEdgeKind.REPLACEMENT,
          sourceKey,
          targetKey,
          sourceEventId: previous.event.eventId,
          targetEventId: current.event.eventId,
          explicit: true,
          weight: 8,
          late: current.completeness === TraceCompleteness.LATE,
          missingSource: false,
          missingTarget: false,
          label: "provider retry",
          evidenceEventIds: [previous.event.eventId, current.event.eventId],
          metadata: { providerId, retryOrdinal: index },
        })
      }
    }
  }

  #connectStableSequence(
    groups: ReadonlyMap<string, readonly TraceEventAdmission[]>,
    dimension: "worker" | "session",
  ): void {
    for (const [identity, admissions] of groups) {
      for (let index = 1; index < admissions.length; index += 1) {
        const previous = admissions[index - 1]
        const current = admissions[index]
        if (!previous || !current) continue
        if (current.event.causationId === previous.event.eventId) continue
        if (current.event.correlationId === previous.event.correlationId) continue
        const sourceKey = traceIdentityKey(TraceNodeKind.EVENT, previous.event.eventId)
        const targetKey = traceIdentityKey(TraceNodeKind.EVENT, current.event.eventId)
        if (!sourceKey || !targetKey) continue
        this.#addEdge({
          kind: TraceEdgeKind.SEQUENCE,
          sourceKey,
          targetKey,
          sourceEventId: previous.event.eventId,
          targetEventId: current.event.eventId,
          explicit: false,
          weight: 0.5,
          late: current.completeness === TraceCompleteness.LATE,
          missingSource: false,
          missingTarget: false,
          label: `next ${dimension} event`,
          evidenceEventIds: [previous.event.eventId, current.event.eventId],
          metadata: { dimension, identity },
        })
      }
    }
  }

  #connectArtifactProducers(): void {
    for (const artifact of Object.values(this.#state.artifacts)) {
      if (artifact.taskId !== this.#taskId) continue
      const artifactKey = traceIdentityKey(TraceNodeKind.ARTIFACT, artifact.id)
      const eventKey = traceIdentityKey(TraceNodeKind.EVENT, artifact.producerEventId)
      if (!artifactKey || !eventKey) continue
      this.#addEdge({
        kind: TraceEdgeKind.ARTIFACT_PRODUCED,
        sourceKey: eventKey,
        targetKey: artifactKey,
        sourceEventId: artifact.producerEventId,
        targetEventId: undefined,
        explicit: true,
        weight: 2,
        late: false,
        missingSource: !this.#nodes.has(eventKey),
        missingTarget: !this.#nodes.has(artifactKey),
        label: "produced artifact",
        evidenceEventIds: [artifact.producerEventId],
        metadata: {
          artifactId: artifact.id,
          mediaType: artifact.mediaType,
          version: artifact.version,
        },
      })
    }
  }

  #assignHierarchy(): void {
    for (const node of this.#nodes.values()) {
      if (node.identity.kind === TraceNodeKind.TASK) continue
      let parentKey: string | undefined
      if (node.identity.kind === TraceNodeKind.RUN) {
        parentKey = traceIdentityKey(TraceNodeKind.TASK, node.taskId)
      } else if (node.identity.kind === TraceNodeKind.SESSION) {
        parentKey = traceIdentityKey(TraceNodeKind.RUN, node.runId)
      } else if (node.identity.kind === TraceNodeKind.EVENT) {
        parentKey =
          traceIdentityKey(TraceNodeKind.SPAN, node.refs.spanId) ??
          traceIdentityKey(TraceNodeKind.WORKER, node.refs.workerId) ??
          traceIdentityKey(TraceNodeKind.SESSION, node.refs.sessionId) ??
          traceIdentityKey(TraceNodeKind.RUN, node.runId)
      } else if (node.identity.kind === TraceNodeKind.ISSUE) {
        parentKey = node.primaryEventId
          ? traceIdentityKey(TraceNodeKind.EVENT, node.primaryEventId)
          : traceIdentityKey(TraceNodeKind.RUN, node.runId)
      } else if (node.identity.kind === TraceNodeKind.SPAN && node.refs.parentSpanId) {
        parentKey = traceIdentityKey(TraceNodeKind.SPAN, node.refs.parentSpanId)
      } else {
        parentKey =
          traceIdentityKey(TraceNodeKind.WORKER, node.refs.workerId) ??
          traceIdentityKey(TraceNodeKind.SESSION, node.refs.sessionId) ??
          traceIdentityKey(TraceNodeKind.RUN, node.runId)
      }
      if (!parentKey || parentKey === node.identity.key || !this.#nodes.has(parentKey)) continue
      node.parentKey = parentKey
      this.#nodes.get(parentKey)?.childKeys.add(node.identity.key)
    }
  }

  #finalize(timeline: WorkerCausalTimelineProjection): TraceBaseProjection {
    const admissionByEvent = Object.freeze(Object.fromEntries(this.#admissions))
    const mutableNodes = [...this.#nodes.values()]
    mutableNodes.sort((left, right) =>
      left.sequence - right.sequence ||
      left.endSequence - right.endSequence ||
      left.identity.key.localeCompare(right.identity.key),
    )
    const nodes = Object.freeze(mutableNodes.map((node) => this.#freezeNode(node, admissionByEvent)))
    const edges = Object.freeze(
      [...this.#edges.values()]
        .sort((left, right) =>
          left.sourceKey.localeCompare(right.sourceKey) ||
          left.targetKey.localeCompare(right.targetKey) ||
          left.kind.localeCompare(right.kind),
        )
        .map((edge) => this.#freezeEdge(edge, admissionByEvent)),
    )
    const nodesByKey = Object.freeze(Object.fromEntries(nodes.map((node) => [node.key, node])))
    const edgesById = Object.freeze(Object.fromEntries(edges.map((edge) => [edge.id, edge])))
    const hierarchy = buildHierarchy(nodesByKey, this.#options.maximumClosureDepth)
    const indexes = buildIndexes(this.#state, nodes, edges)
    const diagnostics = buildDiagnostics(
      this.#state,
      this.#taskId,
      timeline,
      nodes,
      edges,
      this.#quarantine,
    )
    return Object.freeze({
      state: this.#state,
      taskId: this.#taskId,
      timeline,
      nodes,
      edges,
      nodesByKey,
      edgesById,
      admissionByEvent,
      hierarchy,
      indexes,
      quarantine: Object.freeze([...this.#quarantine]),
      diagnostics,
    })
  }

  #freezeNode(
    node: MutableTraceNode,
    admissionByEvent: Readonly<Record<string, TraceEventAdmission>>,
  ): TraceNode {
    const eventIds = [...node.eventIds].sort((left, right) =>
      compareEventIds(admissionByEvent, left, right),
    )
    const started = node.startedAt ? Date.parse(node.startedAt) : NaN
    const ended = node.endedAt ? Date.parse(node.endedAt) : NaN
    const duration = Number.isFinite(started) && Number.isFinite(ended)
      ? Math.max(0, ended - started)
      : node.latencyScore
    return Object.freeze({
      key: node.identity.key,
      identity: node.identity,
      taskId: node.taskId,
      runId: node.runId,
      eventIds: Object.freeze(eventIds),
      primaryEventId: node.primaryEventId ?? eventIds[0],
      parentKey: node.parentKey,
      childKeys: Object.freeze([...node.childKeys].sort()),
      incomingEdgeIds: Object.freeze([...node.incomingEdgeIds].sort()),
      outgoingEdgeIds: Object.freeze([...node.outgoingEdgeIds].sort()),
      sequence: node.sequence === Number.MAX_SAFE_INTEGER ? 0 : node.sequence,
      endSequence: node.endSequence,
      startedAt: node.startedAt,
      endedAt: node.endedAt,
      durationMs: duration,
      title: node.title,
      summary: node.summary,
      semantics: Object.freeze([...node.semantics].sort()),
      completeness: node.completeness,
      terminal: node.terminal,
      effective: node.effective,
      critical: false,
      retryCount: node.retryCount,
      contributionScore: node.contributionScore,
      latencyScore: node.latencyScore,
      costScore: node.costScore,
      refs: node.refs,
      tags: Object.freeze([...node.tags].sort()),
      diagnostics: Object.freeze([...node.diagnostics].sort()),
      safeAttributes: Object.freeze({ ...node.safeAttributes }),
    })
  }

  #freezeEdge(
    edge: MutableTraceEdge,
    admissionByEvent: Readonly<Record<string, TraceEventAdmission>>,
  ): TraceEdge {
    return Object.freeze({
      id: edge.id,
      kind: edge.kind,
      sourceKey: edge.sourceKey,
      targetKey: edge.targetKey,
      sourceEventId: edge.sourceEventId,
      targetEventId: edge.targetEventId,
      explicit: edge.explicit,
      weight: edge.weight,
      critical: false,
      late: edge.late,
      missingSource: edge.missingSource,
      missingTarget: edge.missingTarget,
      label: edge.label,
      evidenceEventIds: Object.freeze(
        [...edge.evidenceEventIds].sort((left, right) =>
          compareEventIds(admissionByEvent, left, right),
        ),
      ),
      metadata: Object.freeze({ ...edge.metadata }),
    })
  }
}

function buildHierarchy(
  nodesByKey: Readonly<Record<string, TraceNode>>,
  maximumDepth: number,
): TraceHierarchy {
  const childrenByKey: Record<string, readonly string[]> = {}
  const parentByKey: Record<string, string> = {}
  for (const node of Object.values(nodesByKey)) {
    childrenByKey[node.key] = node.childKeys
    if (node.parentKey && nodesByKey[node.parentKey]) parentByKey[node.key] = node.parentKey
  }
  const roots = Object.values(nodesByKey)
    .filter((node) => !parentByKey[node.key])
    .map((node) => node.key)
    .sort((left, right) =>
      (nodesByKey[left]?.sequence ?? 0) - (nodesByKey[right]?.sequence ?? 0) || left.localeCompare(right),
    )
  const depthByKey: Record<string, number> = {}
  const ancestorsByKey: Record<string, readonly string[]> = {}
  const descendantsByKey: Record<string, readonly string[]> = {}
  const visiting = new Set<string>()
  const resolveAncestors = (key: string): readonly string[] => {
    if (ancestorsByKey[key]) return ancestorsByKey[key]
    if (visiting.has(key)) return Object.freeze([])
    visiting.add(key)
    const ancestors: string[] = []
    let current = parentByKey[key]
    while (current && ancestors.length < maximumDepth && !ancestors.includes(current)) {
      ancestors.push(current)
      current = parentByKey[current]
    }
    visiting.delete(key)
    depthByKey[key] = ancestors.length
    ancestorsByKey[key] = Object.freeze(ancestors)
    return ancestorsByKey[key]
  }
  for (const key of Object.keys(nodesByKey)) resolveAncestors(key)
  for (const key of Object.keys(nodesByKey)) {
    const descendants: string[] = []
    const queue = [...(childrenByKey[key] ?? [])]
    const seen = new Set<string>()
    while (queue.length && descendants.length < maximumDepth * 64) {
      const current = queue.shift()
      if (!current || seen.has(current)) continue
      seen.add(current)
      descendants.push(current)
      queue.push(...(childrenByKey[current] ?? []))
    }
    descendantsByKey[key] = Object.freeze(descendants)
  }
  return Object.freeze({
    rootKeys: Object.freeze(roots),
    childrenByKey: Object.freeze(childrenByKey),
    parentByKey: Object.freeze(parentByKey),
    depthByKey: Object.freeze(depthByKey),
    ancestorsByKey: Object.freeze(ancestorsByKey),
    descendantsByKey: Object.freeze(descendantsByKey),
  })
}

function pushIndex(
  index: Record<string, string[]>,
  key: string | undefined,
  value: string,
): void {
  if (!key) return
  const values = index[key]
  if (values) {
    if (!values.includes(value)) values.push(value)
  } else index[key] = [value]
}

function freezeIndex(index: Record<string, string[]>): Readonly<Record<string, readonly string[]>> {
  for (const values of Object.values(index)) values.sort()
  return Object.freeze(index)
}

function buildIndexes(
  state: CanonicalProjectionState,
  nodes: readonly TraceNode[],
  edges: readonly TraceEdge[],
): TraceIndexes {
  const byEvent: Record<string, string[]> = {}
  const bySpan: Record<string, string[]> = {}
  const byWorker: Record<string, string[]> = {}
  const byTool: Record<string, string[]> = {}
  const byArtifact: Record<string, string[]> = {}
  const byMutation: Record<string, string[]> = {}
  const byCheckpoint: Record<string, string[]> = {}
  const byFailure: Record<string, string[]> = {}
  const byRecovery: Record<string, string[]> = {}
  const bySemantic: Record<string, string[]> = {}
  const edgesByEvent: Record<string, string[]> = {}
  const eventIdByMutation: Record<string, string> = {}
  const producerEventIdsByArtifact: Record<string, string[]> = {}
  for (const node of nodes) {
    node.eventIds.forEach((eventId) => pushIndex(byEvent, eventId, node.key))
    pushIndex(bySpan, node.refs.spanId, node.key)
    pushIndex(byWorker, node.refs.workerId, node.key)
    pushIndex(byTool, node.refs.toolCallId, node.key)
    node.refs.artifactIds.forEach((artifactId) => pushIndex(byArtifact, artifactId, node.key))
    pushIndex(byMutation, node.refs.mutationId, node.key)
    pushIndex(byCheckpoint, node.refs.checkpointId, node.key)
    pushIndex(byFailure, node.refs.failureId, node.key)
    pushIndex(byRecovery, node.refs.recoveryId, node.key)
    node.semantics.forEach((semantic) => pushIndex(bySemantic, semantic, node.key))
  }
  for (const edge of edges) {
    edge.evidenceEventIds.forEach((eventId) => pushIndex(edgesByEvent, eventId, edge.id))
  }
  for (const mutation of Object.values(state.mutations)) {
    eventIdByMutation[mutation.id] = mutation.eventId
  }
  for (const artifact of Object.values(state.artifacts)) {
    pushIndex(producerEventIdsByArtifact, artifact.id, artifact.producerEventId)
  }
  return Object.freeze({
    nodeKeysByEvent: freezeIndex(byEvent),
    nodeKeysBySpan: freezeIndex(bySpan),
    nodeKeysByWorker: freezeIndex(byWorker),
    nodeKeysByToolCall: freezeIndex(byTool),
    nodeKeysByArtifact: freezeIndex(byArtifact),
    nodeKeysByMutation: freezeIndex(byMutation),
    nodeKeysByCheckpoint: freezeIndex(byCheckpoint),
    nodeKeysByFailure: freezeIndex(byFailure),
    nodeKeysByRecovery: freezeIndex(byRecovery),
    nodeKeysBySemantic: freezeIndex(bySemantic),
    edgeIdsByEvent: freezeIndex(edgesByEvent),
    eventIdByMutation: Object.freeze(eventIdByMutation),
    producerEventIdsByArtifact: freezeIndex(producerEventIdsByArtifact),
  })
}

function buildDiagnostics(
  state: CanonicalProjectionState,
  taskId: string,
  timeline: WorkerCausalTimelineProjection,
  nodes: readonly TraceNode[],
  edges: readonly TraceEdge[],
  quarantine: readonly TraceQuarantineRecord[],
): TraceDiagnostics {
  const cursor = state.cursors[taskId]
  const runtime = state.runtimes[taskId]
  const committedSequence = cursor?.committedSequence ?? runtime?.lastSequence ?? 0
  const highWatermark = cursor?.highWatermark ?? committedSequence
  const lag = Math.max(0, highWatermark - committedSequence)
  const partialCount = nodes.filter((node) => node.completeness === TraceCompleteness.PARTIAL).length
  const lateCount = nodes.filter((node) => node.completeness === TraceCompleteness.LATE).length
  const orphanCount = nodes.filter((node) => node.completeness === TraceCompleteness.ORPHAN).length
  const missingSpanCount = nodes.filter((node) => node.diagnostics.includes("missing-span")).length
  const missingArtifactCount = nodes.reduce(
    (total, node) => total + node.diagnostics.filter((value) => value.startsWith("missing-artifact:")).length,
    0,
  )
  const identityConflictCount = quarantine.filter((record) =>
    record.code === "identity-conflict" || record.code === "identity-owner-conflict",
  ).length
  const warnings = [...timeline.diagnostics.warnings]
  if (quarantine.length) warnings.push(`${quarantine.length} trace record(s) are quarantined.`)
  if (missingSpanCount) warnings.push(`${missingSpanCount} trace node(s) lack a typed span identity.`)
  if (missingArtifactCount) warnings.push(`${missingArtifactCount} artifact reference(s) arrived without a retained artifact.`)
  if (lag) warnings.push(`${lag} canonical event(s) remain behind the high watermark.`)
  return Object.freeze({
    ready: Boolean(state.tasks[taskId] && (cursor?.snapshotComplete ?? true) && lag === 0),
    disabled: false,
    projectionRevision: state.revision,
    committedSequence,
    highWatermark,
    lag,
    eventCount: timeline.events.length,
    nodeCount: nodes.length,
    edgeCount: edges.length,
    partialCount,
    lateCount,
    orphanCount,
    quarantineCount: quarantine.length,
    missingSpanCount,
    missingArtifactCount,
    identityConflictCount,
    cycleCount: timeline.graph.cycleEventIds.length,
    warnings: Object.freeze(unique(warnings)),
  })
}

export function buildTraceBase(
  state: CanonicalProjectionState,
  taskId: string,
  options: Required<TraceProjectionOptions>,
): TraceBaseProjection {
  return new TraceGraphBuilder(state, taskId, options).build()
}

export function artifactsForTraceNode(
  state: CanonicalProjectionState,
  node: TraceNode,
): readonly ArtifactProjection[] {
  return Object.freeze(
    node.refs.artifactIds
      .map((artifactId) => state.artifacts[artifactId])
      .filter((artifact): artifact is ArtifactProjection => Boolean(artifact)),
  )
}
