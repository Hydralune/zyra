import { describe, expect, test } from "bun:test"
import type {
  TaskListProjection,
  TaskProjection,
} from "../../../packages/core/typed-api-client/src/index.ts"
import type { TaskApi } from "../src/api/task-api.ts"
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
