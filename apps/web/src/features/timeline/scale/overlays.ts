import {
  TimelineEventKind,
  type TimelineEventFacts,
  type TimelineRow,
  type WorkerCausalTimelineProjection,
} from "../projection/index.ts"
import type {
  TimelineGoalDrift,
  TimelineOverlayKind,
  TimelineOverlayProjection,
  TimelineRowOverlay,
} from "./contracts.ts"

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function unique(values: Iterable<string>): readonly string[] {
  return freeze(new Set([...values].filter(Boolean)))
}

function normalizedRowText(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): string {
  return [
    row.rowKind,
    row.phase,
    row.title,
    row.summary,
    ...row.tags,
    ...facts.flatMap((fact) => [
      fact.event.eventType,
      fact.title,
      fact.summary,
      ...Object.keys(fact.safeAttributes),
      ...Object.values(fact.safeAttributes)
        .filter(
          (value): value is string | number | boolean =>
            typeof value === "string" ||
            typeof value === "number" ||
            typeof value === "boolean",
        )
        .map(String),
    ]),
  ]
    .join(" ")
    .toLocaleLowerCase()
}

function attribute(
  facts: readonly TimelineEventFacts[],
  names: readonly string[],
): string | undefined {
  for (const fact of facts) {
    for (const name of names) {
      const value = fact.safeAttributes[name]
      if (
        (typeof value === "string" || typeof value === "number") &&
        String(value).trim()
      ) {
        return String(value).trim()
      }
    }
  }
  return undefined
}

function has(text: string, pattern: RegExp): boolean {
  return pattern.test(text)
}

function eventFactsByRow(
  projection: WorkerCausalTimelineProjection,
): ReadonlyMap<string, readonly TimelineEventFacts[]> {
  const byEvent = new Map(
    projection.events.map((facts) => [facts.event.eventId, facts]),
  )
  return new Map(
    projection.rows.map((row) => [
      row.key,
      freeze(
        row.eventIds
          .map((eventId) => byEvent.get(eventId))
          .filter((facts): facts is TimelineEventFacts => Boolean(facts)),
      ),
    ]),
  )
}

function classifyKinds(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
  drift: TimelineGoalDrift,
): readonly TimelineOverlayKind[] {
  const text = normalizedRowText(row, facts)
  const kinds = new Set<TimelineOverlayKind>()
  if (row.effective) kinds.add("effective-step")
  if (
    has(text, /\bheartbeat\b|\bkeepalive\b|\bhealth[-_ ]?check\b/) &&
    !row.terminal
  ) {
    kinds.add("heartbeat")
  }
  if (
    has(text, /\bno[-_ ]?op\b|\bnoop\b|\bunchanged\b|\bduplicate\b/) ||
    row.duplicateCount > 1
  ) {
    kinds.add("noop")
  }
  if (has(text, /\breplay(?:ed|ing)?\b|\brehydrate\b|\brestore replay\b/)) {
    kinds.add("replay")
  }
  if (has(text, /\bcompact(?:ed|ion)?\b|\bcontext[-_ ]?summary\b/)) {
    kinds.add("compact")
  }
  if (
    row.rowKind === TimelineEventKind.CHECKPOINT ||
    has(text, /\bcheckpoint\b|\bresume[-_ ]?token\b/)
  ) {
    kinds.add("checkpoint")
  }
  if (
    row.rowKind === TimelineEventKind.PLACEMENT ||
    Boolean(row.placementId)
  ) {
    kinds.add("placement")
  }
  if (
    row.rowKind === TimelineEventKind.ROUTE ||
    Boolean(row.routeId)
  ) {
    kinds.add("route")
  }
  if (
    has(
      text,
      /\bworker[-_ ]?(?:replacement|replaced|successor|reassign)\b|\blease[-_ ]?fenc/,
    ) ||
    facts.some(
      (fact) =>
        Boolean(fact.safeAttributes.previous_worker_id) &&
        Boolean(fact.safeAttributes.worker_id),
    )
  ) {
    kinds.add("worker-replacement")
  }
  if (
    row.rowKind === TimelineEventKind.COMMAND ||
    has(text, /\/(?:kill|steer|retry|reassign|resume)\b|\bcontrol[-_ ]?command\b/)
  ) {
    kinds.add("control")
  }
  if (
    row.rowKind === TimelineEventKind.POLICY ||
    Boolean(row.permissionId) ||
    has(text, /\bpermission\b|\bsealed autonomous\b|\bdeny\b/)
  ) {
    kinds.add("permission")
  }
  if (
    row.rowKind === TimelineEventKind.FAILURE ||
    Boolean(row.failureId) ||
    row.phase === "failed"
  ) {
    kinds.add("fault")
  }
  if (
    row.rowKind === TimelineEventKind.RECOVERY ||
    Boolean(row.recoveryId) ||
    row.phase === "recovering"
  ) {
    kinds.add("recovery")
  }
  if (drift.driftRowKeys.has(row.key)) kinds.add("goal-drift")
  if (row.critical) kinds.add("critical")
  return freeze(kinds)
}

function effectiveReason(
  row: TimelineRow,
  kinds: readonly TimelineOverlayKind[],
): string {
  if (kinds.includes("heartbeat")) {
    return "excluded: heartbeat does not mutate canonical task state"
  }
  if (kinds.includes("noop")) {
    return "excluded: duplicate or no-op observation"
  }
  if (kinds.includes("replay")) {
    return "excluded: replay reconstructs existing state"
  }
  if (kinds.includes("control")) {
    return row.effective
      ? "effective: control changed a canonical runtime owner"
      : "control request is visible but no owner mutation is proven"
  }
  if (kinds.includes("worker-replacement")) {
    return "effective: worker or lease ownership changed"
  }
  if (kinds.includes("route") || kinds.includes("placement")) {
    return "effective: route or placement state changed"
  }
  if (kinds.includes("checkpoint")) {
    return "effective when checkpoint or exact-resume state changed"
  }
  if (kinds.includes("compact")) {
    return "effective when compacted context changes later reasoning input"
  }
  if (kinds.includes("recovery") || kinds.includes("fault")) {
    return "effective: fault or recovery state transition"
  }
  return row.effective
    ? "effective canonical state transition"
    : "observation only; no qualifying state mutation"
}

function transitionWeight(
  row: TimelineRow,
  kinds: readonly TimelineOverlayKind[],
): number {
  if (
    kinds.includes("heartbeat") ||
    kinds.includes("noop") ||
    kinds.includes("replay")
  ) {
    return 0
  }
  if (!row.effective) return 0
  let weight = 1
  if (kinds.includes("critical")) weight += 0.5
  if (kinds.includes("worker-replacement")) weight += 1
  if (kinds.includes("recovery")) weight += 0.75
  if (kinds.includes("control")) weight += 0.5
  if (kinds.includes("goal-drift")) weight += 0.5
  return weight
}

function driftEpochId(
  drift: TimelineGoalDrift,
  rowKey: string,
): string | undefined {
  return drift.epochs.find((epoch) => epoch.sourceRowKeys.includes(rowKey))?.id
}

function overlayForRow(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
  drift: TimelineGoalDrift,
): TimelineRowOverlay {
  const kinds = classifyKinds(row, facts, drift)
  const effectiveStep =
    row.effective &&
    !kinds.includes("heartbeat") &&
    !kinds.includes("noop") &&
    !kinds.includes("replay")
  const compactId = attribute(facts, [
    "compact_id",
    "compactId",
    "context_compaction_id",
  ])
  const checkpointId =
    attribute(facts, ["checkpoint_id", "checkpointId", "resume_checkpoint_id"]) ??
    row.evidence.find((evidence) => evidence.kind === "checkpoint")?.id
  const controlCommandId =
    attribute(facts, [
      "command_id",
      "commandId",
      "control_receipt_id",
      "request_id",
    ]) ??
    row.evidence.find((evidence) => evidence.kind === "command")?.id
  const labels = kinds.map((kind) => {
    switch (kind) {
      case "effective-step":
        return "effective step"
      case "worker-replacement":
        return "worker replaced"
      case "goal-drift":
        return "goal changed"
      default:
        return kind
    }
  })
  return Object.freeze({
    rowKey: row.key,
    kinds,
    effectiveStep,
    effectiveReason: effectiveReason(row, kinds),
    transitionWeight: transitionWeight(row, kinds),
    compactId,
    checkpointId,
    placementId: row.placementId,
    routeId: row.routeId,
    previousWorkerId: attribute(facts, [
      "previous_worker_id",
      "previousWorkerId",
      "replaced_worker_id",
    ]),
    replacementWorkerId:
      attribute(facts, [
        "replacement_worker_id",
        "replacementWorkerId",
        "successor_worker_id",
        "successorWorkerId",
      ]) ?? (kinds.includes("worker-replacement") ? row.workerId : undefined),
    controlCommandId,
    permissionId: row.permissionId,
    failureId: row.failureId,
    recoveryId: row.recoveryId,
    driftEpochId: driftEpochId(drift, row.key),
    labels: freeze(labels),
    eventIds: unique(row.eventIds),
  })
}

export function projectTimelineOverlays(
  projection: WorkerCausalTimelineProjection,
  drift: TimelineGoalDrift,
): TimelineOverlayProjection {
  const facts = eventFactsByRow(projection)
  const byRowKey = new Map<string, TimelineRowOverlay>()
  const rowsByKind = new Map<TimelineOverlayKind, string[]>()
  for (const row of projection.rows) {
    const overlay = overlayForRow(row, facts.get(row.key) ?? [], drift)
    byRowKey.set(row.key, overlay)
    for (const kind of overlay.kinds) {
      const rows = rowsByKind.get(kind) ?? []
      rows.push(row.key)
      rowsByKind.set(kind, rows)
    }
  }
  const readonlyRowsByKind = new Map<TimelineOverlayKind, readonly string[]>()
  for (const [kind, rows] of rowsByKind) {
    readonlyRowsByKind.set(kind, freeze(rows))
  }
  const count = (kind: TimelineOverlayKind): number =>
    readonlyRowsByKind.get(kind)?.length ?? 0
  return Object.freeze({
    byRowKey,
    rowsByKind: readonlyRowsByKind,
    effectiveStepCount: [...byRowKey.values()].filter(
      (overlay) => overlay.effectiveStep,
    ).length,
    excludedNoopCount:
      count("heartbeat") + count("noop") + count("replay"),
    compactCount: count("compact"),
    checkpointCount: count("checkpoint"),
    placementCount: count("placement") + count("route"),
    controlCount: count("control"),
    recoveryCount: count("recovery"),
  })
}

export function timelineOverlayForRow(
  projection: TimelineOverlayProjection,
  rowKey: string,
): TimelineRowOverlay | undefined {
  return projection.byRowKey.get(rowKey)
}

export function effectiveTimelineRows(
  rows: readonly TimelineRow[],
  overlays: TimelineOverlayProjection,
): readonly TimelineRow[] {
  return freeze(
    rows.filter((row) => overlays.byRowKey.get(row.key)?.effectiveStep),
  )
}
