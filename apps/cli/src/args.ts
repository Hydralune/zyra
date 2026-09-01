import {
  bindCommandArguments,
  type CommandArgumentSpec,
  type CommandToken,
} from "@zyra/commands"
import {
  CliUsageError,
  type CliCommand,
  type CommonOptions,
  type ScenarioAction,
} from "./contracts.ts"

const DEFAULT_BASE_URL = "http://127.0.0.1:8000"
const DEFAULT_STARTUP_TIMEOUT_MS = 60_000
// A zero command timeout means that the agent run has no CLI-owned total
// deadline.  Local HTTP/tool operations remain independently bounded and an
// outer harness or caller AbortSignal can still cancel the run.
const DEFAULT_RUN_TIMEOUT_MS = 0
const MAX_RUN_TIMEOUT_MS = 4 * 60 * 60_000

const COMMON_SPECS: readonly CommandArgumentSpec[] = [
  {
    name: "baseUrl",
    kind: "string",
    required: false,
    flag: "--base-url",
    maximumBytes: 2_048,
    description: "Zyra daemon base URL.",
  },
  {
    name: "autoStart",
    kind: "boolean",
    required: false,
    flag: "--autostart",
    description: "Start the local daemon when it is unavailable.",
  },
  {
    name: "startupTimeoutMs",
    kind: "duration",
    required: false,
    flag: "--startup-timeout",
    minimum: 1_000,
    maximum: 5 * 60_000,
    description: "Bounded daemon startup timeout.",
  },
  {
    name: "timeoutMs",
    kind: "duration",
    required: false,
    flag: "--timeout",
    minimum: 0,
    maximum: MAX_RUN_TIMEOUT_MS,
    description: "Optional total command timeout; 0 keeps the agent run open.",
  },
]

const RUN_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "file",
    kind: "string",
    required: false,
    flag: "--file",
    aliases: ["-f"],
    maximumBytes: 32 * 1024,
    description: "Read the task goal from a UTF-8 file.",
  },
  {
    name: "cancelAfterMs",
    kind: "duration",
    required: false,
    flag: "--cancel-after",
    minimum: 1,
    maximum: 10 * 60_000,
    description: "Cancel the submitted task after a bounded duration.",
  },
  {
    name: "sealed",
    kind: "boolean",
    required: false,
    flag: "--sealed",
    description: "Run with sealed autonomous competition policy.",
  },
  {
    name: "goal",
    kind: "string",
    required: false,
    variadic: true,
    maximumBytes: 256 * 1024,
    description: "The non-interactive task goal.",
  },
]

const INTERACTIVE_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "goal",
    kind: "string",
    required: false,
    variadic: true,
    maximumBytes: 256 * 1024,
    description: "Start an interactive task with this goal.",
  },
]

const RESUME_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "identity",
    kind: "identity",
    required: true,
    maximumBytes: 512,
    description: "Task or task-backed session identity.",
  },
]

const LIST_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "status",
    kind: "string",
    required: false,
    flag: "--status",
    maximumBytes: 64,
    description: "Server-side task and session status filter.",
  },
  {
    name: "limit",
    kind: "integer",
    required: false,
    flag: "--limit",
    minimum: 1,
    maximum: 1_000,
    description: "Maximum tasks and sessions to return.",
  },
]

const SCENARIO_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  { name: "scenarioId", kind: "identity", required: false, flag: "--scenario-id", description: "Scenario definition id." },
  { name: "definitionVersion", kind: "string", required: false, flag: "--definition-version", description: "Scenario definition version." },
  { name: "profileId", kind: "identity", required: false, flag: "--profile-id", description: "Scenario profile id." },
  { name: "policyId", kind: "identity", required: false, flag: "--policy-id", description: "Scenario policy id." },
  { name: "policyDigest", kind: "string", required: false, flag: "--policy-digest", description: "Expected policy digest." },
  { name: "mode", kind: "string", required: false, flag: "--mode", choices: ["sealed", "interactive"], description: "Scenario run mode." },
  { name: "seed", kind: "integer", required: false, flag: "--seed", minimum: 0, description: "Deterministic scenario seed." },
  { name: "labels", kind: "json", required: false, flag: "--labels", description: "JSON object of scenario labels." },
  { name: "preflight", kind: "json", required: false, flag: "--preflight", description: "JSON array of server-validated clean-state targets." },
  { name: "reason", kind: "string", required: false, flag: "--reason", maximumBytes: 8 * 1024, description: "Cancellation reason." },
  { name: "wait", kind: "boolean", required: false, flag: "--wait", description: "Wait for scenario completion." },
  { name: "includeArchived", kind: "boolean", required: false, flag: "--include-archived", description: "Include archived scenario runs." },
  { name: "limit", kind: "integer", required: false, flag: "--limit", minimum: 1, maximum: 10_000, description: "Scenario page size." },
  { name: "offset", kind: "integer", required: false, flag: "--offset", minimum: 0, description: "Scenario page offset." },
  { name: "value", kind: "string", required: false, variadic: true, maximumBytes: 256 * 1024, description: "Run id or scenario input." },
]

const DAEMON_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  { name: "force", kind: "boolean", required: false, flag: "--force", description: "Stop a managed daemon even when tasks are active." },
]

const UI_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "webPort",
    kind: "integer",
    required: false,
    flag: "--web-port",
    minimum: 0,
    maximum: 65_535,
    description: "Loopback Web port; use 0 for an ephemeral port.",
  },
  {
    name: "open",
    kind: "boolean",
    required: false,
    flag: "--open",
    description: "Open the product route in the system browser.",
  },
  {
    name: "taskId",
    kind: "identity",
    required: false,
    flag: "--task",
    description: "Open an existing canonical task directly.",
  },
]

const DOCTOR_SPECS: readonly CommandArgumentSpec[] = [
  ...COMMON_SPECS,
  {
    name: "bundle",
    kind: "string",
    required: false,
    flag: "--bundle",
    maximumBytes: 32 * 1024,
    description: "Write a redacted JSON diagnostic bundle inside the current workspace.",
  },
]

function tokens(values: readonly string[]): CommandToken[] {
  let offset = 0
  return values.map((value) => {
    const token: CommandToken = {
      value,
      raw: value,
      start: offset,
      end: offset + value.length,
      quoted: false,
    }
    offset = token.end + 1
    return token
  })
}

function bound(values: readonly string[], specs: readonly CommandArgumentSpec[]): Readonly<Record<string, unknown>> {
  const parsed = bindCommandArguments(tokens(values), specs)
  if (parsed.errors.length) {
    throw new CliUsageError(parsed.errors.map((entry) => entry.message).join(" "), {
      errors: parsed.errors.map((entry) => ({ code: entry.code, argument: entry.argument })),
    })
  }
  return parsed.values
}

function common(values: Readonly<Record<string, unknown>>): CommonOptions {
  const baseUrl = String(values.baseUrl || process.env.ZYRA_API_URL || DEFAULT_BASE_URL)
  try {
    const parsed = new URL(baseUrl)
    if (
      !["http:", "https:"].includes(parsed.protocol)
      || parsed.username
      || parsed.password
      || (parsed.pathname !== "/" && parsed.pathname !== "")
    ) throw new TypeError("invalid origin")
  } catch {
    throw new CliUsageError("--base-url must be an HTTP(S) origin without credentials or a path.")
  }
  return {
    baseUrl,
    autoStart: values.autoStart === undefined ? true : values.autoStart === true,
    startupTimeoutMs: Number(values.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS),
    timeoutMs: Number(values.timeoutMs ?? DEFAULT_RUN_TIMEOUT_MS),
  }
}

function labels(value: unknown): Readonly<Record<string, string>> {
  if (value === undefined) return {}
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CliUsageError("--labels must be a JSON object.")
  }
  const result: Record<string, string> = {}
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry !== "string") throw new CliUsageError("Every --labels value must be a string.")
    result[key] = entry
  }
  return result
}

function preflight(value: unknown): readonly Readonly<Record<string, unknown>>[] | undefined {
  if (value === undefined) return undefined
  if (!Array.isArray(value) || value.length > 32) {
    throw new CliUsageError("--preflight must be a JSON array with at most 32 targets.")
  }
  return value.map((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      throw new CliUsageError("Every --preflight target must be a JSON object.")
    }
    return { ...entry as Record<string, unknown> }
  })
}

export function parseCliArgs(argv: readonly string[]): CliCommand {
  if (argv[0] === "--help" || argv[0] === "-h") {
    return { kind: "help" }
  }
  if (argv[0] === "--version" || argv[0] === "-V") {
    return { kind: "version" }
  }
  const command = argv[0]!
  if (!argv.length || command.startsWith("-")) {
    const values = bound(argv, INTERACTIVE_SPECS)
    return {
      kind: "interactive",
      ...common(values),
      goal: typeof values.goal === "string" && values.goal.trim()
        ? values.goal.trim()
        : undefined,
    }
  }
  if (command === "run") {
    const values = bound(argv.slice(1), RUN_SPECS)
    const goal = typeof values.goal === "string" ? values.goal.trim() : undefined
    const file = typeof values.file === "string" ? values.file : undefined
    if (goal && file) throw new CliUsageError("Use either a goal or --file, not both.")
    return {
      kind: "run",
      ...common(values),
      goal,
      file,
      cancelAfterMs: values.cancelAfterMs === undefined ? undefined : Number(values.cancelAfterMs),
      sealed: values.sealed === true,
    }
  }
  if (command === "scenario") {
    const action = argv[1] as ScenarioAction | undefined
    if (!action || !["ls", "registry", "create", "start", "cancel", "verify", "evidence"].includes(action)) {
      throw new CliUsageError("zyra scenario requires ls, registry, create, start, cancel, verify, or evidence.")
    }
    const values = bound(argv.slice(2), SCENARIO_SPECS)
    const value = typeof values.value === "string" ? values.value.trim() : undefined
    if (["start", "cancel", "verify", "evidence"].includes(action) && !value) {
      throw new CliUsageError(`zyra scenario ${action} requires a scenario run id.`)
    }
    if (action === "create" && !value) {
      throw new CliUsageError("zyra scenario create requires scenario input.")
    }
    return {
      kind: "scenario",
      action,
      ...common(values),
      runId: action === "create" ? undefined : value,
      input: action === "create" ? value : undefined,
      scenarioId: values.scenarioId as string | undefined,
      definitionVersion: values.definitionVersion as string | undefined,
      profileId: values.profileId as string | undefined,
      policyId: values.policyId as string | undefined,
      policyDigest: values.policyDigest as string | undefined,
      mode: values.mode === "interactive" ? "interactive" : "sealed",
      seed: Number(values.seed ?? 0),
      labels: labels(values.labels),
      preflight: preflight(values.preflight),
      reason: values.reason as string | undefined,
      wait: values.wait === true,
      includeArchived: values.includeArchived === true,
      limit: Number(values.limit ?? 100),
      offset: Number(values.offset ?? 0),
    }
  }
  if (command === "daemon") {
    const action = argv[1]
    if (action !== "start" && action !== "stop" && action !== "status") {
      throw new CliUsageError("zyra daemon requires start, stop, or status.")
    }
    const values = bound(argv.slice(2), DAEMON_SPECS)
    return {
      kind: "daemon",
      action,
      ...common(values),
      force: values.force === true,
    }
  }
  if (command === "resume") {
    const values = bound(argv.slice(1), RESUME_SPECS)
    return {
      kind: "resume",
      ...common(values),
      identity: String(values.identity),
    }
  }
  if (command === "dev") {
    const values = bound(argv.slice(1), INTERACTIVE_SPECS)
    return {
      kind: "dev",
      ...common(values),
      goal: typeof values.goal === "string" && values.goal.trim()
        ? values.goal.trim()
        : undefined,
    }
  }
  if (command === "events") {
    const values = bound(argv.slice(1), RESUME_SPECS)
    return {
      kind: "events",
      ...common(values),
      identity: String(values.identity),
    }
  }
  if (command === "ls") {
    const values = bound(argv.slice(1), LIST_SPECS)
    return {
      kind: "ls",
      ...common(values),
      status: typeof values.status === "string" ? values.status.trim().toLowerCase() : undefined,
      limit: Number(values.limit ?? 100),
    }
  }
  if (command === "ui") {
    const values = bound(argv.slice(1), UI_SPECS)
    return {
      kind: "ui",
      ...common(values),
      webPort: Number(values.webPort ?? 5173),
      open: values.open === undefined ? true : values.open === true,
      taskId: values.taskId as string | undefined,
    }
  }
  if (command === "doctor") {
    const values = bound(argv.slice(1), DOCTOR_SPECS)
    return {
      kind: "doctor",
      ...common(values),
      // Diagnostics are read-only by default.  Starting a daemon requires an
      // explicit --autostart=true on this command.
      autoStart: values.autoStart === true,
      bundle: typeof values.bundle === "string" ? values.bundle.trim() : undefined,
    }
  }
  if (!command.startsWith("-")) {
    const values = bound(argv, INTERACTIVE_SPECS)
    const goal = typeof values.goal === "string" ? values.goal.trim() : ""
    if (!goal) throw new CliUsageError("Interactive goal must not be empty.")
    return { kind: "interactive", ...common(values), goal }
  }
  throw new CliUsageError(`Unknown Zyra CLI option: ${command}`)
}

export const CLI_USAGE = `Zyra CLI command surface

  zyra                              product TUI
  zyra "<goal>"                     product TUI with an initial goal
  zyra resume <task|session>        resume in the product TUI
  zyra dev [<goal>]                 developer event interface
  zyra events <task|session>        observe raw canonical events
  zyra run <goal | -f file | stdin> non-interactive JSONL execution
  zyra ls                           list canonical tasks and sessions
  zyra scenario <action> [...]      scenario lifecycle over the daemon API
  zyra ui [--task <id>]             ensure daemon, start Web, open product route
  zyra doctor [--bundle <file>]     read-only product diagnostics
  zyra daemon <start|stop|status>   local daemon supervision

Product TTY mode renders an inline conversation and never exposes raw runtime
events. Developer mode keeps the append-only canonical event transcript.
Non-TTY automation commands emit JSONL on stdout and diagnostics on stderr.`
