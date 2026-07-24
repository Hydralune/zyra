import {
  createIdentity,
  createIdempotencyKey,
  normalizeIdempotencyKey,
} from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type {
  ControlReceiptPhase,
  TopologyControlAction,
  TopologyControlReceipt,
  TopologyControlRequest,
  TopologyControlTransport,
  TopologyGraphModel,
} from "./contracts.ts"

export interface TopologyControlRuntimeOptions {
  transport: TopologyControlTransport
  now?: () => number
  maximumReceipts?: number
  requestId?: () => string
}

interface NormalizedControlRequest extends TopologyControlRequest {
  commandName: string
  commandText: string
  requestId: string
  commandId: string
  idempotencyKey: string
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function boundedText(value: unknown, label: string, maximum: number): string {
  const text = String(value ?? "").trim()
  if (!text) throw new TypeError(`${label} must not be empty.`)
  if (new TextEncoder().encode(text).byteLength > maximum) {
    throw new TypeError(`${label} exceeds ${maximum} bytes.`)
  }
  return text
}

function identifier(value: unknown, label: string): string {
  const text = boundedText(value, label, 256)
  if (/[\u0000-\u001f\u007f]/.test(text)) {
    throw new TypeError(`${label} contains control characters.`)
  }
  return text
}

function safeInteger(value: unknown, label: string): number | undefined {
  if (value === undefined || value === null || value === "") return undefined
  const number = Number(value)
  if (!Number.isSafeInteger(number) || number < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer.`)
  }
  return number
}

function randomId(prefix: string): string {
  const uuid =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${uuid}`
}

function commandForAction(
  request: TopologyControlRequest,
): { name: string; text: string; arguments: Record<string, unknown> } {
  if (request.action === "time-travel") {
    const checkpointId = identifier(request.checkpointId, "Checkpoint id")
    return {
      name: "/rewind",
      text: `/rewind ${checkpointId}`,
      arguments: {
        raw: checkpointId,
        target: checkpointId,
        checkpoint_ref: checkpointId,
        expected_revision: safeInteger(request.expectedRevision, "Expected revision"),
      },
    }
  }
  if (request.action === "resume-checkpoint") {
    const sessionId = identifier(
      request.sessionId ?? request.checkpointId,
      "Session or checkpoint id",
    )
    return {
      name: "/resume",
      text: `/resume ${sessionId}`,
      arguments: {
        raw: sessionId,
        target: sessionId,
        target_session_id: request.sessionId ?? sessionId,
        checkpoint_ref: request.checkpointId,
        expected_revision: safeInteger(request.expectedRevision, "Expected revision"),
      },
    }
  }
  if (request.action === "requirement-change") {
    const text = boundedText(request.text, "Requirement change", 32 * 1_024)
    return {
      name: "/change",
      text: `/change ${text}`,
      arguments: {
        raw: text,
        requirement: text,
        node_id: request.nodeId,
        expected_revision: safeInteger(request.expectedRevision, "Expected revision"),
        ...(request.fields ?? {}),
      },
    }
  }
  const nodeId = identifier(request.nodeId, "Node id")
  const fields = request.fields ?? {}
  if (Object.keys(fields).length === 0 && !request.text?.trim()) {
    throw new TypeError("Local update requires at least one changed field.")
  }
  const payload = {
    node_id: nodeId,
    expected_revision: safeInteger(request.expectedRevision, "Expected revision"),
    change_scope: "local_update_state",
    fields,
    note: request.text?.trim() || undefined,
  }
  const raw = JSON.stringify(payload)
  return {
    name: "/change",
    text: `/change ${raw}`,
    arguments: { ...payload, raw },
  }
}

function normalizeRequest(
  request: TopologyControlRequest,
  requestId: () => string,
): NormalizedControlRequest {
  const taskId = identifier(request.taskId, "Task id")
  const runId = identifier(request.runId, "Run id")
  const actorId = identifier(request.actorId || "zyra-web-topology", "Actor id")
  const command = commandForAction(request)
  const generatedRequestId = requestId()
  const commandId = createIdentity("control_command")
  const binding = { taskId, runId, controlCommandId: commandId }
  const body = {
    text: command.text,
    arguments: command.arguments,
    request_id: generatedRequestId,
    command_id: commandId,
    actor_id: actorId,
    sealed: request.sealed,
    competition_mode: request.sealed ? "sealed_autonomous" : "interactive",
    expected_session_revision: safeInteger(
      request.expectedRevision,
      "Expected revision",
    ),
  }
  return Object.freeze({
    ...request,
    taskId,
    runId,
    actorId,
    commandName: command.name,
    commandText: command.text,
    requestId: generatedRequestId,
    commandId,
    idempotencyKey: normalizeIdempotencyKey(
      createIdempotencyKey("task.control-command", binding, body),
    ),
    fields: Object.freeze({ ...request.fields, __command_arguments: command.arguments }),
  })
}

function rawRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? Object.freeze({ ...(value as Record<string, unknown>) })
    : Object.freeze({})
}

function recordAt(
  value: Readonly<Record<string, unknown>>,
  key: string,
): Readonly<Record<string, unknown>> {
  return rawRecord(value[key])
}

function stringAt(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): string | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim()
  }
  return undefined
}

function booleanAt(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): boolean | undefined {
  for (const key of keys) {
    if (typeof value[key] === "boolean") return Boolean(value[key])
  }
  return undefined
}

function numberAt(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): number | undefined {
  for (const key of keys) {
    const candidate = Number(value[key])
    if (Number.isFinite(candidate)) return candidate
  }
  return undefined
}

function stringsAt(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): readonly string[] {
  for (const key of keys) {
    const candidate = value[key]
    if (Array.isArray(candidate)) {
      return freeze(candidate.map(String).map((item) => item.trim()).filter(Boolean))
    }
  }
  return freeze([])
}

function responsePhase(
  raw: Readonly<Record<string, unknown>>,
): ControlReceiptPhase {
  const result = recordAt(raw, "command_result")
  const status = (
    stringAt(result, "status", "phase") ??
    stringAt(raw, "status", "phase") ??
    ""
  ).toLocaleLowerCase()
  const ok = booleanAt(result, "ok") ?? booleanAt(raw, "ok")
  const error = stringAt(raw, "error") ?? stringAt(result, "error_code", "error")
  if (
    status.includes("denied") ||
    status.includes("forbidden") ||
    error?.includes("permission") ||
    error?.includes("sealed")
  ) return "denied"
  if (
    status.includes("queued") ||
    status.includes("pending") ||
    status.includes("waiting")
  ) return "pending"
  if (
    status.includes("failed") ||
    status.includes("rejected") ||
    status.includes("conflict") ||
    ok === false ||
    error
  ) return "failed"
  return "committed"
}

function receiptFromResponse(
  request: NormalizedControlRequest,
  previous: TopologyControlReceipt,
  response: Readonly<Record<string, unknown>>,
  now: number,
): TopologyControlReceipt {
  const result = recordAt(response, "command_result")
  const nestedResult = recordAt(result, "result")
  const data = recordAt(result, "data")
  const phase = responsePhase(response)
  const error = recordAt(result, "error")
  const checkpointId =
    stringAt(nestedResult, "checkpoint_ref", "checkpoint_id") ??
    stringAt(data, "checkpoint_ref", "checkpoint_id") ??
    stringAt(response, "checkpoint_id") ??
    request.checkpointId
  const observedEventIds = freeze([
    ...stringsAt(result, "event_ids", "observed_event_ids"),
    ...stringsAt(nestedResult, "event_ids", "observed_event_ids"),
    ...stringsAt(data, "event_ids", "observed_event_ids"),
    ...(() => {
      const event = recordAt(response, "event")
      const id = stringAt(event, "event_id", "id")
      return id ? [id] : []
    })(),
  ])
  const summary =
    stringAt(result, "summary", "display_text", "message") ??
    stringAt(response, "summary", "message") ??
    (phase === "committed"
      ? `${request.commandName} committed.`
      : phase === "pending"
        ? `${request.commandName} is pending.`
        : phase === "denied"
          ? `${request.commandName} was denied.`
          : `${request.commandName} failed.`)
  return Object.freeze({
    ...previous,
    phase,
    checkpointId,
    observedRevision:
      numberAt(nestedResult, "revision", "commit_revision", "graph_revision") ??
      numberAt(data, "revision", "commit_revision", "graph_revision"),
    observedEventIds,
    updatedAt: now,
    summary,
    errorCode:
      stringAt(error, "code") ??
      stringAt(result, "error_code") ??
      stringAt(response, "error", "code"),
    errorMessage:
      stringAt(error, "message") ??
      stringAt(result, "error_message") ??
      stringAt(response, "message"),
    denied: phase === "denied",
    interventionCounted:
      booleanAt(result, "intervention_counted") ??
      booleanAt(data, "intervention_counted") ??
      booleanAt(response, "intervention_counted") ??
      (phase === "denied" && request.sealed),
    replayed:
      booleanAt(result, "replayed") ??
      booleanAt(response, "replayed", "receipt_replayed") ??
      false,
    raw: response,
  })
}

function failedReceipt(
  previous: TopologyControlReceipt,
  error: unknown,
  now: number,
): TopologyControlReceipt {
  const message = error instanceof Error ? error.message : String(error)
  const code =
    error && typeof error === "object" && "code" in error
      ? String((error as { code: unknown }).code)
      : "topology_control_failed"
  const denied =
    /permission|denied|forbidden|sealed|intervention/i.test(`${code} ${message}`)
  return Object.freeze({
    ...previous,
    phase: denied ? "denied" : "failed",
    updatedAt: now,
    summary: denied
      ? `${previous.commandName} was denied by the backend policy owner.`
      : `${previous.commandName} failed.`,
    errorCode: code,
    errorMessage: message,
    denied,
    interventionCounted: denied && previous.raw?.sealed === true,
  })
}

export class TopologyControlRuntime {
  readonly transport: TopologyControlTransport
  readonly now: () => number
  readonly maximumReceipts: number
  readonly requestId: () => string
  #receipts = new Map<string, TopologyControlReceipt>()
  #abort = new Map<string, AbortController>()
  #listeners = new Set<() => void>()
  #closed = false

  constructor(options: TopologyControlRuntimeOptions) {
    this.transport = options.transport
    this.now = options.now ?? Date.now
    this.maximumReceipts = Math.max(10, Math.floor(options.maximumReceipts ?? 200))
    this.requestId = options.requestId ?? (() => createIdentity("request"))
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  receipts(): readonly TopologyControlReceipt[] {
    return freeze(
      [...this.#receipts.values()].sort(
        (left, right) =>
          right.updatedAt - left.updatedAt || left.id.localeCompare(right.id),
      ),
    )
  }

  receipt(id: string): TopologyControlReceipt | undefined {
    return this.#receipts.get(id)
  }

  inFlight(): readonly TopologyControlReceipt[] {
    return freeze(
      this.receipts().filter((receipt) =>
        ["validating", "submitting", "pending"].includes(receipt.phase),
      ),
    )
  }

  async submit(
    request: TopologyControlRequest,
  ): Promise<TopologyControlReceipt> {
    this.#assertOpen()
    let normalized: NormalizedControlRequest
    try {
      normalized = normalizeRequest(request, this.requestId)
    } catch (error) {
      const now = this.now()
      const receipt = Object.freeze({
        id: randomId("topology-receipt"),
        action: request.action,
        phase: "failed" as const,
        taskId: String(request.taskId || ""),
        runId: String(request.runId || ""),
        commandName: commandNameForAction(request.action),
        requestId: "",
        commandId: "",
        idempotencyKey: "",
        checkpointId: request.checkpointId,
        expectedRevision: request.expectedRevision,
        observedEventIds: freeze([]),
        submittedAt: now,
        updatedAt: now,
        summary: "Topology control validation failed.",
        errorCode: "topology_control_validation_failed",
        errorMessage: error instanceof Error ? error.message : String(error),
        denied: false,
        interventionCounted: false,
        replayed: false,
      })
      this.#remember(receipt)
      return receipt
    }
    const now = this.now()
    const receiptId = `receipt:${normalized.commandId}`
    const initial: TopologyControlReceipt = Object.freeze({
      id: receiptId,
      action: normalized.action,
      phase: "submitting",
      taskId: normalized.taskId,
      runId: normalized.runId,
      commandName: normalized.commandName,
      requestId: normalized.requestId,
      commandId: normalized.commandId,
      idempotencyKey: normalized.idempotencyKey,
      checkpointId: normalized.checkpointId,
      expectedRevision: normalized.expectedRevision,
      observedEventIds: freeze([]),
      submittedAt: now,
      updatedAt: now,
      summary: `Submitting ${normalized.commandName}.`,
      denied: false,
      interventionCounted: false,
      replayed: false,
      raw: Object.freeze({
        sealed: normalized.sealed,
        actor_id: normalized.actorId,
        command_text: normalized.commandText,
      }),
    })
    this.#remember(initial)
    const controller = new AbortController()
    this.#abort.set(receiptId, controller)
    try {
      const response = await this.transport.submit(normalized, controller.signal)
      const next = receiptFromResponse(
        normalized,
        initial,
        rawRecord(response),
        this.now(),
      )
      this.#remember(next)
      return next
    } catch (error) {
      const next =
        controller.signal.aborted
          ? Object.freeze({
              ...initial,
              phase: "cancelled" as const,
              updatedAt: this.now(),
              summary: `${initial.commandName} submission cancelled.`,
              errorCode: "topology_control_cancelled",
              errorMessage: String(controller.signal.reason ?? "cancelled"),
            })
          : failedReceipt(initial, error, this.now())
      this.#remember(next)
      return next
    } finally {
      this.#abort.delete(receiptId)
    }
  }

  cancel(receiptId: string, reason = "Cancelled from topology control panel."): boolean {
    const controller = this.#abort.get(receiptId)
    if (!controller) return false
    controller.abort(reason)
    return true
  }

  observe(model: TopologyGraphModel): readonly TopologyControlReceipt[] {
    let changed = false
    for (const receipt of this.#receipts.values()) {
      if (!["pending", "committed"].includes(receipt.phase)) continue
      const observed = observedEffect(receipt, model)
      if (!observed) continue
      const next = Object.freeze({
        ...receipt,
        phase: "committed" as const,
        observedRevision: observed.revision,
        observedEventIds: freeze([
          ...new Set([...receipt.observedEventIds, ...observed.eventIds]),
        ]),
        updatedAt: this.now(),
        summary: observed.summary,
      })
      this.#receipts.set(next.id, next)
      changed = true
    }
    if (changed) this.#publish()
    return this.receipts()
  }

  pruneTerminal(before: number): number {
    let removed = 0
    for (const receipt of this.#receipts.values()) {
      if (
        receipt.updatedAt < before &&
        ["committed", "denied", "failed", "cancelled"].includes(receipt.phase)
      ) {
        this.#receipts.delete(receipt.id)
        removed += 1
      }
    }
    if (removed > 0) this.#publish()
    return removed
  }

  close(reason = "Topology control runtime closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const controller of this.#abort.values()) controller.abort(reason)
    this.#abort.clear()
    this.#listeners.clear()
  }

  #remember(receipt: TopologyControlReceipt): void {
    this.#receipts.set(receipt.id, receipt)
    if (this.#receipts.size > this.maximumReceipts) {
      const removable = [...this.#receipts.values()]
        .filter((value) => !["validating", "submitting", "pending"].includes(value.phase))
        .sort(
          (left, right) =>
            left.updatedAt - right.updatedAt || left.id.localeCompare(right.id),
        )
      for (const value of removable.slice(
        0,
        this.#receipts.size - this.maximumReceipts,
      )) {
        this.#receipts.delete(value.id)
      }
    }
    this.#publish()
  }

  #publish(): void {
    for (const listener of this.#listeners) listener()
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Topology control runtime is closed.")
  }
}

function commandNameForAction(action: TopologyControlAction): string {
  if (action === "time-travel") return "/rewind"
  if (action === "resume-checkpoint") return "/resume"
  return "/change"
}

function observedEffect(
  receipt: TopologyControlReceipt,
  model: TopologyGraphModel,
): { revision: number; eventIds: readonly string[]; summary: string } | undefined {
  if (
    receipt.expectedRevision !== undefined &&
    model.commitRevision <= receipt.expectedRevision
  ) {
    return undefined
  }
  if (receipt.action === "time-travel" || receipt.action === "resume-checkpoint") {
    const checkpoint = receipt.checkpointId
      ? model.checkpointById.get(receipt.checkpointId)
      : model.checkpoints.find((value) =>
          value.evidence.controlCommandIds.includes(receipt.commandId),
        )
    if (!checkpoint) return undefined
    const correlated =
      checkpoint.evidence.controlCommandIds.includes(receipt.commandId) ||
      checkpoint.evidence.eventIds.some((id) =>
        receipt.observedEventIds.includes(id),
      )
    if (!correlated) return undefined
    return {
      revision: Math.max(checkpoint.commitRevision, model.commitRevision),
      eventIds: checkpoint.evidence.eventIds,
      summary: `${receipt.commandName} observed in checkpoint ${checkpoint.id} at commit revision ${checkpoint.commitRevision}.`,
    }
  }
  const change = model.changes.find(
    (value) =>
      value.evidence.controlCommandIds.includes(receipt.commandId) ||
      value.evidence.eventIds.some((id) => receipt.observedEventIds.includes(id)),
  )
  if (!change) return undefined
  return {
    revision: Math.max(change.revision, model.commitRevision),
    eventIds: change.evidence.eventIds,
    summary: `${receipt.commandName} observed as requirement change ${change.id} affecting ${change.affectedNodeIds.length} nodes.`,
  }
}

export function topologyControlTransport(
  submit: (
    input: {
      taskId: string
      runId: string
      text: string
      arguments: Readonly<Record<string, unknown>>
      requestId: string
      commandId: string
      idempotencyKey: string
      actorId: string
      sealed: boolean
      expectedRevision?: number
      signal?: AbortSignal
    },
  ) => Promise<Readonly<Record<string, unknown>>>,
): TopologyControlTransport {
  return Object.freeze({
    submit(request: TopologyControlRequest, signal?: AbortSignal) {
      const normalized = request as NormalizedControlRequest
      const argumentsValue =
        normalized.fields &&
        "__command_arguments" in normalized.fields &&
        normalized.fields.__command_arguments &&
        typeof normalized.fields.__command_arguments === "object"
          ? (normalized.fields.__command_arguments as Readonly<Record<string, unknown>>)
          : Object.freeze({})
      return submit({
        taskId: normalized.taskId,
        runId: normalized.runId,
        text: normalized.commandText,
        arguments: argumentsValue,
        requestId: normalized.requestId,
        commandId: normalized.commandId,
        idempotencyKey: normalized.idempotencyKey,
        actorId: normalized.actorId,
        sealed: normalized.sealed,
        expectedRevision: normalized.expectedRevision,
        signal,
      })
    },
  })
}
