import { lstat, open, readFile, realpath } from "node:fs/promises"
import { arch, platform, release } from "node:os"
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path"
import { spawnSync } from "node:child_process"
import type { RuntimeReadiness } from "@zyra/typed-api-client"
import { CliApi } from "../../api.ts"
import {
  DAEMON_STATE_SCHEMA,
  cliStateDirectory,
  daemonStatus,
  ensureDaemon,
  resolveProjectRoot,
  resolvePythonCommand,
  type DaemonStatus,
} from "../../daemon.ts"
import type { DoctorCommand } from "../../contracts.ts"
import { CliTaskError } from "../../contracts.ts"
import { sanitizeForOutput } from "../../output.ts"

export const DOCTOR_SCHEMA = "zyra.cli-doctor/v1" as const
const BUNDLE_SCHEMA = "zyra.cli-diagnostic-bundle/v1" as const
const CLI_VERSION = "0.1.0"

export interface DoctorCheck {
  id: string
  status: "pass" | "warn" | "fail" | "unknown"
  summary: string
  recovery?: string
}

export interface DoctorReport {
  schema: typeof DOCTOR_SCHEMA
  healthy: boolean
  status: "ready" | "degraded" | "offline"
  generated_at: string
  cli: Readonly<Record<string, unknown>>
  workspace: Readonly<Record<string, unknown>>
  daemon: Readonly<Record<string, unknown>>
  runtime: Readonly<Record<string, unknown>>
  providers: Readonly<Record<string, unknown>>
  checks: readonly DoctorCheck[]
}

export interface DoctorResult {
  report: DoctorReport
  bundle?: { written: true; path: string; bytes: number }
}

export interface DoctorProbeOverrides {
  daemon?: DaemonStatus
  readiness?: RuntimeReadiness
  providerIds?: readonly string[]
  modelCount?: number
  originOccupied?: boolean
}

function within(root: string, candidate: string): boolean {
  const selected = relative(root, candidate)
  return selected === "" || (!selected.startsWith("..") && !isAbsolute(selected))
}

function check(id: string, status: DoctorCheck["status"], summary: string, recovery?: string): DoctorCheck {
  return Object.freeze({ id, status, summary, ...(recovery ? { recovery } : {}) })
}

function safeReport(value: DoctorReport): DoctorReport {
  return sanitizeForOutput(value) as DoctorReport
}

async function stateCheck(): Promise<DoctorCheck> {
  const path = join(cliStateDirectory(), "daemon.json")
  try {
    const parsed: unknown = JSON.parse(await readFile(path, "utf8"))
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || (parsed as Record<string, unknown>).schema !== DAEMON_STATE_SCHEMA) {
      return check("daemon.state", "fail", "daemon state 使用未知 schema。", "关闭相关进程后备份并移走 daemon.json，再重新运行 zyra doctor。")
    }
    return check("daemon.state", "pass", "daemon state schema 可读取。")
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return check("daemon.state", "pass", "没有本地 daemon state；允许首次启动。")
    if (error instanceof SyntaxError) {
      return check("daemon.state", "fail", "daemon state JSON 已损坏。", "备份 daemon.json 后将其移走；不要删除其他 session/task 数据。")
    }
    return check("daemon.state", "fail", "daemon state 无法读取。", "检查 Zyra CLI state 目录权限。")
  }
}

function gitSummary(cwd: string): { check: DoctorCheck; value: Readonly<Record<string, unknown>> } {
  const result = spawnSync("git", ["status", "--porcelain=v1", "--branch", "--untracked-files=no"], {
    cwd,
    encoding: "utf8",
    timeout: 3_000,
    maxBuffer: 512 * 1024,
    windowsHide: true,
  })
  if (result.error || result.status !== 0) {
    return { check: check("workspace.git", "warn", "当前目录不是可读取的 Git 工作区。"), value: { available: false } }
  }
  const lines = result.stdout.split(/\r?\n/u).filter(Boolean)
  const branch = lines[0]?.startsWith("## ") ? lines.shift()!.slice(3).split("...")[0]!.trim() : "unknown"
  return {
    check: check("workspace.git", "pass", `Git 可用；${lines.length} 个已跟踪变更。`),
    value: { available: true, branch, tracked_change_count: lines.length },
  }
}

async function sourceRuntimeCheck(): Promise<{ checks: DoctorCheck[]; value: Readonly<Record<string, unknown>> }> {
  try {
    const root = resolveProjectRoot()
    const python = await resolvePythonCommand(root)
    return {
      checks: [
        check("installation.source", "pass", "Zyra source launcher 闭包可定位。"),
        check("installation.python", "pass", "daemon Python runtime 可定位。"),
      ],
      value: { source_launcher: true, project_label: basename(root), python_label: basename(python) },
    }
  } catch (error) {
    return {
      checks: [check(
        "installation.source",
        "fail",
        error instanceof Error ? error.message : "Zyra source launcher 闭包不可用。",
        "从完整发布包启动，或设置指向有效源码树的 ZYRA_PROJECT_ROOT。",
      )],
      value: { source_launcher: false },
    }
  }
}

async function foreignOriginOccupied(baseUrl: string): Promise<boolean> {
  try {
    const response = await fetch(baseUrl, {
      method: "HEAD",
      redirect: "manual",
      signal: AbortSignal.timeout(1_500),
      headers: { "Cache-Control": "no-store" },
    })
    await response.body?.cancel()
    return true
  } catch {
    return false
  }
}

async function bundlePath(cwd: string, requested: string): Promise<string> {
  const root = await realpath(cwd)
  const target = resolve(isAbsolute(requested) ? requested : join(root, requested))
  if (!within(root, target) || basename(target) === "") {
    throw new CliTaskError("诊断包路径必须位于当前 workspace 内。", "doctor_bundle_path_outside_workspace")
  }
  const parent = await realpath(dirname(target)).catch(() => undefined)
  if (!parent || !within(root, parent)) {
    throw new CliTaskError("诊断包目录不存在或越过 workspace 边界。", "doctor_bundle_parent_invalid")
  }
  try {
    const existing = await lstat(target)
    throw new CliTaskError(
      existing.isSymbolicLink() ? "拒绝写入符号链接诊断包。" : "诊断包目标已存在；不会覆盖。",
      existing.isSymbolicLink() ? "doctor_bundle_symlink_forbidden" : "doctor_bundle_exists",
    )
  } catch (error) {
    if (error instanceof CliTaskError) throw error
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  return target
}

export async function writeDoctorBundle(report: DoctorReport, cwd: string, requested: string): Promise<{ written: true; path: string; bytes: number }> {
  const path = await bundlePath(cwd, requested)
  const payload = `${JSON.stringify({
    schema: BUNDLE_SCHEMA,
    report: safeReport(report),
    redaction: {
      applied: true,
      physical_paths: "redacted",
      credential_fields: "redacted",
      raw_runtime_payloads: "excluded",
    },
  }, null, 2)}\n`
  const handle = await open(path, "wx", 0o600)
  try {
    await handle.writeFile(payload, "utf8")
    await handle.sync()
  } finally {
    await handle.close()
  }
  return Object.freeze({ written: true, path, bytes: Buffer.byteLength(payload, "utf8") })
}

export async function executeDoctor(input: {
  command: DoctorCommand
  token?: string
  cwd?: string
  stdoutIsTty?: boolean
  stdinIsTty?: boolean
  overrides?: DoctorProbeOverrides
}): Promise<DoctorResult> {
  const cwd = resolve(input.cwd ?? process.cwd())
  const checks: DoctorCheck[] = []
  let daemon = input.overrides?.daemon
  let preflightOriginOccupied = input.overrides?.originOccupied
  if (!daemon) {
    // Never send caller credentials to an endpoint until its public health
    // contract identifies it as Zyra.  Explicit autostart also refuses a
    // foreign occupied port before spawning anything.
    const publicStatus = await daemonStatus({ baseUrl: input.command.baseUrl })
    if (input.command.autoStart && !publicStatus.reachable) {
      preflightOriginOccupied ??= await foreignOriginOccupied(input.command.baseUrl)
      daemon = preflightOriginOccupied
        ? publicStatus
        : await ensureDaemon({
            baseUrl: input.command.baseUrl,
            token: input.token,
            autoStart: true,
            startupTimeoutMs: input.command.startupTimeoutMs,
          })
    } else {
      daemon = publicStatus
    }
  }
  const originOccupied = preflightOriginOccupied
    ?? (!daemon.reachable ? await foreignOriginOccupied(input.command.baseUrl) : false)
  checks.push(daemon.reachable
    ? check("daemon.health", "pass", `Zyra daemon 可达；${daemon.managed ? "由 CLI 管理" : "外部管理"}。`)
    : originOccupied
      ? check("daemon.health", "fail", "目标端口已被非 Zyra 服务占用。", "停止冲突服务或使用 --base-url 指定空闲 loopback 端口。")
      : check("daemon.health", "fail", "Zyra daemon 不可达。", "运行 zyra daemon start，或使用 zyra doctor --autostart=true。"))
  if (daemon.staleState) checks.push(check("daemon.identity", "warn", "daemon state 与健康身份不一致。", "不要按 PID 手工终止；先确认端口归属并重新运行 doctor。"))
  checks.push(await stateCheck())

  let readiness = input.overrides?.readiness
  let providerIds = input.overrides?.providerIds ? [...input.overrides.providerIds] : undefined
  let modelCount = input.overrides?.modelCount
  if (daemon.reachable && (!readiness || providerIds === undefined || modelCount === undefined)) {
    const api = new CliApi({
      baseUrl: input.command.baseUrl,
      token: input.token,
      timeoutMs: Math.min(input.command.startupTimeoutMs, 30_000),
    })
    try {
      readiness ??= await api.readiness()
      if (providerIds === undefined || modelCount === undefined) {
        const models = await api.providerModels()
        providerIds ??= [...new Set(models.map((model) => model.providerId))].sort()
        modelCount ??= models.length
      }
    } catch (error) {
      checks.push(check("runtime.contract", "fail", error instanceof Error ? error.message : "runtime readiness 请求失败。", "检查 API/CLI 版本兼容与 provider 配置。"))
    } finally {
      api.close("doctor complete")
    }
  }
  if (readiness) {
    const owners = Object.entries(readiness.owners)
    const readyOwners = owners.filter(([, ready]) => ready).length
    checks.push(readiness.ready
      ? check("runtime.readiness", "pass", `runtime owners ${readyOwners}/${owners.length} ready。`)
      : check("runtime.readiness", "fail", `runtime blocked：${readiness.blockers.slice(0, 4).join("；") || readiness.status}`, "按 blocker 修复 provider/runtime 配置后重试。"))
    checks.push(readiness.apiVersion === "1.0"
      ? check("runtime.api-version", "pass", "API version 1.0 与当前 CLI 兼容。")
      : check("runtime.api-version", "fail", `API version ${readiness.apiVersion} 未声明兼容。`, "升级 CLI 或 daemon，使 API major version 匹配。"))
  }
  checks.push((modelCount ?? 0) > 0
    ? check("providers.catalog", "pass", `${modelCount} 个 canonical provider model 可用。`)
    : check("providers.catalog", daemon.reachable ? "fail" : "unknown", "未确认可用 provider model。", "启动 daemon 并检查 provider 凭据/模型目录。"))

  const git = gitSummary(cwd)
  checks.push(git.check)
  const source = await sourceRuntimeCheck()
  checks.push(...source.checks)
  const failed = checks.some((item) => item.status === "fail")
  const warned = checks.some((item) => item.status === "warn" || item.status === "unknown")
  const report = safeReport({
    schema: DOCTOR_SCHEMA,
    healthy: !failed,
    status: daemon.reachable ? (failed || warned ? "degraded" : "ready") : "offline",
    generated_at: new Date().toISOString(),
    cli: {
      version: CLI_VERSION,
      runtime: process.release.name,
      node: process.versions.node,
      platform: platform(),
      arch: arch(),
      os_release: release(),
      stdin_tty: input.stdinIsTty === true,
      stdout_tty: input.stdoutIsTty === true,
    },
    workspace: {
      label: basename(cwd),
      git: git.value,
      source: source.value,
      entry_loaded: true,
    },
    daemon: {
      reachable: daemon.reachable,
      managed: daemon.managed,
      stale_state: daemon.staleState,
      generation: daemon.generation ?? null,
      pid: daemon.pid ?? null,
      origin: input.command.baseUrl,
      foreign_origin_occupied: originOccupied,
    },
    runtime: readiness ? {
      ready: readiness.ready,
      status: readiness.status,
      api_version: readiness.apiVersion,
      owners: readiness.owners,
      blockers: readiness.blockers.slice(0, 32),
    } : { ready: false, status: "unavailable" },
    providers: {
      catalog_available: (modelCount ?? 0) > 0,
      model_count: modelCount ?? 0,
      provider_ids: providerIds ?? [],
      configuration_present: Boolean(input.token),
    },
    checks: Object.freeze(checks),
  })
  const bundle = input.command.bundle
    ? await writeDoctorBundle(report, cwd, input.command.bundle)
    : undefined
  return Object.freeze({ report, ...(bundle ? { bundle } : {}) })
}
