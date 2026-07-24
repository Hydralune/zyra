import {
  RecoveryControlPhase,
  type RecoveryControlObservation,
  type RecoveryControlOwnerEvidence,
  type RecoveryControlPhaseValue,
  type RecoveryControlProjectionContext,
  type RecoveryControlReceipt,
} from "./contracts.ts"
import type {
  TimelineEventFacts,
  TimelineRow,
} from "../projection/index.ts"

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function unique(values: Iterable<string>): readonly string[] {
  return freeze([...new Set([...values].filter(Boolean))])
}

function tokens(value: string): readonly string[] {
  return freeze(
    value
      .toLocaleLowerCase()
      .split(/[^a-z0-9_-]+/)
      .filter(Boolean),
  )
}

function includes(
  values: readonly string[],
  expected: readonly string[],
): boolean {
  return values.some((value) =>
    expected.some(
      (candidate) =>
        value === candidate ||
        value.startsWith(`${candidate}_`) ||
        value.startsWith(`${candidate}-`) ||
        value.includes(candidate),
    ),
  )
}

function factsMatch(
  receipt: RecoveryControlReceipt,
  facts: TimelineEventFacts,
): boolean {
  const event = facts.event
  if (event.taskId !== receipt.taskId || event.runId !== receipt.runId) {
    return false
  }
  if (event.controlCommandId === receipt.commandId) return true
  if (event.correlationId === receipt.requestId) return true
  if (event.causationId === receipt.commandId) return true
  if (facts.command?.id === receipt.commandId) return true
  if (facts.command?.controlCommandId === receipt.commandId) return true
  if (facts.command?.correlationId === receipt.requestId) return true
  if (facts.recovery?.causationId === receipt.commandId) return true
  if (facts.recovery?.correlationId === receipt.requestId) return true
  const attributes = facts.safeAttributes
  return [
    attributes.command_id,
    attributes.control_command_id,
    attributes.request_id,
    attributes.correlation_id,
    attributes.causation_id,
  ].some(
    (value) =>
      value === receipt.commandId || value === receipt.requestId,
  )
}

function rowsForFacts(
  rows: readonly TimelineRow[],
  eventIds: ReadonlySet<string>,
): readonly TimelineRow[] {
  return freeze(
    rows.filter((row) =>
      row.eventIds.some((eventId) => eventIds.has(eventId)),
    ),
  )
}

function commandPhase(
  facts: readonly TimelineEventFacts[],
  rows: readonly TimelineRow[],
): RecoveryControlPhaseValue {
  const combined = tokens(
    [
      ...facts.flatMap((item) => [
        item.event.eventType,
        item.event.summary,
        item.phase ?? "",
        item.command?.lifecycle ?? "",
        item.command?.status ?? "",
        item.command?.errorCode ?? "",
        item.recovery?.lifecycle ?? "",
        item.recovery?.status ?? "",
      ]),
      ...rows.flatMap((row) => [
        row.phase,
        row.title,
        row.summary,
        ...row.tags,
      ]),
    ].join(" "),
  )
  if (
    includes(combined, [
      "permission_denied",
      "denied",
      "forbidden",
      "sealed",
      "rejected_by_policy",
    ])
  ) {
    return RecoveryControlPhase.DENIED
  }
  if (
    includes(combined, [
      "command_failed",
      "failed",
      "rejected",
      "conflict",
      "owner_mismatch",
      "lease_expired",
      "unavailable",
    ])
  ) {
    return RecoveryControlPhase.FAILED
  }
  if (
    includes(combined, [
      "command_succeeded",
      "succeeded",
      "applied",
      "committed",
      "completed",
      "checkpoint_resumed",
      "control_applied",
    ])
  ) {
    return RecoveryControlPhase.APPLIED
  }
  return RecoveryControlPhase.PENDING
}

function evidenceFromRow(
  row: TimelineRow,
): readonly RecoveryControlOwnerEvidence[] {
  const values: RecoveryControlOwnerEvidence[] = []
  for (const evidence of row.evidence) {
    if (
      ![
        "worker",
        "lease",
        "route",
        "placement",
        "recovery",
        "checkpoint",
        "command",
        "mutation",
        "node",
      ].includes(evidence.kind)
    ) {
      continue
    }
    const metadata = evidence.metadata
    const owner =
      typeof metadata.owner === "string"
        ? metadata.owner
        : typeof metadata.canonical_owner === "string"
          ? metadata.canonical_owner
          : evidence.kind === "worker" || evidence.kind === "lease"
            ? "WorkerPoolStore"
            : evidence.kind === "recovery"
              ? "RecoveryApplication"
              : evidence.kind === "checkpoint"
                ? "CheckpointCommitRuntime"
                : evidence.kind === "route" || evidence.kind === "placement"
                  ? "LayeredRouteRuntime"
                  : "CanonicalEventStore"
    values.push(
      Object.freeze({
        owner,
        operation:
          typeof metadata.operation === "string"
            ? metadata.operation
            : evidence.kind,
        receiptId:
          typeof metadata.receipt_id === "string"
            ? metadata.receipt_id
            : evidence.id,
        accepted:
          typeof metadata.accepted === "boolean"
            ? metadata.accepted
            : !evidence.missing,
        changed:
          typeof metadata.changed === "boolean"
            ? metadata.changed
            : row.effective,
        workerId:
          evidence.kind === "worker" ? evidence.id : row.workerId,
        leaseId:
          evidence.kind === "lease" ? evidence.id : row.leaseId,
        attemptId:
          typeof metadata.attempt_id === "string"
            ? metadata.attempt_id
            : undefined,
        nodeId:
          evidence.kind === "node" ? evidence.id : row.nodeId,
        graphId:
          typeof metadata.graph_id === "string"
            ? metadata.graph_id
            : undefined,
        checkpointId:
          evidence.kind === "checkpoint" ? evidence.id : undefined,
        revision:
          typeof metadata.revision === "number"
            ? metadata.revision
            : row.endSequence,
        raw: Object.freeze({
          kind: evidence.kind,
          id: evidence.id,
          event_ids: evidence.eventIds,
          status: evidence.status,
          metadata,
        }),
      }),
    )
  }
  if (!values.length && row.workerId) {
    values.push(
      Object.freeze({
        owner: "CanonicalEventStore",
        operation: row.rowKind,
        receiptId: row.primaryEventId,
        accepted: !row.partial,
        changed: row.effective,
        workerId: row.workerId,
        leaseId: row.leaseId,
        nodeId: row.nodeId,
        revision: row.endSequence,
        raw: Object.freeze({
          row_key: row.key,
          event_ids: row.eventIds,
        }),
      }),
    )
  }
  return freeze(values)
}

function evidenceKey(value: RecoveryControlOwnerEvidence): string {
  return [
    value.owner,
    value.operation ?? "",
    value.receiptId ?? "",
    value.workerId ?? "",
    value.leaseId ?? "",
    value.checkpointId ?? "",
  ].join("|")
}

function ownerEvidence(
  rows: readonly TimelineRow[],
): readonly RecoveryControlOwnerEvidence[] {
  const result = new Map<string, RecoveryControlOwnerEvidence>()
  for (const row of rows) {
    for (const evidence of evidenceFromRow(row)) {
      result.set(evidenceKey(evidence), evidence)
    }
  }
  return freeze(result.values())
}

function ownerConflict(
  receipt: RecoveryControlReceipt,
  evidence: readonly RecoveryControlOwnerEvidence[],
): string | undefined {
  const expected = receipt.expectedOwner
  for (const item of evidence) {
    if (
      item.accepted === false &&
      item.operation !== "permission"
    ) {
      return `${item.owner} rejected ${item.operation ?? receipt.action}`
    }
    if (
      expected.workerId &&
      item.workerId &&
      expected.workerId !== item.workerId &&
      receipt.action !== "reassign"
    ) {
      return `expected worker ${expected.workerId}, observed ${item.workerId}`
    }
    if (
      expected.leaseId &&
      item.leaseId &&
      expected.leaseId !== item.leaseId &&
      receipt.action !== "reassign"
    ) {
      return `expected lease ${expected.leaseId}, observed ${item.leaseId}`
    }
    if (
      expected.checkpointId &&
      item.checkpointId &&
      expected.checkpointId !== item.checkpointId
    ) {
      return (
        `expected checkpoint ${expected.checkpointId}, ` +
        `observed ${item.checkpointId}`
      )
    }
  }
  return undefined
}

function observationSummary(
  receipt: RecoveryControlReceipt,
  phase: RecoveryControlPhaseValue,
  rows: readonly TimelineRow[],
  conflict?: string,
): string {
  if (conflict) {
    return `${receipt.commandName} owner evidence conflicted: ${conflict}.`
  }
  const terminal = rows
    .filter((row) => row.terminal || row.effective)
    .sort(
      (left, right) =>
        right.endSequence - left.endSequence ||
        right.key.localeCompare(left.key),
    )[0]
  if (phase === RecoveryControlPhase.APPLIED) {
    return terminal
      ? `${receipt.commandName} observed at #${terminal.endSequence}: ${terminal.summary}`
      : `${receipt.commandName} observed in canonical runtime events.`
  }
  if (phase === RecoveryControlPhase.DENIED) {
    return `${receipt.commandName} was denied; no manual mutation was applied.`
  }
  if (phase === RecoveryControlPhase.FAILED) {
    return terminal
      ? `${receipt.commandName} failed at #${terminal.endSequence}: ${terminal.summary}`
      : `${receipt.commandName} failed in canonical runtime events.`
  }
  return `${receipt.commandName} is waiting for terminal canonical evidence.`
}

export function observeRecoveryControlReceipt(
  receipt: RecoveryControlReceipt,
  context: RecoveryControlProjectionContext,
): RecoveryControlObservation | undefined {
  const facts = context.projection.events.filter((item) =>
    factsMatch(receipt, item),
  )
  if (!facts.length) return undefined
  const eventIds = new Set(facts.map((item) => item.event.eventId))
  const rows = rowsForFacts(context.rows, eventIds)
  const evidence = ownerEvidence(rows)
  const inferred = commandPhase(facts, rows)
  const conflict = ownerConflict(receipt, evidence)
  const phase = conflict ? RecoveryControlPhase.FAILED : inferred
  const observedRevision = Math.max(
    ...facts.map((item) => item.event.sequence),
    ...evidence
      .map((item) => item.revision)
      .filter((value): value is number => value !== undefined),
  )
  return Object.freeze({
    phase,
    observedAt: context.now ?? Date.now(),
    observedRevision,
    eventIds: unique([
      ...eventIds,
      ...rows.flatMap((row) => row.eventIds),
    ]),
    rowKeys: unique(rows.map((row) => row.key)),
    ownerEvidence: evidence,
    summary: observationSummary(receipt, phase, rows, conflict),
    source: "canonical-event",
    terminal:
      phase === RecoveryControlPhase.APPLIED ||
      phase === RecoveryControlPhase.DENIED ||
      phase === RecoveryControlPhase.FAILED,
    consistent: !conflict,
    conflictReason: conflict,
  })
}

export function observeAllRecoveryControlReceipts(
  receipts: readonly RecoveryControlReceipt[],
  context: RecoveryControlProjectionContext,
): ReadonlyMap<string, RecoveryControlObservation> {
  const result = new Map<string, RecoveryControlObservation>()
  for (const receipt of receipts) {
    const observation = observeRecoveryControlReceipt(receipt, context)
    if (observation) result.set(receipt.id, observation)
  }
  return result
}

export function canonicalReceiptRows(
  receipt: RecoveryControlReceipt,
  projection: RecoveryControlProjectionContext["projection"],
): readonly TimelineRow[] {
  const eventIds = new Set(receipt.observedEventIds)
  for (const event of projection.events) {
    if (factsMatch(receipt, event)) eventIds.add(event.event.eventId)
  }
  return freeze(
    projection.rows.filter((row) =>
      row.eventIds.some((eventId) => eventIds.has(eventId)),
    ),
  )
}

export function controlReceiptWorkerChanged(
  receipt: RecoveryControlReceipt,
): boolean {
  if (receipt.action !== "reassign") return false
  const expected = receipt.expectedOwner.workerId
  return receipt.ownerEvidence.some(
    (item) =>
      item.changed === true &&
      Boolean(item.workerId) &&
      item.workerId !== expected,
  )
}

export function controlReceiptLeaseFenced(
  receipt: RecoveryControlReceipt,
): boolean {
  if (receipt.action !== "kill" && receipt.action !== "reassign") {
    return false
  }
  return receipt.ownerEvidence.some((item) => {
    const text = [
      item.operation,
      item.owner,
      JSON.stringify(item.raw),
    ]
      .join(" ")
      .toLocaleLowerCase()
    return (
      text.includes("fence") ||
      text.includes("cancel") ||
      text.includes("supersed") ||
      text.includes("lease_expired")
    )
  })
}

export function controlReceiptCheckpointResumed(
  receipt: RecoveryControlReceipt,
): boolean {
  if (receipt.action !== "resume") return false
  return receipt.ownerEvidence.some(
    (item) =>
      item.checkpointId === receipt.expectedOwner.checkpointId &&
      item.accepted !== false,
  )
}
