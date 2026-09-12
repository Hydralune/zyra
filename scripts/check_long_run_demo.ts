/**
 * Headless browser check for the long-horizon demo launchers (zero dependencies).
 *
 * Launches the locally installed Chrome with the DevTools Protocol enabled and
 * drives it over the built-in WebSocket (Node 22+).  It clicks a demo button,
 * waits for the sealed run to reach a terminal phase, and writes a screenshot
 * plus a JSON summary.  Use it to verify the WebUI path without a human at the
 * keyboard and without any browser-automation package.
 *
 * Usage:
 *   node --experimental-strip-types scripts/check_long_run_demo.ts \
 *        [--task software|cross-source] [--url http://127.0.0.1:5173]
 *        [--out tmp/demo-check] [--timeout-seconds 300]
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs"
import { spawn, type ChildProcess } from "node:child_process"
import { resolve } from "node:path"
import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

interface Options {
  task: "software" | "cross-source"
  url: string
  out: string
  timeoutMs: number
  port: number
}

interface CdpMessage {
  id?: number
  method?: string
  params?: Record<string, unknown>
  result?: Record<string, unknown>
  error?: { message?: string }
  sessionId?: string
}

function parse(argv: string[]): Options {
  const options: Options = {
    task: "software",
    url: "http://127.0.0.1:5173",
    out: "tmp/demo-check",
    timeoutMs: 5 * 60_000,
    port: 9333,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index]
    const value = argv[index + 1]
    if (flag === "--task" && (value === "software" || value === "cross-source")) {
      options.task = value
      index += 1
    } else if (flag === "--url" && value) {
      options.url = value
      index += 1
    } else if (flag === "--out" && value) {
      options.out = value
      index += 1
    } else if (flag === "--port" && value) {
      options.port = Number(value)
      index += 1
    } else if (flag === "--timeout-seconds" && value) {
      options.timeoutMs = Number(value) * 1000
      index += 1
    }
  }
  return options
}

function chromePath(): string {
  for (const candidate of CHROME_CANDIDATES) {
    if (existsSync(candidate)) return candidate
  }
  throw new Error("A local Chrome executable was not found.")
}

const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms))

/** Minimal Chrome DevTools Protocol client over the built-in WebSocket. */
class Cdp {
  #socket: WebSocket
  #nextId = 1
  #pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>()

  private constructor(socket: WebSocket) {
    this.#socket = socket
    socket.addEventListener("message", (event) => {
      let message: CdpMessage
      try {
        message = JSON.parse(String(event.data)) as CdpMessage
      } catch {
        return
      }
      if (typeof message.id !== "number") return
      const entry = this.#pending.get(message.id)
      if (!entry) return
      this.#pending.delete(message.id)
      if (message.error) entry.reject(new Error(message.error.message ?? "CDP error"))
      else entry.resolve(message.result ?? {})
    })
  }

  static async connect(wsUrl: string): Promise<Cdp> {
    const socket = new WebSocket(wsUrl)
    await new Promise<void>((done, fail) => {
      socket.addEventListener("open", () => done(), { once: true })
      socket.addEventListener("error", () => fail(new Error("CDP socket error")), { once: true })
    })
    return new Cdp(socket)
  }

  send(method: string, params: Record<string, unknown> = {}): Promise<any> {
    const id = this.#nextId++
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject })
      this.#socket.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.#pending.delete(id)) reject(new Error(`CDP timeout: ${method}`))
      }, 60_000)
    })
  }

  close(): void {
    try {
      this.#socket.close()
    } catch {
      // ignore
    }
  }
}

async function waitForTarget(port: number, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`)
      const targets = (await response.json()) as Array<Record<string, unknown>>
      const page = targets.find((item) => item.type === "page" && item.webSocketDebuggerUrl)
      if (page) return String(page.webSocketDebuggerUrl)
    } catch {
      // Chrome not ready yet.
    }
    await sleep(500)
  }
  throw new Error("Chrome DevTools endpoint did not become ready.")
}

async function evaluate(cdp: Cdp, expression: string): Promise<any> {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  })
  if (result.exceptionDetails) {
    throw new Error(
      `evaluate failed: ${result.exceptionDetails.text ?? "unknown"}`,
    )
  }
  return result.result?.value
}

/** Poll an in-page expression until it returns a non-null value. */
async function waitFor(
  cdp: Cdp,
  label: string,
  expression: string,
  timeoutMs: number,
  intervalMs = 1_000,
): Promise<any> {
  const deadline = Date.now() + timeoutMs
  let last: unknown = null
  while (Date.now() < deadline) {
    last = await evaluate(cdp, expression)
    if (last !== null && last !== undefined && last !== false) return last
    await sleep(intervalMs)
  }
  throw new Error(`timed out waiting for ${label}; last=${JSON.stringify(last)}`)
}

async function main(): Promise<number> {
  const options = parse(process.argv.slice(2))
  const outDir = resolve(options.out)
  mkdirSync(outDir, { recursive: true })
  const profileDir = mkdtempSync(join(tmpdir(), "zyra-demo-chrome-"))

  let chrome: ChildProcess | undefined
  let cdp: Cdp | undefined
  try {
    chrome = spawn(
      chromePath(),
      [
        `--remote-debugging-port=${options.port}`,
        `--user-data-dir=${profileDir}`,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
      ],
      { stdio: "ignore" },
    )

    const wsUrl = await waitForTarget(options.port, 30_000)
    cdp = await Cdp.connect(wsUrl)
    await cdp.send("Page.enable")
    await cdp.send("Runtime.enable")

    const taskIndex = options.task === "software" ? 0 : 1
    const buttonSelector = `.long-run-demo .long-run-demo-card:nth-child(${taskIndex + 1}) button`

    await cdp.send("Page.navigate", { url: options.url })
    await waitFor(
      cdp,
      "demo panel rendered",
      `(() => document.querySelector(".long-run-demo") ? true : null)()`,
      60_000,
    )

    const connectionBefore = await evaluate(
      cdp,
      `(() => { const t = document.querySelector(".long-run-demo .tag"); return t ? t.textContent : ""; })()`,
    )

    // Wait until the launcher is enabled (console connected).
    await waitFor(
      cdp,
      "demo launcher enabled",
      `(() => { const b = document.querySelector(${JSON.stringify(buttonSelector)}); return b && !b.disabled ? true : null; })()`,
      60_000,
    )

    await evaluate(
      cdp,
      `(() => { const b = document.querySelector(${JSON.stringify(buttonSelector)}); b && b.click(); return true; })()`,
    )

    const runId = await waitFor(
      cdp,
      "run id on the card",
      `(() => {
         const card = document.querySelectorAll(".long-run-demo-card")[${taskIndex}];
         const facts = card ? card.querySelectorAll(".long-run-demo-facts dd") : [];
         const value = facts[1] ? facts[1].textContent : "";
         return value && value.startsWith("scenario_") ? value : null;
       })()`,
      options.timeoutMs,
      1_000,
    )

    const phase = await waitFor(
      cdp,
      "terminal phase",
      `(async () => {
         const res = await fetch("/api/scenarios/runs/${runId}");
         if (!res.ok) return null;
         const body = await res.json();
         const value = body && body.run ? body.run.phase : null;
         return ["succeeded","failed","archived","cancelled"].includes(value) ? value : null;
       })()`,
      options.timeoutMs,
      2_000,
    )

    const cardState = await evaluate(
      cdp,
      `(() => {
         const card = document.querySelectorAll(".long-run-demo-card")[${taskIndex}];
         const facts = card ? card.querySelectorAll(".long-run-demo-facts dd") : [];
         return {
           status: facts[0] ? facts[0].textContent : "",
           runId: facts[1] ? facts[1].textContent : "",
           phase: card ? card.getAttribute("data-phase") : "",
         };
       })()`,
    )

    // Full-page screenshot via CDP.
    const metrics = await cdp.send("Page.getLayoutMetrics")
    const size = metrics.cssContentSize ?? metrics.contentSize ?? { width: 1440, height: 960 }
    const shot = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: size.width, height: size.height, scale: 1 },
    })
    const screenshot = resolve(outDir, `${options.task}.png`)
    writeFileSync(screenshot, Buffer.from(String(shot.data), "base64"))

    const summary = {
      ok: phase === "succeeded",
      task: options.task,
      url: options.url,
      connectionBefore,
      runId,
      phase,
      cardState,
      screenshot,
    }
    writeFileSync(
      resolve(outDir, `${options.task}.json`),
      JSON.stringify(summary, null, 2) + "\n",
      "utf8",
    )
    process.stdout.write(JSON.stringify(summary) + "\n")
    return summary.ok ? 0 : 1
  } finally {
    cdp?.close()
    if (chrome && !chrome.killed) chrome.kill()
  }
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`)
    process.exit(2)
  })
