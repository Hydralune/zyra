import { spawn, type ChildProcess } from "node:child_process"
import { createServer } from "node:net"
import {
  access,
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises"
import { constants as fsConstants } from "node:fs"
import { platform } from "node:os"
import { join, resolve } from "node:path"
import {
  cliStateDirectory,
  resolveProjectRoot,
  resolvePythonCommand,
} from "./daemon.ts"
import { CliDaemonError, CliTaskError } from "./contracts.ts"

export const UI_STATE_SCHEMA = "zyra.cli-ui-state.v1" as const
export const UI_RESULT_SCHEMA = "zyra.cli-ui-result.v1" as const

export interface UiState {
  schema: typeof UI_STATE_SCHEMA
  pid: number
  generation: string
  web_origin: string
  api_origin: string
  started_at: string
  project_id: string
}

export interface UiLaunchReceipt {
  schema: typeof UI_RESULT_SCHEMA
  url: string
  web_origin: string
  api_origin: string
  pid: number
  generation: string
  started: boolean
  already_running: boolean
  browser_opened: boolean
}

export interface UiLaunchOptions {
  baseUrl: string
  webPort: number
  startupTimeoutMs: number
  open: boolean
  taskId?: string
}

export type UiProbe = "zyra" | "occupied" | "unavailable"

export interface UiLauncherEnvironment {
  reservePort(requested: number): Promise<number>
  probe(origin: string): Promise<UiProbe>
  readState(): Promise<UiState | undefined>
  writeState(state: UiState): Promise<void>
  removeState(): Promise<void>
  processAlive(pid: number): boolean
  buildWeb(): Promise<void>
  startWeb(port: number): Promise<{ pid: number }>
  stopWeb(pid: number): Promise<void>
  openBrowser(url: string): Promise<boolean>
  now(): number
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException | undefined)?.code === code
}

function normalizeOrigin(value: string, label: string): URL {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new CliTaskError(`${label} must be a valid HTTP origin.`, "ui_invalid_origin")
  }
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "")
  if (
    url.protocol !== "http:"
    || !["127.0.0.1", "localhost", "::1"].includes(host)
    || url.username
    || url.password
    || (url.pathname !== "/" && url.pathname !== "")
    || url.search
    || url.hash
  ) {
    throw new CliTaskError(
      `${label} must be a credential-free loopback HTTP origin.`,
      "ui_invalid_origin",
    )
  }
  return url
}

export function buildProductUrl(input: {
  webOrigin: string
  apiOrigin: string
  taskId?: string
}): string {
  const web = normalizeOrigin(input.webOrigin, "Web URL")
  const api = normalizeOrigin(input.apiOrigin, "API URL")
  web.pathname = input.taskId
    ? `/tasks/${encodeURIComponent(input.taskId)}`
    : "/tasks"
  web.searchParams.set("api", api.origin)
  return web.toString()
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
}

export async function launchUi(
  options: UiLaunchOptions,
  environment: UiLauncherEnvironment = defaultUiEnvironment(),
): Promise<UiLaunchReceipt> {
  const api = normalizeOrigin(options.baseUrl, "API URL")
  const prior = await environment.readState()
  if (prior && environment.processAlive(prior.pid)) {
    const priorOrigin = normalizeOrigin(prior.web_origin, "Recorded Web URL")
    const priorPort = Number(priorOrigin.port || 80)
    if (options.webPort !== 0 && options.webPort !== priorPort) {
      throw new CliDaemonError(
        "A managed Zyra Web process is already running on another port.",
        {
          pid: prior.pid,
          generation: prior.generation,
          web_origin: prior.web_origin,
          requested_port: options.webPort,
        },
      )
    }
    const priorProbe = await environment.probe(priorOrigin.origin)
    if (priorProbe !== "zyra") {
      throw new CliDaemonError(
        "The recorded Zyra Web process is alive but its product endpoint is unavailable.",
        {
          pid: prior.pid,
          generation: prior.generation,
          web_origin: prior.web_origin,
          observed: priorProbe,
        },
      )
    }
    const url = buildProductUrl({
      webOrigin: priorOrigin.origin,
      apiOrigin: api.origin,
      taskId: options.taskId,
    })
    const browserOpened = options.open
      ? await environment.openBrowser(url)
      : false
    return Object.freeze({
      schema: UI_RESULT_SCHEMA,
      url,
      web_origin: priorOrigin.origin,
      api_origin: api.origin,
      pid: prior.pid,
      generation: prior.generation,
      started: false,
      already_running: true,
      browser_opened: browserOpened,
    })
  }
  if (prior) await environment.removeState()
  const port = await environment.reservePort(options.webPort)
  const webOrigin = `http://127.0.0.1:${port}`
  const url = buildProductUrl({
    webOrigin,
    apiOrigin: api.origin,
    taskId: options.taskId,
  })
  const initialProbe = await environment.probe(webOrigin)
  if (initialProbe === "occupied") {
    throw new CliTaskError(
      `Port ${port} is occupied by a service that is not Zyra Web.`,
      "ui_port_conflict",
      { web_origin: webOrigin },
    )
  }
  if (initialProbe === "zyra") {
    const browserOpened = options.open
      ? await environment.openBrowser(url)
      : false
    return Object.freeze({
      schema: UI_RESULT_SCHEMA,
      url,
      web_origin: webOrigin,
      api_origin: api.origin,
      pid: 0,
      generation: "external",
      started: false,
      already_running: true,
      browser_opened: browserOpened,
    })
  }

  await environment.buildWeb()
  const child = await environment.startWeb(port)
  if (!Number.isSafeInteger(child.pid) || child.pid <= 0) {
    throw new CliDaemonError("The Zyra Web launcher did not expose a process id.")
  }
  const generation = crypto.randomUUID()
  const state: UiState = {
    schema: UI_STATE_SCHEMA,
    pid: child.pid,
    generation,
    web_origin: webOrigin,
    api_origin: api.origin,
    started_at: new Date(environment.now()).toISOString(),
    project_id: "zyra",
  }
  try {
    await environment.writeState(state)
    const deadline = environment.now() + options.startupTimeoutMs
    while (environment.now() < deadline) {
      const observed = await environment.probe(webOrigin)
      if (observed === "zyra") {
        const browserOpened = options.open
          ? await environment.openBrowser(url)
          : false
        return Object.freeze({
          schema: UI_RESULT_SCHEMA,
          url,
          web_origin: webOrigin,
          api_origin: api.origin,
          pid: child.pid,
          generation,
          started: true,
          already_running: false,
          browser_opened: browserOpened,
        })
      }
      if (observed === "occupied") {
        throw new CliTaskError(
          "The Web port changed owner while Zyra was starting.",
          "ui_port_generation_conflict",
          { web_origin: webOrigin, generation },
        )
      }
      if (!environment.processAlive(child.pid)) {
        throw new CliDaemonError("The Zyra Web process exited before its product endpoint became ready.", {
          pid: child.pid,
          generation,
        })
      }
      await sleep(100)
    }
    throw new CliDaemonError("Timed out waiting for the Zyra Web product endpoint.", {
      pid: child.pid,
      generation,
      web_origin: webOrigin,
    })
  } catch (error) {
    await environment.removeState()
    if (environment.processAlive(child.pid)) await environment.stopWeb(child.pid)
    throw error
  }
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

function uiStatePath(): string {
  return join(cliStateDirectory(), "ui.json")
}

async function readUiState(): Promise<UiState | undefined> {
  try {
    const decoded: unknown = JSON.parse(await readFile(uiStatePath(), "utf8"))
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) return undefined
    const value = decoded as Record<string, unknown>
    if (
      value.schema !== UI_STATE_SCHEMA
      || !Number.isSafeInteger(value.pid)
      || typeof value.generation !== "string"
      || typeof value.web_origin !== "string"
      || typeof value.api_origin !== "string"
      || typeof value.started_at !== "string"
      || typeof value.project_id !== "string"
    ) return undefined
    return value as unknown as UiState
  } catch (error) {
    if (isErrno(error, "ENOENT") || error instanceof SyntaxError) return undefined
    throw new CliDaemonError("Cannot read the local Zyra Web state.", {}, { cause: error })
  }
}

async function writeUiState(value: UiState): Promise<void> {
  const directory = cliStateDirectory()
  await mkdir(directory, { recursive: true })
  const temporary = join(directory, `ui-${process.pid}-${crypto.randomUUID()}.tmp`)
  await writeFile(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 })
  await rename(temporary, uiStatePath())
}

async function removeUiState(): Promise<void> {
  await rm(uiStatePath(), { force: true })
}

async function reserveLoopbackPort(requested: number): Promise<number> {
  if (!Number.isSafeInteger(requested) || requested < 0 || requested > 65_535) {
    throw new CliTaskError("Web port must be between 0 and 65535.", "ui_invalid_port")
  }
  if (requested > 0) return requested
  return new Promise((resolvePromise, reject) => {
    const server = createServer()
    server.unref()
    server.once("error", reject)
    server.listen(0, "127.0.0.1", () => {
      const address = server.address()
      const selected = typeof address === "object" && address ? address.port : 0
      server.close((error) => {
        if (error) reject(error)
        else if (!selected) reject(new Error("No loopback port was reserved."))
        else resolvePromise(selected)
      })
    })
  })
}

async function probeWeb(origin: string): Promise<UiProbe> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 2_000)
  timer.unref()
  try {
    const response = await fetch(`${origin}/`, {
      headers: { Accept: "text/html", "Cache-Control": "no-store" },
      signal: controller.signal,
    })
    const body = (await response.text()).slice(0, 128 * 1_024)
    return response.ok && body.includes('name="zyra-product-entry"')
      ? "zyra"
      : "occupied"
  } catch {
    return "unavailable"
  } finally {
    clearTimeout(timer)
  }
}

async function commandOutput(child: ChildProcess, label: string, timeoutMs: number): Promise<void> {
  let stdout = ""
  let stderr = ""
  child.stdout?.on("data", (chunk) => {
    if (stdout.length < 256 * 1_024) stdout += String(chunk)
  })
  child.stderr?.on("data", (chunk) => {
    if (stderr.length < 256 * 1_024) stderr += String(chunk)
  })
  await new Promise<void>((resolvePromise, reject) => {
    const timer = setTimeout(() => {
      child.kill("SIGTERM")
      reject(new CliDaemonError(`${label} timed out.`, { stdout, stderr }))
    }, timeoutMs)
    timer.unref()
    child.once("error", (error) => {
      clearTimeout(timer)
      reject(new CliDaemonError(`${label} failed to start.`, {}, { cause: error }))
    })
    child.once("exit", (code, signal) => {
      clearTimeout(timer)
      if (code === 0) resolvePromise()
      else reject(new CliDaemonError(`${label} failed.`, {
        code,
        signal,
        stdout: stdout.slice(-16_384),
        stderr: stderr.slice(-16_384),
      }))
    })
  })
}

async function bunCommand(projectRoot: string): Promise<string> {
  if (process.versions.bun) return process.execPath
  const local = join(
    projectRoot,
    "node_modules",
    "bun",
    "bin",
    platform() === "win32" ? "bun.exe" : "bun",
  )
  try {
    await access(local, fsConstants.X_OK)
    return local
  } catch {
    return "bun"
  }
}

function browserCommand(url: string): { command: string; args: string[] } {
  if (platform() === "win32") {
    return { command: "rundll32.exe", args: ["url.dll,FileProtocolHandler", url] }
  }
  if (platform() === "darwin") return { command: "open", args: [url] }
  return { command: "xdg-open", args: [url] }
}

export function defaultUiEnvironment(): UiLauncherEnvironment {
  const projectRoot = resolveProjectRoot()
  return {
    reservePort: reserveLoopbackPort,
    probe: probeWeb,
    readState: readUiState,
    writeState: writeUiState,
    removeState: removeUiState,
    processAlive,
    async buildWeb() {
      const index = resolve(projectRoot, "apps", "web", "dist", "index.html")
      try {
        await access(index, fsConstants.R_OK)
        return
      } catch {
        // Build the existing Web product; this launcher does not create another UI.
      }
      const bun = await bunCommand(projectRoot)
      const child = spawn(bun, ["run", "build:web"], {
        cwd: projectRoot,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      })
      await commandOutput(child, "Zyra Web build", 5 * 60_000)
    },
    async startWeb(port) {
      const python = await resolvePythonCommand(projectRoot)
      const child = spawn(python, [join(projectRoot, "scripts", "dev_web.py")], {
        cwd: projectRoot,
        detached: true,
        windowsHide: true,
        stdio: "ignore",
        env: {
          ...process.env,
          ZYRA_WEB_HOST: "127.0.0.1",
          ZYRA_WEB_PORT: String(port),
        },
      })
      if (!child.pid) throw new CliDaemonError("The Zyra Web process did not expose a pid.")
      child.unref()
      return { pid: child.pid }
    },
    async stopWeb(pid) {
      if (!processAlive(pid)) return
      try {
        process.kill(pid, "SIGTERM")
      } catch (error) {
        if (!isErrno(error, "ESRCH")) throw error
      }
    },
    async openBrowser(url) {
      const selected = browserCommand(url)
      try {
        const child = spawn(selected.command, selected.args, {
          detached: true,
          windowsHide: true,
          stdio: "ignore",
        })
        return await new Promise<boolean>((resolvePromise) => {
          child.once("error", () => resolvePromise(false))
          child.once("spawn", () => {
            child.unref()
            resolvePromise(Boolean(child.pid))
          })
        })
      } catch {
        return false
      }
    },
    now: () => Date.now(),
  }
}
