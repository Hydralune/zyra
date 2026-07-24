import type { CanonicalProjectionState } from "../../../state/contracts.ts"
import type {
  TimelineDrilldownTarget,
  TimelineEntityKind,
  TimelineEventFacts,
  TimelineEvidenceRef,
} from "./contracts.ts"

function safeDomToken(value: string): string {
  return value.replace(/[^\w-]+/g, "-")
}

function target(
  kind: TimelineEntityKind,
  entityId: string,
  label: string,
  selector: string,
  available: boolean,
  facts: TimelineEventFacts,
  extra: Partial<TimelineDrilldownTarget> = {},
): TimelineDrilldownTarget {
  return Object.freeze({
    id: `${kind}:${entityId}:${facts.event.eventId}`,
    kind,
    entityId,
    label,
    selector,
    available,
    eventId: facts.event.eventId,
    workerId: facts.event.workerId ?? facts.worker?.id,
    nodeId: facts.event.nodeId ?? facts.worker?.nodeId,
    ...extra,
  })
}

function evidence(
  kind: TimelineEntityKind,
  id: string,
  label: string,
  eventIds: readonly string[],
  options: {
    missing?: boolean
    terminal?: boolean
    status?: string
    metadata?: Record<string, string | number | boolean>
  } = {},
): TimelineEvidenceRef {
  return Object.freeze({
    kind,
    id,
    label,
    eventIds: Object.freeze([...new Set(eventIds)]),
    missing: options.missing ?? false,
    terminal: options.terminal ?? false,
    status: options.status,
    metadata: Object.freeze({ ...(options.metadata ?? {}) }),
  })
}

function eventEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef {
  return evidence(
    "event",
    facts.event.eventId,
    facts.event.eventType,
    [facts.event.eventId],
    {
      terminal: facts.event.terminal,
      status: facts.phase,
      metadata: {
        sequence: facts.event.sequence,
        aggregateSequence: facts.event.aggregateSequence,
        effective: facts.event.effective,
      },
    },
  )
}

function workerEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.event.workerId ?? facts.worker?.id
  if (!id) return undefined
  const eventIds = state.causality.byWorker[id] ?? [facts.event.eventId]
  return evidence("worker", id, facts.worker?.title ?? id, eventIds, {
    missing: !facts.worker,
    terminal: facts.worker?.terminal,
    status: facts.worker?.lifecycle,
    metadata: {
      leaseId: facts.leaseId ?? "",
      routeId: facts.routeId ?? "",
      placementId: facts.placementId ?? "",
    },
  })
}

function toolEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.event.toolCallId ?? facts.tool?.id
  if (!id) return undefined
  const eventIds = state.causality.byToolCall[id] ?? [facts.event.eventId]
  return evidence("tool", id, facts.tool?.toolName ?? id, eventIds, {
    missing: !facts.tool,
    terminal: facts.tool?.terminal,
    status: facts.tool?.lifecycle,
    metadata: {
      durationMs: facts.tool?.durationMs ?? 0,
      errorCode: facts.tool?.errorCode ?? "",
    },
  })
}

function permissionEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.permissionId
  if (!id) return undefined
  const permission = state.permissions[id]
  const ids = state.causality.eventOrder.filter((eventId) => {
    const event = state.causality.byEvent[eventId]
    return event?.entityRefs.includes(`permission:${id}`)
  })
  return evidence(
    "permission",
    id,
    permission?.toolName ?? permission?.permissionKind ?? id,
    ids.length ? ids : [facts.event.eventId],
    {
      missing: !permission,
      terminal: permission?.terminal,
      status: permission?.decision ?? permission?.lifecycle,
      metadata: {
        resolved: Boolean(permission?.resolvedAt),
        policyRevision: permission?.policyRevision ?? "",
      },
    },
  )
}

function artifactEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): readonly TimelineEvidenceRef[] {
  const ids = new Set([
    ...facts.event.artifactIds,
    ...facts.artifacts.map((artifact) => artifact.id),
  ])
  return Object.freeze(
    [...ids].map((id) => {
      const artifact = state.artifacts[id]
      const eventIds = state.causality.byArtifact[id] ?? []
      return evidence(
        "artifact",
        id,
        artifact?.title ?? artifact?.mediaType ?? id,
        eventIds.length ? eventIds : [facts.event.eventId],
        {
          missing: !artifact,
          terminal: artifact?.terminal,
          status: artifact?.deleted ? "deleted" : artifact?.lifecycle,
          metadata: {
            version: artifact?.version ?? 0,
            sizeBytes: artifact?.sizeBytes ?? 0,
            digest: artifact?.digest ?? "",
          },
        },
      )
    }),
  )
}

function failureEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.failureId
  if (!id) return undefined
  const eventIds = state.causality.byFailure[id] ?? [facts.event.eventId]
  return evidence("failure", id, `Failure ${id}`, eventIds, {
    missing: eventIds.length === 0,
    terminal: facts.event.terminal,
    status: facts.phase,
    metadata: {
      recoveryId: facts.recoveryId ?? "",
      workerId: facts.event.workerId ?? facts.worker?.id ?? "",
    },
  })
}

function recoveryEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.recoveryId ?? facts.recovery?.id
  if (!id) return undefined
  const recovery = state.recoveries[id]
  const eventIds = state.causality.byRecovery[id] ?? [facts.event.eventId]
  return evidence(
    "recovery",
    id,
    recovery?.strategy ? `Recovery: ${recovery.strategy}` : `Recovery ${id}`,
    eventIds,
    {
      missing: !recovery,
      terminal: recovery?.terminal,
      status: recovery?.lifecycle,
      metadata: {
        attempt: recovery?.attempt ?? 0,
        failureId: recovery?.failureId ?? facts.failureId ?? "",
        replacementWorkerId: recovery?.replacementWorkerId ?? "",
      },
    },
  )
}

function mutationEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef {
  const id = facts.event.mutationId
  const mutation = state.mutations[id]
  const eventIds = state.causality.byMutation[id] ?? [facts.event.eventId]
  return evidence(
    "mutation",
    id,
    mutation ? `${mutation.domain}.${mutation.operation}` : id,
    eventIds,
    {
      missing: !mutation,
      terminal: true,
      status: mutation?.effective ? "effective" : "non-effective",
      metadata: {
        sequence: mutation?.sequence ?? facts.event.sequence,
        path: mutation?.path.join(".") ?? "",
      },
    },
  )
}

function routeEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): readonly TimelineEvidenceRef[] {
  const result: TimelineEvidenceRef[] = []
  if (facts.routeId) {
    const eventIds = state.causality.eventOrder.filter((eventId) => {
      const event = state.causality.byEvent[eventId]
      if (!event || event.taskId !== facts.event.taskId) return false
      if (event.entityRefs.includes(`route:${facts.routeId}`)) return true
      const mutation = state.mutations[event.mutationId]
      return (
        mutation?.entityRefs.includes(`route:${facts.routeId}`) ?? false
      )
    })
    result.push(
      evidence("route", facts.routeId, `Route ${facts.routeId}`, eventIds, {
        missing: eventIds.length === 0,
        terminal: facts.scheduler?.terminal,
        status: facts.scheduler?.lifecycle,
        metadata: {
          placementId: facts.placementId ?? "",
          modelId: facts.scheduler?.modelId ?? "",
          providerId: facts.scheduler?.providerId ?? "",
        },
      }),
    )
  }
  if (facts.placementId) {
    const eventIds = state.causality.eventOrder.filter((eventId) => {
      const event = state.causality.byEvent[eventId]
      return event?.entityRefs.includes(`placement:${facts.placementId}`)
    })
    result.push(
      evidence(
        "placement",
        facts.placementId,
        `Placement ${facts.placementId}`,
        eventIds,
        {
          missing: eventIds.length === 0,
          terminal: facts.scheduler?.terminal,
          status: facts.scheduler?.lifecycle,
          metadata: {
            resourceClass: facts.scheduler?.resourceClass ?? "",
            privacyClass: facts.scheduler?.privacyClass ?? "",
          },
        },
      ),
    )
  }
  return Object.freeze(result)
}

function checkpointEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.event.checkpointId
  if (!id) return undefined
  const eventIds = state.causality.byCheckpoint[id] ?? [facts.event.eventId]
  return evidence("checkpoint", id, `Checkpoint ${id}`, eventIds, {
    missing: eventIds.length === 0,
    terminal: facts.event.terminal,
    status: facts.phase,
  })
}

function commandEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): TimelineEvidenceRef | undefined {
  const id = facts.event.controlCommandId ?? facts.command?.id
  if (!id) return undefined
  const command = state.commands[id]
  const eventIds = state.causality.byControlCommand[id] ?? [
    facts.event.eventId,
  ]
  return evidence(
    "command",
    id,
    command?.commandName ?? `Command ${id}`,
    eventIds,
    {
      missing: !command,
      terminal: command?.terminal,
      status: command?.lifecycle,
      metadata: {
        priority: command?.priority ?? "",
        receiptId: command?.receiptId ?? "",
      },
    },
  )
}

export function buildTimelineEvidence(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): readonly TimelineEvidenceRef[] {
  const result: TimelineEvidenceRef[] = [eventEvidence(state, facts)]
  const optional = [
    workerEvidence(state, facts),
    toolEvidence(state, facts),
    permissionEvidence(state, facts),
    failureEvidence(state, facts),
    recoveryEvidence(state, facts),
    checkpointEvidence(state, facts),
    commandEvidence(state, facts),
  ]
  for (const item of optional) {
    if (item) result.push(item)
  }
  result.push(...artifactEvidence(state, facts))
  result.push(mutationEvidence(state, facts))
  result.push(...routeEvidence(state, facts))
  for (const missing of facts.missingRefs) {
    const split = missing.indexOf(":")
    const kind = (
      split > 0 ? missing.slice(0, split) : "event"
    ) as TimelineEntityKind
    const id = split > 0 ? missing.slice(split + 1) : missing
    if (
      result.some(
        (candidate) => candidate.kind === kind && candidate.id === id,
      )
    ) {
      continue
    }
    result.push(
      evidence(kind, id, `Missing ${kind} ${id}`, [], {
        missing: true,
      }),
    )
  }
  return Object.freeze(
    result.sort(
      (left, right) =>
        Number(left.missing) - Number(right.missing) ||
        left.kind.localeCompare(right.kind) ||
        left.id.localeCompare(right.id),
    ),
  )
}

export function buildTimelineDrilldowns(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
): readonly TimelineDrilldownTarget[] {
  const result: TimelineDrilldownTarget[] = []
  result.push(
    target(
      "event",
      facts.event.eventId,
      `Event ${facts.event.eventType}`,
      `[data-event-id="${facts.event.eventId}"]`,
      true,
      facts,
    ),
  )
  const workerId = facts.event.workerId ?? facts.worker?.id
  if (workerId) {
    result.push(
      target(
        "worker",
        workerId,
        `Worker ${facts.worker?.title ?? workerId}`,
        `[data-topology-entity-id="${workerId}"]`,
        Boolean(state.workers[workerId]),
        facts,
        { workerId },
      ),
    )
  }
  const nodeId = facts.event.nodeId ?? facts.worker?.nodeId
  if (nodeId) {
    result.push(
      target(
        "node",
        nodeId,
        `Topology node ${nodeId}`,
        `[data-topology-entity-id="${nodeId}"]`,
        Boolean(state.nodes[nodeId]),
        facts,
        { nodeId },
      ),
    )
  }
  const toolCallId = facts.event.toolCallId ?? facts.tool?.id
  if (toolCallId) {
    result.push(
      target(
        "tool",
        toolCallId,
        `Tool ${facts.tool?.toolName ?? toolCallId}`,
        `[data-tool-call-id="${toolCallId}"]`,
        Boolean(state.tools[toolCallId]),
        facts,
        { toolCallId },
      ),
    )
  }
  if (facts.permissionId) {
    result.push(
      target(
        "permission",
        facts.permissionId,
        `Permission ${facts.permissionId}`,
        `[data-permission-id="${facts.permissionId}"]`,
        Boolean(state.permissions[facts.permissionId]),
        facts,
        { permissionId: facts.permissionId },
      ),
    )
  }
  for (const artifact of facts.artifacts) {
    result.push(
      target(
        "artifact",
        artifact.id,
        `Artifact ${artifact.title ?? artifact.id}`,
        `[data-artifact-id="${artifact.id}"]`,
        true,
        facts,
        { artifactId: artifact.id },
      ),
    )
  }
  for (const artifactId of facts.event.artifactIds) {
    if (result.some((candidate) => candidate.artifactId === artifactId)) continue
    result.push(
      target(
        "artifact",
        artifactId,
        `Artifact ${artifactId}`,
        `[data-artifact-id="${artifactId}"]`,
        Boolean(state.artifacts[artifactId]),
        facts,
        { artifactId },
      ),
    )
  }
  if (facts.failureId) {
    result.push(
      target(
        "failure",
        facts.failureId,
        `Failure ${facts.failureId}`,
        `[data-failure-id="${facts.failureId}"]`,
        (state.causality.byFailure[facts.failureId] ?? []).length > 0,
        facts,
        { failureId: facts.failureId },
      ),
    )
  }
  if (facts.recoveryId) {
    result.push(
      target(
        "recovery",
        facts.recoveryId,
        `Recovery ${facts.recoveryId}`,
        `[data-recovery-id="${facts.recoveryId}"]`,
        Boolean(state.recoveries[facts.recoveryId]),
        facts,
        { recoveryId: facts.recoveryId },
      ),
    )
  }
  if (facts.routeId) {
    result.push(
      target(
        "route",
        facts.routeId,
        `Route ${facts.routeId}`,
        `[data-topology-route-id="${facts.routeId}"]`,
        true,
        facts,
        { routeId: facts.routeId },
      ),
    )
  }
  if (facts.event.spanId) {
    result.push(
      target(
        "span",
        facts.event.spanId,
        `Span ${facts.event.spanId}`,
        `[data-span-id="${facts.event.spanId}"]`,
        (state.causality.bySpan[facts.event.spanId] ?? []).length > 0,
        facts,
      ),
    )
  }
  result.push(
    target(
      "correlation",
      facts.event.correlationId,
      `Correlation ${facts.event.correlationId}`,
      `[data-correlation-id="${facts.event.correlationId}"]`,
      (state.causality.byCorrelation[facts.event.correlationId] ?? []).length >
        0,
      facts,
    ),
  )
  return Object.freeze(
    result
      .filter(
        (candidate, index) =>
          result.findIndex(
            (other) =>
              other.kind === candidate.kind &&
              other.entityId === candidate.entityId,
          ) === index,
      )
      .map((candidate) =>
        Object.freeze({
          ...candidate,
          selector: candidate.selector.replace(
            candidate.entityId,
            safeDomToken(candidate.entityId) === candidate.entityId
              ? candidate.entityId
              : candidate.entityId,
          ),
        }),
      ),
  )
}
