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
      source: "canonical graph projection",
      status: status(topology.nodes.length + topology.edges.length),
      count: topology.nodes.length + topology.edges.length,
      detail: `graph revision ${topology.revision} · ${topology.nodes.length} nodes · ${topology.edges.length} edges`,
    },
    {
      id: "evidence-topology",
      title: "协作降噪",
      requirement: "SCORE-NOISE",
      source: "policy receipts + causal events",
      status: status(communicationCount),
      count: communicationCount,
      detail: "AgentPrune 原因、密度和重复率只在真实 policy/metric receipt 出现后记为可用。",
    },
    {
      id: "evidence-topology",
      title: "神经符号裁决",
      requirement: "REQ-TRACE-01",
      source: "policy evidence API",
      status: status(symbolicCount),
      count: symbolicCount,
      detail: "proposal → constraint verdict → delta → canonical commit；缺一项就保持 missing/degraded。",
    },
    {
      id: "evidence-continuity-placement",
      title: "记忆连续性",
      requirement: "Memory continuity",
      source: "session/checkpoint/memory projection",
      status: status(continuityCount),
      count: continuityCount,
      detail: `${sessions.rows.length} sessions · ${sessions.compactionCount} compact operations`,
    },
    {
      id: "evidence-continuity-placement",
      title: "端边云物理执行",
      requirement: "REQ-EDGE-01",
      source: "physical dispatch receipt",
      status: status(placementCount),
      count: placementCount,
      detail: "候选选择不等于执行；LOCAL/EDGE/CLOUD 只由已验证 physical_identity.location 决定。",
    },
    {
      id: "evidence-recovery",
      title: "故障与恢复",
      requirement: "SCORE-ROBUST",
      source: "causal recovery timeline",
      status: status(recoveryCount),
      count: recoveryCount,
      detail: "失败、取消、terminal failover 和恢复事件保留原始因果引用。",
    },
    {
      id: "evidence-artifacts",
      title: "交付与验证",
      requirement: "Artifact + verifier",
      source: "artifact custody projection",
      status: status(artifacts.rows.length),
      count: artifacts.rows.length,
      detail: `${artifacts.rows.length} artifacts · ${artifacts.missingProducerIds.length} missing producer refs`,
    },
    {
      id: "evidence-controls",
      title: "权限与控制 ACK",
      requirement: "Control custody",
      source: "runtime command/permission queue",
      status: status(queue.rows.length),
      count: queue.rows.length,
      detail: `${queue.pendingPermissions} pending permissions · ${queue.queuedCommands} queued commands`,
    },
  ]
  return (
    <>
      <header className="evidence-route-header">
        <div>
          <p className="eyebrow">Canonical evidence surface</p>
          <h1>运行证据中心</h1>
          <p>
            这里是同一 task/run 的只读投影。CLI 与 Web 不互相同步；两者都从
            Zyra runtime 的 task、event、receipt 与 artifact owner 读取事实。
          </p>
        </div>
        <div className="evidence-route-actions">
          <button
            className="product-button product-button-primary"
            type="button"
            onClick={() => runtime.router.openTask(task.taskId)}
          >
            返回产品视图
          </button>
          <button
            className="product-button product-button-quiet"
            type="button"
            onClick={() => void runtime.liveSync.refreshNow()}
          >
            刷新真实状态
          </button>
        </div>
      </header>
      <dl className="evidence-route-identity">
        <div><dt>Task</dt><dd>{task.taskId}</dd></div>
        <div><dt>Run</dt><dd>{task.runId}</dd></div>
        <div><dt>Session</dt><dd>{task.sessionId ?? "—"}</dd></div>
        <div><dt>Projection revision</dt><dd>{readiness.revision}</dd></div>
        <div><dt>Committed cursor</dt><dd>{readiness.committedSequence}</dd></div>
        <div>
          <dt>Source health</dt>
          <dd data-evidence-source-status={evidenceSourceStatus({
            connected: readiness.connected,
            ready: readiness.ready,
            stale,
            count: 1,
          })}>
            {readiness.connected ? (readiness.ready ? "live" : "stale") : "degraded"}
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
            <span className="evidence-layer-status">{layer.status}</span>
            <p>{layer.detail}</p>
          </a>
        ))}
      </nav>
      <p className="evidence-truth-legend">
        真实性标签来自 API：real 表示 receipt 已通过真实执行门；simulated、degraded、
        missing、stale 均不会显示为成功。terminal 只有 receipt 明确给出 terminal_id
        且 location=local 时才显示为 TERMINAL · LOCAL。
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
          <p className="eyebrow">Dual-domain sealed evidence</p>
          <h2>双域场景与 evidence manifest</h2>
          <p>
            Software delivery 与 cross-source research 使用同一 scenario owner；
            未形成或未验证的 evidence manifest 保持 degraded/error。
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
