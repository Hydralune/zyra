import { nodeLabel, nodeDescription, nodeStateLabel } from "../../shell/product-copy.ts"
export { nodeLabel, nodeDescription, nodeStateLabel } from "../../shell/product-copy.ts"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type {
  ArtifactProjection,
  PlanNodeProjection,
  TaskProjection,
} from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskDetailState } from "../../shell/workbench-controller.ts"
import type { ProductAssistantStreamSnapshot } from "../../shell/task-live-sync.ts"
import { taskActionSet } from "../../shell/task-action-policy.ts"
import { formatDuration, taskFinishedAt, taskMetrics } from "../../shell/task-metrics.ts"
import { FocusTrap } from "../../shell/focus-trap.ts"
import {
  useCommandSnapshot,
  useLiveSyncSnapshot,
  useQueueSnapshot,
} from "../../app/hooks.ts"
import { EmptyState, ErrorState, LoadingState } from "../status/request-state.tsx"
import { TaskDetail } from "./task-detail.tsx"
import { SafeMarkdown } from "../content/safe-markdown.tsx"
import { CopyButton } from "../content/copy-button.tsx"
import { productArtifacts } from "../../features/artifacts/product-artifacts.ts"

const COMPLETED_NODE_STATES = new Set(["completed", "succeeded", "verified"])
const RUNNING_NODE_STATES = new Set(["running", "active", "dispatched"])
const FAILED_NODE_STATES = new Set(["failed", "error"])
const FOLLOW_THRESHOLD_PX = 72

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
      : 0,
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

export interface PendingConversationTurn {
  id: string
  text: string
  phase: "sending" | "queued"
}

interface PendingSubmissionInput {
  value: string
  phase: string
  taskId?: string
  queueId?: string
}

interface PendingQueueInput {
  id: string
  value: string
  phase: string
  taskId?: string
}

/**
 * Projects the locally owned submission state into optimistic conversation
 * turns so a message never disappears between "Enter" and the backend receipt.
 *
 * Only plain prompts become turns; a slash command is a control action, not
 * something the user said to Zyra.  Nothing here invents backend state — every
 * entry is a real in-flight or queued submission the client already holds.
 */
export function pendingConversationTurns(input: {
  taskId: string
  active?: PendingSubmissionInput
  queued?: readonly PendingQueueInput[]
}): PendingConversationTurn[] {
  const turns: PendingConversationTurn[] = []
  const claimedQueueIds = new Set<string>()
  const conversational = (value: string) =>
    Boolean(value.trim()) && !value.trimStart().startsWith("/")
  const active = input.active
  if (
    active &&
    active.taskId === input.taskId &&
    ["validating", "dispatching"].includes(active.phase) &&
    conversational(active.value)
  ) {
    if (active.queueId) claimedQueueIds.add(active.queueId)
    turns.push({
      id: active.queueId ?? `sending:${active.value}`,
      text: active.value,
      phase: "sending",
    })
  }
  for (const entry of input.queued ?? []) {
    if (entry.taskId !== input.taskId) continue
    if (claimedQueueIds.has(entry.id)) continue
    if (!["queued", "dispatching"].includes(entry.phase)) continue
    if (!conversational(entry.value)) continue
    turns.push({
      id: entry.id,
      text: entry.value,
      phase: entry.phase === "dispatching" ? "sending" : "queued",
    })
  }
  return turns
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

export type ProductStatusTone = "active" | "success" | "danger" | "idle"

export interface ProductStatusCopy {
  eyebrow: string
  title: string
  detail: string
  tone: ProductStatusTone
}

export function statusCopy(task: TaskProjection): ProductStatusCopy {
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
    if (!summary && productArtifacts(task.artifacts).length) {
      return {
        eyebrow: "执行已结束",
        title: "交付物已生成",
        detail: `已生成 ${productArtifacts(task.artifacts).length} 个交付物，但没有提供最终总结。`,
        tone: "idle",
      }
    }
    if (!summary) return {
      eyebrow: "执行已结束", title: "没有可展示的回答",
      detail: "执行已经结束，但没有返回回答内容。你可以查看执行记录，或继续说明需要的结果。", tone: "idle",
    }
    return {
      eyebrow: "已完成",
      title: "任务已完成",
      detail: "回答已生成。",
      tone: "success",
    }
  }
  if (["cancelled", "canceled"].includes(normalized)) {
    return { eyebrow: "已停止", title: "任务已停止", detail: "已有结果已保留。你可以继续对话，或重新运行任务。", tone: "idle" }
  }
  if (["failed", "error"].includes(normalized)) {
    return {
      eyebrow: "需要处理",
      title: normalized.includes("cancel") ? "任务已取消" : "任务未能完成",
      detail: "打开运行详情可以查看失败原因、恢复建议和审计证据。",
      tone: "danger",
    }
  }
  if (["blocked", "needs_revision", "waiting_approval", "awaiting_approval", "paused"].includes(normalized)) {
    return {
      eyebrow: normalized === "needs_revision" ? "需要修改" : normalized.includes("approval") ? "等待许可" : "等待处理",
      title: normalized === "needs_revision" ? "结果需要进一步修改" : normalized.includes("approval") ? "需要你的许可" : "任务暂时无法继续",
      detail: String(task.metadata.failure_reason || task.metadata.blocked_reason || "请查看执行过程中的原因及待处理事项，再决定是否继续。"),
      tone: "danger",
    }
  }
  if (task.active) {
    return {
      eyebrow: "正在工作",
      title: "正在推进任务",
      detail: "任务会持续运行，进度与交付物在这里自动更新。",
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

export function orderedProductPlan(nodes: readonly PlanNodeProjection[]): PlanNodeProjection[] {
  const byId = new Map(nodes.map((node) => [node.nodeId, node]))
  const visited = new Set<string>()
  const visiting = new Set<string>()
  const result: PlanNodeProjection[] = []
  const visit = (node: PlanNodeProjection) => {
    if (visited.has(node.nodeId) || visiting.has(node.nodeId)) return
    visiting.add(node.nodeId)
    for (const id of node.dependsOn) { const dependency = byId.get(id); if (dependency) visit(dependency) }
    visiting.delete(node.nodeId)
    visited.add(node.nodeId)
    result.push(node)
  }
  nodes.forEach(visit)
  return result
}

function artifactLabel(artifact: ArtifactProjection): string {
  return artifact.title || artifact.path || artifact.artifactId
}

function nodeToolLabel(node: PlanNodeProjection): string | undefined {
  const candidates = [
    node.metadata.tool,
    node.metadata.tool_name,
    node.metadata.operator,
    node.metadata.skill,
  ]
  const value = candidates.find(
    (entry): entry is string => typeof entry === "string" && Boolean(entry.trim()),
  )
  return value?.trim()
}

/**
 * Keeps a disclosure controlled without fighting the user: it follows the
 * derived default until the person opens or closes it themselves, after which
 * their choice survives every background refresh.
 */
function useDisclosure(preferredOpen: boolean): {
  open: boolean
  onToggle: (open: boolean) => void
} {
  const [override, setOverride] = useState<boolean | undefined>(undefined)
  return {
    open: override ?? preferredOpen,
    onToggle: setOverride,
  }
}

function TaskControls({
  runtime,
  task,
  onAdvanced,
  onEvidence,
  advancedRef,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  onAdvanced: () => void
  onEvidence: () => void
  advancedRef: React.RefObject<HTMLButtonElement | null>
}) {
  const actions = taskActionSet(task, {
    lifecycleBusy: runtime.api.lifecycle.inFlight().some((record) => record.taskId === task.taskId && record.action === "cancel"),
    transportEnabled: runtime.workbench.getSnapshot().transportEnabled,
  })
  const completed = ["completed", "succeeded", "verified"].includes(task.status.toLowerCase())
  return (
    <div className="product-task-controls">
      <button
        className="product-button product-button-quiet"
        type="button"
        disabled={!actions.refresh.allowed}
        onClick={() => void runtime.liveSync.refreshNow()}
      >
        刷新
      </button>
      {actions.cancel.allowed ? (
        <button
          className="product-button product-button-danger"
          type="button"
          onClick={() => runtime.overlays.open({
            kind: "task-cancel",
            title: "停止任务",
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
            title: "继续任务",
            replaceKind: true,
            payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
          })}
        >
          重新运行
        </button>
      ) : null}
      <button
        className="product-button product-button-quiet"
        type="button"
        onClick={onEvidence}
      >
        完整证据
      </button>
      <button
        ref={advancedRef}
        className="product-button product-button-quiet"
        type="button"
        onClick={onAdvanced}
      >
        运行详情
      </button>
    </div>
  )
}

function ArtifactPreview({
  runtime,
  task,
  turnId,
  expanded = false,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  turnId: string
  expanded?: boolean
}) {
  const artifacts = productArtifacts(task.artifacts)
  const [selectedId, setSelectedId] = useState<string>()
  const selected =
    artifacts.find((artifact) => artifact.artifactId === selectedId)
    ?? artifacts.at(-1)
  const disclosure = useDisclosure(expanded)
  const [preview, setPreview] = useState<{
    phase: "idle" | "loading" | "ready" | "error"
    text?: string
  }>({ phase: "idle" })
  const tabsRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!selected || !disclosure.open) {
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
      if (controller.signal.aborted) return
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
  }, [disclosure.open, runtime, selected?.artifactId, task.taskId])

  const moveSelection = (direction: -1 | 1) => {
    if (!selected) return
    const index = artifacts.findIndex(
      (artifact) => artifact.artifactId === selected.artifactId,
    )
    const next = artifacts[(index + direction + artifacts.length) % artifacts.length]
    if (!next) return
    setSelectedId(next.artifactId)
    requestAnimationFrame(() => {
      tabsRef.current
        ?.querySelector<HTMLButtonElement>(`[data-artifact-id="${CSS.escape(next.artifactId)}"]`)
        ?.focus()
    })
  }

  if (!artifacts.length) return null
  const panelId = `artifact-panel-${turnId}`
  return (
    <details
      className="product-disclosure product-deliverables"
      open={disclosure.open}
      onToggle={(event) => disclosure.onToggle(event.currentTarget.open)}
    >
      <summary>
        <span className="product-disclosure-icon" aria-hidden="true">◇</span>
        <span><strong>交付物</strong><small>{artifacts.length} 个文件与结果</small></span>
        <span className="product-disclosure-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="product-disclosure-content">
        <div
          className="product-artifact-tabs"
          role="tablist"
          aria-label="交付物"
          ref={tabsRef}
          onKeyDown={(event) => {
            if (event.key === "ArrowRight" || event.key === "ArrowDown") {
              event.preventDefault()
              moveSelection(1)
            } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
              event.preventDefault()
              moveSelection(-1)
            }
          }}
        >
          {artifacts.map((artifact) => {
            const active = artifact.artifactId === selected?.artifactId
            return (
              <button
                key={artifact.artifactId}
                type="button"
                role="tab"
                data-artifact-id={artifact.artifactId}
                data-active={active || undefined}
                aria-selected={active}
                aria-controls={panelId}
                tabIndex={active ? 0 : -1}
                onClick={() => setSelectedId(artifact.artifactId)}
              >
                <span aria-hidden="true">{artifact.kind === "code" ? "⌘" : "◫"}</span>
                {artifactLabel(artifact)}
              </button>
            )
          })}
        </div>
        {selected ? (
          <div
            className="product-artifact-preview"
            id={panelId}
            role="tabpanel"
            tabIndex={0}
            data-phase={preview.phase}
          >
            <header>
              <div>
                <strong>{artifactLabel(selected)}</strong>
                <span>{selected.mediaType || selected.kind}</span>
              </div>
              <span className="product-verified">可追溯</span>
            </header>
            {preview.phase === "loading" ? (
              <p className="product-muted" role="status">正在读取交付物…</p>
            ) : null}
            {preview.phase === "error" ? (
              <p className="product-error-copy" role="alert">暂时无法预览：{preview.text}</p>
            ) : null}
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
  latest,
  liveAssistant,
  onInspect,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  latest: boolean
  liveAssistant?: ProductAssistantStreamSnapshot
  onInspect: (taskId: string) => void
}) {
  const progress = productTaskProgress(task)
  const metrics = taskMetrics(task)
  const answerTime = new Date(taskFinishedAt(task) ?? Date.parse(task.updatedAt)).toISOString()
  const copy = statusCopy(task)
  const summary = resultSummary(task)
  const detail = runtime.workbench.conversationDetail?.(task.taskId) ?? { phase: "ready" }
  const deliveries = productArtifacts(task.artifacts)
  const evidenceCount = task.artifacts.length - deliveries.length
  // Steps are secondary detail: they open on their own only while the latest
  // turn is still moving or needs attention, and stay put once touched.
  const runDetails = useDisclosure(latest && (task.active || copy.tone === "danger"))
  return (
    <article className="product-turn" data-task-status={task.status} aria-label="对话轮次">
      <div className="product-message product-message-user">
        <div className="product-message-body">
          <span>你</span>
          <p>{task.userGoal}</p>
        </div>
      </div>

      <div className="product-message product-message-zyra" data-tone={copy.tone}>
        <div className="product-avatar product-avatar-zyra" aria-hidden="true">Z</div>
        <div className="product-message-body">
          <div className="product-agent-meta">
            <strong>Zyra</strong>
            <time dateTime={answerTime} title={new Date(answerTime).toLocaleString("zh-CN")}>
              {relativeTime(answerTime)}
            </time>
          </div>
          {summary ? (
            <SafeMarkdown text={summary} className="product-answer product-answer-markdown" />
          ) : liveAssistant?.text ? (
            <div className="product-live-answer" aria-label="Zyra 实时回答">
              <SafeMarkdown
                text={liveAssistant.text}
                className="product-answer product-answer-markdown"
                streaming={!liveAssistant.settling}
                truncated={liveAssistant.truncated || liveAssistant.partial}
              />
              <small>
                {liveAssistant.settling
                  ? "正在确认最终回答…"
                  : liveAssistant.partial
                    ? "已从当前可用的实时片段继续显示"
                    : "实时生成中"}
              </small>
            </div>
          ) : detail.phase !== "ready" ? (
            <div className="product-answer-loading" role={detail.phase === "error" ? "alert" : "status"}>
              <strong>{detail.phase === "error" ? "回答加载失败" : "正在加载回答…"}</strong>
              <p>{detail.phase === "error" ? "暂时无法读取这轮对话，任务记录仍保留。" : "正在读取这轮对话的完整内容。"}</p>
              {detail.phase === "error" ? <button className="product-button" type="button" onClick={() => void runtime.workbench.retryConversationDetail(task.taskId)}>重新加载回答</button> : null}
            </div>
          ) : task.active ? (
            <p className="product-working-copy">
              <span className="product-working-spinner" aria-hidden="true" />
              <span>{copy.detail}</span>
            </p>
          ) : (
            <div className="product-empty-answer" data-tone={copy.tone}>
              <strong>{copy.title}</strong>
              <span>{copy.detail}</span>
            </div>
          )}
          {summary ? <div className="product-message-actions"><CopyButton text={summary} /></div> : null}

          <details
            className="product-disclosure product-run-summary"
            open={runDetails.open}
            onToggle={(event) => runDetails.onToggle(event.currentTarget.open)}
          >
            <summary>
              <span className="product-disclosure-icon" data-tone={copy.tone} aria-hidden="true">
                {task.active ? "◴" : copy.tone === "danger" ? "!" : copy.tone === "success" ? "✓" : "○"}
              </span>
              <span>
                <strong>执行过程</strong>
                <small>
                  {copy.eyebrow}
                  {progress.total ? ` · ${progress.completed}/${progress.total} 步骤` : ""}
                  {` · ${formatDuration(metrics.elapsedMs)}`}
                </small>
              </span>
              <span className="product-disclosure-chevron" aria-hidden="true">⌄</span>
            </summary>
            <div className="product-disclosure-content">
              <div
                className="product-progress"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress.percentage}
                aria-label="任务进度"
              >
                <span style={{ width: `${progress.percentage}%` }} />
              </div>
              <div className="product-result-facts">
                <span><strong>{progress.completed}/{progress.total}</strong> 步骤</span>
                <span><strong>{deliveries.length}</strong> 交付物</span>
                {evidenceCount ? <span><strong>{evidenceCount}</strong> 运行记录</span> : null}
                <span><strong>{formatDuration(metrics.elapsedMs)}</strong> 用时</span>
              </div>
              {task.planNodes.length ? (
                <ol className="product-plan-list">
                  {orderedProductPlan(task.planNodes).map((node) => {
                    const normalized = node.status.toLowerCase()
                    const tool = nodeToolLabel(node)
                    return (
                      <li key={node.nodeId} data-status={normalized}>
                        <span className="product-step-icon" aria-hidden="true">
                          {COMPLETED_NODE_STATES.has(normalized)
                            ? "✓"
                            : RUNNING_NODE_STATES.has(normalized)
                              ? "◴"
                              : FAILED_NODE_STATES.has(normalized)
                                ? "!"
                                : "○"}
                        </span>
                        <span>
                          <strong>{nodeLabel(node)}</strong>
                          <small>
                            <span className="product-step-state">{nodeStateLabel(node.status)}</span>
                            {tool ? <span className="product-step-tool">{tool}</span> : null}
                            {node.description ? <span>{nodeDescription(node)}</span> : null}
                          </small>
                        </span>
                      </li>
                    )
                  })}
                </ol>
              ) : (
                <p className="product-muted">运行时还没有发布可展示的执行步骤。</p>
              )}
              <button
                className="product-evidence-link"
                type="button"
                onClick={() => onInspect(task.taskId)}
              >
                查看完整证据链 →
              </button>
            </div>
          </details>
          <ArtifactPreview runtime={runtime} task={task} turnId={task.taskId} />
        </div>
      </div>
    </article>
  )
}

function PendingTurn({ turn }: { turn: PendingConversationTurn }) {
  return (
    <article className="product-turn product-turn-pending" data-phase={turn.phase} aria-label="待发送的对话轮次">
      <div className="product-message product-message-user">
        <div className="product-message-body">
          <span>你</span>
          <p>{turn.text}</p>
        </div>
      </div>
      <div className="product-message product-message-zyra" data-tone="active">
        <div className="product-avatar product-avatar-zyra" aria-hidden="true">Z</div>
        <div className="product-message-body">
          <p className="product-working-copy">
            <span className="product-working-spinner" aria-hidden="true" />
            <span>{turn.phase === "queued" ? "已排队，将在当前任务之后执行。" : "正在提交给运行时…"}</span>
          </p>
        </div>
      </div>
    </article>
  )
}

function AdvancedRunDrawer({
  runtime,
  state,
  onClose,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  onClose: () => void
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const trapRef = useRef<FocusTrap | undefined>(undefined)

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (!panelRef.current) return
      const trap = new FocusTrap(panelRef.current)
      trapRef.current = trap
      trap.activate({ fallbackToContainer: true })
    })
    return () => {
      cancelAnimationFrame(frame)
      trapRef.current?.deactivate()
      trapRef.current = undefined
    }
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (runtime.overlays.active()) return
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        event.stopPropagation()
        return
      }
      if (event.key === "Escape") {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      }
    }
    window.addEventListener("keydown", onKeyDown, true)
    return () => window.removeEventListener("keydown", onKeyDown, true)
  }, [onClose, runtime])

  return (
    <div className="advanced-drawer">
      <button className="advanced-drawer-backdrop" type="button" aria-label="关闭运行详情" onClick={onClose} />
      <div
        ref={panelRef}
        className="advanced-drawer-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="advanced-drawer-title"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Tab" && trapRef.current?.handleTab(event.nativeEvent)) {
            event.stopPropagation()
          }
        }}
      >
        <header className="advanced-drawer-header">
          <div>
            <p>高级详情</p>
            <h2 id="advanced-drawer-title">运行详情与证据</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭运行详情">×</button>
        </header>
        <div className="advanced-drawer-content">
          <TaskDetail runtime={runtime} state={state} />
        </div>
      </div>
    </div>
  )
}

function ProductDetailContent({
  runtime,
  state,
  task,
  tasks,
  view,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  task: TaskProjection
  tasks: readonly TaskProjection[]
  view?: "artifacts"
}) {
  const [advanced, setAdvanced] = useState(false)
  const artifactView = view === "artifacts"
  const command = useCommandSnapshot(runtime)
  const queue = useQueueSnapshot(runtime)
  const live = useLiveSyncSnapshot(runtime)
  const copy = statusCopy(task)
  const advancedTriggerRef = useRef<HTMLButtonElement | null>(null)
  const timeline = useMemo(
    () => productConversationTasks(task, tasks),
    [task, tasks],
  )
  const pending = useMemo(
    () => pendingConversationTurns({
      taskId: task.taskId,
      active: command.active,
      queued: queue.entries,
    }),
    [command.active, queue.entries, task.taskId],
  )
  const turnCount = timeline.length + pending.length

  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const seenRef = useRef(turnCount)
  const [following, setFollowing] = useState(true)
  const [unseen, setUnseen] = useState(0)

  const settle = useCallback((behavior: ScrollBehavior) => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior })
  }, [])

  const jumpToLatest = useCallback(() => {
    followRef.current = true
    seenRef.current = turnCount
    setFollowing(true)
    setUnseen(0)
    settle("smooth")
  }, [settle, turnCount])

  const onScroll = useCallback(() => {
    const element = scrollRef.current
    if (!element) return
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight
    const atBottom = distance <= FOLLOW_THRESHOLD_PX
    if (followRef.current === atBottom) return
    followRef.current = atBottom
    setFollowing(atBottom)
    if (atBottom) {
      seenRef.current = turnCount
      setUnseen(0)
    }
  }, [turnCount])

  // Opening a different conversation always starts pinned to the newest turn.
  useEffect(() => {
    followRef.current = !artifactView
    setFollowing(!artifactView)
    setUnseen(0)
    const frame = requestAnimationFrame(() => {
      if (artifactView) scrollRef.current?.scrollTo({ top: 0 })
      else settle("auto")
    })
    return () => cancelAnimationFrame(frame)
  }, [artifactView, settle, task.taskId])

  // Streaming answers and expanding disclosures grow the transcript without a
  // scroll event; keep the viewport pinned while the reader is following.
  useEffect(() => {
    const viewport = scrollRef.current
    const content = contentRef.current
    if (!viewport || !content || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(() => {
      if (!followRef.current) return
      viewport.scrollTop = viewport.scrollHeight
    })
    observer.observe(content)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (followRef.current) {
      seenRef.current = turnCount
      setUnseen(0)
      return
    }
    setUnseen(Math.max(0, turnCount - seenRef.current))
  }, [turnCount])

  // Reading a long transcript should not be interrupted, but a state change
  // still has to reach someone using a screen reader.
  const lastStatusRef = useRef<string | undefined>(undefined)
  useEffect(() => {
    const signature = `${task.taskId}:${task.status}:${task.active}`
    if (lastStatusRef.current === undefined) {
      lastStatusRef.current = signature
      return
    }
    if (lastStatusRef.current === signature) return
    lastStatusRef.current = signature
    runtime.announcer.announce(
      `${copy.title}。${copy.detail}`,
      copy.tone === "danger" ? "assertive" : "polite",
    )
  }, [copy.detail, copy.title, copy.tone, runtime, task.active, task.status, task.taskId])

  const closeAdvanced = useCallback(() => {
    setAdvanced(false)
    requestAnimationFrame(() => advancedTriggerRef.current?.focus())
  }, [])

  // Evidence for an older turn lives on that turn's own run, so the drawer has
  // to be pointed at it before it opens.
  const inspectTurn = useCallback((taskId: string) => {
    if (taskId !== task.taskId) {
      runtime.router.openTask(taskId, { focus: "task-detail" })
      return
    }
    setAdvanced(true)
  }, [runtime, task.taskId])

  const stale = Boolean(state.staleSince) && !state.syncing
  const busy = state.phase === "loading" || Boolean(state.syncing)

  return (
    <section className="product-task-view" data-task-status={task.status}>
      <header className="product-session-toolbar">
        <div className="product-session-status">
          <span className="product-status-dot" data-tone={copy.tone} aria-hidden="true" />
          <span>
            <strong>{copy.eyebrow}</strong>
            <small>
              {timeline.length > 1 ? `${timeline.length} 轮对话 · ` : ""}
              {live.live && live.paused
                ? "已暂停自动更新"
                : live.live
                  ? "实时更新中"
                  : relativeTime(new Date(taskFinishedAt(task) ?? Date.parse(task.updatedAt)).toISOString())}
            </small>
          </span>
        </div>
        <TaskControls
          runtime={runtime}
          task={task}
          advancedRef={advancedTriggerRef}
          onAdvanced={() => setAdvanced(true)}
          onEvidence={() => runtime.router.openEvidence(task.taskId)}
        />
        <span className="product-sync-bar" data-busy={busy || undefined} aria-hidden="true" />
      </header>

      {stale ? (
        <p className="product-stale-notice" role="status">
          最新状态暂时读取失败，下方内容可能不是最新的。
          <button type="button" onClick={() => void runtime.liveSync.refreshNow()}>立即重试</button>
        </p>
      ) : null}

      <div className="product-task-scroll" ref={scrollRef} onScroll={onScroll}>
        {/*
          `role="log"` announces turns as they are added without re-reading the
          transcript; in-place answer edits stay silent, which is what a reader
          following a long run actually wants.
        */}
        <div className="product-conversation" ref={contentRef} role={artifactView ? undefined : "log"} aria-label={artifactView ? "会话交付物" : "会话消息"}>
          {artifactView ? (
            <section className="product-artifact-library">
              <header>
                <h1>会话交付物</h1>
                <button className="product-button product-button-quiet" type="button" onClick={() => runtime.router.openTask(task.taskId)}>返回对话</button>
              </header>
              {!timeline.some((turn) => productArtifacts(turn.artifacts).length) ? <p className="product-muted">这个会话没有单独交付的文件。回答保留在对话中，内部运行记录可在证据中心查看。</p> : null}
              {timeline.filter((turn) => productArtifacts(turn.artifacts).length).map((turn) => (
                <section key={turn.taskId}>
                  <h2>{turn.userGoal}</h2>
                  <ArtifactPreview runtime={runtime} task={turn} turnId={turn.taskId} expanded />
                </section>
              ))}
              <button className="product-button" type="button" onClick={() => runtime.router.openEvidence(task.taskId)}>查看运行记录与完整证据</button>
            </section>
          ) : timeline.map((turn, index) => (
            <ConversationTurn
              key={turn.taskId}
              runtime={runtime}
              task={turn}
              latest={index === timeline.length - 1}
              liveAssistant={turn.taskId === live.taskId ? live.assistant : undefined}
              onInspect={inspectTurn}
            />
          ))}
          {!artifactView && pending.map((turn) => <PendingTurn key={turn.id} turn={turn} />)}
        </div>
      </div>

      {!following && !artifactView ? (
        <button className="product-jump-latest" type="button" onClick={jumpToLatest}>
          ↓ 跳到最新{unseen ? ` · ${unseen} 条新消息` : ""}
        </button>
      ) : null}

      {advanced ? (
        <AdvancedRunDrawer runtime={runtime} state={state} onClose={closeAdvanced} />
      ) : null}
    </section>
  )
}

export function ProductTaskDetail({
  runtime,
  state,
  tasks = [],
  view,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  tasks?: readonly TaskProjection[]
  view?: "artifacts"
}) {
  // A projection that is already on screen outranks any request phase: a
  // refresh, a reconnect, or a failed background poll must never blank out the
  // conversation the user is reading.
  if (state.task && state.task.taskId === state.taskId) {
    return (
      <ProductDetailContent
        runtime={runtime}
        state={state}
        task={state.task}
        tasks={tasks}
        view={view}
      />
    )
  }
  if (!state.taskId && state.phase === "idle") {
    return (
      <section className="product-route-state">
        <EmptyState title="选择一个任务" detail="从左侧最近会话中选择，或创建一个新任务。" />
      </section>
    )
  }
  if (state.phase === "not-found") {
    return (
      <section className="product-route-state">
        <EmptyState
          title="没有找到任务"
          detail="该任务不在当前运行时的可见范围内。"
          action={
            <button className="product-button product-button-quiet" type="button" onClick={() => runtime.router.openTasks()}>
              返回首页
            </button>
          }
        />
      </section>
    )
  }
  if (state.phase === "error") {
    return (
      <section className="product-route-state">
        <ErrorState
          title="暂时无法读取任务"
          failure={state.failure}
          retryLabel="重试"
          onRetry={state.taskId ? () => void runtime.workbench.loadTask(state.taskId!) : undefined}
        />
      </section>
    )
  }
  return (
    <section className="product-route-state">
      <LoadingState title="正在载入会话" detail={state.taskId} />
    </section>
  )
}
