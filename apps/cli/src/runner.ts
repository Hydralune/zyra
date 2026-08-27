import { readFile } from "node:fs/promises"
import type { Readable } from "node:stream"
import {
  RequestCancelledError,
  ZyraApiError,
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
import { materializeWorkspaceDelivery, stageWorkspace } from "./workspace-transfer.ts"

export interface CommandOutcome {
  exitCode: CliExitCode
  status: string
  taskId?: string
  runId?: string
  verifier?: Readonly<Record<string, unknown>>
  result?: Readonly<Record<string, unknown>>
  canonicalOutcome?: Readonly<Record<string, unknown>>
  diagnostics?: readonly OutcomeDiagnostic[]
}

export interface OutcomeDiagnostic {
  schema: "zyra.task-outcome-diagnostic/v1"
  stage: "task_execution" | "result_read" | "event_sync" | "log_enrichment" | "client_connection" | "workspace_materialization"
  error_type: string
  message: string
  recoverable: boolean
  cause?: string
  cause_chain?: readonly string[]
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

function persistedVerifierEvidence(task: TaskProjection): VerifierEvidence {
  const outcome = record(task.metadata.canonical_task_outcome)
  if (outcome.schema !== "zyra.task-outcome/v1") return {}
  const verification = record(outcome.verification)
  const final = record(verification.final_verifier)
  const gate = record(verification.completion_gate)
  return {
    final: typeof final.passed === "boolean" ? final : undefined,
    gate: typeof gate.hard_conditions_passed === "boolean" ? gate : undefined,
  }
}

function mergedVerifierEvidence(
  task: TaskProjection,
  evidence: VerifierEvidence,
): VerifierEvidence {
  const persisted = persistedVerifierEvidence(task)
  return {
    final: evidence.final ?? persisted.final,
    gate: evidence.gate ?? persisted.gate,
  }
}

export function mutationTransportDetached(error: unknown): boolean {
  return error instanceof RequestCancelledError || (
    error instanceof ZyraApiError
    && (error.category === "disconnect" || error.category === "timeout")
  )
}

export function taskHasSettledRunResult(
  task: TaskProjection,
  evidence: { finalPassed?: boolean; completionGatePresent?: boolean },
): boolean {
  const persisted = persistedVerifierEvidence(task)
  const finalPassed = evidence.finalPassed
    ?? (persisted.final?.passed === true
      ? true
      : persisted.final?.passed === false
        ? false
        : undefined)
  const completionGatePresent = evidence.completionGatePresent === true
    || persisted.gate !== undefined
  return task.terminal || (
    task.status === "blocked"
    && (finalPassed === false || completionGatePresent)
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
  diagnostics: readonly OutcomeDiagnostic[] = [],
): CommandOutcome {
  evidence = mergedVerifierEvidence(task, evidence)
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
  const persistedCanonicalOutcome = record(task.metadata.canonical_task_outcome)
  const finalize = (outcome: Omit<CommandOutcome, "canonicalOutcome" | "diagnostics">): CommandOutcome => ({
    ...outcome,
    canonicalOutcome: persistedCanonicalOutcome.schema === "zyra.task-outcome/v1"
      ? {
        ...persistedCanonicalOutcome,
        diagnostic_count: diagnostics.length,
      }
      : {
        schema: "zyra.task-outcome/v1",
        revision: 1,
        task_id: task.taskId,
        run_id: task.runId,
        task_status: task.status,
        terminal: task.terminal,
        command_status: outcome.status,
        exit_code: outcome.exitCode,
        execution_result_source: "canonical_task_projection",
        diagnostic_count: diagnostics.length,
      },
    ...(diagnostics.length > 0 ? { diagnostics: [...diagnostics] } : {}),
  })
  if (task.status === "cancelled") {
    return finalize({
      exitCode: CliExitCode.CANCELLED,
      status: "cancelled",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    })
  }
  if (evidence.final && evidence.final.passed !== true) {
    return finalize({
      exitCode: CliExitCode.VERIFIER_FAILED,
      status: "verifier_failed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    })
  }
  if (evidence.gate && evidence.gate.hard_conditions_passed !== true) {
    return finalize({
      exitCode: CliExitCode.VERIFIER_FAILED,
      status: "verifier_failed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    })
  }
  if (task.status === "completed") {
    if (!evidence.final || !evidence.gate) {
      return finalize({
        exitCode: CliExitCode.VERIFIER_FAILED,
        status: "verifier_evidence_missing",
        taskId: task.taskId,
        runId: task.runId,
        verifier,
        result,
      })
    }
    return finalize({
      exitCode: CliExitCode.SUCCESS,
      status: "completed",
      taskId: task.taskId,
      runId: task.runId,
      verifier,
      result,
    })
  }
  return finalize({
    exitCode: CliExitCode.TASK_FAILED,
    status: task.status || "failed",
    taskId: task.taskId,
    runId: task.runId,
    verifier,
    result,
  })
}

function outcomeDiagnostic(
  stage: OutcomeDiagnostic["stage"],
  error: unknown,
  recoverable = true,
): OutcomeDiagnostic {
  const value = error instanceof Error ? error : new Error(String(error))
  const declaredRetryable = (value as Error & { retryable?: unknown }).retryable
  const causeChain: string[] = []
  const visited = new Set<unknown>()
  let cause: unknown = value.cause
  while (cause !== undefined && cause !== null && !visited.has(cause)) {
    visited.add(cause)
    if (cause instanceof Error) {
      causeChain.push(`${cause.name}: ${cause.message}`)
      cause = cause.cause
    } else {
      causeChain.push(String(cause))
      break
    }
  }
  return {
    schema: "zyra.task-outcome-diagnostic/v1",
    stage,
    error_type: value.name || "Error",
    message: value.message,
    recoverable: typeof declaredRetryable === "boolean" ? declaredRetryable : recoverable,
    cause: causeChain[0],
    cause_chain: causeChain.length > 0 ? causeChain : undefined,
  }
}

export async function executeRun(input: {
  command: RunCommand
  api: CliApi
  output: CliOutput
  stdin: Readable
  signal: AbortSignal
  workspaceRoot?: string
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
  if (input.workspaceRoot) {
    const staged = await stageWorkspace(input.api, task, input.workspaceRoot, input.signal)
    input.output.event(
      {
        schema: "zyra.cli-workspace-transfer.v1",
        phase: "staged",
        workspace_id: staged.workspaceId,
        file_count: staged.fileCount,
        bytes: staged.bytes,
        paths: staged.paths,
      },
      { taskId: task.taskId, runId: task.runId },
    )
  }

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
    const cancelDiagnostics: OutcomeDiagnostic[] = []
    let replay: readonly EventProjection[] = []
    try {
      replay = await input.api.events(task.taskId)
    } catch (error) {
      cancelDiagnostics.push(outcomeDiagnostic("event_sync", error))
    }
    for (const event of replay) emitLegacyEvent(input.output, event, accumulator)
    return {
      ...classifyTaskOutcome(finalTask, accumulator.verifier, cancelDiagnostics),
      exitCode: CliExitCode.CANCELLED,
      status: "cancelled",
    }
  }

  let ingressError: unknown
  const diagnostics: OutcomeDiagnostic[] = []
  let observedSettlement: TaskProjection | undefined
  let lastSettlementProbeAt = 0
  const settlementEvidence = () => ({
    finalPassed: accumulator.verifier.final?.passed === true
      ? true
      : accumulator.verifier.final?.passed === false
        ? false
        : undefined,
    completionGatePresent: accumulator.verifier.gate !== undefined,
  })
  const probeSettlement = async (): Promise<boolean> => {
    try {
      const observed = await input.api.task(task.taskId)
      if (!taskHasSettledRunResult(observed, settlementEvidence())) return false
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
      return true
    } catch {
      // The long mutation request remains authoritative while a best-effort
      // settlement probe is unavailable.
      return false
    }
  }
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
        if (await probeSettlement()) break
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 50))
      }
      break
    }
    const now = Date.now()
    if (
      !settled
      && now - lastSettlementProbeAt >= SETTLEMENT_PROBE_INTERVAL_MS
    ) {
      lastSettlementProbeAt = now
      if (await probeSettlement()) break
    }
  }

  if (input.signal.aborted) {
    return finishCancelled()
  }

  const run = observedSettlement ? undefined : await runPromise
  if (run && !run.ok && mutationTransportDetached(run.error) && !input.signal.aborted) {
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
    while (
      !taskHasSettledRunResult(observed, settlementEvidence())
      && !input.signal.aborted
    ) {
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
          reason: observed.terminal
            ? undefined
            : "canonical_task_result_settled_after_mutation_transport_detached",
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

  let finalTask = observedSettlement
    ?? (run?.ok && taskHasSettledRunResult(run.value.task, settlementEvidence())
      ? run.value.task
      : undefined)
  if (ingressError && finalTask) diagnostics.push(outcomeDiagnostic("client_connection", ingressError))
  if (!finalTask) {
    try {
      finalTask = await input.api.task(task.taskId)
    } catch (error) {
      if (ingressError) throw ingressError
      throw error
    }
  } else if (!observedSettlement) {
    try {
      const refreshed = await input.api.task(task.taskId)
      if (taskHasSettledRunResult(refreshed, settlementEvidence())) finalTask = refreshed
    } catch (error) {
      diagnostics.push(outcomeDiagnostic("result_read", error))
    }
  }
  let replay: readonly EventProjection[] = []
  try {
    replay = await input.api.events(task.taskId)
  } catch (error) {
    if (!finalTask) throw error
    diagnostics.push(outcomeDiagnostic("event_sync", error))
  }
  for (const event of replay) emitLegacyEvent(input.output, event, accumulator)

  if (run && !run.ok && !(run.error instanceof RequestCancelledError)) {
    const outcome = classifyTaskOutcome(finalTask, accumulator.verifier, diagnostics)
    if (outcome.exitCode !== CliExitCode.SUCCESS && outcome.exitCode !== CliExitCode.VERIFIER_FAILED) {
      throw run.error
    }
    diagnostics.push(outcomeDiagnostic("client_connection", run.error))
  }
  if (input.workspaceRoot) {
    try {
      const materialized = await materializeWorkspaceDelivery(
        input.api,
        finalTask,
        input.workspaceRoot,
        input.signal,
      )
      input.output.event(
        {
          schema: "zyra.cli-workspace-transfer.v1",
          phase: "materialized",
          workspace_id: materialized.workspaceId,
          file_count: materialized.fileCount,
          bytes: materialized.bytes,
          paths: materialized.paths,
        },
        { taskId: finalTask.taskId, runId: finalTask.runId },
      )
    } catch (error) {
      // Partial-delivery retrieval is useful after a failed task, but a
      // secondary workspace-files error must not replace the canonical task
      // failure. Successful tasks still fail closed when their declared
      // delivery cannot be materialized.
      if (finalTask.status === "completed") throw error
      diagnostics.push(outcomeDiagnostic("workspace_materialization", error))
      input.output.event(
        {
          schema: "zyra.cli-workspace-transfer.v1",
          phase: "materialization_failed",
          workspace_id: String(record(finalTask.metadata.delivery).workspace_id ?? ""),
          error: error instanceof Error ? error.message : String(error),
          primary_task_status_preserved: true,
        },
        { taskId: finalTask.taskId, runId: finalTask.runId },
      )
    }
  }
  return classifyTaskOutcome(finalTask, accumulator.verifier, diagnostics)
}
