import { phaseLabel } from "../../shell/product-copy.ts"
import { useEffect, useMemo } from "react"
import type { TaskProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskDetailState } from "../../shell/workbench-controller.ts"
import { TaskDetail } from "../../components/tasks/task-detail.tsx"
import { useProjectionSelector } from "../../app/hooks.ts"
import {
  selectArtifactPanel,
  selectOperatorQueue,
  selectProjectionReadiness,
  selectSessionPanel,
  selectTimelinePanel,
  selectTopologyPanel,
} from "../../state/panel-selectors.ts"
import { ScenarioWorkbench } from "../scenarios/index.ts"

export type EvidenceSourceStatus = "live" | "degraded" | "missing" | "stale"

export interface EvidenceLayerItem {
  id: string
  title: string
  requirement: string
  source: string
  status: EvidenceSourceStatus
  count: number
  detail: string
}

export function evidenceSourceStatus(input: {
  connected: boolean
  ready: boolean
  stale: boolean
  count: number
}): EvidenceSourceStatus {
  if (!input.connected) return "degraded"
  if (input.stale || !input.ready) return "stale"
  if (input.count <= 0) return "missing"
  return "live"
}

function EvidenceLayerIndex({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const readinessSelector = useMemo(
    () => selectProjectionReadiness(task.taskId),
    [task.taskId],
  )
  const topologySelector = useMemo(
    () => selectTopologyPanel(task.taskId),
    [task.taskId],
  )
  const timelineSelector = useMemo(
    () => selectTimelinePanel(task.taskId, {
      limit: 1_000,
      includeNonEffective: true,
    }),
    [task.taskId],
  )
  const artifactSelector = useMemo(
    () => selectArtifactPanel(task.taskId),
    [task.taskId],
  )
  const sessionSelector = useMemo(
    () => selectSessionPanel(task.taskId),
    [task.taskId],
  )
  const queueSelector = useMemo(
    () => selectOperatorQueue(task.taskId),
    [task.taskId],
  )
  const readiness = useProjectionSelector(runtime, readinessSelector)
  const topology = useProjectionSelector(runtime, topologySelector)
  const timeline = useProjectionSelector(runtime, timelineSelector)
  const artifacts = useProjectionSelector(runtime, artifactSelector)
  const sessions = useProjectionSelector(runtime, sessionSelector)
  const queue = useProjectionSelector(runtime, queueSelector)
  const eventTypes = timeline.entries.map((entry) => entry.event.eventType)
  const countMatching = (pattern: RegExp) =>
    eventTypes.filter((eventType) => pattern.test(eventType)).length
  const stale = readiness.lag > 0 || readiness.integrityWarnings.length > 0
  const status = (count: number) => evidenceSourceStatus({
    connected: readiness.connected,
    ready: readiness.ready,
    stale,
    count,
  })
  const communicationCount = countMatching(/prun|communication|entropy|message/i)
  const symbolicCount = countMatching(/constraint|symbolic|proposal|policy|graph/i)
  const continuityCount = sessions.compactionCount + countMatching(
    /checkpoint|compact|restore|memory|requirement/i,
  )
  const placementCount = countMatching(
    /backend\.dispatch|backend\.failover|scheduler|placement|lease|provider/i,
  )
  const recoveryCount = countMatching(/recovery|failover|fault|failed/i)
  const layers: readonly EvidenceLayerItem[] = [
    {
      id: "evidence-topology",
      title: "动态拓扑",
      requirement: "SCORE-ORG",
      source: "任务关系图",
      status: status(topology.nodes.length + topology.edges.length),
      count: topology.nodes.length + topology.edges.length,
      detail: `版本 ${topology.revision} · ${topology.nodes.length} 个节点 · ${topology.edges.length} 条关系`,
    },
    {
      id: "evidence-topology",
      title: "协作降噪",
      requirement: "SCORE-NOISE",
      source: "策略回执与事件",
      status: status(communicationCount),
      count: communicationCount,
      detail: "协作消息与策略记录，可展开查看原因和指标。",
    },
    {
      id: "evidence-topology",
      title: "神经符号裁决",
      requirement: "REQ-TRACE-01",
      source: "策略验证记录",
      status: status(symbolicCount),
      count: symbolicCount,
      detail: "查看方案、约束检查、变更和最终提交的对应关系。",
    },
    {
      id: "evidence-continuity-placement",
      title: "记忆连续性",
      requirement: "Memory continuity",
      source: "会话、检查点与记忆",
      status: status(continuityCount),
      count: continuityCount,
      detail: `${sessions.rows.length} 个会话 · ${sessions.compactionCount} 次压缩`,
    },
    {
      id: "evidence-continuity-placement",
      title: "端边云物理执行",
      requirement: "REQ-EDGE-01",
      source: "执行调度回执",
      status: status(placementCount),
      count: placementCount,
      detail: "实际执行位置由已验证的执行回执提供。",
    },
    {
      id: "evidence-recovery",
      title: "故障与恢复",
      requirement: "SCORE-ROBUST",
      source: "故障恢复时间线",
      status: status(recoveryCount),
      count: recoveryCount,
      detail: "查看失败、取消和恢复事件及其关联记录。",
    },
    {
      id: "evidence-artifacts",
      title: "交付与验证",
      requirement: "Artifact + verifier",
      source: "产物记录",
      status: status(artifacts.rows.length),
      count: artifacts.rows.length,
      detail: `${artifacts.rows.length} 项产物 · ${artifacts.missingProducerIds.length} 项来源待确认`,
    },
    {
      id: "evidence-controls",
      title: "权限与控制回执",
      requirement: "Control custody",
      source: "命令与审批队列",
      status: status(queue.rows.length),
      count: queue.rows.length,
      detail: `${queue.pendingPermissions} 项待审批 · ${queue.queuedCommands} 条排队命令`,
    },
  ]
  return (
    <>
      <header className="evidence-route-header">
        <div>
          <p className="eyebrow">任务运行记录</p>
          <h1>运行证据中心</h1>
          <p>
            查看当前任务的执行过程、运行产物和验证记录。这里与 CLI 读取同一份后端数据。
          </p>
        </div>
        <div className="evidence-route-actions">
          <button
            className="product-button product-button-primary"
            type="button"
            onClick={() => runtime.router.openTask(task.taskId)}
          >
            返回对话
          </button>
          <button
            className="product-button product-button-quiet"
            type="button"
            onClick={() => void runtime.liveSync.refreshNow()}
          >
            刷新状态
          </button>
        </div>
      </header>
      <dl className="evidence-route-identity">
        <div><dt>任务编号</dt><dd>{task.taskId}</dd></div>
        <div><dt>运行编号</dt><dd>{task.runId}</dd></div>
        <div><dt>会话编号</dt><dd>{task.sessionId ?? "—"}</dd></div>
        <div><dt>数据版本</dt><dd>{readiness.revision}</dd></div>
        <div><dt>事件序号</dt><dd>{readiness.committedSequence}</dd></div>
        <div>
          <dt>数据状态</dt>
          <dd data-evidence-source-status={evidenceSourceStatus({
            connected: readiness.connected,
            ready: readiness.ready,
            stale,
            count: 1,
          })}>
            {phaseLabel(readiness.connected ? (readiness.ready ? "live" : "stale") : "degraded")}
          </dd>
        </div>
      </dl>
      {readiness.integrityWarnings.length ? (
        <div className="evidence-route-warning" role="alert">
          证据投影不是最新或不完整：{readiness.integrityWarnings.join(" · ")}
        </div>
      ) : null}
      <nav className="evidence-layer-index" aria-label="证据层导航">
        {layers.map((layer) => (
          <a
            href={`#${layer.id}`}
            key={`${layer.requirement}:${layer.title}`}
            data-evidence-source-status={layer.status}
          >
            <span>
              <strong>{layer.title}</strong>
              <small>{layer.requirement} · {layer.source}</small>
            </span>
            <span className="evidence-layer-count">{layer.count}</span>
            <span className="evidence-layer-status">{phaseLabel(layer.status)}</span>
            <p>{layer.detail}</p>
          </a>
        ))}
      </nav>
      <p className="evidence-truth-legend">
        记录状态由后端提供。暂无记录、连接不稳定或待更新表示证据尚不充分；展开对应面板可查看原始记录与验证结果。
      </p>
    </>
  )
}

function LoadedEvidenceWorkbench({
  runtime,
  state,
  task,
  section,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  task: TaskProjection
  section?: string
}) {
  useEffect(() => {
    if (!section) return
    const target = document.getElementById(section)
    if (!target) return
    target.scrollIntoView({ block: "start" })
    target.tabIndex = -1
    target.focus({ preventScroll: true })
  }, [section, task.taskId])
  return (
    <section className="evidence-route" data-task-id={task.taskId}>
      <EvidenceLayerIndex runtime={runtime} task={task} />
      <TaskDetail runtime={runtime} state={state} />
      <section id="evidence-sealed-scenarios" className="evidence-scenario-layer">
        <header>
          <p className="eyebrow">场景验收</p>
          <h2>场景与证据清单</h2>
          <p>
            查看软件交付和跨源研究的场景记录及验收结果。
          </p>
        </header>
        <ScenarioWorkbench runtime={runtime.scenarioConsole} />
      </section>
    </section>
  )
}

export function EvidenceWorkbench({
  runtime,
  state,
  section,
}: {
  runtime: WorkbenchRuntime
  state: TaskDetailState
  section?: string
}) {
  if (state.task && state.task.taskId === state.taskId) {
    return (
      <LoadedEvidenceWorkbench
        runtime={runtime}
        state={state}
        task={state.task}
        section={section}
      />
    )
  }
  return (
    <section className="evidence-route evidence-route-loading">
      <TaskDetail runtime={runtime} state={state} />
    </section>
  )
}
