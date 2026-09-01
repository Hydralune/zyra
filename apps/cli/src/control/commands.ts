import {
  CommandExecutionPolicy,
  admitCommandReceipt,
  buildCommandRequest,
  createCommandRegistry,
  parseCommand,
  retryCommandRequest,
  type CommandDeliveryMode,
  type CommandPriority,
  type CommandQueueSnapshot,
  type CommandReceipt,
  type CommandTaskContext,
  type CommandTransportRequest,
} from "@zyra/commands"
import { ZyraApiError, type TaskProjection } from "@zyra/typed-api-client"
import { CliApi } from "../api.ts"
import { CliTaskError } from "../contracts.ts"

export const ACTIVE_CONTROL_COMMANDS = Object.freeze([
  "/queue",
  "/now",
  "/next",
  "/later",
  "/cancel",
  "/cancel-command",
  "/continue",
  "/redirect",
  "/interrupt",
  "/retry",
  "/approve",
  "/deny",
])

export type ControlIntent =
  | { kind: "queue" }
  | { kind: "task-cancel"; reason: string }
  | { kind: "task-continue" }
  | { kind: "command-cancel"; requestId: string }
  | { kind: "command-retry"; requestId: string }
  | { kind: "permission"; requestId: string; effect: "allow" | "deny"; feedback?: string }
  | { kind: "submit"; text: string; mode: CommandDeliveryMode; priority: CommandPriority }

function requiredTail(value: string, command: string): string {
  const tail = value.slice(command.length).trim()
  if (!tail) throw new CliTaskError(`${command} requires an argument.`, "control_command_argument_missing")
  return tail
}

export function parseControlIntent(value: string): ControlIntent {
  const line = value.trim()
  if (!line) throw new CliTaskError("Control input is empty.", "control_command_empty")
  if (line === "/queue") return { kind: "queue" }
  if (line === "/continue") return { kind: "task-continue" }
  if (line === "/cancel" || line.startsWith("/cancel ")) {
    return { kind: "task-cancel", reason: line.slice("/cancel".length).trim() || "Cancelled from the interactive Zyra CLI." }
  }
  if (line.startsWith("/cancel-command ")) {
    return { kind: "command-cancel", requestId: requiredTail(line, "/cancel-command") }
  }
  if (line.startsWith("/retry ")) {
    return { kind: "command-retry", requestId: requiredTail(line, "/retry") }
  }
  for (const [trigger, effect] of [["/approve", "allow"], ["/deny", "deny"]] as const) {
    if (line.startsWith(`${trigger} `)) {
      const [requestId, ...feedback] = requiredTail(line, trigger).split(/\s+/)
      return { kind: "permission", requestId: requestId!, effect, feedback: feedback.join(" ") || undefined }
    }
  }
  if (line.startsWith("/redirect ")) {
    return { kind: "submit", text: `/change ${requiredTail(line, "/redirect")}`, mode: "steer", priority: "now" }
  }
  if (line.startsWith("/interrupt ")) {
    return { kind: "submit", text: `/change ${requiredTail(line, "/interrupt")}`, mode: "interrupt", priority: "now" }
  }
  for (const [trigger, mode, priority] of [
    ["/now", "interrupt", "now"],
    ["/next", "enqueue", "next"],
    ["/later", "enqueue", "later"],
  ] as const) {
    if (line.startsWith(`${trigger} `)) {
      const command = requiredTail(line, trigger)
      if (!command.startsWith("/")) {
        throw new CliTaskError(`${trigger} must be followed by a slash command.`, "control_command_invalid")
      }
      return { kind: "submit", text: command, mode, priority }
    }
  }
  if (line.startsWith("/")) return { kind: "submit", text: line, mode: "enqueue", priority: "next" }
  return { kind: "submit", text: `/change ${line}`, mode: "enqueue", priority: "next" }
}

function queueLine(item: CommandQueueSnapshot["items"][number]): string {
  return `${String(item.position + 1).padStart(2, "0")} ${item.priority.padEnd(5)} ${item.phase.padEnd(9)} ${item.requestId ?? item.queueId} ${item.text ?? item.name ?? "command"}`
}

export function formatCommandQueue(snapshot: CommandQueueSnapshot): string {
  if (!snapshot.items.length) return "command queue · empty (canonical owner: PromptQueueRuntime)"
  return [
    `command queue · ${snapshot.items.length} item(s) · server sequence ${snapshot.sequence} · canonical owner: PromptQueueRuntime`,
    ...snapshot.items.map(queueLine),
  ].join("\n")
}

export function formatCommandReceipt(receipt: CommandReceipt): string {
  const revision = receipt.revisionAfter === undefined
    ? ""
    : ` · revision ${receipt.revisionBefore ?? "?"}->${receipt.revisionAfter}`
  const replayed = receipt.replayed ? " · replayed" : ""
  const error = receipt.error ? ` · ${receipt.error.code}: ${receipt.error.message}` : ""
  return `${receipt.phase} ${receipt.name} · request ${receipt.requestId}${revision}${replayed}${error}`
}

function conflictBody(error: unknown): Record<string, unknown> | undefined {
  const body = (error as { body?: unknown } | undefined)?.body
  return body && typeof body === "object" && !Array.isArray(body)
    ? body as Record<string, unknown>
    : undefined
}

function actualRevision(value: Record<string, unknown> | undefined): number | undefined {
  if (!value) return undefined
  const result = value.command_result && typeof value.command_result === "object"
    ? value.command_result as Record<string, unknown>
    : {}
  const error = result.error && typeof result.error === "object"
    ? result.error as Record<string, unknown>
    : {}
  const details = error.details && typeof error.details === "object"
    ? error.details as Record<string, unknown>
    : {}
  return Number.isSafeInteger(details.actual) ? Number(details.actual) : undefined
}

function ambiguousMutation(error: unknown): error is ZyraApiError {
  return error instanceof ZyraApiError
    && ["disconnect", "timeout"].includes(error.category)
}

function commandResultStatus(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return ""
  const raw = value as Record<string, unknown>
  const result = raw.command_result && typeof raw.command_result === "object"
    && !Array.isArray(raw.command_result)
    ? raw.command_result as Record<string, unknown>
    : raw
  return typeof result.status === "string" ? result.status.trim().toLowerCase() : ""
}

export class CliControlSession {
  readonly #api: CliApi
  readonly #registry = createCommandRegistry()
  readonly #policy = new CommandExecutionPolicy(this.#registry)
  #task: TaskProjection
  #revision?: number

  constructor(input: { api: CliApi; task: TaskProjection; revision?: number }) {
    this.#api = input.api
    this.#task = input.task
    this.#revision = input.revision
  }

  get task(): TaskProjection { return this.#task }
  get revision(): number | undefined { return this.#revision }

  refreshTask(task: TaskProjection): void {
    if (task.taskId !== this.#task.taskId || task.runId !== this.#task.runId) {
      throw new CliTaskError(
        "Control session cannot bind a canonical task from another task or run.",
        "control_task_binding_invalid",
        {
          expected_task_id: this.#task.taskId,
          expected_run_id: this.#task.runId,
          actual_task_id: task.taskId,
          actual_run_id: task.runId,
        },
      )
    }
    this.#task = task
  }

  async queue(signal?: AbortSignal, includeTerminal = false): Promise<CommandQueueSnapshot> {
    return this.#api.commandQueue({
      taskId: this.#task.taskId,
      sessionId: this.#sessionId(),
      includeTerminal,
      signal,
    })
  }

  async cancelTask(reason: string): Promise<TaskProjection> {
    try {
      const mutation = await this.#api.cancelTask(this.#task, reason)
      this.#task = mutation.task
      return this.#task
    } catch (error) {
      if (!ambiguousMutation(error)) throw error
      try {
        const latest = await this.#api.task(this.#task.taskId)
        this.#task = latest
        if (["cancelled", "canceled"].includes(latest.status)) return latest
        throw new CliTaskError(
          latest.terminal
            ? "The task reached another canonical terminal state before cancellation could be confirmed."
            : "Task cancellation delivery is ambiguous and the canonical task is not cancelled.",
          latest.terminal
            ? "task_cancel_not_applied"
            : "task_cancel_outcome_unknown",
          {
            task_id: latest.taskId,
            canonical_status: latest.status,
            automatic_retry: false,
            mutation_replayed: false,
          },
        )
      } catch (reconciliationError) {
        if (reconciliationError instanceof CliTaskError) {
          throw reconciliationError
        }
        throw new CliTaskError(
          "Task cancellation delivery is ambiguous and canonical state could not be queried. Inspect /status before another action.",
          "task_cancel_outcome_unknown",
          {
            task_id: this.#task.taskId,
            automatic_retry: false,
            mutation_replayed: false,
            receipt_query_error: reconciliationError instanceof Error
              ? reconciliationError.message.slice(0, 500)
              : String(reconciliationError).slice(0, 500),
          },
        )
      }
    }
  }

  async continueTask(signal?: AbortSignal): Promise<TaskProjection> {
    const latest = await this.#api.task(this.#task.taskId)
    if (latest.terminal) {
      throw new CliTaskError("A terminal task cannot be continued.", "task_continue_terminal", { task_id: latest.taskId })
    }
    if (!["pending", "paused", "interrupted"].includes(latest.status)) {
      throw new CliTaskError("Task is already running and does not need continuation.", "task_continue_conflict", {
        task_id: latest.taskId,
        status: latest.status,
      })
    }
    try {
      const mutation = await this.#api.runTask(latest, signal)
      this.#task = mutation.task
      return this.#task
    } catch (error) {
      if (!ambiguousMutation(error)) throw error
      try {
        const reconciled = await this.#api.task(latest.taskId)
        this.#task = reconciled
        if (!["pending", "paused", "interrupted"].includes(reconciled.status)) {
          return reconciled
        }
        throw new CliTaskError(
          "Task continuation delivery is ambiguous and canonical state has not advanced. Inspect /status before explicitly continuing again.",
          "task_continue_outcome_unknown",
          {
            task_id: reconciled.taskId,
            canonical_status: reconciled.status,
            automatic_retry: false,
            mutation_replayed: false,
          },
        )
      } catch (reconciliationError) {
        if (reconciliationError instanceof CliTaskError) {
          throw reconciliationError
        }
        throw new CliTaskError(
          "Task continuation delivery is ambiguous and canonical state could not be queried. Inspect /status before another action.",
          "task_continue_outcome_unknown",
          {
            task_id: latest.taskId,
            automatic_retry: false,
            mutation_replayed: false,
            receipt_query_error: reconciliationError instanceof Error
              ? reconciliationError.message.slice(0, 500)
              : String(reconciliationError).slice(0, 500),
          },
        )
      }
    }
  }

  async cancelCommand(requestId: string, signal?: AbortSignal): Promise<Readonly<Record<string, unknown>>> {
    try {
      const result = await this.#api.cancelControlCommand({
        taskId: this.#task.taskId,
        requestId,
        reason: "Cancelled from the interactive Zyra CLI.",
        signal,
      })
      const status = commandResultStatus(result)
      if (["cancelled", "canceled"].includes(status)) return result
      throw new CliTaskError(
        "The command reached another canonical state and was not cancelled.",
        "command_cancel_not_applied",
        { request_id: requestId, canonical_status: status || "unknown" },
      )
    } catch (error) {
      if (!ambiguousMutation(error)) throw error
      try {
        const snapshot = await this.queue(signal, true)
        const item = snapshot.items.find((candidate) => (
          candidate.requestId === requestId || candidate.queueId === requestId
        ))
        if (item?.phase === "cancelled") {
          return Object.freeze({
            request_id: item.requestId ?? requestId,
            command_id: item.commandId,
            status: item.phase,
            reconciled: true,
            mutation_replayed: false,
          })
        }
        throw new CliTaskError(
          "Command cancellation delivery is ambiguous and the canonical queue does not confirm cancellation.",
          "command_cancel_outcome_unknown",
          {
            request_id: requestId,
            canonical_status: item?.phase ?? "absent",
            automatic_retry: false,
            mutation_replayed: false,
          },
        )
      } catch (reconciliationError) {
        if (reconciliationError instanceof CliTaskError) {
          throw reconciliationError
        }
        throw new CliTaskError(
          "Command cancellation delivery is ambiguous and the canonical queue could not be queried.",
          "command_cancel_outcome_unknown",
          {
            request_id: requestId,
            automatic_retry: false,
            mutation_replayed: false,
            receipt_query_error: reconciliationError instanceof Error
              ? reconciliationError.message.slice(0, 500)
              : String(reconciliationError).slice(0, 500),
          },
        )
      }
    }
  }

  async retry(requestId: string, signal?: AbortSignal): Promise<CommandReceipt> {
    const snapshot = await this.queue(signal, true)
    const item = snapshot.items.find((candidate) => candidate.requestId === requestId || candidate.queueId === requestId)
    if (!item) throw new CliTaskError("Command retry target is absent from the canonical queue.", "command_retry_not_found")
    if (!item.retryable || !item.text || !item.requestId || !item.commandId || !item.idempotencyKey) {
      throw new CliTaskError("Command receipt is not retryable or lacks a reconstructable redacted request.", "command_retry_forbidden", {
        request_id: item.requestId,
        phase: item.phase,
      })
    }
    const parsed = parseCommand(item.text, this.#registry)
    const mode: CommandDeliveryMode = item.priority === "now" ? "interrupt" : "enqueue"
    const previous = buildCommandRequest({
      parsed,
      context: this.#context(),
      mode,
      actorId: "zyra-cli",
      identity: {
        requestId: item.requestId,
        commandId: item.commandId,
        idempotencyKey: item.idempotencyKey,
        fingerprint: item.idempotencyKey,
      },
    })
    const request = retryCommandRequest({
      parsed,
      context: this.#context(),
      previous,
      mode,
      signal,
    })
    return this.#transport(item.priority === "later" ? { ...request, priority: "later" } : request)
  }

  async submit(
    text: string,
    input: { mode: CommandDeliveryMode; priority?: CommandPriority; signal?: AbortSignal },
  ): Promise<CommandReceipt> {
    const parsed = parseCommand(text, this.#registry)
    if (parsed.errors.length || !parsed.descriptor) {
      throw new CliTaskError(
        parsed.errors.map((entry) => entry.message).join(" ") || "Control command is invalid.",
        "control_command_invalid",
      )
    }
    if (parsed.descriptor.mutation !== "read-only" && this.#revision === undefined) {
      await this.#bootstrapRevision(input.signal)
    }
    this.#policy.assert({
      parsed,
      context: this.#context(),
      mode: input.mode,
      busy: !this.#task.terminal,
    })
    const built = buildCommandRequest({
      parsed,
      context: this.#context(),
      mode: input.mode,
      actorId: "zyra-cli",
      signal: input.signal,
    })
    const request = input.priority && input.priority !== built.priority
      ? { ...built, priority: input.priority }
      : built
    return this.#transport(request)
  }

  async #bootstrapRevision(signal?: AbortSignal): Promise<void> {
    const parsed = parseCommand("/status", this.#registry)
    const request = buildCommandRequest({
      parsed,
      context: { ...this.#context(), expectedRevision: undefined },
      mode: "enqueue",
      actorId: "zyra-cli-revision-probe",
      signal,
    })
    const receipt = await this.#transport(request)
    this.#revision = receipt.revisionAfter ?? receipt.revisionBefore ?? 0
  }

  async #transport(request: CommandTransportRequest): Promise<CommandReceipt> {
    try {
      const raw = await this.#api.submitControlCommand(request)
      const receipt = admitCommandReceipt(raw, request)
      if (receipt.revisionAfter !== undefined) this.#revision = receipt.revisionAfter
      if (receipt.phase === "rejected" && receipt.error?.code === "revision_conflict") {
        this.#revision = Number.isSafeInteger(receipt.error.details.actual)
          ? Number(receipt.error.details.actual)
          : this.#revision
        throw new CliTaskError(
          "Control command was rejected because the canonical session revision changed; inspect and resubmit explicitly.",
          "command_revision_conflict",
          { expected: request.expectedRevision, actual: this.#revision, automatic_retry: false },
        )
      }
      return receipt
    } catch (error) {
      if (error instanceof CliTaskError) throw error
      if (ambiguousMutation(error)) {
        try {
          const raw = await this.#api.controlCommandReceipt(request)
          const receipt = admitCommandReceipt(raw, request)
          if (receipt.revisionAfter !== undefined) {
            this.#revision = receipt.revisionAfter
          }
          return receipt
        } catch (reconciliationError) {
          throw new CliTaskError(
            "Control command delivery became ambiguous and its durable receipt could not be reconciled. Inspect /queue or /status before explicitly resubmitting.",
            "command_outcome_unknown",
            {
              request_id: request.requestId,
              command_id: request.commandId,
              idempotency_key: request.idempotencyKey,
              automatic_retry: false,
              mutation_replayed: false,
              transport_category: error.category,
              receipt_query_error: reconciliationError instanceof Error
                ? reconciliationError.message.slice(0, 500)
                : String(reconciliationError).slice(0, 500),
            },
          )
        }
      }
      if (error instanceof ZyraApiError && error.status === 409) {
        const body = conflictBody(error)
        const actual = actualRevision(body)
        if (actual !== undefined) this.#revision = actual
        throw new CliTaskError(
          "Control command conflicted with canonical server state; no mutation was automatically retried.",
          actual === undefined ? "command_conflict" : "command_revision_conflict",
          {
            expected: request.expectedRevision,
            actual,
            request_id: request.requestId,
            automatic_retry: false,
          },
        )
      }
      throw error
    }
  }

  #sessionId(): string {
    const metadataSession = this.#task.metadata.query_session_id ?? this.#task.metadata.session_id
    return typeof metadataSession === "string" && metadataSession.trim()
      ? metadataSession.trim()
      : this.#task.sessionId ?? `task:${this.#task.taskId}`
  }

  #context(): CommandTaskContext {
    return {
      taskId: this.#task.taskId,
      runId: this.#task.runId,
      sessionId: this.#sessionId(),
      taskStatus: this.#task.status,
      active: !this.#task.terminal,
      terminal: this.#task.terminal,
      transportEnabled: true,
      sealed: this.#task.metadata.sealed === true || this.#task.metadata.competition_mode === "sealed_autonomous",
      remote: true,
      expectedRevision: this.#revision,
    }
  }
}
