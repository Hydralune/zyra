import { useEffect, useMemo, useState } from "react"
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
    return {
      eyebrow: "Zyra 已完成",
      title: "任务已完成",
      detail: task.artifacts.length
        ? `已生成 ${task.artifacts.length} 个可追溯交付物。`
        : "所有计划步骤已经完成。",
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
      {actions.resume.allowed ? (
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
          继续任务
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

  useEffect(() => {
    if (!selected) {
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
  }, [runtime, selected?.artifactId, task.taskId])

  if (!task.artifacts.length) return null
  return (
    <section className="product-deliverables" aria-labelledby="product-deliverables-heading">
      <div className="product-section-heading">
        <div>
          <p>Deliverables</p>
          <h3 id="product-deliverables-heading">交付物</h3>
        </div>
        <span>{task.artifacts.length}</span>
      </div>
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
    </section>
  )
}

function ProductDetailContent({
  runtime,
  state,
  task,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  task: TaskProjection
}) {
  const [advanced, setAdvanced] = useState(false)
  const progress = useMemo(() => productTaskProgress(task), [task])
  const metrics = useMemo(() => taskMetrics(task), [task])
  const copy = statusCopy(task)
  const summary = resultSummary(task)

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
      <div className="product-task-scroll">
        <header className="product-task-header">
          <div className="product-task-title">
            <span className="product-status-dot" data-tone={copy.tone} aria-hidden="true" />
            <div>
              <p>{copy.eyebrow} · {relativeTime(task.updatedAt)}</p>
              <h1>{task.userGoal || "未命名任务"}</h1>
            </div>
          </div>
          <TaskControls runtime={runtime} task={task} onAdvanced={() => setAdvanced(true)} />
        </header>

        <div className="product-conversation">
          <article className="product-message product-message-user">
            <div className="product-avatar product-avatar-user" aria-hidden="true">你</div>
            <div>
              <span>你</span>
              <p>{task.userGoal}</p>
            </div>
          </article>

          <article className="product-message product-message-zyra" data-tone={copy.tone}>
            <div className="product-avatar product-avatar-zyra" aria-hidden="true">Z</div>
            <div className="product-message-body">
              <span>Zyra</span>
              <div className="product-result-heading">
                <div>
                  <p>{copy.eyebrow}</p>
                  <h2>{copy.title}</h2>
                </div>
                <strong>{progress.percentage}%</strong>
              </div>
              <p className="product-result-detail">{summary || copy.detail}</p>
              <div className="product-progress" aria-label={`任务进度 ${progress.percentage}%`}>
                <span style={{ width: `${progress.percentage}%` }} />
              </div>
              <div className="product-result-facts">
                <span><strong>{progress.completed}/{progress.total}</strong> 步骤</span>
                <span><strong>{task.artifacts.length}</strong> 交付物</span>
                <span><strong>{formatDuration(metrics.elapsedMs)}</strong> 用时</span>
              </div>
            </div>
          </article>
        </div>

        {task.planNodes.length ? (
          <section className="product-plan" aria-labelledby="product-plan-heading">
            <div className="product-section-heading">
              <div>
                <p>Progress</p>
                <h3 id="product-plan-heading">执行进度</h3>
              </div>
              <span>{progress.completed}/{progress.total}</span>
            </div>
            <ol>
              {task.planNodes.map((node) => (
                <li key={node.nodeId} data-status={node.status}>
                  <span className="product-step-icon" aria-hidden="true">
                    {COMPLETED_NODE_STATES.has(node.status.toLowerCase()) ? "✓" : node.status === "running" ? "•" : ""}
                  </span>
                  <div>
                    <strong>{nodeLabel(node)}</strong>
                    <small>{node.description || node.status}</small>
                  </div>
                </li>
              ))}
            </ol>
          </section>
        ) : null}

        <ArtifactPreview runtime={runtime} task={task} />
      </div>

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
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
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
      {state.task ? <ProductDetailContent runtime={runtime} state={state} task={state.task} /> : null}
    </PhaseRegion>
  )
}
