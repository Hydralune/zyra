import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import {
  HttpResponseError,
  TransportDisconnectedError,
  type TaskProjection,
} from "@zyra/typed-api-client"
import { CliApi, type IngressFrame } from "../src/api.ts"
import { observeProductTask } from "../src/commands/product.ts"
import { CliExitCode } from "../src/contracts.ts"
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

function frame(sequence: number, eventType: string, generation = 1): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation,
    taskId: "task_product",
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_${sequence}`,
    eventType,
    cursor: `cursor_${sequence}`,
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
  test("recovers 100 abrupt stream disconnects after verified progress without duplicates", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const running = task("running")
    const completed = task("completed", { final_answer: "100 次断线后完成。" })
    let streamCalls = 0
    let capabilityCalls = 0
    const api = {
      async ingressCapabilities() {
        capabilityCalls += 1
        return {
          taskId: running.taskId,
          generation: 1,
          subscriptionCursor: "cursor_0",
          subscriptionSequence: 0,
          sseAvailable: true,
          raw: {},
        }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { return await new Promise<never>(() => undefined) },
      async *streamIngress() {
        streamCalls += 1
        if (streamCalls <= 100) {
          yield {
            kind: "event",
            taskId: running.taskId,
            generation: 1,
            sequence: streamCalls,
            frame: frame(streamCalls, "runtime.node.updated"),
          }
          throw new Error(`injected disconnect ${streamCalls}`)
        }
        yield {
          kind: "close",
          taskId: running.taskId,
          generation: 1,
          sequence: 100,
          cursor: "cursor_100",
        }
      },
      async task() { return streamCalls > 100 ? completed : running },
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
    expect(streamCalls).toBe(101)
    expect(capabilityCalls).toBe(101)
    expect(output.text).toContain("100 次断线后完成。")
    expect(output.text.match(/100 次断线后完成。/g)).toHaveLength(1)
  }, 20_000)

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

  test("keeps the canonical terminal failure when the run mutation rejects concurrently", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const pending = task("pending")
    const failed = task("failed", { failure_reason: "no provider/model route satisfies the request constraints" })
    const api = {
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() {
        await Promise.resolve()
        throw new HttpResponseError(409, "run rejected after canonical failure", {
          code: "contract_run_rejected",
          body: {},
        })
      },
      async *streamIngress() {
        await Promise.resolve()
        yield { kind: "event", taskId: pending.taskId, generation: 1, sequence: 1, frame: frame(1, "runtime.task.failed") }
      },
      async task() { return failed },
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

    expect(result.status).toBe("failed")
    expect(result.exitCode).toBe(CliExitCode.TASK_FAILED)
    expect(output.text).toContain("no provider/model route satisfies the request constraints")
    expect(output.text).not.toContain("run rejected after canonical failure")
  })

  test("keeps needs_revision controllable until a true canonical terminal state", async () => {
    const output = new Capture(true)
    const stdin = new TtyInput()
    const productShell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    const pending = task("pending")
    const needsRevision = {
      ...task("needs_revision"),
      terminal: false,
      active: true,
      updatedAt: "2026-09-01T00:00:02.000Z",
    } as TaskProjection
    const failed = {
      ...task("failed", { failure_reason: "canonical failure after revision request" }),
      updatedAt: "2026-09-01T00:00:03.000Z",
    } as TaskProjection
    const taskReads = [needsRevision, failed]
    const api = {
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() {
        await Promise.resolve()
        throw new HttpResponseError(409, "review requires revision", {
          code: "contract_run_rejected",
          body: {},
        })
      },
      async *streamIngress() {
        yield { kind: "event", taskId: pending.taskId, generation: 1, sequence: 1, frame: frame(1, "runtime.task.failed") }
        yield { kind: "event", taskId: pending.taskId, generation: 1, sequence: 2, frame: frame(2, "runtime.task.failed") }
      },
      async task() { return taskReads.shift() ?? failed },
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

    expect(result.status).toBe("failed")
    expect(result.exitCode).toBe(CliExitCode.TASK_FAILED)
    expect(productShell.view.taskStatus).toBe("failed")
    expect(output.text).toContain("任务需要修订后继续")
    expect(output.text).toContain("canonical failure after revision request")
    expect(output.text).not.toContain("review requires revision")
  })

  test("aborts a pending run transport after canonical terminal settlement", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const pending = task("pending")
    const failed = task("failed", { failure_reason: "canonical failure" })
    let mutationAborted = false
    const api = {
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask(_task: TaskProjection, signal?: AbortSignal) {
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            mutationAborted = true
            reject(signal.reason)
          }, { once: true })
        })
      },
      async *streamIngress() {
        yield { kind: "event", taskId: pending.taskId, generation: 1, sequence: 1, frame: frame(1, "runtime.task.failed") }
      },
      async task() { return failed },
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

    expect(result.status).toBe("failed")
    expect(mutationAborted).toBeTrue()
    expect(output.text).not.toContain("已从 task task_product 分离")
  })

  test("ignores the prior terminal frame until a failed resume advances canonically", async () => {
    const output = new Capture()
    const productShell = shell(output)
    const initialFailed = task("failed", { failure_reason: "first attempt failed" })
    const running = {
      ...task("running"),
      updatedAt: "2026-09-01T00:00:02.000Z",
    } as TaskProjection
    const resumedFailed = {
      ...task("failed", { failure_reason: "resumed attempt failed" }),
      updatedAt: "2026-09-01T00:00:03.000Z",
    } as TaskProjection
    const taskReads = [initialFailed, running, resumedFailed]
    const api = {
      async ingressCapabilities() {
        return { taskId: initialFailed.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { return { task: running, events: [], receipt: {}, controls: {}, raw: {} } },
      async *streamIngress() {
        yield { kind: "event", taskId: initialFailed.taskId, generation: 1, sequence: 1, frame: frame(1, "runtime.task.failed") }
        yield { kind: "heartbeat", taskId: initialFailed.taskId, generation: 1, sequence: 1, cursor: "cursor_1" }
        yield { kind: "event", taskId: initialFailed.taskId, generation: 1, sequence: 2, frame: frame(2, "runtime.task.failed") }
        yield { kind: "close", taskId: initialFailed.taskId, generation: 1, sequence: 2, cursor: "cursor_2" }
      },
      async task() { return taskReads.shift() ?? resumedFailed },
    } as unknown as CliApi

    productShell.start()
    const result = await observeProductTask({
      api,
      task: initialFailed,
      shell: productShell,
      signal: new AbortController().signal,
      resume: true,
    })
    productShell.finish()

    expect(result.status).toBe("failed")
    expect(result.exitCode).toBe(CliExitCode.TASK_FAILED)
    expect(productShell.view.taskStatus).toBe("failed")
    expect(taskReads).toHaveLength(0)
    expect(output.text).toContain("resumed attempt failed")
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
    expect(capabilityCalls).toBe(2)
    expect(streamCalls).toBe(2)
    expect(output.text).toContain("恢复后完成。")
    expect(output.text.match(/恢复后完成。/g)).toHaveLength(1)
  })

  test("survives daemon downtime, advances the ingress generation, and keeps controls bound to the recovered task", async () => {
    const output = new Capture(true)
    const stdin = new TtyInput()
    const productShell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    const running = task("running", { recovery_epoch: 1 })
    const recovered = task("running", { recovery_epoch: 2 })
    const cancelled = task("cancelled", { recovery_epoch: 2 })
    const snapshotGenerations: number[] = []
    let capabilityCalls = 0
    let streamCalls = 0
    let cancellationCommitted = false
    let cancelledFromEpoch: unknown
    const api = {
      async ingressCapabilities(_taskId: string, _cursor?: string, expectedGeneration?: number) {
        capabilityCalls += 1
        if (capabilityCalls === 2 || capabilityCalls === 3) {
          throw new TransportDisconnectedError("injected daemon outage")
        }
        const generation = expectedGeneration ?? 1
        return {
          taskId: running.taskId,
          generation,
          subscriptionCursor: `cursor_${generation}_0`,
          subscriptionSequence: 0,
          sseAvailable: true,
          raw: {},
        }
      },
      async *snapshotIngress(_taskId: string, generation: number) {
        snapshotGenerations.push(generation)
        yield { cursor: `cursor_${generation}_0`, generation, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async openPermissionSession() { throw new Error("permission custody disabled") },
      async *streamIngress(_taskId: string, _cursor: string, generation: number) {
        streamCalls += 1
        if (streamCalls <= 2) throw new TransportDisconnectedError("injected daemon outage")
        if (streamCalls === 3) {
          throw new HttpResponseError(400, "stale cursor from the previous daemon", {
            code: "cursor_signature_invalid",
            body: { resyncRequired: true },
          })
        }
        while (!cancellationCommitted) {
          await new Promise((resolve) => setTimeout(resolve, 10))
          yield { kind: "heartbeat", taskId: running.taskId, generation, sequence: 0, cursor: `cursor_${generation}_0` }
        }
        yield {
          kind: "event",
          taskId: running.taskId,
          generation,
          sequence: 1,
          frame: frame(1, "runtime.task.cancelled", generation),
        }
      },
      async task() { return cancellationCommitted ? cancelled : recovered },
      async cancelTask(selected: TaskProjection) {
        cancelledFromEpoch = selected.metadata.recovery_epoch
        cancellationCommitted = true
        return { task: cancelled, events: [], receipt: {}, controls: {}, raw: {} }
      },
    } as unknown as CliApi

    productShell.start()
    const observation = observeProductTask({
      api,
      task: running,
      shell: productShell,
      signal: new AbortController().signal,
      resume: false,
    })
    const deadline = Date.now() + 5_000
    while (!snapshotGenerations.includes(2) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
    expect(snapshotGenerations).toEqual([1, 2])
    stdin.write("/status\r")
    stdin.write("/cancel recovery test\r")
    const result = await observation
    productShell.finish()

    expect(result.status).toBe("cancelled")
    expect(result.exitCode).toBe(CliExitCode.CANCELLED)
    expect(cancelledFromEpoch).toBe(2)
    expect(streamCalls).toBe(4)
    expect(output.text).toContain("正在重连")
    expect(output.text).toContain("连接 · 已连接")
    expect(output.text).toContain("任务取消已提交")
    expect(stdin.raw).toBeFalse()
  }, 10_000)

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
    expect(output.text).toContain("权限控制当前不可用")
    expect(output.text).toContain("安全完成。")
    expect(stdin.raw).toBeFalse()
  })

  test("keeps status and detach usable while permission custody recovery is slow", async () => {
    const output = new Capture(true)
    const stdin = new TtyInput()
    const productShell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    const running = task("running")
    const api = {
      async ingressCapabilities() {
        return { taskId: running.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async openPermissionSession() { return await new Promise<never>(() => undefined) },
      async runTask() { return await new Promise<never>(() => undefined) },
      async *streamIngress(_taskId: string, _cursor: string, _generation: number, signal?: AbortSignal) {
        await new Promise<void>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal.reason), { once: true })
        })
      },
      async task() { return running },
    } as unknown as CliApi

    productShell.start()
    const observation = observeProductTask({
      api,
      task: running,
      shell: productShell,
      signal: new AbortController().signal,
      resume: true,
    })
    const readyDeadline = Date.now() + 500
    while (!stdin.raw && Date.now() < readyDeadline) await Bun.sleep(5)
    expect(stdin.raw).toBeTrue()
    stdin.write("/status\r")
    const statusDeadline = Date.now() + 500
    while (!output.text.includes("连接 · 已连接") && Date.now() < statusDeadline) await Bun.sleep(5)
    expect(output.text).toContain("任务 · 运行中")
    stdin.write("/exit\r")
    const result = await observation
    productShell.finish()

    expect(result.status).toBe("detached")
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
    expect(output.text).toContain("使用 zyra resume 或 /resume 选择并恢复")
    expect(output.text).not.toContain(`zyra resume ${running.taskId}`)
    expect(stdin.raw).toBeFalse()
  })
})
