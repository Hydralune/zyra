export const TERMINAL_PROTOCOL = "zyra.terminal.v1"
export const TERMINAL_SOCKET_PROTOCOL = "zyra-terminal-v1"

export type TerminalPhase =
  | "opening"
  | "running"
  | "permission_pending"
  | "exited"
  | "killed"
  | "timed_out"
  | "crashed"
  | "rejected"

export type TerminalControlKind =
  | "hello"
  | "output"
  | "cursor"
  | "resync"
  | "backpressure"
  | "spill"
  | "status"
  | "error"
  | "pong"

export interface TerminalBinding {
  taskId: string
  runId: string
  terminalId: string
  sessionId: string
  workspaceId: string
  workspaceRevision: number
  workerId: string
  commandId: string
  toolCallId: string
  spanId: string
}

export interface TerminalPermissionProjection {
  effect: "allow" | "ask" | "deny"
  decisionId: string
  requestId?: string
  permitId?: string
  reasonCode: string
  reason: string
  canonicalOwner: "typescript.PermissionCoordinator"
}

export interface TerminalSpillProjection {
  artifactId: string
  revision: string
  mediaType: string
  sha256: string
  byteLength: number
  firstCursor: number
  nextCursor: number
  binary: boolean
  redacted: boolean
}

export interface TerminalStatusProjection {
  phase: TerminalPhase
  pid?: number
  cwd: string
  title: string
  rows: number
  cols: number
  cursor: number
  earliestCursor: number
  exitCode?: number
  signal?: string
  startedAt: string
  updatedAt: string
  exitedAt?: string
  viewers: number
  spilledBytes: number
  binaryBytes: number
  inputSequence: number
  resizeSequence: number
  stateMutationId: string
}

export interface TerminalSessionProjection {
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  status: TerminalStatusProjection
  permission: TerminalPermissionProjection
  socketPath: string
  ticket?: string
  ticketExpiresAt?: string
  correlationId: string
  causationId: string
  eventId?: string
  interventionCounted: boolean
  humanInterventionCount: number
}

export interface TerminalOutputFrame {
  kind: "output"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  firstCursor: number
  nextCursor: number
  text: string
  byteLength: number
  sha256: string
  redacted: boolean
  sequence: number
}

export interface TerminalHelloFrame {
  kind: "hello"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  status: TerminalStatusProjection
  acceptedCursor: number
  earliestCursor: number
  connectionId: string
  heartbeatMs: number
  maximumUnackedBytes: number
}

export interface TerminalCursorFrame {
  kind: "cursor"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  cursor: number
  sequence: number
}

export interface TerminalResyncFrame {
  kind: "resync"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  requestedCursor: number
  acceptedCursor: number
  earliestCursor: number
  reason: "stale_cursor" | "future_cursor" | "spill_boundary" | "generation_changed"
  spills: readonly TerminalSpillProjection[]
}

export interface TerminalBackpressureFrame {
  kind: "backpressure"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  unackedBytes: number
  maximumUnackedBytes: number
  paused: boolean
  requiredCursor: number
}

export interface TerminalSpillFrame {
  kind: "spill"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  spill: TerminalSpillProjection
}

export interface TerminalStatusFrame {
  kind: "status"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  status: TerminalStatusProjection
}

export interface TerminalErrorFrame {
  kind: "error"
  protocol: typeof TERMINAL_PROTOCOL
  binding?: TerminalBinding
  code: string
  message: string
  retryable: boolean
  closeCode?: number
}

export interface TerminalPongFrame {
  kind: "pong"
  protocol: typeof TERMINAL_PROTOCOL
  binding: TerminalBinding
  nonce: string
  serverTime: string
}

export type TerminalServerFrame =
  | TerminalHelloFrame
  | TerminalOutputFrame
  | TerminalCursorFrame
  | TerminalResyncFrame
  | TerminalBackpressureFrame
  | TerminalSpillFrame
  | TerminalStatusFrame
  | TerminalErrorFrame
  | TerminalPongFrame

export interface TerminalInputCommand {
  kind: "input"
  protocol: typeof TERMINAL_PROTOCOL
  terminalId: string
  connectionId: string
  sequence: number
  data: string
  actorId: string
  permissionPermitId?: string
}

export interface TerminalResizeCommand {
  kind: "resize"
  protocol: typeof TERMINAL_PROTOCOL
  terminalId: string
  connectionId: string
  sequence: number
  rows: number
  cols: number
}

export interface TerminalAckCommand {
  kind: "ack"
  protocol: typeof TERMINAL_PROTOCOL
  terminalId: string
  connectionId: string
  sequence: number
  cursor: number
}

export interface TerminalKillCommand {
  kind: "kill"
  protocol: typeof TERMINAL_PROTOCOL
  terminalId: string
  connectionId: string
  sequence: number
  reason: string
  actorId: string
  permissionPermitId?: string
}

export interface TerminalPingCommand {
  kind: "ping"
  protocol: typeof TERMINAL_PROTOCOL
  terminalId: string
  connectionId: string
  nonce: string
}

export type TerminalClientCommand =
  | TerminalInputCommand
  | TerminalResizeCommand
  | TerminalAckCommand
  | TerminalKillCommand
  | TerminalPingCommand

export class TerminalContractError extends TypeError {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "TerminalContractError"
    this.code = code
  }
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TerminalContractError("terminal_contract_object", `${label} must be an object.`)
  }
  return value as Record<string, unknown>
}

function text(
  value: unknown,
  label: string,
  options: { minimum?: number; maximum?: number; pattern?: RegExp; allowEmpty?: boolean } = {},
): string {
  if (typeof value !== "string") {
    throw new TerminalContractError("terminal_contract_text", `${label} must be text.`)
  }
  const minimum = options.allowEmpty ? 0 : (options.minimum ?? 1)
  const maximum = options.maximum ?? 4_096
  if (value.length < minimum || value.length > maximum) {
    throw new TerminalContractError(
      "terminal_contract_text_length",
      `${label} must contain ${minimum}..${maximum} characters.`,
    )
  }
  if (/[\u0000\r\n]/u.test(value) && options.pattern) {
    throw new TerminalContractError("terminal_contract_text_control", `${label} contains a control character.`)
  }
  if (options.pattern && !options.pattern.test(value)) {
    throw new TerminalContractError("terminal_contract_text_format", `${label} has an invalid format.`)
  }
  return value
}

const IDENTITY = /^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/u
const DIGEST = /^[a-f0-9]{64}$/u

function identity(value: unknown, label: string): string {
  return text(value, label, { maximum: 255, pattern: IDENTITY })
}

function optionalIdentity(value: unknown, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return identity(value, label)
}

function integer(
  value: unknown,
  label: string,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new TerminalContractError(
      "terminal_contract_integer",
      `${label} must be a safe integer in ${minimum}..${maximum}.`,
    )
  }
  return Number(value)
}

function optionalInteger(
  value: unknown,
  label: string,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number | undefined {
  if (value === undefined || value === null) return undefined
  return integer(value, label, minimum, maximum)
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new TerminalContractError("terminal_contract_boolean", `${label} must be boolean.`)
  }
  return value
}

function timestamp(value: unknown, label: string): string {
  const selected = text(value, label, { maximum: 64 })
  if (!Number.isFinite(Date.parse(selected))) {
    throw new TerminalContractError("terminal_contract_timestamp", `${label} must be an ISO timestamp.`)
  }
  return selected
}

function literal<T extends string>(
  value: unknown,
  label: string,
  options: readonly T[],
): T {
  if (typeof value !== "string" || !options.includes(value as T)) {
    throw new TerminalContractError(
      "terminal_contract_literal",
      `${label} must be one of ${options.join(", ")}.`,
    )
  }
  return value as T
}

function protocol(value: unknown): typeof TERMINAL_PROTOCOL {
  if (value !== TERMINAL_PROTOCOL) {
    throw new TerminalContractError(
      "terminal_protocol_unsupported",
      `Terminal protocol must be ${TERMINAL_PROTOCOL}.`,
    )
  }
  return TERMINAL_PROTOCOL
}

export function parseTerminalBinding(value: unknown): TerminalBinding {
  const input = record(value, "terminal binding")
  return Object.freeze({
    taskId: identity(input.task_id ?? input.taskId, "terminal task"),
    runId: identity(input.run_id ?? input.runId, "terminal run"),
    terminalId: identity(input.terminal_id ?? input.terminalId, "terminal"),
    sessionId: identity(input.session_id ?? input.sessionId, "terminal session"),
    workspaceId: identity(input.workspace_id ?? input.workspaceId, "terminal workspace"),
    workspaceRevision: integer(
      input.workspace_revision ?? input.workspaceRevision,
      "terminal workspace revision",
    ),
    workerId: identity(input.worker_id ?? input.workerId, "terminal worker"),
    commandId: identity(input.command_id ?? input.commandId, "terminal command"),
    toolCallId: identity(input.tool_call_id ?? input.toolCallId, "terminal tool call"),
    spanId: identity(input.span_id ?? input.spanId, "terminal span"),
  })
}

export function sameTerminalBinding(
  left: TerminalBinding,
  right: TerminalBinding,
): boolean {
  return (
    left.taskId === right.taskId
    && left.runId === right.runId
    && left.terminalId === right.terminalId
    && left.sessionId === right.sessionId
    && left.workspaceId === right.workspaceId
    && left.workspaceRevision === right.workspaceRevision
    && left.workerId === right.workerId
    && left.commandId === right.commandId
    && left.toolCallId === right.toolCallId
    && left.spanId === right.spanId
  )
}

export function terminalBindingKey(binding: TerminalBinding): string {
  return [
    binding.taskId,
    binding.runId,
    binding.terminalId,
    binding.sessionId,
    binding.workspaceId,
    binding.workspaceRevision,
    binding.workerId,
    binding.commandId,
    binding.toolCallId,
    binding.spanId,
  ].join("|")
}

export function parseTerminalPermission(value: unknown): TerminalPermissionProjection {
  const input = record(value, "terminal permission")
  const owner = text(
    input.canonical_owner ?? input.canonicalOwner,
    "terminal permission owner",
    { maximum: 128 },
  )
  if (owner !== "typescript.PermissionCoordinator") {
    throw new TerminalContractError(
      "terminal_permission_owner_invalid",
      "Terminal permission must come from typescript.PermissionCoordinator.",
    )
  }
  return Object.freeze({
    effect: literal(input.effect, "terminal permission effect", ["allow", "ask", "deny"] as const),
    decisionId: identity(input.decision_id ?? input.decisionId, "terminal permission decision"),
    requestId: optionalIdentity(input.request_id ?? input.requestId, "terminal permission request"),
    permitId: optionalIdentity(input.permit_id ?? input.permitId, "terminal permission permit"),
    reasonCode: identity(input.reason_code ?? input.reasonCode, "terminal permission reason code"),
    reason: text(input.reason, "terminal permission reason", { maximum: 4_096 }),
    canonicalOwner: "typescript.PermissionCoordinator",
  })
}

export function parseTerminalSpill(value: unknown): TerminalSpillProjection {
  const input = record(value, "terminal spill")
  const firstCursor = integer(input.first_cursor ?? input.firstCursor, "terminal spill first cursor")
  const nextCursor = integer(input.next_cursor ?? input.nextCursor, "terminal spill next cursor")
  if (nextCursor < firstCursor) {
    throw new TerminalContractError(
      "terminal_spill_cursor_order",
      "Terminal spill next cursor precedes its first cursor.",
    )
  }
  const byteLength = integer(
    input.byte_length ?? input.byteLength,
    "terminal spill byte length",
    0,
    1_099_511_627_776,
  )
  if (byteLength !== nextCursor - firstCursor) {
    throw new TerminalContractError(
      "terminal_spill_cursor_length",
      "Terminal spill byte length does not match its cursor range.",
    )
  }
  return Object.freeze({
    artifactId: identity(input.artifact_id ?? input.artifactId, "terminal spill artifact"),
    revision: identity(input.revision, "terminal spill revision"),
    mediaType: text(input.media_type ?? input.mediaType, "terminal spill media type", {
      maximum: 255,
      pattern: /^[a-z0-9][a-z0-9.+-]*\/[a-z0-9][a-z0-9.+-]*$/u,
    }),
    sha256: text(input.sha256, "terminal spill digest", { minimum: 64, maximum: 64, pattern: DIGEST }),
    byteLength,
    firstCursor,
    nextCursor,
    binary: booleanValue(input.binary, "terminal spill binary"),
    redacted: booleanValue(input.redacted, "terminal spill redacted"),
  })
}

export function parseTerminalStatus(value: unknown): TerminalStatusProjection {
  const input = record(value, "terminal status")
  const earliestCursor = integer(
    input.earliest_cursor ?? input.earliestCursor,
    "terminal earliest cursor",
  )
  const cursor = integer(input.cursor, "terminal cursor")
  if (earliestCursor > cursor) {
    throw new TerminalContractError(
      "terminal_cursor_window_invalid",
      "Terminal earliest cursor exceeds the current cursor.",
    )
  }
  const rows = integer(input.rows, "terminal rows", 2, 500)
  const cols = integer(input.cols, "terminal columns", 2, 1_000)
  const startedAt = timestamp(input.started_at ?? input.startedAt, "terminal started time")
  const updatedAt = timestamp(input.updated_at ?? input.updatedAt, "terminal updated time")
  const exitedAt = input.exited_at ?? input.exitedAt
  return Object.freeze({
    phase: literal(input.phase, "terminal phase", [
      "opening",
      "running",
      "permission_pending",
      "exited",
      "killed",
      "timed_out",
      "crashed",
      "rejected",
    ] as const),
    pid: optionalInteger(input.pid, "terminal pid", 1, 4_294_967_295),
    cwd: text(input.cwd, "terminal cwd", { maximum: 4_096 }),
    title: text(input.title, "terminal title", { maximum: 256 }),
    rows,
    cols,
    cursor,
    earliestCursor,
    exitCode: optionalInteger(input.exit_code ?? input.exitCode, "terminal exit code", -2_147_483_648, 2_147_483_647),
    signal: input.signal ? text(input.signal, "terminal signal", { maximum: 128 }) : undefined,
    startedAt,
    updatedAt,
    exitedAt: exitedAt ? timestamp(exitedAt, "terminal exit time") : undefined,
    viewers: integer(input.viewers, "terminal viewers", 0, 10_000),
    spilledBytes: integer(
      input.spilled_bytes ?? input.spilledBytes,
      "terminal spilled bytes",
      0,
      1_099_511_627_776,
    ),
    binaryBytes: integer(
      input.binary_bytes ?? input.binaryBytes,
      "terminal binary bytes",
      0,
      1_099_511_627_776,
    ),
    inputSequence: integer(input.input_sequence ?? input.inputSequence, "terminal input sequence"),
    resizeSequence: integer(input.resize_sequence ?? input.resizeSequence, "terminal resize sequence"),
    stateMutationId: identity(
      input.state_mutation_id ?? input.stateMutationId,
      "terminal state mutation",
    ),
  })
}

export function parseTerminalSession(value: unknown): TerminalSessionProjection {
  const input = record(value, "terminal session")
  protocol(input.protocol)
  const binding = parseTerminalBinding(input.binding)
  const status = parseTerminalStatus(input.status)
  if (status.cwd.includes("\u0000")) {
    throw new TerminalContractError("terminal_cwd_control", "Terminal cwd contains a control character.")
  }
  if (binding.terminalId.length === 0) {
    throw new TerminalContractError("terminal_binding_empty", "Terminal binding is empty.")
  }
  const socketPath = text(input.socket_path ?? input.socketPath, "terminal socket path", {
    maximum: 2_048,
    pattern: /^\/[A-Za-z0-9._~!$&'()*+,;=:@/%-]+$/u,
  })
  if (
    socketPath.startsWith("//")
    || socketPath.includes("\\")
    || socketPath.split("/").some((part) => part === "." || part === "..")
    || !socketPath.includes(encodeURIComponent(binding.taskId))
    || !socketPath.includes(encodeURIComponent(binding.terminalId))
  ) {
    throw new TerminalContractError(
      "terminal_socket_path_unsafe",
      "Terminal socket path is not safely bound to task and terminal.",
    )
  }
  const humanInterventionCount = integer(
    input.human_intervention_count ?? input.humanInterventionCount,
    "terminal human intervention count",
  )
  const interventionCounted = booleanValue(
    input.intervention_counted ?? input.interventionCounted,
    "terminal intervention counted",
  )
  if (interventionCounted && humanInterventionCount === 0) {
    throw new TerminalContractError(
      "terminal_intervention_receipt_invalid",
      "A counted terminal intervention requires a positive intervention count.",
    )
  }
  return Object.freeze({
    protocol: TERMINAL_PROTOCOL,
    binding,
    status,
    permission: parseTerminalPermission(input.permission),
    socketPath,
    ticket: input.ticket ? text(input.ticket, "terminal ticket", { maximum: 8_192 }) : undefined,
    ticketExpiresAt: input.ticket_expires_at ?? input.ticketExpiresAt
      ? timestamp(input.ticket_expires_at ?? input.ticketExpiresAt, "terminal ticket expiry")
      : undefined,
    correlationId: identity(input.correlation_id ?? input.correlationId, "terminal correlation"),
    causationId: identity(input.causation_id ?? input.causationId, "terminal causation"),
    eventId: optionalIdentity(input.event_id ?? input.eventId, "terminal event"),
    interventionCounted,
    humanInterventionCount,
  })
}

function parseFrameBinding(
  input: Record<string, unknown>,
  expected?: TerminalBinding,
): TerminalBinding {
  const binding = parseTerminalBinding(input.binding)
  if (expected && !sameTerminalBinding(binding, expected)) {
    throw new TerminalContractError(
      "terminal_frame_binding_mismatch",
      "Terminal frame is bound to another session.",
    )
  }
  return binding
}

function parseOutputFrame(
  input: Record<string, unknown>,
  expected?: TerminalBinding,
): TerminalOutputFrame {
  const binding = parseFrameBinding(input, expected)
  const firstCursor = integer(input.first_cursor ?? input.firstCursor, "terminal output first cursor")
  const nextCursor = integer(input.next_cursor ?? input.nextCursor, "terminal output next cursor")
  const byteLength = integer(
    input.byte_length ?? input.byteLength,
    "terminal output byte length",
    0,
    8 * 1_024 * 1_024,
  )
  if (nextCursor < firstCursor || nextCursor - firstCursor !== byteLength) {
    throw new TerminalContractError(
      "terminal_output_cursor_invalid",
      "Terminal output cursor range does not match its byte length.",
    )
  }
  const selected = text(input.text, "terminal output", {
    maximum: 8 * 1_024 * 1_024,
    allowEmpty: byteLength === 0,
  })
  if (new TextEncoder().encode(selected).byteLength > byteLength && byteLength > 0) {
    throw new TerminalContractError(
      "terminal_output_text_oversized",
      "Terminal output text exceeds the declared byte range.",
    )
  }
  return Object.freeze({
    kind: "output",
    protocol: TERMINAL_PROTOCOL,
    binding,
    firstCursor,
    nextCursor,
    text: selected,
    byteLength,
    sha256: text(input.sha256, "terminal output digest", {
      minimum: 64,
      maximum: 64,
      pattern: DIGEST,
    }),
    redacted: booleanValue(input.redacted, "terminal output redacted"),
    sequence: integer(input.sequence, "terminal output sequence", 1),
  })
}

export function parseTerminalServerFrame(
  value: unknown,
  expected?: TerminalBinding,
): TerminalServerFrame {
  const input = record(value, "terminal server frame")
  protocol(input.protocol)
  const kind = literal(input.kind, "terminal frame kind", [
    "hello",
    "output",
    "cursor",
    "resync",
    "backpressure",
    "spill",
    "status",
    "error",
    "pong",
  ] as const)
  if (kind === "error") {
    const binding = input.binding ? parseFrameBinding(input, expected) : undefined
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      code: identity(input.code, "terminal error code"),
      message: text(input.message, "terminal error message", { maximum: 8_192 }),
      retryable: booleanValue(input.retryable, "terminal error retryable"),
      closeCode: optionalInteger(input.close_code ?? input.closeCode, "terminal close code", 1000, 4999),
    })
  }
  const binding = parseFrameBinding(input, expected)
  if (kind === "output") return parseOutputFrame(input, expected)
  if (kind === "hello") {
    const acceptedCursor = integer(
      input.accepted_cursor ?? input.acceptedCursor,
      "terminal accepted cursor",
    )
    const earliestCursor = integer(
      input.earliest_cursor ?? input.earliestCursor,
      "terminal hello earliest cursor",
    )
    if (acceptedCursor < earliestCursor) {
      throw new TerminalContractError(
        "terminal_hello_cursor_invalid",
        "Terminal hello accepted cursor precedes the replay window.",
      )
    }
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      status: parseTerminalStatus(input.status),
      acceptedCursor,
      earliestCursor,
      connectionId: identity(input.connection_id ?? input.connectionId, "terminal connection"),
      heartbeatMs: integer(input.heartbeat_ms ?? input.heartbeatMs, "terminal heartbeat", 250, 120_000),
      maximumUnackedBytes: integer(
        input.maximum_unacked_bytes ?? input.maximumUnackedBytes,
        "terminal maximum unacknowledged bytes",
        1_024,
        64 * 1_024 * 1_024,
      ),
    })
  }
  if (kind === "cursor") {
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      cursor: integer(input.cursor, "terminal cursor"),
      sequence: integer(input.sequence, "terminal cursor sequence", 1),
    })
  }
  if (kind === "resync") {
    const spillsValue = input.spills
    if (!Array.isArray(spillsValue) || spillsValue.length > 1_024) {
      throw new TerminalContractError(
        "terminal_resync_spills_invalid",
        "Terminal resync spills must be a bounded array.",
      )
    }
    const requestedCursor = integer(
      input.requested_cursor ?? input.requestedCursor,
      "terminal requested cursor",
    )
    const acceptedCursor = integer(
      input.accepted_cursor ?? input.acceptedCursor,
      "terminal accepted cursor",
    )
    const earliestCursor = integer(
      input.earliest_cursor ?? input.earliestCursor,
      "terminal resync earliest cursor",
    )
    if (acceptedCursor < earliestCursor) {
      throw new TerminalContractError(
        "terminal_resync_cursor_invalid",
        "Terminal resync accepted cursor precedes the replay window.",
      )
    }
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      requestedCursor,
      acceptedCursor,
      earliestCursor,
      reason: literal(input.reason, "terminal resync reason", [
        "stale_cursor",
        "future_cursor",
        "spill_boundary",
        "generation_changed",
      ] as const),
      spills: Object.freeze(spillsValue.map(parseTerminalSpill)),
    })
  }
  if (kind === "backpressure") {
    const unackedBytes = integer(
      input.unacked_bytes ?? input.unackedBytes,
      "terminal unacknowledged bytes",
      0,
      1_099_511_627_776,
    )
    const maximumUnackedBytes = integer(
      input.maximum_unacked_bytes ?? input.maximumUnackedBytes,
      "terminal maximum unacknowledged bytes",
      1_024,
      64 * 1_024 * 1_024,
    )
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      unackedBytes,
      maximumUnackedBytes,
      paused: booleanValue(input.paused, "terminal backpressure paused"),
      requiredCursor: integer(
        input.required_cursor ?? input.requiredCursor,
        "terminal required cursor",
      ),
    })
  }
  if (kind === "spill") {
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      spill: parseTerminalSpill(input.spill),
    })
  }
  if (kind === "status") {
    return Object.freeze({
      kind,
      protocol: TERMINAL_PROTOCOL,
      binding,
      status: parseTerminalStatus(input.status),
    })
  }
  return Object.freeze({
    kind: "pong",
    protocol: TERMINAL_PROTOCOL,
    binding,
    nonce: identity(input.nonce, "terminal pong nonce"),
    serverTime: timestamp(input.server_time ?? input.serverTime, "terminal server time"),
  })
}

export function encodeTerminalClientCommand(command: TerminalClientCommand): string {
  protocol(command.protocol)
  identity(command.terminalId, "terminal command terminal")
  identity(command.connectionId, "terminal command connection")
  if (command.kind === "input") {
    integer(command.sequence, "terminal input sequence", 1)
    identity(command.actorId, "terminal input actor")
    if (command.permissionPermitId) identity(command.permissionPermitId, "terminal input permit")
    const bytes = new TextEncoder().encode(command.data).byteLength
    if (bytes < 1 || bytes > 256 * 1_024) {
      throw new TerminalContractError(
        "terminal_input_size",
        "Terminal input must contain 1..262144 UTF-8 bytes.",
      )
    }
  }
  if (command.kind === "resize") {
    integer(command.sequence, "terminal resize sequence", 1)
    integer(command.rows, "terminal resize rows", 2, 500)
    integer(command.cols, "terminal resize columns", 2, 1_000)
  }
  if (command.kind === "ack") {
    integer(command.sequence, "terminal acknowledgement sequence", 1)
    integer(command.cursor, "terminal acknowledgement cursor")
  }
  if (command.kind === "kill") {
    integer(command.sequence, "terminal kill sequence", 1)
    identity(command.actorId, "terminal kill actor")
    text(command.reason, "terminal kill reason", { maximum: 2_048 })
    if (command.permissionPermitId) identity(command.permissionPermitId, "terminal kill permit")
  }
  if (command.kind === "ping") identity(command.nonce, "terminal ping nonce")
  return JSON.stringify({
    ...command,
    protocol: TERMINAL_PROTOCOL,
    terminal_id: command.terminalId,
    connection_id: command.connectionId,
    terminalId: undefined,
    connectionId: undefined,
    ...(command.kind === "input"
      ? {
          actor_id: command.actorId,
          permission_permit_id: command.permissionPermitId,
          actorId: undefined,
          permissionPermitId: undefined,
        }
      : {}),
    ...(command.kind === "kill"
      ? {
          actor_id: command.actorId,
          permission_permit_id: command.permissionPermitId,
          actorId: undefined,
          permissionPermitId: undefined,
        }
      : {}),
  })
}

export function decodeTerminalFrameData(
  data: string | ArrayBuffer | Blob,
): Promise<unknown> {
  if (typeof data === "string") {
    if (new TextEncoder().encode(data).byteLength > 8 * 1_024 * 1_024) {
      return Promise.reject(
        new TerminalContractError("terminal_frame_oversized", "Terminal frame exceeds 8 MiB."),
      )
    }
    try {
      return Promise.resolve(JSON.parse(data))
    } catch {
      return Promise.reject(
        new TerminalContractError("terminal_frame_json", "Terminal frame is not valid JSON."),
      )
    }
  }
  if (data instanceof ArrayBuffer) {
    if (data.byteLength > 8 * 1_024 * 1_024) {
      return Promise.reject(
        new TerminalContractError("terminal_frame_oversized", "Terminal frame exceeds 8 MiB."),
      )
    }
    try {
      return Promise.resolve(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data)))
    } catch {
      return Promise.reject(
        new TerminalContractError("terminal_frame_binary", "Terminal binary frame is not UTF-8 JSON."),
      )
    }
  }
  if (data.size > 8 * 1_024 * 1_024) {
    return Promise.reject(
      new TerminalContractError("terminal_frame_oversized", "Terminal frame exceeds 8 MiB."),
    )
  }
  return data.arrayBuffer().then(decodeTerminalFrameData)
}
