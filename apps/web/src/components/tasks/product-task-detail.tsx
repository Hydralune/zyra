import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type {
  ArtifactProjection,
  PlanNodeProjection,
  TaskProjection,
} from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskDetailState } from "../../shell/workbench-controller.ts"
import { taskActionSet } from "../../shell/task-action-policy.ts"
import { formatDuration, taskMetrics } from "../../shell/task-metrics.ts"
import { EmptyState, ErrorState, LoadingState, PhaseRegion } from "../status/request-state.tsx"
import { TaskDetail } from "./task-detail.tsx"

const COMPLETED_NODE_STATES = new Set(["completed", "succeeded", "verified"])

export interface ProductTaskProgress {
  completed: number
  total: number
  percentage: number
}

export function productTaskProgress(
  task: Pick<TaskProjection, "planNodes" | "terminal">,
): ProductTaskProgress {
  const total = task.planNodes.length
  const completed = task.planNodes.filter((node) =>
    COMPLETED_NODE_STATES.has(node.status.toLowerCase()),
  ).length
  return {
    completed,
    total,
    percentage: total
      ? Math.round((completed / total) * 100)
      : task.terminal ? 100 : 0,
  }
}

export function productConversationTasks(
  selected: TaskProjection,
  tasks: readonly TaskProjection[],
): TaskProjection[] {
  const sessionId = selected.sessionId
  const values = new Map<string, TaskProjection>()
  if (sessionId) {
    for (const task of tasks) {
      if (task.sessionId === sessionId) values.set(task.taskId, task)
    }
  }
  values.set(selected.taskId, selected)
  return [...values.values()].sort((left, right) => {
    const created = Date.parse(left.createdAt) - Date.parse(right.createdAt)
    return created || left.taskId.localeCompare(right.taskId)
  })
}

export function extractArtifactText(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  const content = (value as Record<string, unknown>).content
  if (!content || typeof content !== "object" || Array.isArray(content)) return undefined
  const text = (content as Record<string, unknown>).text
  return typeof text === "string" && text.trim() ? text : undefined
}

function resultSummary(task: TaskProjection): string | undefined {
  const candidates = [
    task.metadata.final_answer,
    task.metadata.finalAnswer,
    task.metadata.result,
    task.metadata.summary,
    task.metadata.output,
  ]
  return candidates.find((value): value is string =>
    typeof value === "string" && Boolean(value.trim()),
  )
}

function statusCopy(task: TaskProjection): {
  eyebrow: string
  title: string
  detail: string
  tone: "active" | "success" | "danger" | "idle"
} {
  const normalized = task.status.toLowerCase()
  if (["completed", "succeeded", "verified"].includes(normalized)) {
    const summary = resultSummary(task)
    const goalContract = task.metadata.goal_contract
    const requiresDirectResponse = Boolean(
      goalContract
      && typeof goalContract === "object"
      && !Array.isArray(goalContract)
      && (goalContract as Record<string, unknown>).kind === "direct_response",
    )
    if (requiresDirectResponse && !summary) {
      return {
        eyebrow: "结果未闭环",
        title: "没有生成有效回答",
        detail: "执行步骤已经结束，但最终回答没有满足用户目标。请查看运行详情。",
        tone: "danger",
      }
    }
    if (!summary && task.artifacts.length) {
      return {
        eyebrow: "执行已结束",
        title: "交付物已生成",
        detail: `已生成 ${task.artifacts.length} 个交付物，但运行时没有提供面向用户的最终总结。`,
        tone: "idle",
      }
    }
    return {
      eyebrow: "Zyra 已完成",
      title: "任务已完成",
      detail: "最终回答已通过任务目标验证。",
      tone: "success",
    }
  }
  if (["failed", "error", "cancelled", "canceled"].includes(normalized)) {
    return {
      eyebrow: "需要处理",
      title: normalized.includes("cancel") ? "任务已取消" : "任务未能完成",
      detail: "打开运行详情可以查看失败原因、恢复建议和审计证据。",
      tone: "danger",
    }
  }
  if (task.active) {
    return {
      eyebrow: "Zyra 正在工作",
      title: "正在推进任务",
      detail: "任务会持续运行，进度与交付物将在这里自动更新。",
      tone: "active",
    }
  }
  return {
    eyebrow: "等待运行",
    title: "任务已创建",
    detail: "运行时正在准备计划和执行资源。",
    tone: "idle",
  }
}

function relativeTime(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  const seconds = Math.round((timestamp - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second")
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute")
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour")
  return formatter.format(Math.round(hours / 24), "day")
}

function nodeLabel(node: PlanNodeProjection): string {
  return node.title || node.nodeId
}

function artifactLabel(artifact: ArtifactProjection): string {
  return artifact.title || artifact.path || artifact.artifactId
}

function TaskControls({
  runtime,
  task,
  onAdvanced,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  onAdvanced: () => void
}) {
  const actions = taskActionSet(task, {
    lifecycleBusy: runtime.api.lifecycle.inFlight().length > 0,
    transportEnabled: runtime.workbench.getSnapshot().transportEnabled,
  })
  const completed = ["completed", "succeeded", "verified"].includes(task.status.toLowerCase())
  return (
    <div className="product-task-controls">
      <button
        className="product-button product-button-quiet"
        type="button"
        disabled={!actions.refresh.allowed}
        onClick={() => void runtime.workbench.loadTask(task.taskId)}
      >
        刷新
      </button>
      {actions.cancel.allowed ? (
        <button
          className="product-button product-button-danger"
          type="button"
          onClick={() => runtime.overlays.open({
            kind: "task-cancel",
            title: "Cancel task",
            replaceKind: true,
            payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
          })}
        >
          停止
        </button>
      ) : null}
      {actions.resume.allowed && !completed ? (
        <button
          className="product-button product-button-primary"
          type="button"
          onClick={() => runtime.overlays.open({
            kind: "task-resume",
            title: "Resume task",
            replaceKind: true,
            payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
          })}
        >
          重新运行
        </button>
      ) : null}
      <button className="product-button product-button-quiet" type="button" onClick={onAdvanced}>
        运行详情
      </button>
    </div>
  )
}

function ArtifactPreview({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const selected = task.artifacts.at(-1)
  const [preview, setPreview] = useState<{
    phase: "idle" | "loading" | "ready" | "error"
    text?: string
  }>({ phase: "idle" })
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!selected || !open) {
      setPreview({ phase: "idle" })
      return
    }
    const controller = new AbortController()
    setPreview({ phase: "loading" })
    void runtime.api.tasks.artifactContent(task.taskId, selected.artifactId, {
      length: 48 * 1024,
      purpose: "preview",
      signal: controller.signal,
      timeoutMs: 15_000,
    }).then((value) => {
      const text = extractArtifactText(value)
      setPreview({ phase: "ready", ...(text ? { text } : {}) })
    }).catch((error) => {
      if (controller.signal.aborted) return
      setPreview({
        phase: "error",
        text: error instanceof Error ? error.message : String(error),
      })
    })
    return () => controller.abort()
  }, [open, runtime, selected?.artifactId, task.taskId])

  if (!task.artifacts.length) return null
  return (
    <details
      className="product-disclosure product-deliverables"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="product-disclosure-icon" aria-hidden="true">◇</span>
        <span><strong>交付物</strong><small>{task.artifacts.length} 个文件与结果</small></span>
        <span className="product-disclosure-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="product-disclosure-content">
        <div className="product-artifact-tabs" role="list">
          {task.artifacts.map((artifact, index) => (
            <span role="listitem" key={artifact.artifactId} data-active={index === task.artifacts.length - 1 || undefined}>
              <span aria-hidden="true">{artifact.kind === "code" ? "⌘" : "◫"}</span>
              {artifactLabel(artifact)}
            </span>
          ))}
        </div>
        {selected ? (
          <div className="product-artifact-preview" data-phase={preview.phase}>
            <header>
              <div>
                <strong>{artifactLabel(selected)}</strong>
                <span>{selected.mediaType || selected.kind}</span>
              </div>
              <span className="product-verified">可追溯</span>
            </header>
            {preview.phase === "loading" ? <p className="product-muted">正在读取交付物…</p> : null}
            {preview.phase === "error" ? <p className="product-error-copy">暂时无法预览：{preview.text}</p> : null}
            {preview.phase === "ready" && preview.text ? <pre><code>{preview.text}</code></pre> : null}
            {preview.phase === "ready" && !preview.text ? (
              <p className="product-muted">此交付物不支持内联预览，可在运行详情中检查。</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </details>
  )
}

function ConversationTurn({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const progress = productTaskProgress(task)
  const metrics = taskMetrics(task)
  const copy = statusCopy(task)
  const summary = resultSummary(task)
  return (
    <div className="product-turn" data-task-status={task.status}>
      <article className="product-message product-message-user">
        <div className="product-message-body">
          <span>你</span>
          <p>{task.userGoal}</p>
        </div>
      </article>

      <article className="product-message product-message-zyra" data-tone={copy.tone}>
        <div className="product-avatar product-avatar-zyra" aria-hidden="true">Z</div>
        <div className="product-message-body">
          <div className="product-agent-meta">
            <strong>Zyra</strong>
            <time dateTime={task.updatedAt}>{relativeTime(task.updatedAt)}</time>
          </div>
          {summary ? (
            <p className="product-answer">{summary}</p>
          ) : task.active ? (
            <div className="product-working-copy" role="status">
              <span className="product-working-spinner" aria-hidden="true" />
              <span><strong>{copy.title}</strong><small>{copy.detail}</small></span>
            </div>
          ) : (
            <div className="product-empty-answer" data-tone={copy.tone}>
              <strong>{copy.title}</strong>
              <span>{copy.detail}</span>
            </div>
          )}

          <details className="product-disclosure product-run-summary" open={task.active || copy.tone === "danger" ? true : undefined}>
            <summary>
              <span className="product-disclosure-icon" data-tone={copy.tone} aria-hidden="true">
                {task.active ? "·" : copy.tone === "danger" ? "!" : "✓"}
              </span>
              <span>
                <strong>{copy.title}</strong>
                <small>{progress.total ? `${progress.completed}/${progress.total} 步骤` : copy.eyebrow} · {formatDuration(metrics.elapsedMs)}</small>
              </span>
              <span className="product-disclosure-chevron" aria-hidden="true">⌄</span>
            </summary>
            <div className="product-disclosure-content">
              <div className="product-progress" aria-label={`任务进度 ${progress.percentage}%`}>
                <span style={{ width: `${progress.percentage}%` }} />
              </div>
              <div className="product-result-facts">
                <span><strong>{progress.completed}/{progress.total}</strong> 步骤</span>
                <span><strong>{task.artifacts.length}</strong> 交付物</span>
                <span><strong>{formatDuration(metrics.elapsedMs)}</strong> 用时</span>
              </div>
              {task.planNodes.length ? (
                <ol className="product-plan-list">
                  {task.planNodes.map((node) => (
                    <li key={node.nodeId} data-status={node.status}>
                      <span className="product-step-icon" aria-hidden="true">
                        {COMPLETED_NODE_STATES.has(node.status.toLowerCase()) ? "✓" : node.status === "running" ? "·" : ""}
                      </span>
                      <span><strong>{nodeLabel(node)}</strong><small>{node.description || node.status}</small></span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </div>
          </details>
          <ArtifactPreview runtime={runtime} task={task} />
        </div>
      </article>
    </div>
  )
}

function ProductDetailContent({
  runtime,
  state,
  task,
  tasks,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  task: TaskProjection
  tasks: readonly TaskProjection[]
}) {
  const [advanced, setAdvanced] = useState(false)
  const copy = statusCopy(task)
  const timeline = useMemo(
    () => productConversationTasks(task, tasks),
    [task, tasks],
  )
  const scrollRef = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)

  const jumpToLatest = useCallback((behavior: ScrollBehavior = "smooth") => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior })
    setAtBottom(true)
  }, [])

  const updateScrollState = useCallback(() => {
    const element = scrollRef.current
    if (!element) return
    setAtBottom(element.scrollHeight - element.scrollTop - element.clientHeight < 48)
  }, [])

  useEffect(() => {
    const frame = requestAnimationFrame(() => jumpToLatest("auto"))
    return () => cancelAnimationFrame(frame)
  }, [jumpToLatest, task.taskId])

  useEffect(() => {
    if (!atBottom) return
    const frame = requestAnimationFrame(() => jumpToLatest("smooth"))
    return () => cancelAnimationFrame(frame)
  }, [atBottom, jumpToLatest, timeline.length, task.status, task.updatedAt])

  useEffect(() => {
    if (!advanced) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAdvanced(false)
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [advanced])

  return (
    <section className="product-task-view" data-task-status={task.status}>
      <div className="product-task-scroll" ref={scrollRef} onScroll={updateScrollState}>
        <header className="product-session-toolbar">
          <div className="product-session-status">
            <span className="product-status-dot" data-tone={copy.tone} aria-hidden="true" />
            <span><strong>{copy.eyebrow}</strong><small>{timeline.length > 1 ? `${timeline.length} 轮对话` : relativeTime(task.updatedAt)}</small></span>
          </div>
          <TaskControls runtime={runtime} task={task} onAdvanced={() => setAdvanced(true)} />
        </header>

        <div className="product-conversation" aria-label="会话消息">
          {timeline.map((turn) => <ConversationTurn key={turn.taskId} runtime={runtime} task={turn} />)}
        </div>
      </div>

      {!atBottom ? (
        <button className="product-jump-latest" type="button" onClick={() => jumpToLatest()}>
          ↓ 跳到最新
        </button>
      ) : null}

      {advanced ? (
        <div className="advanced-drawer" role="dialog" aria-modal="true" aria-label="高级运行详情">
          <button className="advanced-drawer-backdrop" type="button" aria-label="关闭运行详情" onClick={() => setAdvanced(false)} />
          <div className="advanced-drawer-panel">
            <header className="advanced-drawer-header">
              <div>
                <p>Advanced workbench</p>
                <h2>运行详情与证据</h2>
              </div>
              <button type="button" onClick={() => setAdvanced(false)} aria-label="关闭运行详情">×</button>
            </header>
            <div className="advanced-drawer-content">
              <TaskDetail runtime={runtime} state={state} />
            </div>
          </div>
        </div>
      ) : null}
    </section>
  )
}

export function ProductTaskDetail({
  runtime,
  state,
  tasks = [],
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  tasks?: readonly TaskProjection[]
}) {
  if (!state.taskId && state.phase === "idle") {
    return (
      <section className="product-route-state">
        <EmptyState title="选择一个任务" detail="从左侧最近任务中选择，或创建一个新任务。" />
      </section>
    )
  }
  return (
    <PhaseRegion
      phase={state.phase}
      loading={<LoadingState title="正在载入任务" detail={state.taskId} />}
      error={
        state.phase === "not-found" ? (
          <EmptyState
            title="没有找到任务"
            detail="该任务不在当前运行时的可见范围内。"
            action={<button className="product-button product-button-quiet" type="button" onClick={() => runtime.router.openTasks()}>返回首页</button>}
          />
        ) : (
          <ErrorState
            title="暂时无法读取任务"
            failure={state.failure}
            onRetry={state.taskId ? () => void runtime.workbench.loadTask(state.taskId!) : undefined}
          />
        )
      }
    >
      {state.task ? <ProductDetailContent runtime={runtime} state={state} task={state.task} tasks={tasks} /> : null}
    </PhaseRegion>
  )
}
