import type { Writable } from "node:stream"
import { CliApi } from "../api.ts"
import { CliExitCode, type ListCommand } from "../contracts.ts"
import type { CliOutput } from "../output.ts"
import type { CommandOutcome } from "../runner.ts"

export async function executeList(input: {
  command: ListCommand
  api: CliApi
  stdout: Writable
  jsonl: CliOutput
}): Promise<CommandOutcome> {
  const [tasks, sessions] = await Promise.all([
    input.api.tasks({ status: input.command.status, limit: input.command.limit }),
    input.api.sessions({ status: input.command.status, limit: input.command.limit }),
  ])
  const tty = Boolean((input.stdout as Writable & { isTTY?: boolean }).isTTY)
  const result = {
    schema: "zyra.cli-session-list.v1",
    state_owner: sessions.stateOwner,
    tasks: tasks.tasks.map((task) => ({
      task_id: task.taskId,
      session_id: task.sessionId,
      run_id: task.runId,
      status: task.status,
      updated_at: task.updatedAt,
    })),
    sessions: sessions.sessions.map((session) => ({
      session_id: session.sessionId,
      resolution: session.resolution,
      resume_task_id: session.resumeTaskId,
      status: session.statuses,
      updated_at: session.updatedAt,
    })),
  }
  if (tty) {
    input.stdout.write("TASK / SESSION                         STATUS          SERVER REVISION\n")
    for (const task of tasks.tasks) input.stdout.write(`${task.taskId.padEnd(38)} ${task.status.padEnd(15)} ${task.updatedAt}\n`)
    for (const session of sessions.sessions) input.stdout.write(`${session.sessionId.padEnd(38)} ${session.resolution.padEnd(15)} ${session.updatedAt ?? "n/a"}\n`)
  } else {
    input.jsonl.event(result)
  }
  return { exitCode: CliExitCode.SUCCESS, status: "listed", result }
}
