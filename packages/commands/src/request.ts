import {
  assertIdentity,
  commandBytes,
  createCommandIdentity,
  normalizeIdempotencyKey,
} from "./identity.ts"
import type {
  CommandDeliveryMode,
  CommandRequestIdentity,
  CommandTaskContext,
  CommandTransportRequest,
  ParsedCommand,
} from "./contracts.ts"
import { priorityFor } from "./policy.ts"

function requireContext(
  context: CommandTaskContext,
): Required<Pick<CommandTaskContext, "taskId" | "runId" | "sessionId">> {
  return {
    taskId: assertIdentity(context.taskId ?? "", "Task identity"),
    runId: assertIdentity(context.runId ?? "", "Run identity"),
    sessionId: assertIdentity(
      context.sessionId ?? `task:${context.taskId ?? ""}`,
      "Session identity",
    ),
  }
}

function boundedText(value: string): string {
  const normalized = value.trim()
  if (!normalized.startsWith("/")) {
    throw new TypeError("Control command text must begin with a slash.")
  }
  if (commandBytes(normalized) > 256 * 1024) {
    throw new TypeError("Control command text exceeds 256 KiB.")
  }
  return normalized
}

function commandArguments(
  parsed: ParsedCommand,
  overrides?: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const descriptor = parsed.descriptor
  if (!descriptor) {
    throw new TypeError("Cannot build a request for an unknown command.")
  }
  if (parsed.errors.length) {
    throw new TypeError(
      parsed.errors.map((entry) => entry.message).join(" "),
    )
  }
  const values: Record<string, unknown> = {
    ...parsed.arguments.values,
    raw: parsed.tokens
      .slice(1)
      .map((token) => token.raw)
      .join(" ")
      .trim(),
    argv: parsed.tokens
      .slice(1)
      .map((token) => token.value),
  }
  if (descriptor.name === "/btw") {
    values.question = String(values.question ?? "").trim()
  }
  if (descriptor.name === "/change") {
    values.instruction = String(values.instruction ?? "").trim()
  }
  for (const [name, value] of Object.entries(overrides ?? {})) {
    if (name === "raw" || name === "argv") {
      throw new TypeError(`Command argument override cannot replace ${name}.`)
    }
    if (!descriptor.arguments.some((argument) => argument.name === name)) {
      throw new TypeError(`Unknown command argument override: ${name}.`)
    }
    values[name] = value
  }
  return Object.freeze(values)
}

export function buildCommandRequest(input: {
  parsed: ParsedCommand
  context: CommandTaskContext
  mode: CommandDeliveryMode
  actorId?: string
  identity?: CommandRequestIdentity
  retryOf?: string
  signal?: AbortSignal
  timeoutMs?: number
  argumentOverrides?: Readonly<Record<string, unknown>>
}): CommandTransportRequest {
  const context = requireContext(input.context)
  const text = boundedText(input.parsed.normalized)
  const args = commandArguments(input.parsed, input.argumentOverrides)
  const identity =
    input.identity ??
    createCommandIdentity({
      ...context,
      text,
      arguments: args,
      mode: input.mode,
      retryOf: input.retryOf,
    })
  const expectedRevision = input.context.expectedRevision
  if (
    expectedRevision !== undefined &&
    (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0)
  ) {
    throw new TypeError(
      "Expected command revision must be a non-negative safe integer.",
    )
  }
  const timeout = input.timeoutMs ?? (input.parsed.descriptor?.name === "/btw" ? 120_000 : 30_000)
  if (!Number.isSafeInteger(timeout) || timeout < 100 || timeout > 600_000) {
    throw new TypeError("Command timeout must be 100 through 600000 ms.")
  }
  return {
    ...context,
    text,
    arguments: args,
    requestId: assertIdentity(identity.requestId, "Request identity"),
    commandId: assertIdentity(identity.commandId, "Command identity"),
    idempotencyKey: normalizeIdempotencyKey(identity.idempotencyKey),
    actorId: assertIdentity(
      input.actorId ?? "zyra-web-command-surface",
      "Command actor identity",
    ),
    expectedRevision,
    sealed: input.context.sealed,
    priority: priorityFor(input.mode),
    deliveryMode: input.mode,
    retryOf: input.retryOf,
    signal: input.signal,
    timeoutMs: timeout,
  }
}

export function retryCommandRequest(input: {
  parsed: ParsedCommand
  context: CommandTaskContext
  previous: CommandTransportRequest
  mode?: CommandDeliveryMode
  signal?: AbortSignal
}): CommandTransportRequest {
  const argumentOverrides = Object.fromEntries(
    (input.parsed.descriptor?.arguments ?? [])
      .filter((argument) =>
        Object.prototype.hasOwnProperty.call(
          input.previous.arguments,
          argument.name,
        ))
      .map((argument) => [
        argument.name,
        input.previous.arguments[argument.name],
      ]),
  )
  return buildCommandRequest({
    parsed: input.parsed,
    context: input.context,
    mode: input.mode ?? input.previous.deliveryMode,
    retryOf: input.previous.requestId,
    signal: input.signal,
    timeoutMs: input.previous.timeoutMs,
    argumentOverrides,
  })
}

export function transportBody(
  request: CommandTransportRequest,
): Readonly<Record<string, unknown>> {
  return Object.freeze({
    text: request.text,
    arguments: request.arguments,
    request_id: request.requestId,
    command_id: request.commandId,
    idempotency_key: request.idempotencyKey,
    actor_id: request.actorId,
    session_id: request.sessionId,
    expected_session_revision: request.expectedRevision,
    sealed: request.sealed,
    competition_mode:
      request.sealed ? "sealed_autonomous" : "interactive",
    priority: request.priority,
    delivery_mode: request.deliveryMode,
    retry_of_request_id: request.retryOf,
  })
}
