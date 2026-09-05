import { nodeLabel, nodeDescription, nodeStateLabel, phaseLabel } from "../../shell/product-copy.ts"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import type {
  PlanNodeProjection,
  TaskProjection,
} from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskDetailState } from "../../shell/workbench-controller.ts"
import { EmptyState, ErrorState, LoadingState, PhaseRegion } from "../status/request-state.tsx"
import { buildTaskTree, type TaskTreeNode } from "../../shell/task-tree.ts"
import { taskActionSet } from "../../shell/task-action-policy.ts"
import { formatDuration, taskHealthLabel, taskMetrics } from "../../shell/task-metrics.ts"
import { useProjectionSelector } from "../../app/hooks.ts"
import {
  selectTaskSummary,
  selectEventsForTask,
  selectRevision,
} from "../../state/selectors.ts"
import { TopologyWorkbench } from "../../features/topology/view/topology-workbench.tsx"
import { WorkerCausalTimelineWorkbench } from "../../features/timeline/view/timeline-workbench.tsx"
import { ArtifactWorkbench } from "../../features/artifacts/view/artifact-workbench.tsx"
import { DiffReviewWorkbench } from "../../features/diff-review/view/diff-review-workbench.tsx"
import { TerminalWorkbench } from "../../features/terminal/view/terminal-workbench.tsx"
import { BrowserWorkbench } from "../../features/browser/view/browser-workbench.tsx"
import { CausalTraceWorkbench } from "../../features/trace/view/trace-workbench.tsx"
import { PermissionWorkbench } from "../../features/permissions/index.ts"
import { SessionConsoleWorkbench } from "../../features/session/index.ts"
import { McpWorkbench } from "../../features/mcp/index.ts"
import { SkillWorkbench } from "../../features/skills/index.ts"
import { SubagentWorkbench } from "../../features/subagents/index.ts"
import { LoopXWorkbench } from "../../features/long-horizon/loopx/index.ts"
import { CommandQueuePanel } from "../../features/commands/queue-panel.tsx"
import { permissionDisplayActor } from "../../features/permissions/index.ts"

function dateTime(value: string | undefined): string {
  if (!value) return "—"
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(timestamp)
}

function PlanNode({ entry, root }: { entry: TaskTreeNode; root: boolean }) {
  const node = entry.node
  return (
    <li className="plan-node" style={{ marginLeft: `${Math.min(6, entry.depth) * 14}px` }}>
      <span className={`status-marker status-${node.status}`} aria-hidden="true" />
      <div>
        <div className="plan-node-heading">
          <strong>{nodeLabel(node)}</strong>
          {root ? <span className="tag">目标</span> : null}
          <span className="tag tag-muted">{nodeStateLabel(node.status)}</span>
          {entry.dependencyBlocked ? <span className="tag tag-danger">等待依赖</span> : null}
        </div>
        {node.description ? <p>{nodeDescription(node)}</p> : null}
        <dl className="inline-facts">
          {node.assignedWorkerId ? (
            <>
              <dt>执行者</dt>
              <dd>{node.assignedWorkerId}</dd>
            </>
          ) : null}
          {node.dependsOn.length ? (
            <>
              <dt>依赖</dt>
              <dd>{node.dependsOn.join(", ")}</dd>
            </>
          ) : null}
          {node.artifactIds.length ? (
            <>
              <dt>产物</dt>
              <dd>{node.artifactIds.length}</dd>
            </>
          ) : null}
        </dl>
      </div>
    </li>
  )
}

function TaskActions({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const actions = taskActionSet(task, {
    lifecycleBusy: runtime.api.lifecycle.inFlight().some((record) => record.taskId === task.taskId && record.action === "cancel"),
    transportEnabled: runtime.workbench.getSnapshot().transportEnabled,
  })
  const cancel = () => {
    runtime.overlays.open({
      kind: "task-cancel",
      title: "停止任务",
      replaceKind: true,
      payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
    })
  }
  const resume = () => {
    runtime.overlays.open({
      kind: "task-resume",
      title: "继续任务",
      replaceKind: true,
      payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
    })
  }
  return (
    <div className="task-actions">
      <button
        className="button button-secondary"
        type="button"
        data-task-detail-action
        disabled={!actions.refresh.allowed}
        title={actions.refresh.reason}
        onClick={() => void runtime.workbench.loadTask(task.taskId)}
      >
        刷新
      </button>
      {actions.cancel.allowed ? (
        <button
          className="button button-danger"
          type="button"
          data-task-detail-action
          title={actions.cancel.reason}
          onClick={cancel}
        >
          停止任务
        </button>
      ) : null}
      {actions.resume.allowed ? (
        <button
          className="button button-primary"
          type="button"
          data-task-detail-action
          title={actions.resume.reason}
          onClick={resume}
        >
          继续任务
        </button>
      ) : null}
    </div>
  )
}

function BoundSubagentWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const [bindingError, setBindingError] = useState("")
  useEffect(() => {
    try {
      runtime.subagentConsole.openViewer()
      runtime.subagentConsole.bind(task.taskId, task.runId)
      setBindingError("")
    } catch (error) {
      setBindingError(
        error instanceof Error ? error.message : String(error),
      )
    }
    return () => runtime.subagentConsole.closeViewer()
  }, [runtime.subagentConsole, task.runId, task.taskId])
  if (bindingError) {
    return (
      <div className="plan-warning" role="alert">
        子代理信息暂不可用： {bindingError}
      </div>
    )
  }
  return <SubagentWorkbench controller={runtime.subagentConsole} />
}

function EvidencePanel({ id, title, runtime, children }: {
  id: string; title: string; runtime: WorkbenchRuntime; children: ReactNode
}) {
  const [open, setOpen] = useState(() => runtime.router.current.query.section === id
    || (typeof location !== "undefined" && location.hash === `#${id}`))
  const ref = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    const reveal = () => {
      if (runtime.router.current.query.section !== id && window.location.hash !== `#${id}`) return
      setOpen(true)
      requestAnimationFrame(() => ref.current?.scrollIntoView({ block: "start" }))
    }
    const unsubscribe = runtime.router.listen(reveal)
    window.addEventListener("hashchange", reveal)
    return () => { unsubscribe(); window.removeEventListener("hashchange", reveal) }
  }, [id, runtime])
  return (
    <details ref={ref} id={id} className="evidence-panel" open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{title}</summary>
      {open ? <div className="evidence-panel-content">{children}</div> : null}
    </details>
  )
}

function DetailContent({ runtime, task }: { runtime: WorkbenchRuntime; task: TaskProjection }) {
  const tree = useMemo(() => buildTaskTree(task), [task])
  const metrics = useMemo(() => taskMetrics(task), [task])
  const live = useProjectionSelector(runtime, selectTaskSummary(task.taskId))
  const recentEvents = useProjectionSelector(
    runtime,
    selectEventsForTask(task.taskId, { limit: 8 }),
  )
  const projectionRevision = useProjectionSelector(runtime, selectRevision())
  return (
    <div className="task-detail-scroll" data-task-id={task.taskId}>
      <header className="task-detail-header">
        <div>
          <div className="task-identity">
            <span className={`status-marker status-${task.status}`} aria-hidden="true" />
            <span>{phaseLabel(task.status)}</span>
            {task.active ? <span className="tag">正在执行</span> : null}
            {task.terminal ? <span className="tag tag-muted">执行已结束</span> : null}
          </div>
          <h2 id="task-detail-heading" tabIndex={-1}>{task.userGoal || "未命名任务"}</h2>
          <p className="task-id">{task.taskId}</p>
        </div>
        <TaskActions runtime={runtime} task={task} />
      </header>

      <EvidencePanel runtime={runtime} id="evidence-diagnostics" title="运行信息与诊断">
      <dl className="fact-grid">
        <div>
          <dt>运行编号</dt>
          <dd>{task.runId}</dd>
        </div>
        <div>
          <dt>目标节点</dt>
          <dd>{task.rootNodeId}</dd>
        </div>
        <div>
          <dt>创建时间</dt>
          <dd>{dateTime(task.createdAt)}</dd>
        </div>
        <div>
          <dt>更新时间</dt>
          <dd>{dateTime(task.updatedAt)}</dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>{formatDuration(metrics.elapsedMs)}</dd>
        </div>
        <div>
          <dt>运行状态</dt>
          <dd>{taskHealthLabel(metrics)}</dd>
        </div>
        <div>
          <dt>执行者</dt>
          <dd>{metrics.workerCount}</dd>
        </div>
        <div>
          <dt>依赖数量</dt>
          <dd>{metrics.dependencyEdges}</dd>
        </div>
        <div>
          <dt>数据版本</dt>
          <dd>{projectionRevision}</dd>
        </div>
        <div>
          <dt>事件序号</dt>
          <dd>{live.lastSequence}</dd>
        </div>
        <div>
          <dt>活跃执行者</dt>
          <dd>{live.activeWorkers}/{live.workers}</dd>
        </div>
        <div>
          <dt>待处理许可</dt>
          <dd>{live.pendingPermissions}</dd>
        </div>
      </dl>
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-canonical-events" title="最近事件">
      <section className="detail-section" aria-labelledby="recent-events-heading">
        <div className="section-heading">
          <h3 id="recent-events-heading">最近事件</h3>
          <span>{recentEvents.length}</span>
        </div>
        {recentEvents.length ? (
          <ol className="plan-list">
            {recentEvents.map((event) => (
              <li
                className="plan-node"
                key={event.eventId}
                data-event-id={event.eventId}
                data-correlation-id={event.correlationId}
                tabIndex={-1}
              >
                <span className="status-marker status-running" aria-hidden="true" />
                <div>
                  <div className="plan-node-heading">
                    <strong>{event.eventType}</strong>
                    <span className="tag tag-muted">#{event.sequence}</span>
                  </div>
                  <p>{event.summary}</p>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted-copy">
            正在恢复事件记录，或等待第一个事件。
          </p>
        )}
      </section>
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-topology" title="任务拓扑与协作">
        <TopologyWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-long-horizon" title="长程任务">
        <LoopXWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-controls" title="权限与控制">
        <PermissionWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-command-queue" title="命令队列">
        <CommandQueuePanel
          runtime={runtime.controlCommands}
          permissionRuntime={runtime.permissionConsole}
          actorId={permissionDisplayActor(task.metadata)}
        />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-continuity-placement" title="会话、记忆与执行位置">
        <SessionConsoleWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="mcp-runtime-panel" title="MCP 服务">
        <McpWorkbench
          controller={runtime.mcpConsole}
          taskId={task.taskId}
          runId={task.runId}
        />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="skill-runtime-panel" title="技能">
        <SkillWorkbench
          controller={runtime.skillConsole}
          taskId={task.taskId}
          runId={task.runId}
        />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="subagent-runtime-panel" title="子代理">
        <BoundSubagentWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-recovery" title="时间线与故障恢复">
        <WorkerCausalTimelineWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-causal-trace" title="事件追踪">
        <CausalTraceWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-plan" title="执行计划">
      <section className="detail-section" aria-labelledby="plan-heading">
        <div className="section-heading">
          <h3 id="plan-heading">执行计划</h3>
          <span>{tree.completed}/{tree.total} 已完成 · {tree.progress}%</span>
        </div>
        {tree.cycles.length || tree.orphans.length || Object.keys(tree.missingDependencies).length ? (
          <div className="plan-warning" role="status">
            {tree.cycles.length ? `${tree.cycles.length} 个循环依赖。` : ""}
            {tree.orphans.length ? `${tree.orphans.length} 个孤立节点。` : ""}
            {Object.keys(tree.missingDependencies).length
              ? `${Object.keys(tree.missingDependencies).length} 个节点缺少依赖。`
              : ""}
          </div>
        ) : null}
        {task.planNodes.length ? (
          <ol className="plan-list">
            {tree.flat.map((entry) => (
              <PlanNode
                key={`${entry.path.join("/")}:${entry.node.nodeId}`}
                entry={entry}
                root={entry.node.nodeId === task.rootNodeId}
              />
            ))}
          </ol>
        ) : (
          <p className="muted-copy">尚未生成执行计划。</p>
        )}
      </section>
      </EvidencePanel>

      <EvidencePanel runtime={runtime} title="全部产物、运行记录与内容校验"
        id="evidence-artifacts"
      >
        <ArtifactWorkbench runtime={runtime} taskId={task.taskId} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-diff" title="代码差异与变更审查">
        <DiffReviewWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-browser" title="浏览器执行记录">
        <BrowserWorkbench runtime={runtime} task={task} />
      </EvidencePanel>

      <EvidencePanel runtime={runtime} id="evidence-terminal" title="工作区终端">
        <TerminalWorkbench runtime={runtime} task={task} />
      </EvidencePanel>
    </div>
  )
}

export function TaskDetail({
  runtime,
  state,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
}) {
  if (!state.taskId && state.phase === "idle") {
    return (
      <section className="task-detail-panel">
        <EmptyState
          title="选择一个任务"
          detail="选择任务后，可以查看执行过程、计划与产物。"
        />
      </section>
    )
  }
  return (
    <section className="task-detail-panel" aria-labelledby="task-detail-heading">
      <PhaseRegion
        phase={state.phase}
        loading={<LoadingState title="正在加载任务" detail={state.taskId} />}
        error={
          state.phase === "not-found" ? (
            <EmptyState
              title="未找到任务"
              detail="当前服务中没有可访问的对应任务。"
              action={
                <button className="button button-secondary" type="button" onClick={() => runtime.router.openTasks()}>
                  返回任务列表
                </button>
              }
            />
          ) : (
            <ErrorState
              title="暂时无法加载任务"
              failure={state.failure}
              onRetry={state.taskId ? () => void runtime.workbench.loadTask(state.taskId!) : undefined}
            />
          )
        }
      >
        {state.task ? <DetailContent runtime={runtime} task={state.task} /> : null}
      </PhaseRegion>
    </section>
  )
}
