import type { Readable, Writable } from "node:stream"
import {
  RequestCancelledError,
  ZyraApiError,
} from "@zyra/typed-api-client"
import { parseCliArgs, CLI_USAGE } from "./args.ts"
import { CliApi } from "./api.ts"
import {
  CliDaemonError,
  CliError,
  CliExitCode,
  CliTaskError,
  type CliCommand,
} from "./contracts.ts"
import {
  daemonStatus,
  ensureDaemon,
  stopManagedDaemon,
} from "./daemon.ts"
import { CliOutput } from "./output.ts"
import { executeRun, type CommandOutcome } from "./runner.ts"
import { executeScenario } from "./scenario.ts"
import { executeInteractive, executeResume } from "./commands/interactive.ts"
import { executeList } from "./commands/list.ts"
import { TerminalNodeLifecycle } from "./terminal/lifecycle.ts"

export interface MainEnvironment {
  stdout?: Writable
  stderr?: Writable
  stdin?: Readable
  signal?: AbortSignal
  token?: string
}

function commandLabel(command: CliCommand | undefined): string {
  if (!command) return "unknown"
  if (command.kind === "scenario" || command.kind === "daemon") {
    return `${command.kind}.${command.action}`
  }
  return command.kind
}

function classifyError(error: unknown): CliError {
  if (error instanceof CliError) return error
  if (error instanceof RequestCancelledError) {
    return new CliError(error.message, CliExitCode.CANCELLED, "request_cancelled")
  }
  if (error instanceof ZyraApiError) {
    if (error.category === "disconnect" || error.category === "timeout") {
      return new CliDaemonError("The Zyra daemon transport became unavailable.", {
        category: error.category,
        operation: error.context.operation,
      })
    }
    if (error.category === "cancellation") {
      return new CliError(error.message, CliExitCode.CANCELLED, "request_cancelled")
    }
    return new CliTaskError("The Zyra API contract rejected the command.", error.code, {
      category: error.category,
      operation: error.context.operation,
      status: error.context.status,
    })
  }
  return new CliTaskError(
    error instanceof Error ? error.message : "The Zyra CLI command failed.",
    "cli_internal_failure",
  )
}

async function daemonCommand(
  command: Extract<CliCommand, { kind: "daemon" }>,
  token: string | undefined,
): Promise<CommandOutcome> {
  if (command.action === "start") {
    const status = await ensureDaemon({
      baseUrl: command.baseUrl,
      token,
      autoStart: true,
      startupTimeoutMs: command.startupTimeoutMs,
    })
    return {
      exitCode: CliExitCode.SUCCESS,
      status: "running",
      result: { schema: "zyra.cli-daemon-result.v1", ...status },
    }
  }
  if (command.action === "stop") {
    const status = await stopManagedDaemon({
      baseUrl: command.baseUrl,
      token,
      force: command.force,
      timeoutMs: command.startupTimeoutMs,
    })
    return {
      exitCode: CliExitCode.SUCCESS,
      status: "stopped",
      result: { schema: "zyra.cli-daemon-result.v1", ...status },
    }
  }
  const status = await daemonStatus({ baseUrl: command.baseUrl, token })
  return {
    exitCode: CliExitCode.SUCCESS,
    status: status.reachable ? "running" : "unavailable",
    result: { schema: "zyra.cli-daemon-result.v1", ...status },
  }
}

function bridgeSignal(external?: AbortSignal): {
  controller: AbortController
  dispose: () => void
} {
  const controller = new AbortController()
  let interrupted = false
  const interrupt = (signal: "SIGINT" | "SIGTERM") => {
    if (interrupted) return
    interrupted = true
    controller.abort(new Error(`Zyra CLI received ${signal}.`))
  }
  const sigint = () => interrupt("SIGINT")
  const sigterm = () => interrupt("SIGTERM")
  process.once("SIGINT", sigint)
  process.once("SIGTERM", sigterm)
  const externalListener = () => controller.abort(external?.reason ?? new Error("CLI operation cancelled."))
  if (external?.aborted) externalListener()
  else external?.addEventListener("abort", externalListener, { once: true })
  return {
    controller,
    dispose() {
      process.removeListener("SIGINT", sigint)
      process.removeListener("SIGTERM", sigterm)
      external?.removeEventListener("abort", externalListener)
    },
  }
}

export async function runMain(argv: readonly string[], environment: MainEnvironment = {}): Promise<number> {
  const stdout = environment.stdout ?? process.stdout
  const stderr = environment.stderr ?? process.stderr
  const stdin = environment.stdin ?? process.stdin
  let command: CliCommand | undefined
  let output: CliOutput | undefined
  const requestId = `request_${crypto.randomUUID().replaceAll("-", "")}`
  try {
    command = parseCliArgs(argv)
    output = new CliOutput({
      stdout,
      stderr,
      requestId,
      command: commandLabel(command),
    })
    if (command.kind === "help") {
      output.diagnostic(CLI_USAGE)
      output.event({ schema: "zyra.cli-help.v1", command_count: 8 })
      output.result({ ok: true, exit_code: CliExitCode.SUCCESS, status: "help" })
      return CliExitCode.SUCCESS
    }
    if (command.kind === "version") {
      output.event({ schema: "zyra.cli-version.v1", version: "0.1.0" })
      output.result({ ok: true, exit_code: CliExitCode.SUCCESS, status: "version" })
      return CliExitCode.SUCCESS
    }
    const token = environment.token ?? (process.env.ZYRA_API_TOKEN?.trim() || undefined)
    if (command.kind === "daemon") {
      const outcome = await daemonCommand(command, token)
      output.event(outcome.result ?? {}, { taskId: outcome.taskId, runId: outcome.runId })
      output.result({
        ok: outcome.exitCode === CliExitCode.SUCCESS,
        exit_code: outcome.exitCode,
        status: outcome.status,
        task_id: outcome.taskId,
        run_id: outcome.runId,
        result: outcome.result,
      })
      return outcome.exitCode
    }

    const daemon = await ensureDaemon({
      baseUrl: command.baseUrl,
      token,
      autoStart: command.autoStart,
      startupTimeoutMs: command.startupTimeoutMs,
    })
    const lineMode = command.kind === "interactive" || command.kind === "resume"
    const terminalMode = lineMode || command.kind === "run" || command.kind === "scenario"
    const listTty = command.kind === "ls" && Boolean((stdout as Writable & { isTTY?: boolean }).isTTY)
    if (!lineMode && !listTty) {
      output.event({
        schema: "zyra.cli-daemon-connection.v1",
        reachable: daemon.reachable,
        managed: daemon.managed,
        generation: daemon.generation,
      })
    }
    const api = new CliApi({
      baseUrl: command.baseUrl,
      token,
      timeoutMs: command.timeoutMs,
    })
    const signal = bridgeSignal(environment.signal)
    const timer = setTimeout(
      () => signal.controller.abort(new Error("Zyra CLI command timeout expired.")),
      command.timeoutMs,
    )
    timer.unref()
    const cancelTimer = command.kind === "run" && command.cancelAfterMs !== undefined
      ? setTimeout(
          () => signal.controller.abort(new Error("Zyra CLI explicit cancel deadline reached.")),
          command.cancelAfterMs,
        )
      : undefined
    cancelTimer?.unref()
    let outcome: CommandOutcome
    let terminal: TerminalNodeLifecycle | undefined
    try {
      if (terminalMode) {
        terminal = await TerminalNodeLifecycle.create({
          baseUrl: command.baseUrl,
          token,
          timeoutMs: Math.min(command.timeoutMs, 15_000),
          startupRoot: process.cwd(),
        })
        const registration = await terminal.start()
        if (lineMode) stderr.write(`terminal node ${registration.backend_id} registered · generation ${registration.generation.slice(0, 8)}\n`)
      }
      if (command.kind === "run") outcome = await executeRun({ command, api, output, signal: signal.controller.signal })
      else if (command.kind === "scenario") outcome = await executeScenario({ command, api, output })
      else if (command.kind === "interactive") {
        outcome = await executeInteractive({ command, api, stdin, stdout, stderr, signal: signal.controller.signal, terminalStatus: () => terminal!.status() })
      } else if (command.kind === "resume") {
        outcome = await executeResume({ command, api, stdin, stdout, stderr, signal: signal.controller.signal, terminalStatus: () => terminal!.status() })
      } else {
        outcome = await executeList({ command, api, stdout, jsonl: output })
      }
    } finally {
      try {
        await terminal?.stop("Zyra CLI session complete")
      } finally {
        clearTimeout(timer)
        if (cancelTimer) clearTimeout(cancelTimer)
        signal.dispose()
        api.close("CLI command complete")
      }
    }
    if (!lineMode && !listTty) output.result({
      ok: outcome.exitCode === CliExitCode.SUCCESS,
      exit_code: outcome.exitCode,
      status: outcome.status,
      task_id: outcome.taskId,
      run_id: outcome.runId,
      verifier: outcome.verifier,
      result: outcome.result,
    })
    return outcome.exitCode
  } catch (unknownError) {
    const error = classifyError(unknownError)
    output ??= new CliOutput({
      stdout,
      stderr,
      requestId,
      command: commandLabel(command),
    })
    const lineMode = command?.kind === "interactive" || command?.kind === "resume"
    if (lineMode) {
      stderr.write(`Zyra: ${error.message}\n`)
      return error.exitCode
    }
    output.diagnostic(error.message)
    output.result({
      ok: false,
      exit_code: error.exitCode,
      status: error.code,
      error: {
        schema: "zyra.cli-error.v1",
        code: error.code,
        message: error.message,
        details: error.details,
      },
    })
    return error.exitCode
  }
}
