import { spawn } from "node:child_process"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { CliApi, type IngressFrame } from "../apps/cli/src/api.ts"
import { projectProductEvents } from "../apps/cli/src/presentation/projector.ts"
import { reduceProductEvents, renderProductSnapshot } from "../apps/cli/src/presentation/renderer.ts"
import { renderMarkdown } from "../apps/cli/src/tui/markdown.ts"

interface SmokeOptions {
  baseUrl: string
  goal: string
  timeout: string
  width: number
}

function options(argv: readonly string[]): SmokeOptions {
  const values = new Map(argv.flatMap((entry) => {
    const index = entry.indexOf("=")
    return index > 0 ? [[entry.slice(0, index), entry.slice(index + 1)] as const] : []
  }))
  const width = Number(values.get("--width") ?? 80)
  if (!Number.isSafeInteger(width) || width < 40 || width > 240) throw new Error("--width must be an integer from 40 to 240")
  return {
    baseUrl: values.get("--base-url") ?? process.env.ZYRA_API_URL ?? "http://127.0.0.1:8000",
    goal: values.get("--goal") ?? "测试，收到请回复",
    timeout: values.get("--timeout") ?? "3m",
    width,
  }
}

function runCli(root: string, workspace: string, input: SmokeOptions): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolvePromise, reject) => {
    const cli = resolve(root, "apps/cli/dist/zyra.js")
    const child = spawn(process.env.ZYRA_NODE_BINARY ?? "node", [
      cli,
      `--base-url=${input.baseUrl}`,
      `--timeout=${input.timeout}`,
      input.goal,
    ], {
      cwd: workspace,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    child.stdout.setEncoding("utf8").on("data", (chunk: string) => { stdout += chunk })
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => { stderr += chunk })
    child.once("error", reject)
    child.once("exit", (code) => {
      if (code === 0) resolvePromise({ stdout, stderr })
      else reject(new Error(`product zyra exited with ${code ?? "no code"}${stderr.trim() ? `: ${stderr.trim()}` : ""}`))
    })
  })
}

function compactText(value: string): string {
  return value.replace(/\s+/gu, " ").trim()
}

function executorCwd(metadata: Readonly<Record<string, unknown>>): string {
  const environment = metadata.executor_environment
  if (!environment || typeof environment !== "object" || Array.isArray(environment)) return ""
  const cwd = (environment as Record<string, unknown>).cwd
  return typeof cwd === "string" ? resolve(cwd) : ""
}

async function main(): Promise<void> {
  const input = options(process.argv.slice(2))
  const root = resolve(import.meta.dir, "..")
  const workspace = await mkdtemp(join(tmpdir(), "zyra-product-tui-smoke-"))
  process.stderr.write(`product TUI smoke · ${input.baseUrl} · ${input.width} columns\n`)
  const api = new CliApi({ baseUrl: input.baseUrl, timeoutMs: 30_000 })
  try {
    const before = new Set((await api.tasks({ limit: 100 })).tasks.map((task) => task.taskId))
    const child = await runCli(root, workspace, input)
    if (
      child.stdout.includes("runtime.")
      || child.stdout.includes("\u001b[?1049")
      || /\b(?:task|run|session)_[A-Za-z0-9_-]+\b/u.test(child.stdout)
    ) {
      throw new Error("product CLI leaked a raw runtime event/internal identity or entered alternate-screen mode")
    }
    const newTasks = (await api.tasks({ limit: 100 })).tasks.filter((task) => (
      !before.has(task.taskId)
      && task.userGoal === input.goal
    ))
    const candidates = (await Promise.all(newTasks.map((task) => api.task(task.taskId))))
      .filter((task) => executorCwd(task.metadata) === resolve(workspace))
    if (candidates.length !== 1) {
      throw new Error(`canonical smoke task resolution expected 1 exact task, observed ${candidates.length}`)
    }
    const task = candidates[0]!
    const frames: IngressFrame[] = []
    try {
      const capabilities = await api.ingressCapabilities(task.taskId)
      for await (const page of api.snapshotIngress(task.taskId, capabilities.generation)) frames.push(...page.frames)
    } catch {
      // The canonical task snapshot still provides an honest final-answer fallback.
    }
    const events = projectProductEvents({ task, frames })
    const view = reduceProductEvents(events)
    const rendered = renderProductSnapshot(events, {
      width: input.width,
      workspace,
    })
    const finalAnswer = String(task.metadata.final_answer ?? "")
    if (!finalAnswer) throw new Error("canonical smoke task omitted its final answer")
    const productFinalAnswer = compactText(renderMarkdown(finalAnswer, 10_000).join("\n"))
    if (!compactText(child.stdout).includes(productFinalAnswer)) {
      throw new Error("product CLI omitted the rendered canonical final answer")
    }
    if (!view.messages.some((message) => message.role === "assistant" && message.text === finalAnswer)) {
      throw new Error("product state projection omitted the canonical final answer")
    }
    if (rendered.includes("runtime.")) throw new Error("deterministic product replay leaked a raw runtime event")
    process.stdout.write(child.stdout)
    process.stdout.write(`\n--- ${input.width}-column deterministic replay ---\n`)
    process.stdout.write(rendered)
  } finally {
    api.close()
    await rm(workspace, { recursive: true, force: true })
  }
}

await main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
  process.exitCode = 1
})
