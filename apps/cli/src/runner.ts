import { readFile } from "node:fs/promises"
import type { Readable } from "node:stream"
import {
  RequestCancelledError,
  type EventProjection,
  type TaskMutationProjection,
  type TaskProjection,
} from "@zyra/typed-api-client"
import { CliApi, type IngressFrame } from "./api.ts"
import {
  CliExitCode,
  CliUsageError,
  type RunCommand,
} from "./contracts.ts"
import type { CliOutput } from "./output.ts"

export interface CommandOutcome {
  exitCode: CliExitCode
  status: string
  taskId?: string
  runId?: string
  verifier?: Readonly<Record<string, unknown>>
  result?: Readonly<Record<string, unknown>>
}

interface VerifierEvidence {
  final?: Readonly<Record<string, unknown>>
  gate?: Readonly<Record<string, unknown>>
}

interface EventAccumulator {
  seen: Set<string>
  verifier: VerifierEvidence
}

const SETTLEMENT_PROBE_INTERVAL_MS = 250

export function taskHasSettledRunResult(
  task: TaskProjection,
  evidence: { finalPassed?: boolean; completionGatePresent?: boolean },
): boolean {
  return task.terminal || (
    task.status === "blocked"
    && (evidence.finalPassed === false || evidence.completionGatePresent === true)
  )
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : []
}

function eventPayload(frame: IngressFrame): Readonly<Record<string, unknown>> {
  const value = frame.event.payload
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

function captureVerifier(
  payload: Readonly<Record<string, unknown>>,
  evidence: VerifierEvidence,
): void {
  if (payload.schema === "zyra.production-independent-final-verifier/v2") {
    evidence.final = payload
  }
  if (payload.schema === "zyra.production-adaptive-depth-completion-gate/v1") {
    evidence.gate = payload
  }
}

function emitFrame(output: CliOutput, frame: IngressFrame, accumulator: EventAccumulator): void {
  captureVerifier(eventPayload(frame), accumulator.verifier)
  captureVerifier(record(frame.event.inline), accumulator.verifier)
  if (accumulator.seen.has(frame.eventId)) return
  accumulator.seen.add(frame.eventId)
  output.event(
    {
      schema: "zyra.cli-task-event.v1",
      source: "event-ingress",
      sequence: frame.sequence,
      previous_sequence: frame.previousSequence,
      event_id: frame.eventId,
      event_type: frame.eventType,
      event: frame.event,
    },
    {
      taskId: frame.taskId,
      runId: typeof frame.event.runId === "string" ? frame.event.runId : undefined,
      cursor: frame.cursor,
      generation: frame.generation,
    },
  )
}

function emitLegacyEvent(
  output: CliOutput,
  event: EventProjection,
  accumulator: EventAccumulator,
): void {
  captureVerifier(event.payload, accumulator.verifier)
  if (accumulator.seen.has(event.eventId)) return
  accumulator.seen.add(event.eventId)
  output.event(
    {
      schema: "zyra.cli-task-event.v1",
      source: "task-events-replay",
      event_id: event.eventId,
      event_type: event.eventType,
      created_at: event.createdAt,
      payload: event.payload,
    },
    { taskId: event.taskId, runId: event.runId },
  )
}

const MAX_GOAL_BYTES = 256 * 1024

async function readStdinGoal(stdin: Readable, signal: AbortSignal): Promise<string> {
  if ((stdin as Readable & { isTTY?: boolean }).isTTY === true) {
    throw new CliUsageError("zyra run requires a goal, --file, or piped stdin.")
  }
  const chunks: Buffer[] = []
  let bytes = 0
  const read = (async () => {
    for await (const chunk of stdin) {
      if (signal.aborted) throw signal.reason
      const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), "utf8")
      bytes += value.byteLength
      if (bytes > MAX_GOAL_BYTES) {
        throw new CliUsageError("Piped task goal must not exceed 256 KiB of UTF-8 input.")
      }
      chunks.push(value)
    }
    return Buffer.concat(chunks).toString("utf8")
  })()
  let rejectAbort: (reason?: unknown) => void = () => undefined
  const onAbort = () => rejectAbort(signal.reason)
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = reject
    if (signal.aborted) reject(signal.reason)
    else signal.addEventListener("abort", onAbort, { once: true })
  })
  try {
    return await Promise.race([read, aborted])
  } finally {
    signal.removeEventListener("abort", onAbort)
  }
}

export async function goalFrom(
  command: RunCommand,
  stdin: Readable,
  signal: AbortSignal,
): Promise<string> {
  if (command.goal) return command.goal
  let content: string
  if (command.file) {
    try {
      content = await readFile(command.file, "utf8")
    } catch {
      throw new CliUsageError("Cannot read the UTF-8 task goal file.")
    }
  } else {
    content = await readStdinGoal(stdin, signal)
  }
  const goal = content.trim()
  const bytes = new TextEncoder().encode(goal).byteLength
  if (!goal || bytes > MAX_GOAL_BYTES) {
    throw new CliUsageError("Task goal input must contain at most 256 KiB of non-empty UTF-8 text.")
  }
  return goal
}

export function classifyTaskOutcome(
  task: TaskProjection,
  evidence: VerifierEvidence,
): CommandOutcome {
  const delivery = record(task.metadata.delivery)
  const verifier = {
    schema: "zyra.cli-verifier-summary.v1",
    present: Boolean(evidence.final),
    passed: evidence.final?.passed === true,
    completion_gate_present: Boolean(evidence.gate),
    completion_gate_passed: evidence.gate?.hard_conditions_passed === true,
    final_verifier_receipt_ref: evidence.final?.verifier_receipt_ref,
    failed_conditions: Array.isArray(evidence.gate?.failed_conditions)
      ? evidence.gate?.failed_conditions
      : [],
  }
  const result = {
    schema: "zyra.cli-task-result.v1",
    task_status: task.status,
    final_answer: typeof task.metadata.final_answer === "string"
      ? task.metadata.final_answer
      : undefined,
    artifact_ids: task.artifacts.map((artifact) => artifact.artifactId),
    workspace_delivery: delivery.schema === "zyra.task-workspace-delivery/v1"
      ? {
          schema: delivery.schema,
          workspace_id: typeof delivery.workspace_id === "string"
            ? delivery.workspace_id
            : undefined,
          created_paths: stringList(delivery.created_paths),
          modified_paths: stringList(delivery.modified_paths),
          deleted_paths: stringList(delivery.deleted_paths),
          changed_paths: stringList(delivery.changed_paths),
          file_api_resource: typeof delivery.workspace_id === "string"
            ? `workspaces/${delivery.workspace_id}/files`
            : undefined,
        }
      : undefined,
  }
  if (task.status === "cancelled") {
    return {
      exitCode: CliExitCode.CANCELLED,
      status: "cancelled",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    }
  }
  if (evidence.final && evidence.final.passed !== true) {
    return {
      exitCode: CliExitCode.VERIFIER_FAILED,
      status: "verifier_failed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    }
  }
  if (evidence.gate && evidence.gate.hard_conditions_passed !== true) {
    return {
      exitCode: CliExitCode.VERIFIER_FAILED,
      status: "verifier_failed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    }
  }
  if (task.status === "completed") {
    if (!evidence.final || !evidence.gate) {
      return {
        exitCode: CliExitCode.VERIFIER_FAILED,
        status: "verifier_evidence_missing",
        taskId: task.taskId,
        runId: task.runId,
        verifier,
        result,
      }
    }
    return {
      exitCode: CliExitCode.SUCCESS,
      status: "completed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    }
  }
  return {
    exitCode: CliExitCode.TASK_FAILED,
    status: task.status || "failed",
    taskId: task.taskId,
    runId: task.runId,
    verifier,
    result,
  }
}

export async function executeRun(input: {
  command: RunCommand
  api: CliApi
  output: CliOutput
  stdin: Readable
  signal: AbortSignal
}): Promise<CommandOutcome> {
  const goal = await goalFrom(input.command, input.stdin, input.signal)
  const created = await input.api.createPendingTask(goal, input.command.sealed)
  const task = created.task
  const accumulator: EventAccumulator = { seen: new Set(), verifier: {} }
  for (const event of created.events) emitLegacyEvent(input.output, event, accumulator)
  input.output.event(
    {
      schema: "zyra.cli-task-submission.v1",
      task_id: task.taskId,
      run_id: task.runId,
      status: task.status,
      receipt: created.receipt,
    },
    { taskId: task.taskId, runId: task.runId },
  )

  const firstPage = await input.api.openIngress(task.taskId)
  for (const frame of firstPage.frames) emitFrame(input.output, frame, accumulator)
  let cursor = firstPage.cursor
  let generation = firstPage.generation
  const runController = new AbortController()
  let cancellation: Promise<TaskMutationProjection> | undefined
  const cancel = () => {
    if (cancellation) return
    runController.abort(input.signal.reason ?? "CLI interrupted")
    cancellation = input.api.cancelTask(
      task,
      input.signal.reason instanceof Error
        ? input.signal.reason.message
        : String(input.signal.reason || "Cancelled by Zyra CLI signal policy."),
    )
  }
  input.signal.addEventListener("abort", cancel, { once: true })
  if (input.signal.aborted) cancel()

  let settled = false
  const runPromise = input.api.runTask(task, runController.signal)
    .then((value) => ({ ok: true as const, value }))
    .catch((error: unknown) => ({ ok: false as const, error }))
    .finally(() => { settled = true })
  const runSettledSignal = runPromise.then(() => ({ kind: "run_settled" as const }))

  const finishCancelled = async (): Promise<CommandOutcome> => {
    input.signal.removeEventListener("abort", cancel)
    cancel()
    const cancelled = await cancellation
    for (const event of cancelled?.events ?? []) emitLegacyEvent(input.output, event, accumulator)
    if (cancelled) {
      input.output.event(
        {
          schema: "zyra.cli-cancel-receipt.v1",
          task_id: cancelled.task.taskId,
          run_id: cancelled.task.runId,
          status: cancelled.task.status,
          receipt: cancelled.receipt,
        },
        { taskId: cancelled.task.taskId, runId: cancelled.task.runId },
      )
    }
    void runPromise.then(() => undefined)
    const finalTask = cancelled?.task ?? await input.api.task(task.taskId)
    const replay = await input.api.events(task.taskId)
    for (const event of replay) emitLegacyEvent(input.output, event, accumulator)
    return {
      ...classifyTaskOutcome(finalTask, accumulator.verifier),
      exitCode: CliExitCode.CANCELLED,
      status: "cancelled",
    }
  }

  let ingressError: unknown
  let observedSettlement: TaskProjection | undefined
  let lastSettlementProbeAt = 0
  while (!settled && !input.signal.aborted) {
    const ingressController = new AbortController()
    const ingressPromise = input.api.nextIngress(
      task.taskId,
      cursor,
      generation,
      750,
      ingressController.signal,
    )
      .then((page) => ({ kind: "ingress" as const, page }))
      .catch((error: unknown) => ({ kind: "ingress_error" as const, error }))
    const next = await Promise.race([ingressPromise, runSettledSignal])
    if (next.kind === "run_settled") {
      ingressController.abort("task run request settled")
      void ingressPromise.then(() => undefined)
      break
    }
    if (next.kind === "ingress") {
      const page = next.page
      cursor = page.cursor
      generation = page.generation
      for (const frame of page.frames) emitFrame(input.output, frame, accumulator)
    } else {
      ingressError = next.error
      while (!settled && !input.signal.aborted) {
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 50))
      }
      break
    }
    const now = Date.now()
    if (
      !settled
      && accumulator.verifier.final !== undefined
      && now - lastSettlementProbeAt >= SETTLEMENT_PROBE_INTERVAL_MS
    ) {
      lastSettlementProbeAt = now
      try {
        const observed = await input.api.task(task.taskId)
        if (taskHasSettledRunResult(observed, {
          finalPassed: accumulator.verifier.final?.passed === true
            ? true
            : accumulator.verifier.final?.passed === false
              ? false
              : undefined,
          completionGatePresent: accumulator.verifier.gate !== undefined,
        })) {
          observedSettlement = observed
          runController.abort("canonical task result settled before mutation transport")
          input.output.event(
            {
              schema: "zyra.cli-run-reconciliation.v1",
              phase: "terminal",
              task_id: observed.taskId,
              run_id: observed.runId,
              status: observed.status,
              reason: "canonical_task_result_settled_before_mutation_transport",
            },
            { taskId: observed.taskId, runId: observed.runId },
          )
          break
        }
      } catch {
        // The long mutation request remains authoritative while a best-effort
        // settlement probe is unavailable.
      }
    }
  }

  if (input.signal.aborted) {
    return finishCancelled()
  }

  const run = observedSettlement ? undefined : await runPromise
  if (run && !run.ok && run.error instanceof RequestCancelledError && !input.signal.aborted) {
    input.output.event(
      {
        schema: "zyra.cli-run-reconciliation.v1",
        phase: "waiting",
        task_id: task.taskId,
        run_id: task.runId,
        reason: "mutation_transport_detached_without_command_cancellation",
      },
      { taskId: task.taskId, runId: task.runId },
    )
    let observed = await input.api.task(task.taskId)
    while (!observed.terminal && !input.signal.aborted) {
      try {
        const page = await input.api.nextIngress(task.taskId, cursor, generation)
        cursor = page.cursor
        generation = page.generation
        for (const frame of page.frames) emitFrame(input.output, frame, accumulator)
      } catch (error) {
        ingressError ??= error
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
      }
      observed = await input.api.task(task.taskId)
    }
    if (!input.signal.aborted) {
      input.output.event(
        {
          schema: "zyra.cli-run-reconciliation.v1",
          phase: "terminal",
          task_id: observed.taskId,
          run_id: observed.runId,
          status: observed.status,
        },
        { taskId: observed.taskId, runId: observed.runId },
      )
    }
  }
  if (input.signal.aborted) return finishCancelled()
  input.signal.removeEventListener("abort", cancel)
  for (let attempts = 0; attempts < 8; attempts += 1) {
    try {
      const page = await input.api.nextIngress(task.taskId, cursor, generation, 100)
      cursor = page.cursor
      generation = page.generation
      for (const frame of page.frames) emitFrame(input.output, frame, accumulator)
      if (page.caughtUp && !page.frames.length) break
    } catch (error) {
      ingressError ??= error
      break
    }
  }

  let finalTask: TaskProjection
  let replay: readonly EventProjection[]
  try {
    [finalTask, replay] = await Promise.all([
      observedSettlement ?? input.api.task(task.taskId),
      input.api.events(task.taskId),
    ])
  } catch (error) {
    if (ingressError) throw ingressError
    throw error
  }
  for (const event of replay) emitLegacyEvent(input.output, event, accumulator)

  if (run && !run.ok && !(run.error instanceof RequestCancelledError)) {
    const outcome = classifyTaskOutcome(finalTask, accumulator.verifier)
    if (outcome.exitCode !== CliExitCode.SUCCESS && outcome.exitCode !== CliExitCode.VERIFIER_FAILED) {
      throw run.error
    }
  }
  return classifyTaskOutcome(finalTask, accumulator.verifier)
}
