import { readdir } from "node:fs/promises"
import type { Readable, Writable } from "node:stream"
import type { TaskProjection } from "@zyra/typed-api-client"
import { CliApi } from "../api.ts"
import { CliExitCode, CliTaskError, type InteractiveCommand, type ResumeCommand } from "../contracts.ts"
import { TerminalPrompt } from "../input/terminal-prompt.ts"
import { LineTranscriptRenderer } from "../render/line-renderer.ts"
import { SessionProjection } from "../session/projection.ts"
import type { CommandOutcome } from "../runner.ts"

const LOCAL_COMMANDS = ["/help", "/exit", "/edit", "/restore", "/cancel-draft"] as const

function terminalTask(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "cancelled", "killed"].includes(task.status)
}

export async function observeTask(input: {
  api: CliApi
  task: TaskProjection
  cwd: string
  output: Writable
  signal: AbortSignal
  resume: boolean
}): Promise<CommandOutcome> {
  const capabilities = await input.api.ingressCapabilities(input.task.taskId)
  const projection = new SessionProjection({ taskId: input.task.taskId, generation: capabilities.generation })
  const renderer = new LineTranscriptRenderer(input.output)
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
  if (terminalTask(input.task) || projection.terminal) {
    projection.complete()
    renderer.finish(projection.snapshot())
    return { exitCode: input.task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED, status: input.task.status, taskId: input.task.taskId, runId: input.task.runId }
  }

  let runSettled = false
  let runResult: Promise<unknown> | undefined
  if (["pending", "paused", "interrupted"].includes(input.task.status)) {
    runResult = input.api.runTask(input.task, input.signal).finally(() => { runSettled = true })
  }
  projection.connected()
  renderer.status(projection.snapshot())
  try {
    let windows = 0
    while (!projection.terminal && !input.signal.aborted) {
      let closedCursor: string | undefined
      for await (const message of input.api.streamIngress(
        input.task.taskId,
        cursor,
        capabilities.generation,
        input.signal,
      )) {
        if (message.kind === "event") {
          const record = projection.apply(message.frame)
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
        throw new CliTaskError("SSE disconnected without a resumable close cursor.", "event_stream_disconnected", {
          recovery: "Run zyra resume with the same task or session identity.",
          revision: projection.snapshot().revision,
        })
      }
      windows += 1
      // A completed mutation response is canonical server state. One SSE
      // window that closes after it settles is the bounded final drain; the
      // final task read below validates terminal state without polling.
      if (runSettled && closedCursor) break
      if (windows > 10_000) throw new CliTaskError("Interactive stream exceeded its bounded reconnect window.", "event_stream_budget")
    }
    if (input.signal.aborted) throw input.signal.reason
    await runResult
    const finalTask = await input.api.task(input.task.taskId)
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
  }
}

async function submit(input: {
  goal: string
  command: InteractiveCommand
  api: CliApi
  cwd: string
  output: Writable
  signal: AbortSignal
}): Promise<CommandOutcome> {
  const created = await input.api.createPendingTask(input.goal, false)
  return observeTask({ api: input.api, task: created.task, cwd: input.cwd, output: input.output, signal: input.signal, resume: false })
}

export async function executeInteractive(input: {
  command: InteractiveCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  stderr: Writable
  signal: AbortSignal
  cwd?: string
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  if (input.command.goal) return submit({ goal: input.command.goal, command: input.command, api: input.api, cwd, output: input.stdout, signal: input.signal })
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
      last = await submit({ goal: line, command: input.command, api: input.api, cwd, output: input.stdout, signal: input.signal })
    }
    return last
  } finally {
    terminal.close()
  }
}

export async function executeResume(input: {
  command: ResumeCommand
  api: CliApi
  stdout: Writable
  signal: AbortSignal
  cwd?: string
}): Promise<CommandOutcome> {
  const resolved = await input.api.resolveTask(input.command.identity)
  return observeTask({
    api: input.api,
    task: resolved.task,
    cwd: input.cwd ?? process.cwd(),
    output: input.stdout,
    signal: input.signal,
    resume: true,
  })
}
