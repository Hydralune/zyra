import { spawn } from "node:child_process"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { CliApi, type IngressFrame } from "../apps/cli/src/api.ts"
import { projectProductEvents } from "../apps/cli/src/presentation/projector.ts"
import { reduceProductEvents, renderProductSnapshot } from "../apps/cli/src/presentation/renderer.ts"

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

function productTaskId(stdout: string): string {
  const taskId = stdout.match(/\btask_[A-Za-z0-9_-]+\b/u)?.[0]
  if (!taskId) throw new Error("product TUI did not render its canonical task identity")
  return taskId
}

function compactText(value: string): string {
  return value.replace(/\s+/gu, " ").trim()
}

async function main(): Promise<void> {
  const input = options(process.argv.slice(2))
  const root = resolve(import.meta.dir, "..")
  const workspace = await mkdtemp(join(tmpdir(), "zyra-product-tui-smoke-"))
  process.stderr.write(`product TUI smoke · ${input.baseUrl} · ${input.width} columns\n`)
  try {
    const child = await runCli(root, workspace, input)
    const taskId = productTaskId(child.stdout)
    if (child.stdout.includes("runtime.") || child.stdout.includes("\u001b[?1049")) {
      throw new Error("product CLI leaked a raw runtime event or entered alternate-screen mode")
    }
    const api = new CliApi({ baseUrl: input.baseUrl, timeoutMs: 30_000 })
    try {
      const task = await api.task(taskId)
      const frames: IngressFrame[] = []
      try {
        const capabilities = await api.ingressCapabilities(taskId)
        for await (const page of api.snapshotIngress(taskId, capabilities.generation)) frames.push(...page.frames)
      } catch {
        // The canonical task snapshot still provides an honest final-answer fallback.
      }
      const events = projectProductEvents({ task, frames })
      const view = reduceProductEvents(events)
      const rendered = renderProductSnapshot(events, {
        width: input.width,
        workspace: root,
      })
      const finalAnswer = String(task.metadata.final_answer ?? "")
      if (
        !finalAnswer
        || !compactText(child.stdout).includes(compactText(finalAnswer))
        || !view.messages.some((message) => message.role === "assistant" && message.text === finalAnswer)
        || rendered.includes("runtime.")
      ) {
        throw new Error("product projection omitted the canonical final answer or leaked a raw runtime event")
      }
      process.stdout.write(child.stdout)
      process.stdout.write(`\n--- ${input.width}-column deterministic replay ---\n`)
      process.stdout.write(rendered)
    } finally {
      api.close()
    }
  } finally {
    await rm(workspace, { recursive: true, force: true })
  }
}

await main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
  process.exitCode = 1
})
