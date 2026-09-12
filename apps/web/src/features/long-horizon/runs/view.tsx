import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import { formatDuration } from "../../../shell/task-metrics.ts"

/**
 * Live view of real long-horizon runs (sealed SWE-bench tasks).
 *
 * These are not scenario-console runs.  Each one drives an agent against a real
 * repository in a benchmark container: it reads a production codebase, edits it,
 * runs the project's own test suite and iterates under a turn and token budget.
 * They are ordinary tasks in the canonical store, so this panel reads the task
 * list and links to the task detail page instead of owning a transport.
 *
 * The footer names how many candidates are hidden, because a benchmark
 * container is bound when the API starts: without one, starting a run fails
 * closed, and a button that does nothing reads as broken rather than
 * unavailable.
 */

const RUNNING = new Set(["running", "in_progress", "executing"])
const COMPLETED = new Set(["completed", "succeeded"])
const SURFACE_LIMIT = 6

/**
 * A workspace-change task is one that edits real files.  The task-list
 * projection carries the delivery contract's interaction kind precisely so a
 * list can classify itself; reading `metadata.delivery_contract` here instead
 * would silently match nothing, because the list omits the metadata blob.
 */
function isLongHorizonTask(task: TaskProjection): boolean {
  const record = task as TaskProjection & { interactionKind?: unknown }
  return record.interactionKind === "workspace_change"
}

function createdAtOf(task: TaskProjection): number {
  const value = Date.parse(task.createdAt ?? "")
  return Number.isFinite(value) ? value : 0
}

export function LongHorizonRunsPanel({ runtime }: { runtime: WorkbenchRuntime }) {
  const snapshot = useSyncExternalStore(
    runtime.workbench.subscribe,
    runtime.workbench.getSnapshot,
    runtime.workbench.getSnapshot,
  )
  const [detail, setDetail] = useState<Record<string, TaskProjection>>({})

  const tasks = useMemo(
    () =>
      snapshot.list.tasks
        .filter(isLongHorizonTask)
        .sort((left, right) => createdAtOf(right) - createdAtOf(left)),
    [snapshot.list.tasks],
  )
  const visible = tasks.slice(0, SURFACE_LIMIT)

  // The list projection omits node counts, so read the detail record of the few
  // tasks actually rendered.  Reading every long-horizon task would grow without
  // bound as runs accumulate; the cap keeps the panel O(1) in history size.
  const visibleKey = visible.map((task) => `${task.taskId}:${task.updatedAt}`).join("|")
  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    const pending = visible.filter((task) => !detail[task.taskId])
    if (!pending.length) return
    void Promise.all(
      pending.map(async (task) => {
        try {
          const record = await runtime.api.tasks.get(task.taskId, { signal: controller.signal })
          return [task.taskId, record] as const
        } catch {
          // A task that vanished between list and read simply drops out.
          return undefined
        }
      }),
    ).then((records) => {
      if (cancelled) return
      const found = records.filter(
        (item): item is readonly [string, TaskProjection] => item !== undefined,
      )
      if (found.length) setDetail((current) => ({ ...current, ...Object.fromEntries(found) }))
    })
    return () => {
      cancelled = true
      controller.abort()
    }
    // `visibleKey` folds identity and freshness together so a run whose counters
    // moved refetches without re-running on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtime, visibleKey])

  if (!tasks.length) return null

  return (
    <section className="long-horizon-runs" aria-labelledby="long-horizon-runs-heading">
      <div className="long-horizon-runs-heading">
        <h2 id="long-horizon-runs-heading">真实长程任务</h2>
        <button
          type="button"
          className="settings-text-button"
          onClick={() => void runtime.workbench.refreshTasks({ preserveOnError: true })}
        >
          刷新
        </button>
      </div>
      <p className="long-horizon-runs-copy">
        这些任务运行在真实代码仓库中：读取工程、修改生产代码、执行项目自带测试，
        并在轮次与 token 上限内自主推进。点开可查看真实改动与逐轮工具调用。
      </p>
      <ul className="long-horizon-runs-list">
        {visible.map((task) => {
          const record = detail[task.taskId] ?? task
          const nodes = record.planNodes ?? []
          const completed = nodes.filter((node) => COMPLETED.has(node.status)).length
          const running = RUNNING.has(record.status)
          const elapsed = formatDuration(
            (Number.isFinite(Date.parse(record.updatedAt ?? "")) ? Date.parse(record.updatedAt) : Date.now())
            - createdAtOf(record),
          )
          return (
            <li key={task.taskId} className="long-horizon-run" data-phase={running ? "running" : record.status}>
              <button
                type="button"
                onClick={() => runtime.router.openTask(task.taskId)}
                title={task.userGoal}
              >
                <span className="long-horizon-run-title">
                  {task.userGoal.length > 96 ? `${task.userGoal.slice(0, 96)}…` : task.userGoal}
                </span>
                <span className="long-horizon-run-meta">
                  <span className="long-horizon-run-status">{record.status}</span>
                  <span>{nodes.length ? `${completed}/${nodes.length} 步骤` : "步骤读取中…"}</span>
                  <span>{elapsed}</span>
                  <span className="long-horizon-run-id">{task.taskId}</span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      {tasks.length > visible.length ? (
        <p className="long-horizon-runs-footnote">
          另有 {tasks.length - visible.length} 个历史长程任务未在此展示，可在会话列表或任务详情中查看。
        </p>
      ) : null}
    </section>
  )
}
