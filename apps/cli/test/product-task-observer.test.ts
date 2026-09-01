import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import type { TaskProjection } from "@zyra/typed-api-client"
import { CliApi, type IngressFrame } from "../src/api.ts"
import { observeProductTask } from "../src/commands/product.ts"
import { ProductTuiShell } from "../src/tui/shell.ts"

class Capture extends Writable {
  text = ""
  columns = 100
  rows = 32
  isTTY: boolean

  constructor(tty = false) {
    super()
    this.isTTY = tty
  }

  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(enabled: boolean): void { this.raw = enabled }
}

function task(status: string, metadata: Readonly<Record<string, unknown>> = {}): TaskProjection {
  return {
    taskId: "task_product",
    runId: "run_product",
    sessionId: "session_product",
    userGoal: "测试产品终端",
    status,
    terminal: ["completed", "failed", "cancelled", "blocked", "killed"].includes(status),
    active: !["completed", "failed", "cancelled", "blocked", "killed"].includes(status),
    rootNodeId: "node_root",
    planNodes: [],
    metadata,
    createdAt: "2026-09-01T00:00:00.000Z",
    updatedAt: "2026-09-01T00:00:01.000Z",
  } as unknown as TaskProjection
}

function frame(sequence: number, eventType: string): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation: 1,
    taskId: "task_product",
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_${sequence}`,
    eventType,
    event: {
      schema: "zyra.runtime-event/v1",
      eventType,
      identity: { taskId: "task_product", runId: "run_product" },
      inline: { internal_summary: "must-not-leak", artifact_id: "artifact_secret" },
      artifactRefs: [],
    },
    raw: {},
  }
}

function shell(output: Capture): ProductTuiShell {
  return new ProductTuiShell({
    stdin: new PassThrough(),
    output,
    workspace: "G:\\agent-zoo\\zyra",
  })
}

describe("product task observer", () => {
  test("renders only product semantics and the canonical final answer", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const pending = task("pending")
    const completed = task("completed", { final_answer: "收到，这是最终回答。" })
    const api = {
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_1", subscriptionSequence: 1, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_1", generation: 1, frames: [frame(1, "runtime.node.updated")], hasMore: false, caughtUp: true, nextSequence: 1 }
      },
      async runTask() {
        return { task: completed, events: [], receipt: {}, controls: {}, raw: {} }
      },
      async *streamIngress() {
        await Promise.resolve()
        yield { kind: "heartbeat", taskId: pending.taskId, generation: 1, sequence: 1, cursor: "cursor_1" }
      },
      async task() { return completed },
    } as unknown as CliApi

    productShell.start()
    const result = await observeProductTask({
      api,
      task: pending,
      shell: productShell,
      signal: new AbortController().signal,
      resume: false,
    })
    productShell.finish()

    expect(result.status).toBe("completed")
    expect(output.text).toContain("收到，这是最终回答。")
    expect(output.text).not.toContain("runtime.node.updated")
    expect(output.text).not.toContain("must-not-leak")
    expect(output.text).not.toContain("artifact_secret")
    expect(output.text).not.toContain("\u001b[?1049")
  })

  test("replaces a gapped generation from the canonical snapshot", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const running = task("running")
    const completed = task("completed", { final_answer: "恢复后完成。" })
    let capabilityCalls = 0
    let streamCalls = 0
    let taskCalls = 0
    const api = {
      async ingressCapabilities() {
        capabilityCalls += 1
        const generation = capabilityCalls === 1 ? 1 : 2
        return { taskId: running.taskId, generation, subscriptionCursor: `cursor_${generation}`, subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress(_taskId: string, generation: number) {
        yield { cursor: `cursor_${generation}`, generation, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { return await new Promise<never>(() => undefined) },
      async *streamIngress() {
        streamCalls += 1
        if (streamCalls === 1) {
          yield { kind: "event", taskId: running.taskId, generation: 1, sequence: 2, frame: frame(2, "runtime.node.updated") }
          return
        }
        yield { kind: "close", taskId: running.taskId, generation: 2, sequence: 0, cursor: "cursor_2" }
      },
      async task() {
        taskCalls += 1
        return taskCalls === 1 ? running : completed
      },
    } as unknown as CliApi

    productShell.start()
    const result = await observeProductTask({
      api,
      task: running,
      shell: productShell,
      signal: new AbortController().signal,
      resume: true,
    })
    productShell.finish()

    expect(result.status).toBe("completed")
    expect(capabilityCalls).toBeGreaterThanOrEqual(3)
    expect(streamCalls).toBe(2)
    expect(output.text).toContain("恢复后完成。")
    expect(output.text.match(/恢复后完成。/g)).toHaveLength(1)
  })

  test("fails visibly and does not open a stream when SSE is unavailable", async () => {
    const output = new Capture()
    const productShell = shell(output)
    let streamed = false
    const api = {
      async ingressCapabilities() {
        return { taskId: "task_product", generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: false, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async *streamIngress() { streamed = true; yield undefined as never },
    } as unknown as CliApi

    productShell.start()
    await expect(observeProductTask({
      api,
      task: task("pending"),
      shell: productShell,
      signal: new AbortController().signal,
      resume: false,
    })).rejects.toThrow("实时事件流不可用")
    productShell.close()
    expect(streamed).toBeFalse()
  })

  test("keeps permissions fail-closed when custody is unavailable and restores TTY mode", async () => {
    const output = new Capture(true)
    const stdin = new TtyInput()
    const productShell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    const pending = task("pending")
    const completed = task("completed", { final_answer: "安全完成。" })
    const api = {
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async openPermissionSession() { throw new Error("permission custody disabled") },
      async runTask() { return { task: completed, events: [], receipt: {}, controls: {}, raw: {} } },
      async *streamIngress() {
        await Promise.resolve()
        yield { kind: "heartbeat", taskId: pending.taskId, generation: 1, sequence: 0, cursor: "cursor_0" }
      },
      async task() { return completed },
    } as unknown as CliApi

    productShell.start()
    const result = await observeProductTask({
      api,
      task: pending,
      shell: productShell,
      signal: new AbortController().signal,
      resume: false,
    })
    productShell.finish()

    expect(result.status).toBe("completed")
    expect(output.text).toContain("权限控制保持关闭")
    expect(output.text).toContain("安全完成。")
    expect(stdin.raw).toBeFalse()
  })

  test("detaches immediately without sending a canonical task cancellation", async () => {
    const output = new Capture(true)
    const stdin = new TtyInput()
    const productShell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    const running = task("running")
    let cancelled = 0
    let runTransportAborted = false
    const api = {
      async ingressCapabilities() {
        return { taskId: running.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async openPermissionSession() { throw new Error("permission custody disabled") },
      async runTask(_task: TaskProjection, signal?: AbortSignal) {
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            runTransportAborted = true
            reject(signal.reason)
          }, { once: true })
        })
      },
      async *streamIngress(_taskId: string, _cursor: string, _generation: number, signal?: AbortSignal) {
        await new Promise<void>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal.reason), { once: true })
        })
      },
      async cancelTask() { cancelled += 1; throw new Error("must not cancel") },
      async task() { return running },
    } as unknown as CliApi

    productShell.start()
    const startedAt = performance.now()
    const observation = observeProductTask({
      api,
      task: running,
      shell: productShell,
      signal: new AbortController().signal,
      resume: true,
    })
    setTimeout(() => stdin.write("/exit\r"), 20)
    const result = await observation
    productShell.finish()

    expect(result.status).toBe("detached")
    expect(result.taskId).toBe(running.taskId)
    expect(performance.now() - startedAt).toBeLessThan(1_000)
    expect(cancelled).toBe(0)
    expect(runTransportAborted).toBeTrue()
    expect(output.text).toContain(`zyra resume ${running.taskId}`)
    expect(stdin.raw).toBeFalse()
  })
})
