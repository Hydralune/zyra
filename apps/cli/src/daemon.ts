import { spawn } from "node:child_process"
import {
  access,
  mkdir,
  open,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises"
import { constants as fsConstants, existsSync } from "node:fs"
import { homedir, platform, tmpdir } from "node:os"
import { basename, dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import {
  ACTIVE_TASK_STATUSES,
  OPERATION_NAMES,
  ZyraTypedApiClient,
  type ApiHealth,
  type TaskListProjection,
} from "@zyra/typed-api-client"
import { CliDaemonError, CliTaskError } from "./contracts.ts"

export const DAEMON_STATE_SCHEMA = "zyra.cli-daemon-state.v1" as const
const DAEMON_RUNTIME_IDENTITY_SCHEMA = "zyra.cli-daemon-runtime-identity.v1" as const

export interface DaemonState {
  schema: typeof DAEMON_STATE_SCHEMA
  pid: number
  generation: string
  base_url: string
  started_at: string
  project_id: string
}

interface DaemonRuntimeIdentity {
  schema: typeof DAEMON_RUNTIME_IDENTITY_SCHEMA
  generation: string
  pid: number
}

export interface DaemonHealthIdentity {
  healthy: boolean
  generation?: string
  pid?: number
}

export interface DaemonStatus {
  reachable: boolean
  managed: boolean
  pid?: number
  generation?: string
  baseUrl: string
  staleState: boolean
}

export interface DaemonStopAudit {
  schema: "zyra.cli-daemon-stop-audit.v1"
  audit_id: string
  phase: "rejected" | "committed"
  forced: boolean
  signal: "SIGTERM" | "SIGKILL" | "none"
  pid: number
  generation: string
  base_url: string
  active_task_ids: readonly string[]
  recorded_at: string
  reason: string
}

export interface DaemonStopStatus extends DaemonStatus {
  audit: DaemonStopAudit
}

export interface DaemonOptions {
  baseUrl: string
  autoStart: boolean
  startupTimeoutMs: number
  token?: string
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException | undefined)?.code === code
}

export function cliStateDirectory(): string {
  const explicit = process.env.ZYRA_CLI_STATE_DIR?.trim()
  if (explicit) return resolve(explicit)
  if (platform() === "win32") {
    const local = process.env.LOCALAPPDATA?.trim()
    return resolve(local || join(homedir(), "AppData", "Local"), "Zyra", "cli")
  }
  const xdg = process.env.XDG_STATE_HOME?.trim()
  return resolve(xdg || join(homedir(), ".local", "state"), "zyra", "cli")
}

function statePath(): string {
  return join(cliStateDirectory(), "daemon.json")
}

function lockPath(): string {
  return join(cliStateDirectory(), "daemon.lock")
}

function auditPath(): string {
  return join(cliStateDirectory(), "daemon-stop-audit.jsonl")
}

function startupLogPath(): string {
  return join(cliStateDirectory(), "daemon-startup.log")
}

function runtimeIdentityPath(generation: string): string {
  return join(cliStateDirectory(), `daemon-runtime-${generation}.json`)
}

async function appendDaemonAudit(value: DaemonStopAudit): Promise<void> {
  await mkdir(cliStateDirectory(), { recursive: true })
  const handle = await open(auditPath(), "a", 0o600)
  try {
    await handle.appendFile(`${JSON.stringify(value)}\n`, "utf8")
  } finally {
    await handle.close()
  }
}

function normalizeDaemonUrl(value: string): URL {
  const url = new URL(value)
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new CliDaemonError("The Zyra daemon URL must use HTTP or HTTPS.")
  }
  if (url.username || url.password || (url.pathname !== "/" && url.pathname !== "")) {
    throw new CliDaemonError("The Zyra daemon URL must be an origin without credentials or a path.")
  }
  return url
}

function isLoopback(url: URL): boolean {
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "")
  return host === "127.0.0.1" || host === "localhost" || host === "::1"
}

function processAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return isErrno(error, "EPERM")
  }
}

export function daemonHealthOwnsState(
  state: DaemonState | undefined,
  health: DaemonHealthIdentity,
): boolean {
  return Boolean(
    state
    && health.healthy
    && health.pid === state.pid
    && health.generation === state.generation,
  )
}

function markerExists(candidate: string): boolean {
  return existsSync(join(candidate, "package.json"))
    && existsSync(join(candidate, "scripts", "dev_api.py"))
    && existsSync(join(candidate, "apps", "api"))
}

export function resolveProjectRoot(): string {
  const explicit = process.env.ZYRA_PROJECT_ROOT?.trim()
  if (explicit) {
    const selected = resolve(explicit)
    if (!markerExists(selected)) throw new CliDaemonError("ZYRA_PROJECT_ROOT is not a Zyra source tree.")
    return selected
  }
  const seeds = [process.cwd(), dirname(fileURLToPath(import.meta.url))]
  for (const seed of seeds) {
    let candidate = resolve(seed)
    for (;;) {
      if (markerExists(candidate)) return candidate
      const parent = dirname(candidate)
      if (parent === candidate) break
      candidate = parent
    }
  }
  throw new CliDaemonError(
    "Cannot locate the Zyra source tree required by the repository-local daemon launcher.",
  )
}

async function readState(): Promise<DaemonState | undefined> {
  try {
    const decoded: unknown = JSON.parse(await readFile(statePath(), "utf8"))
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) return undefined
    const value = decoded as Record<string, unknown>
    if (
      value.schema !== DAEMON_STATE_SCHEMA
      || !Number.isSafeInteger(value.pid)
      || typeof value.generation !== "string"
      || typeof value.base_url !== "string"
      || typeof value.started_at !== "string"
      || typeof value.project_id !== "string"
    ) return undefined
    return value as unknown as DaemonState
  } catch (error) {
    if (isErrno(error, "ENOENT") || error instanceof SyntaxError) return undefined
    throw new CliDaemonError("Cannot read the local Zyra daemon state.", {}, { cause: error })
  }
}

async function writeState(value: DaemonState): Promise<void> {
  const directory = cliStateDirectory()
  await mkdir(directory, { recursive: true })
  const temporary = join(directory, `daemon-${process.pid}-${crypto.randomUUID()}.tmp`)
  await writeFile(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 })
  await rename(temporary, statePath())
}

async function removeState(): Promise<void> {
  try {
    await rm(statePath(), { force: true })
  } catch (error) {
    throw new CliDaemonError("Cannot remove stale local daemon state.", {}, { cause: error })
  }
}

async function probeHealth(baseUrl: string, token?: string, timeoutMs = 5_000): Promise<boolean> {
  return (await probeHealthIdentity(baseUrl, token, timeoutMs)).healthy
}

async function probeHealthIdentity(
  baseUrl: string,
  token?: string,
  timeoutMs = 5_000,
): Promise<DaemonHealthIdentity> {
  const client = new ZyraTypedApiClient({
    baseUrl,
    token,
    timeoutMs,
    retry: { attempts: 1 },
    clientName: "zyra-cli-health",
    clientVersion: "0.1.0",
  })
  try {
    const response = await client.endpoint<ApiHealth>(OPERATION_NAMES.health, {
      timeoutMs,
      coordinationKey: "daemon.health",
      deduplicate: true,
    })
    const healthy = response.data.service === "zyra-api" && response.data.apiVersion === "1.0"
    const rawPid = response.data.raw.process_id
    const rawGeneration = response.data.raw.cli_daemon_generation
    return {
      healthy,
      pid: Number.isSafeInteger(rawPid) && Number(rawPid) > 0 ? Number(rawPid) : undefined,
      generation: typeof rawGeneration === "string" && rawGeneration ? rawGeneration : undefined,
    }
  } catch {
    return { healthy: false }
  } finally {
    client.close("health probe complete")
  }
}

async function readRuntimeIdentity(
  path: string,
  generation: string,
): Promise<DaemonRuntimeIdentity | undefined> {
  try {
    const decoded: unknown = JSON.parse(await readFile(path, "utf8"))
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) return undefined
    const value = decoded as Record<string, unknown>
    if (
      value.schema !== DAEMON_RUNTIME_IDENTITY_SCHEMA
      || value.generation !== generation
      || !Number.isSafeInteger(value.pid)
      || Number(value.pid) <= 0
    ) return undefined
    return value as unknown as DaemonRuntimeIdentity
  } catch (error) {
    if (isErrno(error, "ENOENT") || error instanceof SyntaxError) return undefined
    throw new CliDaemonError("Cannot read the Zyra daemon runtime identity.", {}, { cause: error })
  }
}

export async function resolvePythonCommand(projectRoot: string): Promise<string> {
  const explicit = process.env.ZYRA_PYTHON?.trim()
  const candidates = explicit
    ? [explicit]
    : platform() === "win32"
      ? [join(projectRoot, ".venv", "Scripts", "python.exe"), "python"]
      : [join(projectRoot, ".venv", "bin", "python"), "python3", "python"]
  for (const candidate of candidates) {
    if (!candidate.includes("/") && !candidate.includes("\\")) return candidate
    try {
      await access(candidate, fsConstants.X_OK)
      return candidate
    } catch {
      // Try the next bounded launcher candidate.
    }
  }
  throw new CliDaemonError("No Python runtime is available for the repository-local Zyra daemon.")
}

async function acquireLaunchLock(deadline: number): Promise<Awaited<ReturnType<typeof open>>> {
  await mkdir(cliStateDirectory(), { recursive: true })
  for (;;) {
    try {
      return await open(lockPath(), "wx", 0o600)
    } catch (error) {
      if (!isErrno(error, "EEXIST")) throw error
      try {
        const observed = await stat(lockPath())
        if (Date.now() - observed.mtimeMs > 5 * 60_000) await rm(lockPath(), { force: true })
      } catch (stateError) {
        if (!isErrno(stateError, "ENOENT")) throw stateError
      }
      if (Date.now() >= deadline) {
        throw new CliDaemonError("Timed out waiting for another Zyra daemon launcher.")
      }
      await sleep(100)
    }
  }
}

async function releaseLaunchLock(handle: Awaited<ReturnType<typeof open>>): Promise<void> {
  await handle.close()
  await rm(lockPath(), { force: true })
}

async function startManagedDaemon(options: DaemonOptions): Promise<DaemonState> {
  const url = normalizeDaemonUrl(options.baseUrl)
  if (!isLoopback(url)) {
    throw new CliDaemonError("Automatic daemon startup is restricted to loopback addresses.")
  }
  if (url.protocol !== "http:") {
    throw new CliDaemonError("The repository-local daemon launcher supports loopback HTTP only.")
  }
  const deadline = Date.now() + options.startupTimeoutMs
  const lock = await acquireLaunchLock(deadline)
  try {
    if (await probeHealth(options.baseUrl, options.token)) {
      const existing = await readState()
      if (existing) return existing
      return {
        schema: DAEMON_STATE_SCHEMA,
        pid: 0,
        generation: "external",
        base_url: options.baseUrl,
        started_at: new Date().toISOString(),
        project_id: "external",
      }
    }
    const prior = await readState()
    if (prior && processAlive(prior.pid)) {
      while (Date.now() < deadline) {
        if (await probeHealth(options.baseUrl, options.token)) return prior
        await sleep(200)
      }
      throw new CliDaemonError("The recorded Zyra daemon process did not become healthy.", {
        pid: prior.pid,
        generation: prior.generation,
      })
    }
    if (prior) await removeState()
    const projectRoot = resolveProjectRoot()
    const python = await resolvePythonCommand(projectRoot)
    const port = Number(url.port || "80")
    const generation = crypto.randomUUID()
    const runtimeIdentity = runtimeIdentityPath(generation)
    await rm(runtimeIdentity, { force: true })
    await mkdir(cliStateDirectory(), { recursive: true })
    const launchLog = await open(startupLogPath(), "w", 0o600)
    let child: ReturnType<typeof spawn>
    try {
      child = spawn(python, [join(projectRoot, "scripts", "dev_api.py")], {
        cwd: projectRoot,
        detached: true,
        windowsHide: true,
        stdio: ["ignore", launchLog.fd, launchLog.fd],
        env: {
          ...process.env,
          ZYRA_API_HOST: url.hostname,
          ZYRA_API_PORT: String(port),
          ZYRA_CLI_DAEMON_GENERATION: generation,
          ZYRA_CLI_DAEMON_RUNTIME_IDENTITY: runtimeIdentity,
        },
      })
    } finally {
      await launchLog.close()
    }
    if (!child.pid) throw new CliDaemonError("The Zyra daemon process did not expose a pid.")
    child.unref()
    let state: DaemonState = {
      schema: DAEMON_STATE_SCHEMA,
      pid: child.pid,
      generation,
      base_url: options.baseUrl,
      started_at: new Date().toISOString(),
      project_id: basename(projectRoot),
    }
    try {
      await writeState(state)
      const launcherPid = child.pid
      const handoffDeadline = Math.min(deadline, Date.now() + 10_000)
      while (Date.now() < deadline) {
        const handedOff = await readRuntimeIdentity(runtimeIdentity, generation)
        if (handedOff && handedOff.pid !== state.pid) {
          state = { ...state, pid: handedOff.pid }
          await writeState(state)
        }
        const health = await probeHealthIdentity(options.baseUrl, options.token)
        if (health.healthy && health.generation === generation) {
          if (health.pid && health.pid !== state.pid) {
            state = { ...state, pid: health.pid }
            await writeState(state)
          }
          await rm(runtimeIdentity, { force: true })
          return state
        }
        if (!processAlive(state.pid)) {
          if (state.pid === launcherPid && Date.now() < handoffDeadline) {
            await sleep(100)
            continue
          }
          await removeState()
          throw new CliDaemonError("The Zyra daemon exited before becoming healthy.", {
            pid: state.pid,
            generation: state.generation,
          })
        }
        await sleep(250)
      }
      throw new CliDaemonError("Timed out waiting for the Zyra daemon health contract.", {
        pid: state.pid,
        generation: state.generation,
      })
    } catch (error) {
      if (processAlive(state.pid)) {
        try {
          process.kill(state.pid, "SIGKILL")
        } catch (killError) {
          if (!isErrno(killError, "ESRCH")) {
            throw new CliDaemonError("Cannot clean up the failed Zyra daemon launch.", {
              pid: state.pid,
              generation: state.generation,
            }, { cause: killError })
          }
        }
      }
      await rm(runtimeIdentity, { force: true }).catch(() => undefined)
      await removeState().catch(() => undefined)
      throw error
    }
  } finally {
    await releaseLaunchLock(lock)
  }
}

export async function daemonStatus(options: Pick<DaemonOptions, "baseUrl" | "token">): Promise<DaemonStatus> {
  const state = await readState()
  const health = await probeHealthIdentity(options.baseUrl, options.token)
  const selected = state?.base_url === options.baseUrl ? state : undefined
  const managed = daemonHealthOwnsState(selected, health)
  return {
    reachable: health.healthy,
    managed,
    pid: selected?.pid,
    generation: selected?.generation,
    baseUrl: options.baseUrl,
    staleState: Boolean(selected && !managed),
  }
}

export async function ensureDaemon(options: DaemonOptions): Promise<DaemonStatus> {
  const status = await daemonStatus(options)
  if (status.reachable) return status
  if (!options.autoStart) {
    throw new CliDaemonError("The Zyra daemon is unavailable and automatic startup is disabled.")
  }
  await startManagedDaemon(options)
  const started = await daemonStatus(options)
  if (!started.reachable) throw new CliDaemonError("The Zyra daemon did not pass its health contract.")
  return started
}

async function activeTaskIds(baseUrl: string, token?: string): Promise<string[]> {
  const client = new ZyraTypedApiClient({
    baseUrl,
    token,
    timeoutMs: 10_000,
    retry: { attempts: 1 },
    clientName: "zyra-cli-daemon",
    clientVersion: "0.1.0",
  })
  try {
    const response = await client.endpoint<TaskListProjection>(OPERATION_NAMES.taskList, {
      query: { limit: 1_000 },
      coordinationKey: "daemon.active-tasks",
    })
    return response.data.tasks
      .filter((task) => ACTIVE_TASK_STATUSES.has(task.status))
      .map((task) => task.taskId)
      .sort()
  } finally {
    client.close("active task inspection complete")
  }
}

export async function stopManagedDaemon(
  options: Pick<DaemonOptions, "baseUrl" | "token"> & { force: boolean; timeoutMs?: number },
): Promise<DaemonStopStatus> {
  const state = await readState()
  if (!state || state.base_url !== options.baseUrl || !processAlive(state.pid)) {
    throw new CliTaskError(
      "The reachable daemon is not owned by this Zyra CLI state record and will not be stopped.",
      "daemon_not_managed",
    )
  }
  const health = await probeHealthIdentity(options.baseUrl, options.token)
  if (!daemonHealthOwnsState(state, health)) {
    throw new CliTaskError(
      "The daemon health identity does not match this Zyra CLI state record; the recorded PID will not be signalled.",
      "daemon_not_managed",
      {
        recorded_pid: state.pid,
        recorded_generation: state.generation,
        observed_pid: health.pid,
        observed_generation: health.generation,
      },
    )
  }
  const active = await activeTaskIds(options.baseUrl, options.token)
  const auditId = `daemon_stop_${crypto.randomUUID().replaceAll("-", "")}`
  if (!options.force && active.length) {
    await appendDaemonAudit({
      schema: "zyra.cli-daemon-stop-audit.v1",
      audit_id: auditId,
      phase: "rejected",
      forced: false,
      signal: "none",
      pid: state.pid,
      generation: state.generation,
      base_url: options.baseUrl,
      active_task_ids: Object.freeze([...active]),
      recorded_at: new Date().toISOString(),
      reason: "active_tasks_protected",
    })
    throw new CliTaskError(
      "The Zyra daemon has active tasks; use --force=true only when explicit cancellation is intended.",
      "daemon_active_tasks",
      { active_task_ids: active, audit_id: auditId, audit_persisted: true },
    )
  }
  const signal = options.force ? "SIGKILL" : "SIGTERM"
  try {
    process.kill(state.pid, signal)
  } catch (error) {
    if (!isErrno(error, "ESRCH")) throw new CliDaemonError("Cannot stop the managed Zyra daemon.", {}, { cause: error })
  }
  const deadline = Date.now() + Math.max(1_000, options.timeoutMs ?? 15_000)
  while (Date.now() < deadline && processAlive(state.pid)) await sleep(100)
  if (processAlive(state.pid)) {
    throw new CliDaemonError("The managed Zyra daemon did not stop within the bounded timeout.", {
      pid: state.pid,
      generation: state.generation,
    })
  }
  await removeState()
  const audit: DaemonStopAudit = Object.freeze({
    schema: "zyra.cli-daemon-stop-audit.v1",
    audit_id: auditId,
    phase: "committed",
    forced: options.force,
    signal,
    pid: state.pid,
    generation: state.generation,
    base_url: options.baseUrl,
    active_task_ids: Object.freeze([...active]),
    recorded_at: new Date().toISOString(),
    reason: options.force && active.length ? "force_stop_with_active_tasks" : "managed_daemon_stop",
  })
  await appendDaemonAudit(audit)
  return { ...(await daemonStatus(options)), audit }
}
