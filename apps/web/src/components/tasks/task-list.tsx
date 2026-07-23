import { useMemo, useRef, useState } from "react"
import type { TaskProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskListState } from "../../shell/workbench-controller.ts"
import { EmptyState, ErrorState, LoadingState, PhaseRegion } from "../status/request-state.tsx"
import {
  CollectionWindowController,
  nextCollectionIndex,
} from "../../shell/collection-window.ts"
import { queryTasks, taskQuerySummary, type TaskSort } from "../../shell/task-query.ts"

function formatRelativeTime(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return "unknown"
  const seconds = Math.round((timestamp - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second")
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute")
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour")
  return formatter.format(Math.round(hours / 24), "day")
}

function taskProgress(task: TaskProjection): { completed: number; total: number; percentage: number } {
  const total = task.planNodes.length
  const completed = task.planNodes.filter((node) =>
    ["completed", "succeeded", "verified"].includes(node.status),
  ).length
  return {
    completed,
    total,
    percentage: total ? Math.round((completed / total) * 100) : task.terminal ? 100 : 0,
  }
}

function TaskRow({
  task,
  selected,
  onOpen,
}: {
  task: TaskProjection
  selected: boolean
  onOpen: () => void
}) {
  const progress = taskProgress(task)
  return (
    <button
      type="button"
      className="task-row"
      data-task-row
      aria-current={selected ? "true" : undefined}
      onClick={onOpen}
    >
      <span className={`status-marker status-${task.status}`} aria-hidden="true" />
      <span className="task-row-content">
        <span className="task-row-title">{task.userGoal || "Untitled task"}</span>
        <span className="task-row-meta">
          <span>{task.status}</span>
          <span aria-hidden="true">·</span>
          <span>{formatRelativeTime(task.updatedAt)}</span>
        </span>
        <span className="task-progress" aria-label={`${progress.completed} of ${progress.total} plan nodes complete`}>
          <span className="task-progress-track">
            <span style={{ width: `${progress.percentage}%` }} />
          </span>
          <span>{progress.percentage}%</span>
        </span>
      </span>
    </button>
  )
}

export function TaskList({
  runtime,
  state,
  selectedTaskId,
}: {
  runtime: WorkbenchRuntime
  state: TaskListState
  selectedTaskId?: string
}) {
  const [queryText, setQueryText] = useState("")
  const [sort, setSort] = useState<TaskSort>("updated-desc")
  const queriedTasks = useMemo(
    () => queryTasks(state.tasks, { text: queryText, sort }),
    [queryText, sort, state.tasks],
  )
  const scrollRef = useRef<HTMLDivElement>(null)
  const windowController = useMemo(
    () => new CollectionWindowController({ itemHeight: 88, overscan: 6 }),
    [],
  )
  const [windowRevision, setWindowRevision] = useState(0)
  const viewport = scrollRef.current?.clientHeight ?? 600
  const windowState = windowController.update({
    itemCount: queriedTasks.length,
    viewportHeight: viewport,
    scrollTop: scrollRef.current?.scrollTop ?? 0,
  })
  const virtualized = queriedTasks.length > 50
  const visibleTasks = virtualized
    ? queriedTasks.slice(windowState.overscanStart, windowState.overscanEnd)
    : queriedTasks
  const statusCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const task of state.tasks) counts.set(task.status, (counts.get(task.status) ?? 0) + 1)
    return [...counts.entries()].sort((left, right) => right[1] - left[1])
  }, [state.tasks])

  const changeFilter = (status?: string) => {
    runtime.router.openTasks({ status })
    void runtime.workbench.refreshTasks({ status })
  }

  return (
    <section className="task-list-panel" aria-labelledby="task-list-heading">
      <header className="panel-heading">
        <div>
          <p className="eyebrow">Runtime work</p>
          <h1 id="task-list-heading" tabIndex={-1}>Tasks</h1>
        </div>
        <button
          className="button button-primary"
          type="button"
          onClick={() => runtime.router.openNewTask()}
        >
          New task
        </button>
      </header>

      <div className="task-filters" aria-label="Filter tasks">
        <button
          type="button"
          aria-pressed={!state.status}
          onClick={() => changeFilter()}
        >
          All <span>{state.total}</span>
        </button>
        {statusCounts.slice(0, 5).map(([status, count]) => (
          <button
            key={status}
            type="button"
            aria-pressed={state.status === status}
            onClick={() => changeFilter(status)}
          >
            {status} <span>{count}</span>
          </button>
        ))}
      </div>

      <div className="task-search">
        <label>
          <span className="sr-only">Search tasks</span>
          <input
            type="search"
            value={queryText}
            placeholder="Search task, run, node, artifact…"
            onChange={(event) => setQueryText(event.target.value)}
          />
        </label>
        <select
          aria-label="Sort tasks"
          value={sort}
          onChange={(event) => setSort(event.target.value as TaskSort)}
        >
          <option value="updated-desc">Recently updated</option>
          <option value="updated-asc">Oldest update</option>
          <option value="created-desc">Recently created</option>
          <option value="goal">Goal</option>
          <option value="status">Status</option>
        </select>
        <output aria-live="polite">
          {taskQuerySummary(state.tasks, queriedTasks, { text: queryText, sort })}
        </output>
      </div>

      <PhaseRegion
        phase={state.phase}
        loading={<LoadingState title="Loading tasks" detail="Reading canonical task projections…" />}
        empty={
          <EmptyState
            title={state.status ? `No ${state.status} tasks` : "No tasks yet"}
            detail={
              state.status
                ? "Change the filter or create a task."
                : "Describe a goal below to create the first real task."
            }
            action={
              <button className="button button-primary" type="button" onClick={() => runtime.focus.focusId("workbench-command-input")}>
                Focus command input
              </button>
            }
          />
        }
        error={
          <ErrorState
            title="Tasks unavailable"
            failure={state.failure}
            onRetry={() => void runtime.workbench.refreshTasks({
              status: state.status,
              preserveOnError: true,
            })}
          />
        }
      >
        <div
          ref={scrollRef}
          className="task-list-scroll"
          role="list"
          aria-label="Task results"
          tabIndex={0}
          onScroll={() => setWindowRevision((revision) => revision + 1)}
          onKeyDown={(event) => {
            const current = Math.max(
              0,
              queriedTasks.findIndex((task) => task.taskId === selectedTaskId),
            )
            const action =
              event.key === "ArrowDown" ? "next"
                : event.key === "ArrowUp" ? "previous"
                  : event.key === "Home" ? "first"
                    : event.key === "End" ? "last"
                      : event.key === "PageDown" ? "page-next"
                        : event.key === "PageUp" ? "page-previous"
                          : undefined
            if (!action) return
            event.preventDefault()
            const next = nextCollectionIndex(
              current,
              queriedTasks.length,
              action,
              Math.max(1, Math.floor(viewport / 88)),
            )
            const task = queriedTasks[next]
            if (!task) return
            runtime.router.openTask(task.taskId)
            if (virtualized && scrollRef.current) {
              scrollRef.current.scrollTop = windowController.scrollToIndex(next)
              setWindowRevision(windowRevision + 1)
            }
          }}
        >
          {virtualized && windowState.beforeHeight ? (
            <div aria-hidden="true" style={{ height: windowState.beforeHeight }} />
          ) : null}
          {visibleTasks.map((task) => (
            <div role="listitem" key={task.taskId}>
              <TaskRow
                task={task}
                selected={selectedTaskId === task.taskId}
                onOpen={() => runtime.router.openTask(task.taskId)}
              />
            </div>
          ))}
          {virtualized && windowState.afterHeight ? (
            <div aria-hidden="true" style={{ height: windowState.afterHeight }} />
          ) : null}
          {state.cursor ? (
            <button
              className="button button-secondary load-more"
              type="button"
              disabled={state.phase === "loading"}
              onClick={() => void runtime.workbench.loadMoreTasks()}
            >
              Load more
            </button>
          ) : null}
        </div>
      </PhaseRegion>
    </section>
  )
}
