import { describe, expect, test } from "bun:test"
import type {
  TaskListProjection,
  TaskProjection,
} from "../../../packages/core/typed-api-client/src/index.ts"
import type { TaskApi } from "../src/api/task-api.ts"
import type {
  IngressBatch,
  IngressLiveFrame,
  JsonObject,
} from "../src/events/ingress/index.ts"
import { WorkbenchController } from "../src/shell/workbench-controller.ts"
import {
  TaskLiveSync,
  type TaskLiveSyncEnvironment,
} from "../src/shell/task-live-sync.ts"

function task(
  taskId = "task_live_001",
  status = "running",
  updatedAt = "2026-08-04T01:00:00.000Z",
): TaskProjection {
  const terminal = ["completed", "failed", "cancelled", "succeeded"].includes(status)
  return {
    taskId,
    runId: `run_${taskId.slice(5)}`,
    sessionId: "session_live_001",
    rootNodeId: "node_root_001",
    userGoal: "Keep the conversation live",
    status,
    createdAt: "2026-08-04T00:00:00.000Z",
    updatedAt,
    planNodes: [],
    artifacts: [],
    metadata: {},
    binding: { taskId, runId: `run_${taskId.slice(5)}` },
    terminal,
    active: !terminal,
  }
}

class TestEnvironment implements TaskLiveSyncEnvironment {
  time = 1_000
  hiddenValue = false
  onlineValue = true
  #sequence = 1
  #timers: { id: number; at: number; callback: () => void }[] = []
  #listeners = new Set<() => void>()

  now(): number {
    return this.time
  }

  setTimeout(callback: () => void, delayMs: number): unknown {
    const id = this.#sequence++
    this.#timers.push({ id, at: this.time + delayMs, callback })
    return id
  }

  clearTimeout(handle: unknown): void {
    this.#timers = this.#timers.filter((timer) => timer.id !== handle)
  }

  hidden(): boolean {
    return this.hiddenValue
  }

  online(): boolean {
    return this.onlineValue
  }

  listen(listener: () => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  get pending(): number {
    return this.#timers.length
  }

  notify(): void {
    for (const listener of [...this.#listeners]) listener()
  }

  async advance(ms: number): Promise<void> {
    this.time += ms
    const due = this.#timers.filter((timer) => timer.at <= this.time)
    this.#timers = this.#timers.filter((timer) => timer.at > this.time)
    for (const timer of due) timer.callback()
    await settle()
  }
}

function settle(): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, 0))
}

function assistantPresentation(
  phase: "started" | "completed",
  text?: string,
): Readonly<JsonObject> {
  return Object.freeze({
    schema: "zyra.product-presentation/v1",
    kind: "assistant",
    phase,
    identity: "answer_live_001",
    label: "Assistant",
    streamId: "stream_live_001",
    ...(text === undefined ? {} : { text }),
  })
}

function ingressBatch(
  generation: number,
  phase: "started" | "completed",
  text?: string,
): IngressBatch {
  return {
    taskId: "task_live_001",
    generation,
    events: [],
    presentations: [{
      eventId: `event_${phase}_${generation}`,
      eventType: phase === "started" ? "runtime.text.started" : "runtime.text.ended",
      sequence: phase === "started" ? 1 : 2,
      presentation: assistantPresentation(phase, text),
    }],
    receipts: [],
    fromSequence: 0,
    sequence: phase === "started" ? 1 : 2,
    highWatermark: 2,
    receivedAt: 1_000,
    transport: "sse",
    snapshot: false,
    caughtUp: true,
  }
}

function liveFrame(
  liveSequence: number,
  text: string,
  generation = 1,
): IngressLiveFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "live",
    source: "runtime-live-bus",
    generation,
    taskId: "task_live_001",
    sequence: 1,
    liveSequence,
    eventId: `live_${generation}_${liveSequence}`,
    eventType: "runtime.text.delta",
    observedAtMs: 1_000 + liveSequence,
    presentation: Object.freeze({
      schema: "zyra.product-presentation/v1",
      kind: "assistant",
      phase: "delta",
      identity: "answer_live_001",
      label: "Assistant",
      streamId: "stream_live_001",
      text,
    }),
  }
}

function harness(options: { values?: TaskProjection[] } = {}) {
  const state = { current: options.values?.[0] ?? task(), failure: undefined as Error | undefined, reads: 0 }
  const api = {
    list: async (): Promise<TaskListProjection> => ({
      tasks: [state.current],
      total: 1,
    }),
    get: async () => {
      state.reads += 1
      if (state.failure) throw state.failure
      return state.current
    },
  } as unknown as TaskApi
  const workbench = new WorkbenchController(api)
  const environment = new TestEnvironment()
  const sync = new TaskLiveSync(workbench, {
    environment,
    activeIntervalMs: 1_000,
    pendingIntervalMs: 4_000,
  })
  return { api, workbench, environment, sync, state }
}

describe("task live sync", () => {
  test("renders exact live product text then converges to canonical final task", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")

    value.sync.observeBatch(ingressBatch(1, "started"))
    value.sync.observeLive(liveFrame(1, "你好"))
    value.sync.observeLive(liveFrame(2, " world\n"))
    value.sync.observeLive(liveFrame(2, "duplicate"))
    expect(value.sync.getSnapshot().assistant).toMatchObject({
      text: "你好 world\n",
      partial: false,
      settling: false,
      firstLiveSequence: 1,
      lastLiveSequence: 2,
    })

    await value.environment.advance(75)
    expect(value.state.reads).toBe(2)

    value.state.current = task(
      "task_live_001",
      "completed",
      "2026-08-04T01:05:00.000Z",
    )
    value.state.current.metadata = { final_answer: "你好 world\n" }
    value.sync.observeBatch(ingressBatch(1, "completed", "你好 world\n"))
    expect(value.sync.getSnapshot().assistant?.settling).toBe(true)
    value.sync.observeLive(liveFrame(3, " world\n"))
    value.sync.observeBatch(ingressBatch(1, "started"))
    expect(value.sync.getSnapshot().assistant?.text).toBe("你好 world\n")
    expect(value.sync.getSnapshot().assistant?.settling).toBe(true)
    await value.environment.advance(75)
    expect(value.workbench.getSnapshot().detail.task?.metadata.final_answer)
      .toBe("你好 world\n")
    expect(value.sync.getSnapshot().assistant).toBeUndefined()
    expect(value.sync.getSnapshot().live).toBe(false)
    value.sync.close()
    value.workbench.close()
  })

  test("marks a delta-first stream partial and resets it across ingress generations", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")
    value.sync.observeLive(liveFrame(7, "中途接入"))
    expect(value.sync.getSnapshot().assistant).toMatchObject({
      text: "中途接入",
      partial: true,
      generation: 1,
    })

    value.sync.observeLive(liveFrame(1, "新 generation", 2))
    expect(value.sync.getSnapshot().assistant).toMatchObject({
      text: "新 generation",
      partial: true,
      generation: 2,
      firstLiveSequence: 1,
    })
    value.sync.observeLive(liveFrame(99, "stale", 1))
    expect(value.sync.getSnapshot().assistant?.text).toBe("新 generation")
    value.sync.close()
    value.workbench.close()
  })

  test("polls a running task and stops as soon as the backend reports terminal", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")
    expect(value.sync.getSnapshot()).toMatchObject({ live: true, paused: false })
    expect(value.state.reads).toBe(1)

    await value.environment.advance(1_000)
    expect(value.state.reads).toBe(2)
    // A background refresh keeps the rendered projection instead of dropping
    // the surface back into a loading skeleton.
    expect(value.workbench.getSnapshot().detail.phase).toBe("ready")
    expect(value.workbench.getSnapshot().detail.syncing).toBeFalsy()

    value.state.current = task("task_live_001", "completed", "2026-08-04T01:05:00.000Z")
    await value.environment.advance(1_000)
    expect(value.state.reads).toBe(3)
    expect(value.sync.getSnapshot().live).toBe(false)
    expect(value.environment.pending).toBe(0)

    await value.environment.advance(10_000)
    expect(value.state.reads).toBe(3)
    value.sync.close()
    value.workbench.close()
  })

  test("pauses while the tab is hidden and catches up when it becomes visible", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")

    value.environment.hiddenValue = true
    value.environment.notify()
    expect(value.sync.getSnapshot()).toMatchObject({ live: true, paused: true })
    await value.environment.advance(5_000)
    expect(value.state.reads).toBe(1)

    value.environment.hiddenValue = false
    value.environment.notify()
    await settle()
    expect(value.state.reads).toBe(2)
    expect(value.sync.getSnapshot().paused).toBe(false)
    value.sync.close()
    value.workbench.close()
  })

  test("a failed background refresh marks the projection stale without erasing it", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")

    value.state.failure = new Error("network disconnected")
    await value.environment.advance(1_000)
    const detail = value.workbench.getSnapshot().detail
    expect(detail.phase).toBe("ready")
    expect(detail.task?.taskId).toBe("task_live_001")
    expect(detail.staleSince).toBeGreaterThan(0)
    expect(value.sync.getSnapshot().consecutiveFailures).toBe(1)

    // Recovery clears the stale marker and resets the backoff.
    value.state.failure = undefined
    await value.sync.refreshNow()
    expect(value.workbench.getSnapshot().detail.staleSince).toBeUndefined()
    expect(value.sync.getSnapshot().consecutiveFailures).toBe(0)
    value.sync.close()
    value.workbench.close()
  })

  test("a missing task still fails closed instead of showing a stale conversation", async () => {
    const value = harness()
    await value.workbench.loadTask("task_live_001")
    value.sync.bind("task_live_001")

    value.state.failure = new Error("404 task not found")
    await value.environment.advance(1_000)
    expect(value.workbench.getSnapshot().detail.phase).toBe("not-found")
    expect(value.workbench.getSnapshot().detail.task).toBeUndefined()
    expect(value.sync.getSnapshot().live).toBe(false)
    value.sync.close()
    value.workbench.close()
  })

  test("a list refresh never cancels the detail read that is already in flight", async () => {
    let releaseDetail: (() => void) | undefined
    const detailValue = task("task_live_001", "running", "2026-08-04T02:00:00.000Z")
    const listValue = task("task_live_001", "running", "2026-08-04T01:00:00.000Z")
    const api = {
      list: async (): Promise<TaskListProjection> => ({ tasks: [listValue], total: 1 }),
      get: async () => {
        await new Promise<void>((resolve) => { releaseDetail = resolve })
        return detailValue
      },
    } as unknown as TaskApi
    const workbench = new WorkbenchController(api)
    const detail = workbench.loadTask("task_live_001")
    await settle()
    await workbench.refreshTasks()
    releaseDetail?.()
    expect((await detail).task?.updatedAt).toBe("2026-08-04T02:00:00.000Z")
    expect(workbench.getSnapshot().detail.task?.updatedAt).toBe("2026-08-04T02:00:00.000Z")
    workbench.close()
  })
})
