import type {
  CommandErrorReceipt,
  CommandReceipt,
  CommandUsageReceipt,
} from "./contracts.ts"
import type {
  IndexedCommandCausalEvent,
  CommandProjectionTrace,
} from "./projection-index.ts"

export type ResultTone = "neutral" | "info" | "success" | "warning" | "error"

export interface CommandResultField {
  id: string
  label: string
  value: string
  tone: ResultTone
  copyable: boolean
}

export interface CommandResultRow {
  id: string
  kind:
    | "property"
    | "event"
    | "node"
    | "edge"
    | "artifact"
    | "permission"
    | "check"
    | "answer"
    | "error"
  title: string
  summary: string
  status?: string
  tone: ResultTone
  timestamp?: string
  identity?: string
  parentIdentity?: string
  fields: readonly CommandResultField[]
  links: readonly string[]
  searchableText: string
}

export interface CommandResultSection {
  id: string
  title: string
  description?: string
  tone: ResultTone
  collapsed: boolean
  rows: readonly CommandResultRow[]
  count: number
}

export interface CommandResultModel {
  schema: "zyra.command-result-model/v1"
  commandId: string
  requestId: string
  name: string
  phase: string
  title: string
  summary: string
  displayText: string
  tone: ResultTone
  terminal: boolean
  retryable: boolean
  durable: boolean
  replayed: boolean
  createdAt: string
  finishedAt?: string
  elapsedMs?: number
  fields: readonly CommandResultField[]
  sections: readonly CommandResultSection[]
  usage?: CommandUsageReceipt
  error?: CommandErrorReceipt
  eventIds: readonly string[]
  checkpointRef?: string
  searchText: string
  diagnostics: readonly string[]
}

export interface ResultModelOptions {
  trace?: CommandProjectionTrace
  maximumDepth?: number
  maximumRows?: number
  maximumValueLength?: number
  redactKeys?: readonly string[]
}

interface FlatValue {
  path: string
  value: string
  source: unknown
}

const secretKey =
  /(^|[_-])(token|secret|password|authorization|cookie|private[_-]?key|credential|claim[_-]?token)($|[_-])/i

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function stringValue(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function iso(value: unknown): string | undefined {
  const text = stringValue(value).trim()
  if (!text) return undefined
  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : undefined
}

function titleCase(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_./:-]+/g, " ")
    .trim()
    .split(/\s+/)
    .map((part) => `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`)
    .join(" ")
}

function bounded(value: string, maximum: number): string {
  const normalized = value.replace(/\u0000/g, "")
  return normalized.length <= maximum
    ? normalized
    : `${normalized.slice(0, Math.max(0, maximum - 1))}…`
}

function display(value: unknown, maximum: number): string {
  if (value === null) return "null"
  if (value === undefined) return ""
  if (typeof value === "string") return bounded(value, maximum)
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return bounded(JSON.stringify(value), maximum)
  } catch {
    return bounded(String(value), maximum)
  }
}

function resultTone(value: unknown): ResultTone {
  const status = stringValue(value).trim().toLowerCase()
  if (
    ["failed", "failure", "error", "rejected", "denied", "unhealthy", "critical"]
      .some((part) => status.includes(part))
  ) {
    return "error"
  }
  if (
    ["warning", "warn", "degraded", "pending", "queued", "expired", "cancelled"]
      .some((part) => status.includes(part))
  ) {
    return "warning"
  }
  if (
    ["ok", "pass", "passed", "success", "healthy", "applied", "complete", "allow"]
      .some((part) => status.includes(part))
  ) {
    return "success"
  }
  if (status) return "info"
  return "neutral"
}

function terminal(phase: string): boolean {
  return ["applied", "rejected", "expired", "cancelled"].includes(
    phase.toLowerCase(),
  )
}

function flatten(
  value: unknown,
  options: {
    path?: string
    depth: number
    maximumDepth: number
    maximumRows: number
    maximumValueLength: number
    redactKeys: ReadonlySet<string>
    seen: WeakSet<object>
    output: FlatValue[]
  },
): void {
  if (options.output.length >= options.maximumRows) return
  const path = options.path ?? "value"
  const key = path.split(".").at(-1) ?? path
  if (
    secretKey.test(key) ||
    options.redactKeys.has(key.toLowerCase())
  ) {
    options.output.push({ path, value: "[redacted]", source: undefined })
    return
  }
  if (
    value === null ||
    value === undefined ||
    typeof value !== "object"
  ) {
    options.output.push({
      path,
      value: display(value, options.maximumValueLength),
      source: value,
    })
    return
  }
  if (options.seen.has(value)) {
    options.output.push({ path, value: "[circular]", source: undefined })
    return
  }
  if (options.depth >= options.maximumDepth) {
    options.output.push({
      path,
      value: Array.isArray(value)
        ? `[${value.length} values]`
        : `{${Object.keys(value).length} fields}`,
      source: value,
    })
    return
  }
  options.seen.add(value)
  const entries = Array.isArray(value)
    ? value.map((entry, index) => [String(index), entry] as const)
    : Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
  if (!entries.length) {
    options.output.push({
      path,
      value: Array.isArray(value) ? "[]" : "{}",
      source: value,
    })
  }
  for (const [child, entry] of entries) {
    if (options.output.length >= options.maximumRows) break
    flatten(entry, {
      ...options,
      path: `${path}.${child}`,
      depth: options.depth + 1,
    })
  }
  options.seen.delete(value)
}

function flatValues(
  value: unknown,
  options: Required<Pick<
    ResultModelOptions,
    "maximumDepth" | "maximumRows" | "maximumValueLength"
  >> & { redactKeys: ReadonlySet<string> },
): FlatValue[] {
  const output: FlatValue[] = []
  flatten(value, {
    path: "data",
    depth: 0,
    ...options,
    seen: new WeakSet(),
    output,
  })
  return output
}

function field(
  id: string,
  label: string,
  value: unknown,
  tone: ResultTone = "neutral",
  copyable = true,
  maximum = 1000,
): CommandResultField {
  return Object.freeze({
    id,
    label,
    value: display(value, maximum),
    tone,
    copyable,
  })
}

function row(input: Omit<CommandResultRow, "searchableText">): CommandResultRow {
  const searchableText = [
    input.title,
    input.summary,
    input.status,
    input.identity,
    ...input.fields.flatMap((entry) => [entry.label, entry.value]),
  ].filter(Boolean).join(" ").toLowerCase()
  return Object.freeze({
    ...input,
    fields: Object.freeze([...input.fields]),
    links: Object.freeze([...new Set(input.links.filter(Boolean))]),
    searchableText,
  })
}

function section(input: Omit<CommandResultSection, "count">): CommandResultSection {
  return Object.freeze({
    ...input,
    rows: Object.freeze([...input.rows]),
    count: input.rows.length,
  })
}

function propertyRows(
  data: unknown,
  options: Required<Pick<
    ResultModelOptions,
    "maximumDepth" | "maximumRows" | "maximumValueLength"
  >> & { redactKeys: ReadonlySet<string> },
): CommandResultRow[] {
  return flatValues(data, options).map((entry, index) => {
    const label = entry.path.replace(/^data\.?/, "") || "value"
    return row({
      id: `property:${index}:${label}`,
      kind: "property",
      title: titleCase(label),
      summary: entry.value,
      tone: resultTone(entry.source),
      fields: [field(`field:${index}`, label, entry.value)],
      links: [],
    })
  })
}

function listCandidates(
  data: Record<string, unknown>,
  names: readonly string[],
): unknown[] {
  for (const name of names) {
    const candidate = data[name]
    if (Array.isArray(candidate)) return candidate
    const nested = record(candidate)
    for (const child of ["items", "entries", "results", "values"]) {
      if (Array.isArray(nested[child])) return nested[child] as unknown[]
    }
  }
  return []
}

function graphSections(
  data: Record<string, unknown>,
  maximum: number,
): CommandResultSection[] {
  const nodes = listCandidates(data, ["nodes", "vertices", "tasks", "graph"])
  const edges = listCandidates(data, ["edges", "links", "dependencies"])
  const nodeRows = nodes.slice(0, maximum).map((value, index) => {
    const item = record(value)
    const identity = stringValue(
      item.node_id ?? item.nodeId ?? item.task_id ?? item.taskId ?? item.id,
    ) || `node-${index + 1}`
    const status = stringValue(item.status ?? item.lifecycle ?? item.phase)
    const title = stringValue(item.title ?? item.name ?? item.role) || identity
    return row({
      id: `node:${identity}`,
      kind: "node",
      title,
      summary: stringValue(item.summary ?? item.description ?? item.goal),
      status,
      tone: resultTone(status),
      identity,
      fields: [
        field("node-id", "Node", identity),
        field("node-status", "Status", status, resultTone(status)),
        field("node-worker", "Worker", item.worker_id ?? item.workerId),
        field("node-route", "Route", item.route_id ?? item.routeId),
      ].filter((entry) => entry.value),
      links: [identity],
    })
  })
  const edgeRows = edges.slice(0, maximum).map((value, index) => {
    const item = record(value)
    const source = stringValue(item.source ?? item.from ?? item.parent_id ?? item.parentId)
    const target = stringValue(item.target ?? item.to ?? item.child_id ?? item.childId)
    const identity = stringValue(item.edge_id ?? item.edgeId ?? item.id) ||
      `${source || "unknown"}->${target || "unknown"}`
    const status = stringValue(item.status ?? item.lifecycle)
    return row({
      id: `edge:${identity}:${index}`,
      kind: "edge",
      title: `${source || "Unknown"} → ${target || "Unknown"}`,
      summary: stringValue(item.reason ?? item.label ?? item.kind),
      status,
      tone: resultTone(status),
      identity,
      parentIdentity: source || undefined,
      fields: [
        field("edge-source", "Source", source),
        field("edge-target", "Target", target),
        field("edge-kind", "Kind", item.kind ?? item.type),
      ].filter((entry) => entry.value),
      links: [source, target],
    })
  })
  const output: CommandResultSection[] = []
  if (nodeRows.length) {
    output.push(section({
      id: "graph-nodes",
      title: "Nodes",
      tone: nodeRows.some((entry) => entry.tone === "error") ? "warning" : "info",
      collapsed: false,
      rows: nodeRows,
    }))
  }
  if (edgeRows.length) {
    output.push(section({
      id: "graph-edges",
      title: "Edges",
      tone: "info",
      collapsed: edgeRows.length > 25,
      rows: edgeRows,
    }))
  }
  return output
}

function artifactSections(
  data: Record<string, unknown>,
  maximum: number,
): CommandResultSection[] {
  const artifacts = listCandidates(data, [
    "artifacts",
    "outputs",
    "deliverables",
    "files",
  ])
  if (!artifacts.length) return []
  const rows = artifacts.slice(0, maximum).map((value, index) => {
    const item = record(value)
    const identity = stringValue(
      item.artifact_id ?? item.artifactId ?? item.digest ?? item.id,
    ) || `artifact-${index + 1}`
    const name = stringValue(item.name ?? item.title ?? item.path ?? item.uri) ||
      identity
    const status = stringValue(item.status ?? item.lifecycle)
    return row({
      id: `artifact:${identity}`,
      kind: "artifact",
      title: name,
      summary: stringValue(item.summary ?? item.description ?? item.media_type),
      status,
      tone: resultTone(status),
      identity,
      fields: [
        field("artifact-id", "Artifact", identity),
        field("artifact-media", "Media type", item.media_type ?? item.mediaType),
        field("artifact-size", "Size", item.size_bytes ?? item.sizeBytes),
        field("artifact-version", "Version", item.version),
        field("artifact-uri", "URI", item.uri ?? item.path),
      ].filter((entry) => entry.value),
      links: [identity, stringValue(item.uri ?? item.path)],
    })
  })
  return [section({
    id: "artifacts",
    title: "Artifacts",
    tone: "info",
    collapsed: rows.length > 25,
    rows,
  })]
}

function permissionSections(
  data: Record<string, unknown>,
  maximum: number,
): CommandResultSection[] {
  const permissions = listCandidates(data, [
    "permissions",
    "requests",
    "decisions",
    "rules",
  ])
  if (!permissions.length) return []
  const rows = permissions.slice(0, maximum).map((value, index) => {
    const item = record(value)
    const identity = stringValue(
      item.request_id ?? item.requestId ?? item.rule_id ?? item.ruleId ?? item.id,
    ) || `permission-${index + 1}`
    const decision = stringValue(
      item.decision ?? item.status ?? item.effect ?? item.action,
    )
    return row({
      id: `permission:${identity}`,
      kind: "permission",
      title: stringValue(item.tool_name ?? item.toolName ?? item.kind) || identity,
      summary: stringValue(item.reason ?? item.description ?? item.pattern),
      status: decision,
      tone: resultTone(decision),
      identity,
      fields: [
        field("permission-id", "Request", identity),
        field("permission-decision", "Decision", decision, resultTone(decision)),
        field("permission-owner", "Owner", item.owner_id ?? item.ownerId),
        field("permission-expiry", "Expires", item.expires_at ?? item.expiresAt),
      ].filter((entry) => entry.value),
      links: [identity],
    })
  })
  return [section({
    id: "permissions",
    title: "Permission decisions",
    tone: rows.some((entry) => entry.tone === "error") ? "warning" : "info",
    collapsed: rows.length > 25,
    rows,
  })]
}

function checkSections(
  data: Record<string, unknown>,
  maximum: number,
): CommandResultSection[] {
  const checks = listCandidates(data, [
    "checks",
    "probes",
    "results",
    "evaluations",
    "verifications",
    "diagnostics",
  ])
  if (!checks.length) return []
  const rows = checks.slice(0, maximum).map((value, index) => {
    const item = record(value)
    const identity = stringValue(
      item.check_id ?? item.checkId ?? item.probe_id ?? item.probeId ?? item.id,
    ) || `check-${index + 1}`
    const status = stringValue(
      item.status ?? item.result ?? item.outcome ?? item.health,
    )
    return row({
      id: `check:${identity}`,
      kind: "check",
      title: stringValue(item.title ?? item.name ?? item.owner) || identity,
      summary: stringValue(
        item.summary ?? item.message ?? item.reason ?? item.description,
      ),
      status,
      tone: resultTone(status),
      identity,
      fields: [
        field("check-id", "Check", identity),
        field("check-status", "Status", status, resultTone(status)),
        field("check-owner", "Owner", item.owner),
        field("check-duration", "Duration", item.duration_ms ?? item.durationMs),
      ].filter((entry) => entry.value),
      links: [identity],
    })
  })
  return [section({
    id: "checks",
    title: "Checks",
    tone: rows.some((entry) => entry.tone === "error")
      ? "error"
      : rows.some((entry) => entry.tone === "warning")
        ? "warning"
        : "success",
    collapsed: rows.length > 25,
    rows,
  })]
}

function eventRow(event: IndexedCommandCausalEvent): CommandResultRow {
  const status = event.terminal ? "terminal" : event.effective === false ? "no-op" : "effective"
  return row({
    id: `event:${event.eventId}`,
    kind: "event",
    title: event.eventType || "Runtime event",
    summary: event.summary || event.eventId,
    status,
    tone: event.effective === false ? "warning" : "info",
    timestamp: iso(event.committedAt ?? event.createdAt),
    identity: event.eventId,
    parentIdentity: event.causationId,
    fields: [
      field("event-id", "Event", event.eventId),
      field("event-span", "Span", event.spanId),
      field("event-mutation", "Mutation", event.mutationId),
      field("event-checkpoint", "Checkpoint", event.checkpointId),
      field("event-tool", "Tool call", event.toolCallId),
      field("event-failure", "Failure", event.failureId),
      field("event-recovery", "Recovery", event.recoveryId),
    ].filter((entry) => entry.value),
    links: [
      event.eventId,
      event.spanId ?? "",
      event.mutationId ?? "",
      event.checkpointId ?? "",
      event.toolCallId ?? "",
      ...(event.artifactIds ?? []),
    ],
  })
}

function traceSections(trace?: CommandProjectionTrace): CommandResultSection[] {
  if (!trace) return []
  const rows = trace.events.map(eventRow)
  const correlation = trace.correlation
  const identityFields = [
    field("trace-command", "Command", correlation.commandId),
    field("trace-request", "Request", correlation.requestId),
    field("trace-queue", "Queue", correlation.queueId),
    field("trace-spans", "Spans", correlation.spanIds.join(", ")),
    field("trace-mutations", "Mutations", correlation.mutationIds.join(", ")),
    field("trace-checkpoints", "Checkpoints", correlation.checkpointIds.join(", ")),
    field("trace-artifacts", "Artifacts", correlation.artifactIds.join(", ")),
    field("trace-tools", "Tool calls", correlation.toolCallIds.join(", ")),
    field("trace-failures", "Failures", correlation.failureIds.join(", ")),
    field("trace-recoveries", "Recoveries", correlation.recoveryIds.join(", ")),
  ].filter((entry) => entry.value)
  return [
    section({
      id: "correlation",
      title: "Causal identities",
      description: correlation.complete
        ? "Command receipt and runtime events are fully correlated."
        : `Missing correlation: ${correlation.missing.join(", ")}`,
      tone: correlation.complete ? "success" : "warning",
      collapsed: false,
      rows: [
        row({
          id: "correlation-identities",
          kind: "property",
          title: "Correlation",
          summary: correlation.complete ? "Complete" : "Incomplete",
          status: correlation.complete ? "complete" : "incomplete",
          tone: correlation.complete ? "success" : "warning",
          identity: correlation.commandId,
          fields: identityFields,
          links: [
            correlation.commandId,
            ...correlation.eventIds,
            ...correlation.spanIds,
            ...correlation.mutationIds,
            ...correlation.checkpointIds,
          ],
        }),
      ],
    }),
    section({
      id: "causal-events",
      title: "Causal events",
      description: `${trace.roots.length} root(s), ${trace.leaves.length} leaf/leaves.`,
      tone: trace.complete ? "info" : "warning",
      collapsed: rows.length > 50,
      rows,
    }),
  ]
}

function sideQuestionSections(
  receipt: CommandReceipt,
): CommandResultSection[] {
  if (receipt.name !== "/btw") return []
  const data = record(receipt.data)
  const answer = stringValue(
    data.answer ?? data.response ?? data.text ?? receipt.displayText,
  )
  const session = record(
    data.side_question ?? data.sideQuestion ?? data.session,
  )
  return [section({
    id: "side-question",
    title: "Isolated side question",
    description:
      "This single-turn answer did not mutate the main query session or tool history.",
    tone: receipt.error ? "error" : "info",
    collapsed: false,
    rows: [
      row({
        id: `answer:${receipt.commandId}`,
        kind: receipt.error ? "error" : "answer",
        title: receipt.error ? "Side question failed" : "Answer",
        summary: answer || receipt.summary,
        status: receipt.phase,
        tone: receipt.error ? "error" : "info",
        identity: receipt.commandId,
        fields: [
          field("btw-session", "Session", session.session_id ?? session.sessionId),
          field("btw-tools", "Tools enabled", session.tools_enabled ?? false),
          field("btw-isolated", "Main session mutated", session.main_session_mutated ?? false),
          field("btw-input", "Input tokens", receipt.usage?.inputTokens),
          field("btw-output", "Output tokens", receipt.usage?.outputTokens),
          field("btw-cost", "Cost USD", receipt.usage?.costUsd),
        ].filter((entry) => entry.value),
        links: [receipt.commandId, receipt.requestId],
      }),
    ],
  })]
}

function specializedSections(
  receipt: CommandReceipt,
  options: {
    trace?: CommandProjectionTrace
    maximumRows: number
  },
): CommandResultSection[] {
  const data = record(receipt.data)
  switch (receipt.name) {
    case "/graph":
      return [...graphSections(data, options.maximumRows), ...traceSections(options.trace)]
    case "/trace":
      return traceSections(options.trace)
    case "/artifacts":
      return [...artifactSections(data, options.maximumRows), ...traceSections(options.trace)]
    case "/permissions":
      return permissionSections(data, options.maximumRows)
    case "/doctor":
    case "/verify":
    case "/eval":
      return [...checkSections(data, options.maximumRows), ...traceSections(options.trace)]
    case "/btw":
      return sideQuestionSections(receipt)
    case "/inject":
    case "/change":
      return traceSections(options.trace)
    default:
      return traceSections(options.trace)
  }
}

function receiptFields(receipt: CommandReceipt): CommandResultField[] {
  return [
    field("receipt-command", "Command", receipt.commandId),
    field("receipt-request", "Request", receipt.requestId),
    field("receipt-queue", "Queue", receipt.queueId),
    field("receipt-task", "Task", receipt.taskId),
    field("receipt-run", "Run", receipt.runId),
    field("receipt-session", "Session", receipt.sessionId),
    field("receipt-phase", "Phase", receipt.phase, resultTone(receipt.phase)),
    field("receipt-scope", "Scope", receipt.scope),
    field("receipt-mode", "Mode", receipt.mode),
    field("receipt-priority", "Priority", receipt.priority),
    field("receipt-idempotency", "Idempotency", receipt.idempotencyKey),
    field("receipt-retry-of", "Retry of", receipt.retryOf),
    field("receipt-checkpoint", "Checkpoint", receipt.checkpointRef),
    field("receipt-revision-before", "Revision before", receipt.revisionBefore),
    field("receipt-revision-after", "Revision after", receipt.revisionAfter),
    field("receipt-events", "Events", receipt.eventIds.join(", ")),
    field("receipt-durable", "Durable", receipt.durable, receipt.durable ? "success" : "warning"),
    field("receipt-replayed", "Replayed", receipt.replayed),
    field("receipt-executed", "Executed", receipt.executed),
  ].filter((entry) => entry.value)
}

function errorSection(receipt: CommandReceipt): CommandResultSection[] {
  if (!receipt.error) return []
  const error = receipt.error
  return [section({
    id: "error",
    title: "Command error",
    tone: "error",
    collapsed: false,
    rows: [
      row({
        id: `error:${error.code}`,
        kind: "error",
        title: error.code,
        summary: error.message,
        status: receipt.phase,
        tone: "error",
        identity: receipt.commandId,
        fields: [
          field("error-code", "Code", error.code, "error"),
          field("error-message", "Message", error.message, "error"),
          field("error-retryable", "Retryable", error.retryable),
        ],
        links: [receipt.commandId, receipt.requestId],
      }),
    ],
  })]
}

function elapsed(receipt: CommandReceipt): number | undefined {
  if (!receipt.finishedAt) return receipt.usage?.durationMs
  const value = Date.parse(receipt.finishedAt) - Date.parse(receipt.createdAt)
  return Number.isFinite(value) && value >= 0 ? value : receipt.usage?.durationMs
}

function title(receipt: CommandReceipt): string {
  return {
    "/status": "Runtime status",
    "/graph": "Task graph",
    "/trace": "Causal trace",
    "/artifacts": "Artifacts",
    "/permissions": "Permissions",
    "/context": "Context inspector",
    "/compact": "Context compact",
    "/memory": "Memory inspector",
    "/model": "Provider and model route",
    "/mcp": "MCP servers",
    "/skills": "Skills",
    "/agents": "Agents and subagents",
    "/tasks": "Subagent tasks",
    "/resume": "Session resume",
    "/rewind": "Session rewind",
    "/export": "Session export",
    "/btw": "Side question",
    "/inject": "Fault injection",
    "/change": "Requirement change",
    "/verify": "Verification",
    "/eval": "Evaluation",
    "/doctor": "Runtime doctor",
  }[receipt.name] ?? `${receipt.name} result`
}

export function buildCommandResultModel(
  receipt: CommandReceipt,
  options: ResultModelOptions = {},
): CommandResultModel {
  const maximumDepth = Math.max(1, Math.min(20, options.maximumDepth ?? 8))
  const maximumRows = Math.max(1, Math.min(5000, options.maximumRows ?? 500))
  const maximumValueLength = Math.max(
    32,
    Math.min(100_000, options.maximumValueLength ?? 4000),
  )
  const redactKeys = new Set(
    (options.redactKeys ?? []).map((value) => value.trim().toLowerCase()),
  )
  const genericRows = propertyRows(receipt.data, {
    maximumDepth,
    maximumRows,
    maximumValueLength,
    redactKeys,
  })
  const specialized = specializedSections(receipt, {
    trace: options.trace,
    maximumRows,
  })
  const claimedPaths = new Set(
    specialized.flatMap((entry) =>
      entry.rows.flatMap((item) => item.fields.map((value) => value.label.toLowerCase()))),
  )
  const remaining = genericRows.filter((item) =>
    !claimedPaths.has(item.title.toLowerCase()))
  const sections = [
    ...errorSection(receipt),
    ...specialized,
    ...(remaining.length
      ? [section({
          id: "details",
          title: "Details",
          tone: "neutral",
          collapsed: specialized.length > 0 || remaining.length > 30,
          rows: remaining,
        })]
      : []),
  ]
  const diagnostics: string[] = []
  if (receipt.phase === "queued" && !receipt.queueId) {
    diagnostics.push("Queued receipt has no backend queue identity.")
  }
  if (receipt.durable && !receipt.eventIds.length && receipt.phase === "applied") {
    diagnostics.push("Durable applied receipt has no causal event identity.")
  }
  if (options.trace && !options.trace.complete) {
    diagnostics.push(
      `Causal trace incomplete: ${[
        ...options.trace.correlation.missing,
        ...options.trace.missingParents,
      ].join(", ")}`,
    )
  }
  const topFields = receiptFields(receipt)
  const searchText = [
    title(receipt),
    receipt.summary,
    receipt.displayText,
    receipt.phase,
    receipt.error?.code,
    receipt.error?.message,
    ...topFields.flatMap((entry) => [entry.label, entry.value]),
    ...sections.flatMap((entry) =>
      entry.rows.map((item) => item.searchableText)),
  ].filter(Boolean).join(" ").toLowerCase()
  return Object.freeze({
    schema: "zyra.command-result-model/v1",
    commandId: receipt.commandId,
    requestId: receipt.requestId,
    name: receipt.name,
    phase: receipt.phase,
    title: title(receipt),
    summary: receipt.summary,
    displayText: receipt.displayText,
    tone: receipt.error ? "error" : resultTone(receipt.phase),
    terminal: terminal(receipt.phase),
    retryable: receipt.error?.retryable ?? receipt.phase === "expired",
    durable: receipt.durable,
    replayed: receipt.replayed,
    createdAt: receipt.createdAt,
    finishedAt: receipt.finishedAt,
    elapsedMs: elapsed(receipt),
    fields: Object.freeze(topFields),
    sections: Object.freeze(sections),
    usage: receipt.usage ? Object.freeze({ ...receipt.usage }) : undefined,
    error: receipt.error
      ? Object.freeze({
          ...receipt.error,
          details: Object.freeze({ ...receipt.error.details }),
        })
      : undefined,
    eventIds: Object.freeze([...receipt.eventIds]),
    checkpointRef: receipt.checkpointRef,
    searchText,
    diagnostics: Object.freeze(diagnostics),
  })
}

export function filterCommandResult(
  model: CommandResultModel,
  query: string,
): CommandResultModel {
  const terms = query
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
  if (!terms.length) return model
  const sections = model.sections
    .map((entry) => ({
      ...entry,
      rows: entry.rows.filter((item) =>
        terms.every((term) => item.searchableText.includes(term))),
    }))
    .filter((entry) => entry.rows.length)
    .map((entry) => Object.freeze({
      ...entry,
      rows: Object.freeze(entry.rows),
      count: entry.rows.length,
    }))
  return Object.freeze({
    ...model,
    sections: Object.freeze(sections),
    searchText: terms.join(" "),
  })
}

export function windowCommandResult(
  model: CommandResultModel,
  offset: number,
  limit: number,
): CommandResultModel {
  let remainingOffset = Math.max(0, Math.floor(offset))
  let remainingLimit = Math.max(0, Math.min(5000, Math.floor(limit)))
  const sections: CommandResultSection[] = []
  for (const entry of model.sections) {
    if (remainingLimit <= 0) break
    if (remainingOffset >= entry.rows.length) {
      remainingOffset -= entry.rows.length
      continue
    }
    const selected = entry.rows.slice(
      remainingOffset,
      remainingOffset + remainingLimit,
    )
    remainingOffset = 0
    remainingLimit -= selected.length
    sections.push(Object.freeze({
      ...entry,
      rows: Object.freeze(selected),
      count: selected.length,
    }))
  }
  return Object.freeze({
    ...model,
    sections: Object.freeze(sections),
  })
}
