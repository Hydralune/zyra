import type { MemoryConsoleProjection } from "../memory/projection.ts"
import type { PlacementConsoleProjection } from "../placement/projection.ts"
import type { ProviderConsoleProjection } from "../providers/projection.ts"
import type { SessionConsoleProjection } from "./projection.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  text,
  unique,
} from "./value.ts"

export type ConsoleLinkKind =
  | "session"
  | "checkpoint"
  | "command"
  | "event"
  | "memory"
  | "artifact"
  | "provider"
  | "model"
  | "route"
  | "placement"
  | "worker"
  | "node"
  | "recovery"

export interface ConsoleLink {
  id: string
  kind: ConsoleLinkKind
  targetId: string
  label: string
  panel: "session" | "timeline" | "topology" | "memory" | "provider" | "placement" | "artifact" | "command"
  sequence: number
  priority: number
  correlationId?: string
  sourceIds: readonly string[]
  warnings: readonly string[]
}

export interface ConsoleLinkGraph {
  taskId: string
  links: readonly ConsoleLink[]
  byKind: Readonly<Record<string, readonly ConsoleLink[]>>
  byTarget: Readonly<Record<string, readonly ConsoleLink[]>>
  bySource: Readonly<Record<string, readonly ConsoleLink[]>>
  timelineEventIds: readonly string[]
  topologyNodeIds: readonly string[]
  topologyWorkerIds: readonly string[]
  artifactIds: readonly string[]
  commandIds: readonly string[]
  checkpointIds: readonly string[]
  orphanSourceIds: readonly string[]
  fingerprint: string
}

export function buildConsoleLinkGraph(input: {
  session: SessionConsoleProjection
  memory: MemoryConsoleProjection
  providers: ProviderConsoleProjection
  placement: PlacementConsoleProjection
}): ConsoleLinkGraph {
  const links: ConsoleLink[] = []
  for (const lineage of input.session.lineage) {
    links.push(link({
      kind: "session",
      targetId: lineage.id,
      label: lineage.active ? `Active session ${lineage.id}` : `Session ${lineage.id}`,
      panel: "session",
      sequence: lineage.sequence,
      priority: lineage.active ? 100 : 40,
      sourceIds: [
        lineage.id,
        ...lineage.eventIds,
        ...lineage.commandIds,
        ...lineage.workerIds,
      ],
      warnings: [
        lineage.orphan ? "orphan lineage" : "",
        lineage.cyclic ? "cyclic lineage" : "",
      ].filter(Boolean),
    }))
    for (const workerId of lineage.workerIds) {
      links.push(link({
        kind: "worker",
        targetId: workerId,
        label: `Session worker ${workerId}`,
        panel: "topology",
        sequence: lineage.sequence,
        priority: lineage.active ? 80 : 30,
        sourceIds: [lineage.id],
      }))
    }
  }
  for (const checkpoint of input.session.checkpoints) {
    links.push(link({
      kind: "checkpoint",
      targetId: checkpoint.id,
      label: `${checkpoint.disposition} checkpoint ${checkpoint.id}`,
      panel: "session",
      sequence: checkpoint.sequence,
      priority: checkpoint.disposition === "conflict" ? 100 : checkpoint.disposition === "stale" ? 90 : 60,
      correlationId: checkpoint.correlationId,
      sourceIds: [
        checkpoint.eventId ?? "",
        checkpoint.commandId ?? "",
        checkpoint.recoveryId ?? "",
      ].filter(Boolean),
      warnings: [
        checkpoint.conflictReason ?? "",
        checkpoint.staleReason ?? "",
        checkpoint.pendingWrites > checkpoint.committedWrites ? "pending writes exceed committed writes" : "",
      ].filter(Boolean),
    }))
    if (checkpoint.eventId) {
      links.push(link({
        kind: "event",
        targetId: checkpoint.eventId,
        label: `Checkpoint event ${checkpoint.eventId}`,
        panel: "timeline",
        sequence: checkpoint.sequence,
        priority: 70,
        correlationId: checkpoint.correlationId,
        sourceIds: [checkpoint.id],
      }))
    }
    if (checkpoint.commandId) {
      links.push(link({
        kind: "command",
        targetId: checkpoint.commandId,
        label: `Checkpoint command ${checkpoint.commandId}`,
        panel: "command",
        sequence: checkpoint.sequence,
        priority: 60,
        correlationId: checkpoint.correlationId,
        sourceIds: [checkpoint.id],
      }))
    }
  }
  for (const row of input.memory.rows) {
    links.push(link({
      kind: "memory",
      targetId: row.id,
      label: row.title,
      panel: "memory",
      sequence: row.sequence,
      priority: Math.round(row.combinedScore * 100),
      correlationId: row.evidence[0]?.correlationId,
      sourceIds: [
        ...row.sourceEventIds,
        ...row.sourceArtifactIds,
        ...row.sourceMemoryIds,
      ],
      warnings: [
        row.veracity === "contested" ? "contested memory" : "",
        row.provenanceScore === 0 ? "missing provenance" : "",
        row.staleReason ?? "",
      ].filter(Boolean),
    }))
    for (const eventId of row.sourceEventIds) {
      links.push(link({
        kind: "event",
        targetId: eventId,
        label: `Memory evidence event ${eventId}`,
        panel: "timeline",
        sequence: row.sequence,
        priority: 50,
        sourceIds: [row.id],
      }))
    }
    for (const artifactId of row.sourceArtifactIds) {
      links.push(link({
        kind: "artifact",
        targetId: artifactId,
        label: `Memory evidence artifact ${artifactId}`,
        panel: "artifact",
        sequence: row.sequence,
        priority: 50,
        sourceIds: [row.id],
      }))
    }
  }
  for (const provider of input.providers.providers) {
    links.push(link({
      kind: "provider",
      targetId: provider.id,
      label: provider.name,
      panel: "provider",
      sequence: input.providers.committedSequence,
      priority: provider.health === "healthy" ? 50 : 80,
      sourceIds: [
        provider.lastEventId ?? "",
        ...provider.models.map((model) => model.id),
        ...provider.integrationIds,
      ].filter(Boolean),
      warnings: [
        provider.lastError ?? "",
        provider.secretLeakDetected ? "secret leak detected" : "",
        !provider.credentialPresent ? "credential absent" : "",
      ].filter(Boolean),
    }))
    for (const model of provider.models) {
      links.push(link({
        kind: "model",
        targetId: model.id,
        label: `${provider.id}/${model.name}`,
        panel: "provider",
        sequence: input.providers.committedSequence,
        priority: model.available && !model.deprecated ? 40 : 70,
        sourceIds: [provider.id, ...model.integrationIds],
        warnings: [
          model.deprecated ? "model deprecated" : "",
          !model.available ? "model unavailable" : "",
        ].filter(Boolean),
      }))
    }
  }
  if (input.providers.selectedRoute) {
    const route = input.providers.selectedRoute
    links.push(link({
      kind: "route",
      targetId: route.routeId,
      label: `${route.providerId}/${route.modelId}`,
      panel: "provider",
      sequence: input.providers.committedSequence,
      priority: 100,
      sourceIds: [
        route.providerId,
        route.modelId,
        route.sourceEventId ?? "",
        route.fallbackFromProviderId ?? "",
      ].filter(Boolean),
      warnings: [
        route.degraded ? "degraded route" : "",
        route.fallbackReason ?? "",
      ].filter(Boolean),
    }))
  }
  for (const profile of input.placement.profiles) {
    links.push(link({
      kind: "placement",
      targetId: profile.id,
      label: `${profile.tier} ${profile.label}`,
      panel: "placement",
      sequence: input.placement.committedSequence,
      priority: Math.round(profile.health * 100),
      sourceIds: [
        ...profile.workerIds,
        ...profile.routeIds,
        ...profile.providerIds,
        ...profile.modelIds,
        profile.lastEventId ?? "",
      ].filter(Boolean),
      warnings: [
        !profile.online ? "profile offline" : "",
        !profile.encrypted ? "profile not encrypted" : "",
      ].filter(Boolean),
    }))
  }
  for (const migration of input.placement.migrations) {
    links.push(link({
      kind: "event",
      targetId: migration.sourceEventId,
      label: `${migration.fromTier} → ${migration.toTier} ${migration.phase}`,
      panel: "timeline",
      sequence: migration.sequence,
      priority: migration.phase === "failed" ? 100 : 70,
      correlationId: migration.correlationId,
      sourceIds: [
        migration.fromProfileId ?? "",
        migration.toProfileId ?? "",
        migration.workerId ?? "",
        migration.nodeId ?? "",
        migration.routeId ?? "",
        migration.checkpointId ?? "",
      ].filter(Boolean),
      warnings: migration.phase === "failed" ? [migration.reason] : [],
    }))
  }
  for (const nodeId of input.placement.topologyNodeIds) {
    links.push(link({
      kind: "node",
      targetId: nodeId,
      label: `Topology node ${nodeId}`,
      panel: "topology",
      sequence: input.placement.committedSequence,
      priority: 50,
      sourceIds: input.placement.timelineEventIds,
    }))
  }
  return finalizeLinks(input.session.taskId, links)
}

export function linksForTarget(
  graph: ConsoleLinkGraph,
  targetId: string,
): readonly ConsoleLink[] {
  return graph.byTarget[targetId] ?? Object.freeze([])
}

export function linksForSource(
  graph: ConsoleLinkGraph,
  sourceId: string,
): readonly ConsoleLink[] {
  return graph.bySource[sourceId] ?? Object.freeze([])
}

export function traceLinkedTargets(
  graph: ConsoleLinkGraph,
  startId: string,
  maximumDepth = 8,
): readonly string[] {
  const visited = new Set<string>()
  const queue: Array<{ id: string; depth: number }> = [{ id: startId, depth: 0 }]
  while (queue.length) {
    const current = queue.shift()!
    if (visited.has(current.id) || current.depth > maximumDepth) continue
    visited.add(current.id)
    const neighbors = [
      ...(graph.byTarget[current.id] ?? []),
      ...(graph.bySource[current.id] ?? []),
    ].flatMap((entry) => [entry.targetId, ...entry.sourceIds])
    for (const neighbor of neighbors) {
      if (!visited.has(neighbor)) queue.push({ id: neighbor, depth: current.depth + 1 })
    }
  }
  visited.delete(startId)
  return Object.freeze([...visited])
}

function link(input: Omit<ConsoleLink, "id" | "warnings"> & {
  id?: string
  warnings?: readonly string[]
}): ConsoleLink {
  return Object.freeze({
    ...input,
    id: input.id ?? fingerprint([
      input.kind,
      input.targetId,
      input.panel,
      input.sourceIds,
    ]),
    label: text(input.label, input.targetId),
    sourceIds: Object.freeze(unique(input.sourceIds)),
    warnings: Object.freeze(unique(input.warnings ?? [])),
  })
}

function finalizeLinks(taskId: string, values: readonly ConsoleLink[]): ConsoleLinkGraph {
  const deduplicated = new Map<string, ConsoleLink>()
  for (const value of values) {
    const existing = deduplicated.get(value.id)
    if (!existing || value.priority > existing.priority) deduplicated.set(value.id, value)
  }
  const links = sortStable([...deduplicated.values()], (left, right) =>
    compareNumber(right.priority, left.priority) ||
    compareNumber(right.sequence, left.sequence) ||
    compareText(left.id, right.id))
  const byKind: Record<string, ConsoleLink[]> = {}
  const byTarget: Record<string, ConsoleLink[]> = {}
  const bySource: Record<string, ConsoleLink[]> = {}
  for (const value of links) {
    const kind = byKind[value.kind] ?? []
    kind.push(value)
    byKind[value.kind] = kind
    const target = byTarget[value.targetId] ?? []
    target.push(value)
    byTarget[value.targetId] = target
    for (const sourceId of value.sourceIds) {
      const sources = bySource[sourceId] ?? []
      sources.push(value)
      bySource[sourceId] = sources
    }
  }
  return Object.freeze({
    taskId,
    links: Object.freeze(links),
    byKind: freezeGroups(byKind),
    byTarget: freezeGroups(byTarget),
    bySource: freezeGroups(bySource),
    timelineEventIds: Object.freeze(unique(
      links.filter((value) => value.kind === "event").map((value) => value.targetId),
    )),
    topologyNodeIds: Object.freeze(unique(
      links.filter((value) => value.kind === "node").map((value) => value.targetId),
    )),
    topologyWorkerIds: Object.freeze(unique(
      links.filter((value) => value.kind === "worker").map((value) => value.targetId),
    )),
    artifactIds: Object.freeze(unique(
      links.filter((value) => value.kind === "artifact").map((value) => value.targetId),
    )),
    commandIds: Object.freeze(unique(
      links.filter((value) => value.kind === "command").map((value) => value.targetId),
    )),
    checkpointIds: Object.freeze(unique(
      links.filter((value) => value.kind === "checkpoint").map((value) => value.targetId),
    )),
    orphanSourceIds: Object.freeze(unique(
      links.flatMap((value) => value.sourceIds)
        .filter((sourceId) => !byTarget[sourceId] && !bySource[sourceId]),
    )),
    fingerprint: fingerprint(links.map((value) => [
      value.id,
      value.priority,
      value.sequence,
      value.warnings,
    ])),
  })
}

function freezeGroups(
  groups: Readonly<Record<string, readonly ConsoleLink[]>>,
): Readonly<Record<string, readonly ConsoleLink[]>> {
  return Object.freeze(Object.fromEntries(
    Object.entries(groups).map(([key, values]) => [key, Object.freeze([...values])]),
  ))
}
