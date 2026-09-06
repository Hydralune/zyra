import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"
import { browserTaskLiveSyncEnvironment, type TaskLiveSyncEnvironment } from "./task-live-sync.ts"
import type { WorkbenchController } from "./workbench-controller.ts"

function latest(tasks: readonly TaskProjection[], sessionId: string): TaskProjection | undefined {
  return tasks.filter((task) => task.sessionId === sessionId)
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0]
}

/** Discover CLI-created tasks even when Web is showing a finished conversation. */
export class TaskListSync {
  readonly #environment: TaskLiveSyncEnvironment
  readonly #unlisten: () => void
  #timer?: unknown
  #closed = false
  #inFlight = false
  #failures = 0

  constructor(readonly workbench: WorkbenchController, readonly options: {
    environment?: TaskLiveSyncEnvironment
    onNewTurn?: (task: TaskProjection) => void
  } = {}) {
    this.#environment = options.environment ?? browserTaskLiveSyncEnvironment()
    this.#unlisten = this.#environment.listen(() => {
      this.#clear()
      if (!this.#environment.hidden() && this.#environment.online()) void this.#refresh()
    })
    this.#schedule()
  }

  close(): void { this.#closed = true; this.#clear(); this.#unlisten() }

  #clear(): void {
    if (this.#timer !== undefined) this.#environment.clearTimeout(this.#timer)
    this.#timer = undefined
  }

  #schedule(): void {
    this.#clear()
    if (this.#closed || this.#environment.hidden() || !this.#environment.online()) return
    this.#timer = this.#environment.setTimeout(() => {
      this.#timer = undefined
      void this.#refresh()
    }, Math.min(30_000, 3_000 * 2 ** Math.min(4, this.#failures)))
  }

  async #refresh(): Promise<void> {
    if (this.#closed || this.#inFlight) return
    const before = this.workbench.getSnapshot()
    if (!before.transportEnabled || before.list.phase === "loading") { this.#schedule(); return }
    const selected = before.detail.task
    const followedLatest = selected?.sessionId
      && latest(before.list.tasks, selected.sessionId)?.taskId === selected.taskId
    this.#inFlight = true
    try {
      const result = await this.workbench.refreshTasks({ status: before.list.status, background: true, preserveOnError: true })
      this.#failures = result.phase === "error" ? this.#failures + 1 : 0
      if (!this.#closed && followedLatest && selected?.sessionId
        && this.workbench.getSnapshot().selectedTaskId === selected.taskId) {
        const next = latest(result.tasks, selected.sessionId)
        if (next && Date.parse(next.createdAt) > Date.parse(selected.createdAt)) this.options.onNewTurn?.(next)
      }
    } catch { this.#failures += 1 }
    finally { this.#inFlight = false; this.#schedule() }
  }
}
