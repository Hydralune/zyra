import { readdir } from "node:fs/promises"
import type { Readable, Writable } from "node:stream"
import { ZyraApiError, type TaskProjection } from "@zyra/typed-api-client"
import { CliApi } from "../api.ts"
import { CliExitCode, CliTaskError, type InteractiveCommand, type ResumeCommand } from "../contracts.ts"
import {
  ACTIVE_CONTROL_COMMANDS,
  CliControlSession,
  formatCommandQueue,
  formatCommandReceipt,
  parseControlIntent,
} from "../control/commands.ts"
import { CliPermissionSession } from "../control/permission.ts"
import { TerminalPrompt } from "../input/terminal-prompt.ts"
import { LineTranscriptRenderer } from "../render/line-renderer.ts"
import { SessionProjection } from "../session/projection.ts"
import type { TerminalNodeStatus } from "../terminal/server.ts"
import { mutationTransportDetached, type CommandOutcome } from "../runner.ts"

const LOCAL_COMMANDS = ["/help", "/exit", "/edit", "/restore", "/cancel-draft", ...ACTIVE_CONTROL_COMMANDS] as const

function terminalTask(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)
}

function recoveryNeedsSnapshot(error: unknown): boolean {
  if (error instanceof CliTaskError) {
    return ["gap", "cursor", "generation", "order", "binding"].some((marker) => error.code.includes(marker))
  }
  return error instanceof ZyraApiError && ["conflict", "not_found", "version", "protocol"].includes(error.category)
}

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal.removeEventListener("abort", abort)
      resolve()
    }
    const timer = setTimeout(finish, milliseconds)
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    signal.addEventListener("abort", abort, { once: true })
  })
}

function controlError(error: unknown): string {
  if (error instanceof CliTaskError) {
    const revision = error.details.actual === undefined ? "" : ` (canonical revision ${String(error.details.actual)})`
    return `${error.code}: ${error.message}${revision}`
  }
  if (error instanceof Error) return error.message
  return String(error)
}

async function runActiveControlLoop(input: {
  terminal: TerminalPrompt
  controls: CliControlSession
  permissions: CliPermissionSession
  stderr: Writable
  signal: AbortSignal
}): Promise<void> {
  while (!input.signal.aborted) {
    const result = await input.terminal.read()
    if (result.kind === "exit" || result.text === "/exit") {
      input.stderr.write("\r\ncontrol input detached; canonical task observation continues\n")
      return
    }
    if (result.text === "/help") {
      input.stderr.write(`${ACTIVE_CONTROL_COMMANDS.join("  ")}\nPlain text redirects the active task through /change; /now|/next|/later take a slash command.\n`)
      continue
    }
    try {
      const intent = parseControlIntent(result.text)
      if (intent.kind === "queue") {
        input.stderr.write(`${formatCommandQueue(await input.controls.queue(input.signal, true))}\n`)
      } else if (intent.kind === "task-cancel") {
        const task = await input.controls.cancelTask(intent.reason)
        input.stderr.write(`task cancel committed · ${task.taskId} · ${task.status}\n`)
      } else if (intent.kind === "task-continue") {
        const task = await input.controls.continueTask(input.signal)
        input.stderr.write(`task continuation committed · ${task.taskId} · ${task.status}\n`)
      } else if (intent.kind === "command-cancel") {
        const response = await input.controls.cancelCommand(intent.requestId, input.signal)
        input.stderr.write(`command cancellation committed · ${intent.requestId} · ${JSON.stringify(response)}\n`)
      } else if (intent.kind === "command-retry") {
        input.stderr.write(`${formatCommandReceipt(await input.controls.retry(intent.requestId, input.signal))}\n`)
      } else if (intent.kind === "permission") {
        const response = await input.permissions.resolve({
          requestId: intent.requestId,
          effect: intent.effect,
          feedback: intent.feedback,
          signal: input.signal,
        })
        const receipt = response.receipt && typeof response.receipt === "object"
          ? response.receipt as Record<string, unknown>
          : {}
        input.stderr.write(`permission ${intent.effect} committed · ${intent.requestId} · accepted ${receipt.accepted === true}\n`)
      } else {
        input.stderr.write(`${formatCommandReceipt(await input.controls.submit(intent.text, {
          mode: intent.mode,
          priority: intent.priority,
          signal: input.signal,
        }))}\n`)
      }
    } catch (error) {
      input.stderr.write(`control rejected · ${controlError(error)}\n`)
    }
  }
}

export async function observeTask(input: {
  api: CliApi
  task: TaskProjection
  cwd: string
  output: Writable
  stdin?: Readable
  stderr?: Writable
  signal: AbortSignal
  resume: boolean
  terminalStatus?: () => TerminalNodeStatus
}): Promise<CommandOutcome> {
  let capabilities = await input.api.ingressCapabilities(input.task.taskId)
  let projection = new SessionProjection({ taskId: input.task.taskId, generation: capabilities.generation })
  const renderer = new LineTranscriptRenderer(input.output, undefined, input.terminalStatus)
  renderer.header({
    cwd: input.cwd,
    taskId: input.task.taskId,
    runId: input.task.runId,
    revision: `${capabilities.generation}:${capabilities.subscriptionSequence}`,
  })
  let cursor = ""
  for await (const page of input.api.snapshotIngress(input.task.taskId, capabilities.generation)) {
    cursor = page.cursor
    for (const frame of page.frames) {
      const record = projection.apply(frame)
      if (record) renderer.record(record)
    }
  }
  if (!capabilities.sseAvailable && !terminalTask(input.task)) {
    projection.disconnected()
    renderer.finish(projection.snapshot())
    throw new CliTaskError("SSE event stream is disabled; interactive observation is degraded and stopped.", "event_stream_unavailable", {
      recovery: "Restore SSE and run zyra resume with the same server identity.",
      task_id: input.task.taskId,
      revision: projection.snapshot().revision,
    })
  }
  const resumableTerminal = input.resume
    && ["failed", "blocked"].includes(input.task.status)
  if ((terminalTask(input.task) || projection.terminal) && !resumableTerminal) {
    projection.complete()
    renderer.finish(projection.snapshot())
    return { exitCode: input.task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED, status: input.task.status, taskId: input.task.taskId, runId: input.task.runId }
  }
  if (resumableTerminal) projection.resume()

  const tty = Boolean((input.stdin as Readable & { isTTY?: boolean } | undefined)?.isTTY)
  let terminal: TerminalPrompt | undefined
  let controls: CliControlSession | undefined
  let permissions: CliPermissionSession | undefined
  let controlsPromise: Promise<void> | undefined
  if (tty && input.stdin && input.stderr) {
    terminal = new TerminalPrompt({ stdin: input.stdin, stderr: input.stderr, candidates: LOCAL_COMMANDS })
    controls = new CliControlSession({ api: input.api, task: input.task })
    permissions = new CliPermissionSession({
      api: input.api,
      task: input.task,
      custodyToken: process.env.ZYRA_PERMISSION_CUSTODY_TOKEN,
    })
    const permissionAvailable = await permissions.open(input.signal)
    if (!permissionAvailable) {
      input.stderr.write(`permission controls fail closed · ${permissions.custodyError?.code ?? "permission_custody_unavailable"}\n`)
    }
    input.stderr.write("active controls · /help lists queue, control, recovery, and permission actions\n")
  }
  let runSettled = false
  type RunOutcome =
    | { ok: true; value: Awaited<ReturnType<CliApi["runTask"]>> }
    | { ok: false; error: unknown }
  let runOutcome: RunOutcome | undefined
  let runResult: Promise<RunOutcome> | undefined
  let mutationDetachReported = false
  let detachedSettlement: TaskProjection | undefined
  // `zyra resume` is an explicit request to reacquire task execution, not
  // merely to attach to the event stream.  A daemon/process loss can leave
  // the durable task projection at `running` even though its in-process
  // execution owner no longer exists.  The task-run endpoint owns fencing
  // the stale reservation and restoring the physical continuation.
  if (input.resume || ["pending", "paused", "interrupted"].includes(input.task.status)) {
    runResult = input.api.runTask(input.task, input.signal)
      .then(
        (value): RunOutcome => ({ ok: true, value }),
        (error: unknown): RunOutcome => ({ ok: false, error }),
      )
      .then((outcome) => {
        runOutcome = outcome
        return outcome
      })
      .finally(() => { runSettled = true })
  }
  if (terminal && controls && permissions && input.stderr) {
    controlsPromise = runActiveControlLoop({ terminal, controls, permissions, stderr: input.stderr, signal: input.signal })
  }
  projection.connected()
  renderer.status(projection.snapshot())
  try {
    let windows = 0
    let recoveryAttempts = 0
    while (!projection.terminal && !input.signal.aborted) {
      try {
        let closedCursor: string | undefined
        for await (const message of input.api.streamIngress(
          input.task.taskId,
          cursor,
          capabilities.generation,
          input.signal,
        )) {
          if (message.kind === "event") {
            const record = projection.apply(message.frame)
            if (message.frame.cursor) cursor = message.frame.cursor
            if (record) renderer.record(record)
            renderer.status(projection.snapshot())
          } else if (message.kind === "heartbeat" || message.kind === "close") {
            if (message.sequence < projection.snapshot().lastSequence) {
              throw new CliTaskError("SSE cursor regressed behind rendered state.", "contract_cursor_regression")
            }
            cursor = message.cursor
            if (message.kind === "close") closedCursor = message.cursor
            renderer.status(projection.snapshot())
          }
        }
        if (!closedCursor && !projection.terminal) {
          throw new CliTaskError("SSE disconnected without a close frame; retaining the last server cursor.", "event_stream_disconnected", {
            revision: projection.snapshot().revision,
            cursor_present: Boolean(cursor),
          })
        }
        recoveryAttempts = 0
        windows += 1
        // A completed mutation response is canonical server state. One SSE
        // window that closes after it settles is the bounded final drain; the
        // final task read below validates terminal state without polling.
        if (runSettled && closedCursor) {
          const detached = runOutcome?.ok === false
            && mutationTransportDetached(runOutcome.error)
          if (!detached) break
          if (!mutationDetachReported) {
            mutationDetachReported = true
            projection.recovering("mutation_transport_detached")
            renderer.recovery({
              reason: "mutation transport detached; observing canonical task state without replay",
              cursor,
              generation: capabilities.generation,
              snapshot: false,
            })
            projection.connected()
          }
          // The POST may still own a live physical dispatch. Never replay it:
          // retain SSE as the progress channel and use the canonical task read
          // only to recognize settlement when a failure path has no dedicated
          // runtime.task.* terminal frame.
          const observed = await input.api.task(input.task.taskId)
          if (terminalTask(observed)) {
            detachedSettlement = observed
            break
          }
        }
        if (windows > 10_000) throw new CliTaskError("Interactive stream exceeded its bounded reconnect window.", "event_stream_budget")
      } catch (error) {
        if (input.signal.aborted) throw input.signal.reason
        recoveryAttempts += 1
        projection.disconnected()
        renderer.status(projection.snapshot())
        if (recoveryAttempts > 6) {
          throw new CliTaskError("Event ingress recovery exhausted its bounded retry budget.", "event_stream_recovery_exhausted", {
            task_id: input.task.taskId,
            revision: projection.snapshot().revision,
            cursor_present: Boolean(cursor),
          })
        }
        await wait(Math.min(2_000, 100 * (2 ** (recoveryAttempts - 1))), input.signal)
        let snapshot = recoveryNeedsSnapshot(error)
        if (!snapshot) {
          try {
            const probed = await input.api.ingressCapabilities(input.task.taskId, cursor, capabilities.generation)
            snapshot = probed.generation !== capabilities.generation
            capabilities = probed
          } catch (probeError) {
            if (!recoveryNeedsSnapshot(probeError)) throw probeError
            snapshot = true
          }
        }
        if (snapshot) {
          capabilities = await input.api.ingressCapabilities(input.task.taskId)
          const replacement = new SessionProjection({ taskId: input.task.taskId, generation: capabilities.generation })
          let replacementCursor = ""
          for await (const page of input.api.snapshotIngress(input.task.taskId, capabilities.generation)) {
            replacementCursor = page.cursor
            for (const frame of page.frames) {
              const record = replacement.apply(frame)
              if (record) renderer.record(record)
            }
          }
          projection = replacement
          cursor = replacementCursor
        }
        projection.recovering(snapshot ? "event_snapshot_replaced" : "event_cursor_resumed")
        renderer.recovery({
          reason: error instanceof Error ? error.message : "event ingress disconnected",
          cursor,
          generation: capabilities.generation,
          snapshot,
        })
        projection.connected()
        renderer.status(projection.snapshot())
      }
    }
    if (input.signal.aborted) throw input.signal.reason
    const settledRun = await runResult
    if (settledRun?.ok === false && !mutationTransportDetached(settledRun.error)) {
      throw settledRun.error
    }
    const finalTask = detachedSettlement ?? await input.api.task(input.task.taskId)
    if (!terminalTask(finalTask)) {
      throw new CliTaskError("Task mutation settled without a canonical terminal state.", "task_terminal_state_missing")
    }
    projection.complete()
    renderer.finish(projection.snapshot())
    return {
      exitCode: finalTask.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED,
      status: finalTask.status,
      taskId: finalTask.taskId,
      runId: finalTask.runId,
      result: { schema: "zyra.cli-interactive-result.v1", revision: projection.snapshot().revision, resumed: input.resume },
    }
  } catch (error) {
    projection.disconnected()
    renderer.finish(projection.snapshot())
    throw error
  } finally {
    terminal?.close()
    await controlsPromise?.catch(() => undefined)
  }
}

async function submit(input: {
  goal: string
  command: InteractiveCommand
  api: CliApi
  cwd: string
  output: Writable
  stdin?: Readable
  stderr?: Writable
  signal: AbortSignal
  terminalStatus?: () => TerminalNodeStatus
}): Promise<CommandOutcome> {
  const created = await input.api.createPendingTask(input.goal, false)
  return observeTask({
    api: input.api,
    task: created.task,
    cwd: input.cwd,
    output: input.output,
    signal: input.signal,
    resume: false,
    stdin: input.stdin,
    stderr: input.stderr,
    terminalStatus: input.terminalStatus,
  })
}

export async function executeInteractive(input: {
  command: InteractiveCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  stderr: Writable
  signal: AbortSignal
  cwd?: string
  terminalStatus?: () => TerminalNodeStatus
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  if (input.command.goal) return submit({
    goal: input.command.goal,
    command: input.command,
    api: input.api,
    cwd,
    output: input.stdout,
    stdin: input.stdin,
    stderr: input.stderr,
    signal: input.signal,
    terminalStatus: input.terminalStatus,
  })
  const tty = Boolean((input.stdin as Readable & { isTTY?: boolean }).isTTY)
  if (!tty) throw new CliTaskError("zyra without a goal requires an interactive terminal.", "interactive_terminal_required")
  const references = await readdir(cwd, { withFileTypes: true })
    .then((entries) => entries.slice(0, 500).map((entry) => `@${entry.name}${entry.isDirectory() ? "/" : ""}`))
    .catch(() => [] as string[])
  const terminal = new TerminalPrompt({ stdin: input.stdin, stderr: input.stderr, candidates: [...LOCAL_COMMANDS, ...references] })
  let last: CommandOutcome = { exitCode: CliExitCode.SUCCESS, status: "interactive" }
  try {
    input.stderr.write(`Zyra interactive · ${cwd}\nCtrl+J inserts a newline; Enter submits; Esc stashes a draft; Ctrl+E opens VISUAL/EDITOR.\n`)
    while (!input.signal.aborted) {
      const result = await terminal.read()
      if (result.kind === "exit") break
      const line = result.text
      if (line === "/exit") break
      if (line === "/help") {
        input.stderr.write(`${LOCAL_COMMANDS.join("  ")}\nUse @path references; Ctrl+J inserts a newline without submitting.\n`)
        continue
      }
      if (line === "/cancel-draft") {
        terminal.draft.cancel()
        input.stderr.write("draft stashed; /restore recovers it\n")
        continue
      }
      if (line === "/restore") {
        const restored = terminal.draft.restore()
        input.stderr.write(restored ? `draft restored (${restored.text.length} chars)\n` : "no stashed draft\n")
        continue
      }
      if (line === "/edit") {
        input.stderr.write("Use Ctrl+E while editing a draft to open VISUAL/EDITOR.\n")
        continue
      }
      last = await submit({
        goal: line,
        command: input.command,
        api: input.api,
        cwd,
        output: input.stdout,
        stdin: input.stdin,
        stderr: input.stderr,
        signal: input.signal,
        terminalStatus: input.terminalStatus,
      })
    }
    return last
  } finally {
    terminal.close()
  }
}

export async function executeResume(input: {
  command: ResumeCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  stderr: Writable
  signal: AbortSignal
  cwd?: string
  terminalStatus?: () => TerminalNodeStatus
}): Promise<CommandOutcome> {
  const resolved = await input.api.resolveTask(input.command.identity)
  return observeTask({
    api: input.api,
    task: resolved.task,
    cwd: input.cwd ?? process.cwd(),
    output: input.stdout,
    signal: input.signal,
    resume: true,
    stdin: input.stdin,
    stderr: input.stderr,
    terminalStatus: input.terminalStatus,
  })
}
