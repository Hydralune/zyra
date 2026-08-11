export const CLI_RECORD_SCHEMA = "zyra.cli-record.v1" as const
export const CLI_RESULT_SCHEMA = "zyra.cli-result.v1" as const

export enum CliExitCode {
  SUCCESS = 0,
  TASK_FAILED = 1,
  USAGE = 2,
  DAEMON_UNAVAILABLE = 3,
  CANCELLED = 4,
  VERIFIER_FAILED = 5,
}

export type CliCommandName =
  | "interactive"
  | "resume"
  | "ls"
  | "run"
  | "scenario"
  | "ui"
  | "daemon"
  | "help"
  | "version"

export interface CommonOptions {
  baseUrl: string
  autoStart: boolean
  startupTimeoutMs: number
  timeoutMs: number
}

export interface RunCommand extends CommonOptions {
  kind: "run"
  goal?: string
  file?: string
  cancelAfterMs?: number
  sealed: boolean
}

export interface InteractiveCommand extends CommonOptions {
  kind: "interactive"
  goal?: string
}

export interface ResumeCommand extends CommonOptions {
  kind: "resume"
  identity: string
}

export interface ListCommand extends CommonOptions {
  kind: "ls"
  status?: string
  limit: number
}

export type ScenarioAction =
  | "ls"
  | "registry"
  | "create"
  | "start"
  | "cancel"
  | "verify"
  | "evidence"

export interface ScenarioCommand extends CommonOptions {
  kind: "scenario"
  action: ScenarioAction
  runId?: string
  input?: string
  scenarioId?: string
  definitionVersion?: string
  profileId?: string
  policyId?: string
  policyDigest?: string
  mode: "sealed" | "interactive"
  seed: number
  labels: Readonly<Record<string, string>>
  preflight?: readonly Readonly<Record<string, unknown>>[]
  reason?: string
  wait: boolean
  includeArchived: boolean
  limit: number
  offset: number
}

export interface DaemonCommand extends CommonOptions {
  kind: "daemon"
  action: "start" | "stop" | "status"
  force: boolean
}

export interface UiCommand extends CommonOptions {
  kind: "ui"
  webPort: number
  open: boolean
  taskId?: string
}

export interface HelpCommand {
  kind: "help"
}

export interface VersionCommand {
  kind: "version"
}

export type CliCommand =
  | InteractiveCommand
  | ResumeCommand
  | ListCommand
  | RunCommand
  | ScenarioCommand
  | UiCommand
  | DaemonCommand
  | HelpCommand
  | VersionCommand

export interface CliRecord {
  schema: typeof CLI_RECORD_SCHEMA
  type: "event" | "diagnostic"
  timestamp: string
  request_id: string
  command: string
  task_id?: string
  run_id?: string
  cursor?: string
  generation?: number
  payload: Readonly<Record<string, unknown>>
}

export interface CliResultRecord {
  schema: typeof CLI_RESULT_SCHEMA
  type: "result"
  timestamp: string
  request_id: string
  command: string
  ok: boolean
  exit_code: CliExitCode
  status: string
  task_id?: string
  run_id?: string
  verifier?: Readonly<Record<string, unknown>>
  result?: Readonly<Record<string, unknown>>
  canonical_outcome?: Readonly<Record<string, unknown>>
  diagnostics?: readonly unknown[]
  error?: Readonly<Record<string, unknown>>
}

export class CliError extends Error {
  readonly exitCode: CliExitCode
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    message: string,
    exitCode: CliExitCode,
    code: string,
    details: Readonly<Record<string, unknown>> = {},
    options: ErrorOptions = {},
  ) {
    super(message, options)
    this.name = "CliError"
    this.exitCode = exitCode
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

export class CliUsageError extends CliError {
  constructor(message: string, details: Readonly<Record<string, unknown>> = {}) {
    super(message, CliExitCode.USAGE, "usage_error", details)
    this.name = "CliUsageError"
  }
}

export class CliDaemonError extends CliError {
  constructor(message: string, details: Readonly<Record<string, unknown>> = {}, options: ErrorOptions = {}) {
    super(message, CliExitCode.DAEMON_UNAVAILABLE, "daemon_unavailable", details, options)
    this.name = "CliDaemonError"
  }
}

export class CliTaskError extends CliError {
  constructor(message: string, code = "task_failed", details: Readonly<Record<string, unknown>> = {}) {
    super(message, CliExitCode.TASK_FAILED, code, details)
    this.name = "CliTaskError"
  }
}

export class CliCancelledError extends CliError {
  constructor(message: string, details: Readonly<Record<string, unknown>> = {}) {
    super(message, CliExitCode.CANCELLED, "task_cancelled", details)
    this.name = "CliCancelledError"
  }
}

export class CliVerifierError extends CliError {
  constructor(message: string, details: Readonly<Record<string, unknown>> = {}) {
    super(message, CliExitCode.VERIFIER_FAILED, "verifier_failed", details)
    this.name = "CliVerifierError"
  }
}
