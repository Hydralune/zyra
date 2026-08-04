import { spawn, type ChildProcess } from "node:child_process"
import { randomUUID } from "node:crypto"
import { constants } from "node:fs"
import { access, lstat, mkdir, opendir, readFile, realpath, rm, stat, writeFile } from "node:fs/promises"
import { basename, dirname, extname, isAbsolute, relative, resolve, sep } from "node:path"
import type { TerminalAction, TerminalActionResult, TerminalEnvelope } from "./contracts.ts"
import { TerminalProtocolError } from "./contracts.ts"

const MAX_INLINE_BYTES = 2 * 1024 * 1024
const MAX_FILE_BYTES = 16 * 1024 * 1024
const MAX_SEARCH_FILES = 2_000
const MAX_SEARCH_BYTES = 32 * 1024 * 1024
const MAX_SHELL_OUTPUT_BYTES = 3 * 1024 * 1024

export interface ActionEvents {
  redact(value: string): string
  stdout(value: string): void
  stderr(value: string): void
  heartbeat(): void
  artifact(value: Record<string, unknown>): void
  process(child: ChildProcess | undefined): void
}

export interface ActionContext {
  startupRoot: string
  envelope: TerminalEnvelope
  action: TerminalAction
  signal: AbortSignal
  events: ActionEvents
}

function text(value: unknown, label: string, allowEmpty = false): string {
  if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
    throw new TerminalProtocolError(`${label} must be a ${allowEmpty ? "string" : "non-empty string"}.`, "execution_failed", 422)
  }
  return value
}

function boundedInteger(value: unknown, fallback: number, minimum: number, maximum: number, label: string): number {
  const selected = value === undefined ? fallback : Number(value)
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new TerminalProtocolError(`${label} exceeds the terminal action budget.`, "resource_exhausted", 422)
  }
  return selected
}

function within(root: string, target: string): boolean {
  const path = relative(root, target)
  return path === "" || (!path.startsWith(`..${sep}`) && path !== ".." && !isAbsolute(path))
}

async function existingAncestor(path: string): Promise<string> {
  let current = path
  while (true) {
    try {
      return await realpath(current)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
      const parent = dirname(current)
      if (parent === current) throw error
      current = parent
    }
  }
}

async function approvedRoot(startupRoot: string, candidate: string, label: string): Promise<string> {
  if (!isAbsolute(candidate)) {
    throw new TerminalProtocolError(`${label} must be absolute.`, "workspace_attestation_failed", 409)
  }
  const resolvedStartup = await realpath(startupRoot)
  const resolvedCandidate = await realpath(candidate).catch(async (error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error
    const ancestor = await existingAncestor(candidate)
    if (!within(resolvedStartup, ancestor)) {
      throw new TerminalProtocolError(`${label} escapes the CLI startup root.`, "workspace_attestation_failed", 409)
    }
    await mkdir(candidate, { recursive: true })
    return realpath(candidate)
  })
  if (!within(resolvedStartup, resolvedCandidate)) {
    throw new TerminalProtocolError(`${label} escapes the CLI startup root.`, "workspace_attestation_failed", 409)
  }
  const info = await stat(resolvedCandidate)
  if (!info.isDirectory()) {
    throw new TerminalProtocolError(`${label} is not a directory.`, "workspace_attestation_failed", 409)
  }
  try {
    await access(resolvedCandidate, constants.W_OK)
  } catch (error) {
    throw new TerminalProtocolError(`${label} is not writable.`, "workspace_attestation_failed", 409, { cause: error })
  }
  return resolvedCandidate
}

async function guardedPath(root: string, rawPath: unknown, options: { createParent?: boolean } = {}): Promise<{ absolute: string; relative: string }> {
  const selected = text(rawPath, "path").replaceAll("\\", "/")
  if (selected.includes("\0") || isAbsolute(selected) || selected.split("/").some((part) => part === "..")) {
    throw new TerminalProtocolError("Action path must stay relative to its attested root.", "workspace_attestation_failed", 409)
  }
  const absolute = resolve(root, selected)
  if (!within(root, absolute)) {
    throw new TerminalProtocolError("Action path escapes its attested root.", "workspace_attestation_failed", 409)
  }
  const ancestor = await existingAncestor(absolute)
  if (!within(root, ancestor)) {
    throw new TerminalProtocolError("Action path traverses a symlink outside its attested root.", "workspace_attestation_failed", 409)
  }
  try {
    const existing = await realpath(absolute)
    if (!within(root, existing)) {
      throw new TerminalProtocolError("Action target resolves outside its attested root.", "workspace_attestation_failed", 409)
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  if (options.createParent) {
    await mkdir(dirname(absolute), { recursive: true })
    const parent = await realpath(dirname(absolute))
    if (!within(root, parent)) {
      throw new TerminalProtocolError("Action parent resolves outside its attested root.", "workspace_attestation_failed", 409)
    }
  }
  return { absolute, relative: relative(root, absolute).replaceAll("\\", "/") }
}

function result(action: TerminalAction, input: {
  ok: boolean
  summary: string
  output?: Record<string, unknown>
  error?: string
  metadata?: Record<string, string>
}): TerminalActionResult {
  return {
    schema: "zyra.terminal-action-result/v1",
    tool_call_id: action.tool_call_id,
    ok: input.ok,
    summary: input.summary,
    output: input.output ?? {},
    ...(input.error ? { error: input.error } : {}),
    metadata: {
      execution_mode: "terminal_http",
      physical_action: "true",
      ...(input.metadata ?? {}),
    },
  }
}

async function fileRead(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const target = await guardedPath(workspace, context.action.arguments.path)
  const encoding = text(context.action.arguments.encoding ?? "utf-8", "encoding") as BufferEncoding
  const info = await stat(target.absolute)
  if (!info.isFile() || info.size > MAX_FILE_BYTES) {
    throw new TerminalProtocolError("file_read target is not a bounded regular file.", "resource_exhausted", 422)
  }
  const content = await readFile(target.absolute, encoding)
  if (Buffer.byteLength(content, "utf8") > MAX_INLINE_BYTES) {
    throw new TerminalProtocolError("file_read result exceeds the inline result budget.", "resource_exhausted", 422)
  }
  return result(context.action, {
    ok: true,
    summary: `Read ${target.relative} on terminal node`,
    output: { relative_path: target.relative, content, chars: content.length, truncated: false, physical_location_redacted: true },
  })
}

async function fileWrite(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const target = await guardedPath(workspace, context.action.arguments.path, { createParent: true })
  const content = text(context.action.arguments.content ?? "", "content", true)
  if (Buffer.byteLength(content, "utf8") > MAX_FILE_BYTES) {
    throw new TerminalProtocolError("file_write content exceeds the action budget.", "resource_exhausted", 422)
  }
  const encoding = text(context.action.arguments.encoding ?? "utf-8", "encoding") as BufferEncoding
  await writeFile(target.absolute, content, { encoding, flag: "w" })
  return result(context.action, {
    ok: true,
    summary: `Wrote ${target.relative} on terminal node`,
    output: { relative_path: target.relative, chars: content.length, physical_location_redacted: true },
  })
}

async function fileEdit(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const target = await guardedPath(workspace, context.action.arguments.path)
  const oldText = text(context.action.arguments.old, "old")
  const newText = text(context.action.arguments.new ?? "", "new", true)
  const replaceAll = context.action.arguments.replace_all === true
  const encoding = text(context.action.arguments.encoding ?? "utf-8", "encoding") as BufferEncoding
  const content = await readFile(target.absolute, encoding)
  if (Buffer.byteLength(content, "utf8") > MAX_FILE_BYTES) {
    throw new TerminalProtocolError("file_edit target exceeds the action budget.", "resource_exhausted", 422)
  }
  const occurrences = content.split(oldText).length - 1
  if (occurrences === 0) return result(context.action, { ok: false, summary: `No exact match in ${target.relative}`, error: "old_text_not_found", output: { relative_path: target.relative } })
  if (occurrences > 1 && !replaceAll) return result(context.action, { ok: false, summary: `Multiple matches in ${target.relative}`, error: "ambiguous_edit", output: { relative_path: target.relative, matches: occurrences } })
  const edited = replaceAll ? content.replaceAll(oldText, newText) : content.replace(oldText, newText)
  await writeFile(target.absolute, edited, { encoding, flag: "w" })
  return result(context.action, {
    ok: true,
    summary: `Edited ${target.relative} on terminal node`,
    output: { relative_path: target.relative, replacements: replaceAll ? occurrences : 1, physical_location_redacted: true },
  })
}

async function fileDelete(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const target = await guardedPath(workspace, context.action.arguments.path)
  const info = await lstat(target.absolute)
  if (!info.isFile() && !info.isSymbolicLink()) {
    throw new TerminalProtocolError("file_delete only accepts a file target.", "execution_failed", 422)
  }
  await rm(target.absolute, { force: false, recursive: false })
  return result(context.action, {
    ok: true,
    summary: `Deleted ${target.relative} on terminal node`,
    output: { relative_path: target.relative, deleted: true, physical_location_redacted: true },
  })
}

async function webSearch(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const query = text(context.action.arguments.query, "query")
  const maximum = boundedInteger(context.action.arguments.max_results, 20, 1, 100, "web_search max_results")
  const matches: Array<{ relative_path: string; line: number; preview: string }> = []
  const scanContent = (source: string, content: string): void => {
    for (const [index, line] of content.split(/\r?\n/u).entries()) {
      if (line.toLocaleLowerCase().includes(query.toLocaleLowerCase())) {
        matches.push({ relative_path: source, line: index + 1, preview: line.slice(0, 500) })
        if (matches.length >= maximum) break
      }
    }
  }
  const sourceUrl = typeof context.action.arguments.url === "string" ? context.action.arguments.url.trim() : ""
  if (sourceUrl) {
    let parsed: URL
    try { parsed = new URL(sourceUrl) } catch {
      return result(context.action, { ok: false, summary: "web_search URL is invalid", error: "invalid_url" })
    }
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
      return result(context.action, { ok: false, summary: `Unsupported web_search URL: ${parsed.protocol}`, error: "unsupported_url_scheme" })
    }
    if (context.action.arguments.allow_network !== true) {
      return result(context.action, { ok: false, summary: "Network search requires allow_network=true", error: "network_not_allowed" })
    }
    const allowedDomains = Array.isArray(context.action.arguments.allowed_domains)
      ? new Set(context.action.arguments.allowed_domains.map((item) => String(item).toLocaleLowerCase()))
      : new Set<string>()
    if (allowedDomains.size && !allowedDomains.has(parsed.hostname.toLocaleLowerCase())) {
      return result(context.action, { ok: false, summary: `Network search domain is not allowed: ${parsed.hostname}`, error: "domain_not_allowed" })
    }
    const timeoutSeconds = boundedInteger(context.action.arguments.timeout_seconds, 10, 1, 30, "web_search timeout")
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error("web_search timed out")), timeoutSeconds * 1_000)
    timer.unref()
    const abort = () => controller.abort(context.signal.reason)
    context.signal.addEventListener("abort", abort, { once: true })
    try {
      const response = await fetch(parsed, {
        headers: { "user-agent": "ZyraTerminalSearch/0.1", accept: "text/*, application/json" },
        redirect: "error",
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        throw new TerminalProtocolError(`web_search source returned HTTP ${response.status}.`, "execution_failed", 422)
      }
      const contentLength = Number(response.headers.get("content-length") ?? 0)
      if (Number.isFinite(contentLength) && contentLength > MAX_SEARCH_BYTES) {
        throw new TerminalProtocolError("web_search response byte budget exceeded.", "resource_exhausted", 422)
      }
      const reader = response.body.getReader()
      const chunks: Uint8Array[] = []
      let received = 0
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        received += chunk.value.byteLength
        if (received > MAX_SEARCH_BYTES) {
          await reader.cancel("web_search response byte budget exceeded")
          throw new TerminalProtocolError("web_search response byte budget exceeded.", "resource_exhausted", 422)
        }
        chunks.push(chunk.value)
      }
      const bytes = new Uint8Array(received)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      scanContent(parsed.origin + parsed.pathname, new TextDecoder("utf-8", { fatal: false }).decode(bytes))
      return result(context.action, {
        ok: true,
        summary: `Found ${matches.length} network matches for ${query}`,
        output: { query, matches, result_count: matches.length, network_used: true },
        metadata: { search_surface: "network_url" },
      })
    } finally {
      clearTimeout(timer)
      context.signal.removeEventListener("abort", abort)
    }
  }
  let files = 0
  let bytes = 0
  const scanFile = async (target: string): Promise<void> => {
    files += 1
    if (files > MAX_SEARCH_FILES) throw new TerminalProtocolError("web_search file budget exceeded.", "resource_exhausted", 422)
    const info = await stat(target)
    bytes += info.size
    if (bytes > MAX_SEARCH_BYTES) throw new TerminalProtocolError("web_search byte budget exceeded.", "resource_exhausted", 422)
    if (info.size > MAX_FILE_BYTES) return
    const content = await readFile(target, "utf8").catch(() => "")
    scanContent(relative(workspace, target).replaceAll("\\", "/"), content)
  }
  const visit = async (directory: string): Promise<void> => {
    const entries = await opendir(directory)
    for await (const entry of entries) {
      if (matches.length >= maximum) break
      const target = resolve(directory, entry.name)
      if (entry.isSymbolicLink()) continue
      if (entry.isDirectory()) await visit(target)
      else if (entry.isFile()) await scanFile(target)
    }
  }
  const paths = Array.isArray(context.action.arguments.paths) && context.action.arguments.paths.length
    ? context.action.arguments.paths
    : ["."]
  for (const rawPath of paths) {
    if (matches.length >= maximum) break
    const target = await guardedPath(workspace, rawPath)
    const info = await lstat(target.absolute)
    if (info.isDirectory()) await visit(target.absolute)
    else if (info.isFile()) await scanFile(target.absolute)
  }
  return result(context.action, {
    ok: true,
    summary: `Found ${matches.length} workspace matches for ${query}`,
    output: { query, matches, searched_files: files, network_used: false, physical_location_redacted: true },
    metadata: { search_surface: "local_workspace" },
  })
}

function shellCommand(argumentsValue: Record<string, unknown>): { executable: string; argv: string[] } {
  if (typeof argumentsValue.executable === "string" && Array.isArray(argumentsValue.argv)) {
    return { executable: text(argumentsValue.executable, "executable"), argv: argumentsValue.argv.map((item) => text(item, "argv item", true)) }
  }
  const command = text(argumentsValue.command, "command")
  return process.platform === "win32"
    ? { executable: "powershell.exe", argv: ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command] }
    : { executable: "/bin/sh", argv: ["-c", command] }
}

async function shell(context: ActionContext, workspace: string): Promise<TerminalActionResult> {
  const selected = shellCommand(context.action.arguments)
  const timeoutSeconds = boundedInteger(context.action.arguments.timeout_seconds, 120, 1, 3_600, "shell timeout")
  const cwdInput = context.action.arguments.cwd
  const cwd = cwdInput === undefined ? workspace : (await guardedPath(workspace, cwdInput)).absolute
  const environment = context.action.arguments.environment
  if (environment !== undefined && (!environment || typeof environment !== "object" || Array.isArray(environment))) {
    throw new TerminalProtocolError("shell environment must be an object.", "execution_failed", 422)
  }
  const env = { ...process.env }
  for (const [key, value] of Object.entries((environment ?? {}) as Record<string, unknown>)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || typeof value !== "string" || value.includes("\0")) {
      throw new TerminalProtocolError("shell environment contains an invalid entry.", "execution_failed", 422)
    }
    env[key] = value
  }
  const child = spawn(selected.executable, selected.argv, { cwd, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] })
  context.events.process(child)
  let stdout = ""
  let stderr = ""
  let outputBytes = 0
  let budgetError: TerminalProtocolError | undefined
  const observe = (kind: "stdout" | "stderr", chunk: Buffer) => {
    if (budgetError) return
    outputBytes += chunk.byteLength
    if (outputBytes > MAX_SHELL_OUTPUT_BYTES) {
      budgetError = new TerminalProtocolError("shell output exceeds the result budget.", "resource_exhausted", 422, { recoveryIntent: "reconcile" })
      child.kill("SIGKILL")
      return
    }
    const rendered = context.events.redact(chunk.toString("utf8"))
    if (kind === "stdout") { stdout += rendered; context.events.stdout(rendered) }
    else { stderr += rendered; context.events.stderr(rendered) }
  }
  child.stdout.on("data", (chunk: Buffer) => observe("stdout", chunk))
  child.stderr.on("data", (chunk: Buffer) => observe("stderr", chunk))
  const heartbeat = setInterval(() => context.events.heartbeat(), 2_000)
  heartbeat.unref()
  const timeout = setTimeout(() => child.kill("SIGKILL"), timeoutSeconds * 1_000)
  timeout.unref()
  const abort = () => child.kill("SIGKILL")
  context.signal.addEventListener("abort", abort, { once: true })
  try {
    const exit = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveExit, reject) => {
      child.once("error", reject)
      child.once("exit", (code, signal) => resolveExit({ code, signal }))
    })
    if (context.signal.aborted) throw context.signal.reason
    if (budgetError) throw budgetError
    const timedOut = exit.signal === "SIGKILL" && !context.signal.aborted
    const ok = exit.code === 0 && !timedOut
    return result(context.action, {
      ok,
      summary: timedOut ? `Shell timed out after ${timeoutSeconds}s` : `Shell exited with code ${exit.code ?? -1} on terminal node`,
      output: { returncode: exit.code, stdout, stderr, timed_out: timedOut, cwd_projected: false },
      ...(ok ? {} : { error: timedOut ? "tool_timeout" : "non_zero_exit" }),
    })
  } finally {
    clearInterval(heartbeat)
    clearTimeout(timeout)
    context.signal.removeEventListener("abort", abort)
    context.events.process(undefined)
  }
}

async function artifactWrite(context: ActionContext, artifactRoot: string): Promise<TerminalActionResult> {
  const content = text(context.action.arguments.content ?? "", "content", true)
  if (Buffer.byteLength(content, "utf8") > MAX_FILE_BYTES) {
    throw new TerminalProtocolError("artifact_write content exceeds the action budget.", "resource_exhausted", 422)
  }
  const rawExtension = text(context.action.arguments.extension ?? ".txt", "extension")
  const extension = /^\.[A-Za-z0-9._-]{1,16}$/.test(rawExtension) ? rawExtension : ".txt"
  const artifactId = `artifact_${randomUUID().replaceAll("-", "")}`
  const target = await guardedPath(artifactRoot, `terminal-artifacts/${artifactId}${extension}`, { createParent: true })
  await writeFile(target.absolute, content, { encoding: "utf8", flag: "wx" })
  const projection = { artifact_id: artifactId, relative_path: target.relative, uri: `artifact://${artifactId}`, title: text(context.action.arguments.title ?? "Terminal artifact", "title") }
  context.events.artifact(projection)
  return result(context.action, {
    ok: true,
    summary: `Wrote artifact ${artifactId} on terminal node`,
    output: { ...projection, physical_location_redacted: true },
  })
}

export async function executeTerminalAction(context: ActionContext): Promise<TerminalActionResult> {
  if (
    context.action.permission.allowed !== true
    || typeof context.action.permission.receipt_id !== "string"
    || !context.action.permission.receipt_id.trim()
    || context.action.permission.tool_call_id !== context.action.tool_call_id
  ) {
    throw new TerminalProtocolError("Terminal action requires a consumed allow receipt.", "permission_denied", 403)
  }
  if (Date.now() / 1000 >= context.envelope.deadline_at) {
    throw new TerminalProtocolError("Dispatch deadline already expired.", "backend_timeout", 408, { retryable: true, recoveryIntent: "change_backend" })
  }
  const workspace = await approvedRoot(context.startupRoot, text(context.envelope.workspace_root, "workspace root"), "workspace root")
  const artifactRoot = await approvedRoot(context.startupRoot, text(context.envelope.artifact_root, "artifact root"), "artifact root")
  switch (context.action.tool_name) {
    case "file_read": return fileRead(context, workspace)
    case "file_write": return fileWrite(context, workspace)
    case "file_edit": return fileEdit(context, workspace)
    case "file_delete": return fileDelete(context, workspace)
    case "web_search": return webSearch(context, workspace)
    case "shell": return shell(context, workspace)
    case "artifact_write": return artifactWrite(context, artifactRoot)
    default: throw new TerminalProtocolError(`Unsupported terminal action: ${context.action.tool_name}`, "unsupported_operation", 422)
  }
}
