import type {
  CommandCoordinatorSnapshot,
  CommandQueueSnapshot,
  CommandReceipt,
} from "./contracts.ts"
import type { CommandHistorySnapshot } from "./history.ts"
import { parseCommand } from "./parser.ts"
import type {
  CommandProjectionIndexAudit,
} from "./projection-index.ts"
import type { QueueActionSnapshot } from "./queue-actions.ts"
import type { QueueRecoverySnapshot } from "./queue-recovery.ts"
import type { CommandRegistry } from "./registry.ts"
import type { CommandResultModel } from "./result-model.ts"

export type DiagnosticSeverity = "info" | "warning" | "error"

export interface CommandDiagnostic {
  id: string
  owner: string
  severity: DiagnosticSeverity
  passed: boolean
  summary: string
  details: Readonly<Record<string, unknown>>
  relatedIds: readonly string[]
}

export interface CommandRuntimeAudit {
  schema: "zyra.command-runtime-audit/v1"
  healthy: boolean
  degraded: boolean
  checks: readonly CommandDiagnostic[]
  errors: number
  warnings: number
  passed: number
  generatedAt: string
  owners: Readonly<Record<string, {
    checks: number
    failures: number
  }>>
}

export interface CommandRuntimeAuditInput {
  registry: CommandRegistry
  coordinator: CommandCoordinatorSnapshot
  queue?: CommandQueueSnapshot
  recovery: QueueRecoverySnapshot
  actions: QueueActionSnapshot
  projection: CommandProjectionIndexAudit
  history?: CommandHistorySnapshot
  receipt?: CommandReceipt
  result?: CommandResultModel
  now?: number
}

const exactCommands = [
  "/status",
  "/graph",
  "/trace",
  "/artifacts",
  "/permissions",
  "/context",
  "/compact",
  "/memory",
  "/model",
  "/mcp",
  "/skills",
  "/agents",
  "/tasks",
  "/resume",
  "/rewind",
  "/export",
  "/btw",
  "/inject",
  "/change",
  "/verify",
  "/eval",
  "/doctor",
] as const

function check(input: {
  id: string
  owner: string
  passed: boolean
  summary: string
  severity?: DiagnosticSeverity
  details?: Record<string, unknown>
  relatedIds?: readonly string[]
}): CommandDiagnostic {
  return Object.freeze({
    id: input.id,
    owner: input.owner,
    severity: input.passed ? "info" : input.severity ?? "error",
    passed: input.passed,
    summary: input.summary,
    details: Object.freeze({ ...(input.details ?? {}) }),
    relatedIds: Object.freeze([
      ...new Set((input.relatedIds ?? []).filter(Boolean)),
    ]),
  })
}

function registryChecks(registry: CommandRegistry): CommandDiagnostic[] {
  const descriptors = registry.list()
  const names = descriptors.map((value) => value.name)
  const actual = new Set(names)
  const missing = exactCommands.filter((name) => !actual.has(name))
  const unexpected = names.filter(
    (name) => !(exactCommands as readonly string[]).includes(name),
  )
  const duplicateNames = names.filter(
    (name, index) => names.indexOf(name) !== index,
  )
  const aliasOwners = new Map<string, string[]>()
  for (const descriptor of descriptors) {
    for (const alias of descriptor.aliases) {
      const owners = aliasOwners.get(alias) ?? []
      owners.push(descriptor.name)
      aliasOwners.set(alias, owners)
    }
  }
  const duplicateAliases = [...aliasOwners]
    .filter(([, owners]) => new Set(owners).size > 1)
    .map(([alias]) => alias)
  const parsedFailures: string[] = []
  for (const descriptor of descriptors) {
    const required = descriptor.arguments
      .filter((argument) => argument.required)
      .map((argument) => {
        if (argument.kind === "integer") return "1"
        if (argument.kind === "boolean") return "true"
        if (argument.kind === "enum") return argument.choices?.[0] ?? "value"
        if (argument.kind === "json") return "{}"
        if (argument.kind === "duration") return "1s"
        if (argument.kind === "identity") return "identity_1"
        return "value"
      })
    const parsed = parseCommand(
      [descriptor.name, ...required].join(" "),
      registry,
    )
    if (!parsed.descriptor || parsed.errors.length) {
      parsedFailures.push(descriptor.name)
    }
  }
  return [
    check({
      id: "registry-exact-command-set",
      owner: "CommandRegistry",
      passed: missing.length === 0 && unexpected.length === 0,
      summary:
        missing.length || unexpected.length
          ? "Command registry differs from the frozen M2-04 command surface."
          : "Command registry exposes exactly the frozen M2-04 command surface.",
      details: { missing, unexpected, actual: names },
      relatedIds: names,
    }),
    check({
      id: "registry-unique-identities",
      owner: "CommandRegistry",
      passed: duplicateNames.length === 0 && duplicateAliases.length === 0,
      summary:
        duplicateNames.length || duplicateAliases.length
          ? "Command or alias identities conflict."
          : "Command names and aliases resolve deterministically.",
      details: { duplicateNames, duplicateAliases },
      relatedIds: [...duplicateNames, ...duplicateAliases],
    }),
    check({
      id: "registry-required-argument-samples",
      owner: "CommandParser",
      passed: parsedFailures.length === 0,
      summary:
        parsedFailures.length
          ? "At least one registered command cannot parse its required arguments."
          : "Every registered command parses a canonical required-argument sample.",
      details: { parsedFailures },
      relatedIds: parsedFailures,
    }),
  ]
}

function queueChecks(queue?: CommandQueueSnapshot): CommandDiagnostic[] {
  if (!queue) {
    return [check({
      id: "queue-unbound",
      owner: "PromptQueueRuntime+CanonicalProjectionStore",
      passed: true,
      summary: "No task route is currently bound to a command queue.",
      details: {},
    })]
  }
  const duplicateQueueIds = queue.items
    .map((item) => item.queueId)
    .filter((id, index, values) => values.indexOf(id) !== index)
  const positions = queue.items.map((item) => item.position)
  const invalidPositions = positions.filter((value, index) => value !== index)
  const pendingIds = new Set(queue.pending.map((item) => item.queueId))
  const runningIds = new Set(queue.running.map((item) => item.queueId))
  const settledIds = new Set(queue.settled.map((item) => item.queueId))
  const unclassified = queue.items
    .filter((item) =>
      !pendingIds.has(item.queueId) &&
      !runningIds.has(item.queueId) &&
      !settledIds.has(item.queueId))
    .map((item) => item.queueId)
  const overlap = queue.items
    .filter((item) => {
      const count = Number(pendingIds.has(item.queueId)) +
        Number(runningIds.has(item.queueId)) +
        Number(settledIds.has(item.queueId))
      return count !== 1
    })
    .map((item) => item.queueId)
  const cancellableWithoutRequest = queue.items
    .filter((item) => item.cancellable && (!item.requestId || !item.taskId))
    .map((item) => item.queueId)
  const retryableWithoutText = queue.items
    .filter((item) => item.retryable && (!item.requestId || (!item.text && !item.name)))
    .map((item) => item.queueId)
  const invalidSources = queue.items
    .filter((item) =>
      !["backend-queue", "canonical-projection", "receipt"].includes(item.source))
    .map((item) => item.queueId)
  return [
    check({
      id: "queue-identity-uniqueness",
      owner: "PromptQueueRuntime",
      passed: duplicateQueueIds.length === 0,
      summary:
        duplicateQueueIds.length
          ? "Queue snapshot contains duplicate queue identities."
          : "Queue identities are unique.",
      details: { duplicateQueueIds },
      relatedIds: duplicateQueueIds,
    }),
    check({
      id: "queue-order-and-classification",
      owner: "CommandQueueProjection",
      passed:
        invalidPositions.length === 0 &&
        unclassified.length === 0 &&
        overlap.length === 0,
      summary:
        invalidPositions.length || unclassified.length || overlap.length
          ? "Queue ordering or lifecycle classification is inconsistent."
          : "Queue ordering and lifecycle buckets are consistent.",
      details: { invalidPositions, unclassified, overlap },
      relatedIds: [...unclassified, ...overlap],
    }),
    check({
      id: "queue-action-identities",
      owner: "CommandQueueActions",
      passed:
        cancellableWithoutRequest.length === 0 &&
        retryableWithoutText.length === 0,
      summary:
        cancellableWithoutRequest.length || retryableWithoutText.length
          ? "A visible queue action lacks the identity required for backend execution."
          : "Visible cancel/retry actions have backend identities and command text.",
      details: { cancellableWithoutRequest, retryableWithoutText },
      relatedIds: [...cancellableWithoutRequest, ...retryableWithoutText],
    }),
    check({
      id: "queue-authoritative-sources",
      owner: "CanonicalProjectionStore",
      passed: invalidSources.length === 0,
      summary:
        invalidSources.length
          ? "Queue contains a non-authoritative browser source."
          : "Queue items originate only from backend, projection, or typed receipt sources.",
      details: { invalidSources, restored: queue.restored },
      relatedIds: invalidSources,
    }),
  ]
}

function recoveryChecks(
  recovery: QueueRecoverySnapshot,
): CommandDiagnostic[] {
  const stuck =
    recovery.phase === "restoring" &&
    recovery.lastStartedAt !== undefined &&
    Date.now() - recovery.lastStartedAt > 120_000
  const retryInvariant =
    recovery.nextRetryAt === undefined ||
    recovery.phase === "degraded"
  const boundInvariant =
    recovery.taskId === undefined ||
    recovery.sessionId !== undefined
  return [
    check({
      id: "queue-recovery-progress",
      owner: "CommandQueueRecovery",
      passed: !stuck,
      severity: "warning",
      summary: stuck
        ? "Command queue recovery has remained in-flight for more than two minutes."
        : "Command queue recovery is making bounded progress.",
      details: {
        phase: recovery.phase,
        attempts: recovery.attempts,
        lastStartedAt: recovery.lastStartedAt,
      },
      relatedIds: recovery.taskId ? [recovery.taskId] : [],
    }),
    check({
      id: "queue-recovery-retry-state",
      owner: "CommandQueueRecovery",
      passed: retryInvariant && boundInvariant,
      summary:
        retryInvariant && boundInvariant
          ? "Recovery retry and route binding invariants hold."
          : "Recovery has a retry outside degraded state or a task without session scope.",
      details: {
        phase: recovery.phase,
        taskId: recovery.taskId,
        sessionId: recovery.sessionId,
        nextRetryAt: recovery.nextRetryAt,
      },
      relatedIds: recovery.taskId ? [recovery.taskId] : [],
    }),
  ]
}

function actionChecks(actions: QueueActionSnapshot): CommandDiagnostic[] {
  const duplicate = actions.active
    .map((record) => record.id)
    .filter((id, index, values) => values.indexOf(id) !== index)
  const invalidSettlements = actions.recent
    .filter((record) =>
      record.phase !== "pending" &&
      !record.receipt &&
      !record.error)
    .map((record) => record.id)
  return [
    check({
      id: "queue-action-deduplication",
      owner: "CommandQueueActions",
      passed: duplicate.length === 0,
      summary:
        duplicate.length
          ? "Duplicate cancel/retry actions are active for one queue identity."
          : "Queue actions are deduplicated by backend identity.",
      details: { duplicate, active: actions.active.length },
      relatedIds: duplicate,
    }),
    check({
      id: "queue-action-settlement",
      owner: "CommandQueueActions",
      passed: invalidSettlements.length === 0,
      summary:
        invalidSettlements.length
          ? "A settled queue action has neither receipt nor error."
          : "Settled queue actions retain typed receipt or explicit error evidence.",
      details: { invalidSettlements },
      relatedIds: invalidSettlements,
    }),
  ]
}

function projectionChecks(
  projection: CommandProjectionIndexAudit,
): CommandDiagnostic[] {
  return [
    check({
      id: "projection-index-identity",
      owner: "CanonicalProjectionStore",
      passed:
        projection.duplicateCommandIds.length === 0 &&
        projection.duplicateEventIds.length === 0,
      summary:
        projection.duplicateCommandIds.length ||
        projection.duplicateEventIds.length
          ? "Projection index observed duplicate canonical identities."
          : "Projection command and event identities are unique.",
      details: {
        duplicateCommandIds: projection.duplicateCommandIds,
        duplicateEventIds: projection.duplicateEventIds,
      },
      relatedIds: [
        ...projection.duplicateCommandIds,
        ...projection.duplicateEventIds,
      ],
    }),
    check({
      id: "projection-index-causality",
      owner: "CommandProjectionIndex",
      passed: projection.orphanEventIds.length === 0,
      severity: "warning",
      summary:
        projection.orphanEventIds.length
          ? "Projection index contains causal events whose parent cannot be resolved."
          : "Indexed command event parents resolve within the causal projection.",
      details: {
        orphanEventIds: projection.orphanEventIds,
        commandsWithoutEvents: projection.commandsWithoutEvents,
        eventsWithoutCommands: projection.eventsWithoutCommands,
      },
      relatedIds: projection.orphanEventIds,
    }),
  ]
}

function receiptChecks(
  receipt?: CommandReceipt,
  result?: CommandResultModel,
): CommandDiagnostic[] {
  if (!receipt) return []
  const identity =
    Boolean(receipt.requestId) &&
    Boolean(receipt.commandId) &&
    Boolean(receipt.taskId) &&
    Boolean(receipt.runId)
  const queueIdentity = receipt.phase !== "queued" || Boolean(receipt.queueId)
  const durableEvidence =
    !receipt.durable ||
    receipt.phase !== "applied" ||
    receipt.eventIds.length > 0
  const btwIsolation =
    receipt.name !== "/btw" ||
    (
      receipt.executed &&
      receipt.humanInterventionCount === 0 &&
      receipt.interventionCounted === false
    )
  const modelIdentity =
    !result ||
    (
      result.commandId === receipt.commandId &&
      result.requestId === receipt.requestId
    )
  return [
    check({
      id: "receipt-required-identities",
      owner: "RuntimeControlDispatcher",
      passed: identity && queueIdentity,
      summary:
        identity && queueIdentity
          ? "Typed receipt carries required request, command, task, run, and queue identities."
          : "Typed receipt is missing a required canonical identity.",
      details: {
        requestId: receipt.requestId,
        commandId: receipt.commandId,
        taskId: receipt.taskId,
        runId: receipt.runId,
        queueId: receipt.queueId,
        phase: receipt.phase,
      },
      relatedIds: [
        receipt.requestId,
        receipt.commandId,
        receipt.queueId ?? "",
      ],
    }),
    check({
      id: "receipt-durable-causality",
      owner: "CanonicalEventLog",
      passed: durableEvidence,
      severity: "warning",
      summary: durableEvidence
        ? "Durable applied receipt carries causal event evidence."
        : "Durable applied receipt does not identify a causal event.",
      details: {
        durable: receipt.durable,
        eventIds: receipt.eventIds,
      },
      relatedIds: receipt.eventIds,
    }),
    check({
      id: "receipt-btw-isolation",
      owner: "SideQuestionRuntime",
      passed: btwIsolation,
      summary: btwIsolation
        ? "Side-question receipt preserves tool-disabled, non-intervention isolation."
        : "Side-question receipt violates isolation or intervention accounting.",
      details: {
        command: receipt.name,
        executed: receipt.executed,
        interventionCounted: receipt.interventionCounted,
        humanInterventionCount: receipt.humanInterventionCount,
      },
      relatedIds: [receipt.commandId],
    }),
    check({
      id: "result-model-correlation",
      owner: "CommandResultModel",
      passed: modelIdentity,
      summary: modelIdentity
        ? "Local result model retains canonical receipt identity."
        : "Local result model identity diverges from the typed receipt.",
      details: {
        receiptCommandId: receipt.commandId,
        modelCommandId: result?.commandId,
      },
      relatedIds: [receipt.commandId, result?.commandId ?? ""],
    }),
  ]
}

function historyChecks(history?: CommandHistorySnapshot): CommandDiagnostic[] {
  if (!history) return []
  const leakedSideQuestions = history.entries
    .filter((entry) => entry.name === "/btw" && entry.text !== "/btw [redacted]")
    .map((entry) => entry.id)
  const duplicateIds = history.entries
    .map((entry) => entry.id)
    .filter((id, index, values) => values.indexOf(id) !== index)
  return [
    check({
      id: "history-sensitive-input",
      owner: "CommandHistoryStore",
      passed: leakedSideQuestions.length === 0,
      summary:
        leakedSideQuestions.length
          ? "Side-question text leaked into local command history."
          : "Side-question content is redacted from local command history.",
      details: { leakedSideQuestions },
      relatedIds: leakedSideQuestions,
    }),
    check({
      id: "history-identity",
      owner: "CommandHistoryStore",
      passed: duplicateIds.length === 0,
      summary:
        duplicateIds.length
          ? "Local history contains duplicate entry identities."
          : "Local history entries are deterministically deduplicated.",
      details: { duplicateIds },
      relatedIds: duplicateIds,
    }),
  ]
}

export function auditCommandRuntime(
  input: CommandRuntimeAuditInput,
): CommandRuntimeAudit {
  const checks = [
    ...registryChecks(input.registry),
    ...queueChecks(input.queue),
    ...recoveryChecks(input.recovery),
    ...actionChecks(input.actions),
    ...projectionChecks(input.projection),
    ...receiptChecks(input.receipt, input.result),
    ...historyChecks(input.history),
  ]
  const errors = checks.filter(
    (entry) => !entry.passed && entry.severity === "error",
  ).length
  const warnings = checks.filter(
    (entry) => !entry.passed && entry.severity === "warning",
  ).length
  const owners: Record<string, { checks: number; failures: number }> = {}
  for (const entry of checks) {
    const selected = owners[entry.owner] ?? { checks: 0, failures: 0 }
    selected.checks += 1
    if (!entry.passed) selected.failures += 1
    owners[entry.owner] = selected
  }
  return Object.freeze({
    schema: "zyra.command-runtime-audit/v1",
    healthy: errors === 0 && warnings === 0,
    degraded: errors === 0 && warnings > 0,
    checks: Object.freeze(checks),
    errors,
    warnings,
    passed: checks.filter((entry) => entry.passed).length,
    generatedAt: new Date(input.now ?? Date.now()).toISOString(),
    owners: Object.freeze(
      Object.fromEntries(
        Object.entries(owners)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([owner, value]) => [owner, Object.freeze({ ...value })]),
      ),
    ),
  })
}
