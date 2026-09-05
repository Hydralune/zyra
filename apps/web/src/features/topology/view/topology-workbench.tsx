import {
  useEffect,
  useMemo,
  useSyncExternalStore,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import { selectTopologyProjection } from "../projection/index.ts"
import type { NavigationIntent } from "./contracts.ts"
import type { EvidenceNavigationTarget } from "../../evidence/model.ts"
import { TopologyWorkbenchController } from "./controller.ts"
import { topologyControlTransport } from "./controls.ts"
import { TopologyGraphCanvas } from "./graph-canvas.tsx"
import { TopologyInspector } from "./inspector.tsx"
import { TopologyMinimap } from "./minimap.tsx"
import { TopologyToolbar } from "./toolbar.tsx"
import { safeDomId } from "./accessibility.ts"
import { dispatchArtifactNavigation } from "../../artifacts/catalog.ts"
import {
  PolicyEvidenceRuntime,
  PolicyEvidenceView,
} from "../policy/index.ts"
import type {
  PolicyCausalReference,
  PolicyEvidenceTransition,
} from "../../../api/policy-api.ts"

function scrollToTarget(selector: string): boolean {
  if (typeof document === "undefined") return false
  const element = document.querySelector<HTMLElement>(selector)
  if (!element) return false
  element.scrollIntoView({ behavior: "smooth", block: "center" })
  element.focus({ preventScroll: true })
  element.dataset.topologyTarget = "true"
  window.setTimeout(() => {
    delete element.dataset.topologyTarget
  }, 2_500)
  return true
}
function domAttribute(value: string): string {
  return String(value).replaceAll("\\", "\\\\").replaceAll("\"", "\\\"")
}

function handleNavigation(
  runtime: WorkbenchRuntime,
  intent: NavigationIntent,
): void {
  let resolved = false
  if (intent.kind === "open-artifact" && intent.artifactId) {
    if (typeof window !== "undefined") {
      dispatchArtifactNavigation(
        {
          artifactId: intent.artifactId,
          source: "topology",
          sourceEventId: intent.eventId,
          focus: true,
        },
        window,
      )
    }
    resolved =
      scrollToTarget(".artifact-workbench")
      || scrollToTarget(
        `[data-artifact-id="${domAttribute(intent.artifactId)}"]`,
      )
  } else if (intent.kind === "open-timeline" && intent.eventId) {
    resolved = scrollToTarget(
      `[data-event-id="${domAttribute(intent.eventId)}"]`,
    )
  } else if (intent.kind === "open-causation") {
    const id = intent.eventId ?? intent.causationId ?? intent.correlationId
    if (id) {
      resolved =
        scrollToTarget(`[data-event-id="${domAttribute(id)}"]`) ||
        scrollToTarget(`[data-correlation-id="${domAttribute(id)}"]`)
    }
  }
  const label =
    intent.kind === "open-artifact"
      ? `artifact ${intent.artifactId}`
      : intent.kind === "open-timeline"
        ? `timeline event ${intent.eventId}`
        : `causation ${intent.causationId ?? intent.correlationId}`
  if (resolved) {
    runtime.announcer.announce(`Jumped from topology to ${label}.`)
    return
  }
  runtime.notifications.push({
    id: `topology-navigation-${safeDomId(intent.id)}`,
    title: "Canonical target is outside the current task detail window",
    message: `${label} remains linked by canonical evidence and will be available in the dedicated panel.`,
    tone: "warning",
    durationMs: 8_000,
    taskId: intent.taskId,
  })
  runtime.announcer.announce(
    `${label} is linked but not present in the current detail window.`,
  )
}

function handlePolicyNavigation(
  runtime: WorkbenchRuntime,
  reference: PolicyCausalReference,
  transition: PolicyEvidenceTransition,
): void {
  let resolved = false
  if (reference.kind === "artifact" && typeof window !== "undefined") {
    dispatchArtifactNavigation(
      {
        artifactId: reference.id,
        source: "topology",
        sourceEventId: transition.event_id,
        focus: true,
      },
      window,
    )
    resolved =
      scrollToTarget(".artifact-workbench")
      || scrollToTarget(
        `[data-artifact-id="${domAttribute(reference.id)}"]`,
      )
  } else if (reference.kind === "event") {
    resolved = scrollToTarget(
      `[data-event-id="${domAttribute(reference.id)}"]`,
    )
  } else {
    resolved =
      scrollToTarget(`[data-receipt-id="${domAttribute(reference.id)}"]`)
      || scrollToTarget(`[data-event-id="${domAttribute(reference.id)}"]`)
  }
  if (resolved) {
    runtime.announcer.announce(
      `Opened ${reference.kind} ${reference.id} from policy evidence.`,
    )
    return
  }
  runtime.notifications.push({
    id: `policy-evidence-${safeDomId(reference.kind)}-${safeDomId(reference.id)}`,
    title: "Canonical evidence target remains linked",
    message:
      `${reference.kind} ${reference.id} is not mounted in the current viewport. `
      + `Its route and source digest remain in the exported evidence chain.`,
    tone: "warning",
    durationMs: 8_000,
    taskId: transition.task_id,
  })
}

function TopologyStatus({
  loading,
  reconnecting,
  error,
  warnings,
}: {
  loading: boolean
  reconnecting: boolean
  error?: string
  warnings: readonly string[]
}) {
  if (!loading && !reconnecting && !error && warnings.length === 0) return null
  return (
    <div
      className={[
        "topology-status-banner",
        error ? "is-error" : reconnecting ? "is-warning" : "is-loading",
      ].join(" ")}
      role={error ? "alert" : "status"}
    >
      <strong>
        {error
          ? "任务关系图加载异常"
          : reconnecting
            ? "正在恢复事件连接"
            : loading ? "正在加载任务关系图" : "部分关系记录尚未同步完整"}
      </strong>
      {error ? <p>{error}</p> : null}
      {warnings.length > 0 ? <details><summary>查看同步详情</summary><p>{warnings.join(" · ")}</p></details> : null}
    </div>
  )
}

function SearchResults({
  controller,
  matches,
}: {
  controller: TopologyWorkbenchController
  matches: ReturnType<TopologyWorkbenchController["getSnapshot"]>["filtered"]["searchMatches"]
}) {
  if (matches.length === 0) return null
  return (
    <div className="topology-search-results" aria-label="Topology search results">
      <span>找到 {matches.length} 项</span>
      <div>
        {matches.slice(0, 20).map((match) => (
          <button
            key={`${match.kind}:${match.entityId}`}
            type="button"
            onClick={() => {
              if (controller.select(match.kind, match.entityId, "search")) {
                if (match.kind === "node") {
                  controller.viewport.expandToNode(match.entityId)
                  controller.viewport.focusNode(match.entityId, 1.1)
                }
              }
            }}
          >
            <strong>{match.primary}</strong>
            <span>{match.kind} · {match.secondary}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

export function TopologyWorkbench({
  runtime,
  task,
  onEvidenceNavigate,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  onEvidenceNavigate?: (target: EvidenceNavigationTarget) => void
}) {
  const selector = useMemo(
    () =>
      selectTopologyProjection(task.taskId, {
        includeRemoved: true,
        includeRejected: true,
        includeNonEffective: true,
        maximumEntities: 250_000,
        maximumEvidenceEvents: 50_000,
      }),
    [task.taskId],
  )
  const projection = useProjectionSelector(runtime, selector)
  const controller = useMemo(
    () =>
      new TopologyWorkbenchController(task.taskId, {
        maximumRenderedNodes: 800,
        maximumRenderedEdges: 1_600,
        largeGraphThreshold: 1_000,
        controlTransport: topologyControlTransport((input) =>
          runtime.api.tasks.controlCommand({
            taskId: input.taskId,
            runId: input.runId,
            text: input.text,
            arguments: input.arguments,
            requestId: input.requestId,
            commandId: input.commandId,
            idempotencyKey: input.idempotencyKey,
            actorId: input.actorId,
            sealed: input.sealed,
            expectedRevision: input.expectedRevision,
            signal: input.signal,
          }),
        ),
      }),
    [runtime, task.taskId],
  )
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  )
  useEffect(() => {
    try {
      controller.project(projection)
    } catch (error) {
      controller.setConnectionState({
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }, [controller, projection])
  useEffect(
    () => () => controller.close("Topology route unmounted."),
    [controller],
  )
  useEffect(() => {
    for (const message of controller.takeAnnouncements()) {
      runtime.announcer.announce(message)
    }
  }, [controller, runtime, snapshot.revision])
  return (
    <section
      className="topology-workbench"
      aria-labelledby="topology-workbench-heading"
      data-topology-revision={snapshot.model.graphRevision}
      data-projection-revision={snapshot.model.projectionRevision}
      data-task-id={task.taskId}
    >
      <header className="topology-workbench-heading">
        <div>
          <h3 id="topology-workbench-heading">任务关系图</h3>
          <p className="muted-copy">点击节点查看详情，拖动画布移动视图。</p>
        </div>
        <dl className="topology-revision-facts">
          <div>
            <dt>数据版本</dt>
            <dd>{snapshot.model.projectionRevision}</dd>
          </div>
          <div>
            <dt>关系图</dt>
            <dd>{snapshot.model.graphRevision}</dd>
          </div>
          <div>
            <dt>已提交</dt>
            <dd>{snapshot.model.commitRevision}</dd>
          </div>
          <div>
            <dt>待同步</dt>
            <dd>{snapshot.model.diagnostics.lag}</dd>
          </div>
        </dl>
      </header>
      <TopologyStatus
        loading={snapshot.loading}
        reconnecting={snapshot.reconnecting}
        error={snapshot.error}
        warnings={snapshot.model.diagnostics.warnings}
      />
      <TopologyToolbar controller={controller} snapshot={snapshot} />
      <SearchResults
        controller={controller}
        matches={snapshot.filtered.searchMatches}
      />
      <div className="topology-workspace">
        <div className="topology-graph-region" id="topology-graph-region">
          <TopologyGraphCanvas controller={controller} snapshot={snapshot} />
          <TopologyMinimap controller={controller} snapshot={snapshot} />
          {snapshot.model.nodes.length === 0 && !snapshot.loading ? (
            <div className="topology-empty-overlay">
              <strong>暂无任务关系记录</strong>
              <p>该任务尚未提供可展示的关系图。</p>
            </div>
          ) : null}
        </div>
        <TopologyInspector
          controller={controller}
          snapshot={snapshot}
          task={task}
          onNavigate={(intent) => onEvidenceNavigate && (intent.artifactId || intent.eventId)
            ? onEvidenceNavigate({ artifactId: intent.artifactId, eventId: intent.eventId })
            : handleNavigation(runtime, intent)}
        />
      </div>
      <div className="visually-hidden" aria-live="polite" aria-atomic="true">
        {controller.announcements().at(-1) ?? ""}
      </div>
    </section>
  )
}

export function PolicyWorkbench({ runtime, task, onEvidenceNavigate }: {
  runtime: WorkbenchRuntime; task: TaskProjection; onEvidenceNavigate?: (target: EvidenceNavigationTarget) => void
}) {
  const policyEvidence = useMemo(() => new PolicyEvidenceRuntime({
    api: runtime.api.policy, query: { taskId: task.taskId }, pageLimit: 100, maximumTransitions: 5_000,
  }), [runtime.api.policy, task.taskId])
  return <PolicyEvidenceView runtime={policyEvidence} title="策略与执行回执"
    onNavigate={(reference, transition) => onEvidenceNavigate && ["artifact", "event"].includes(reference.kind)
      ? onEvidenceNavigate(reference.kind === "artifact" ? { artifactId: reference.id } : { eventId: reference.id })
      : handlePolicyNavigation(runtime, reference, transition)} />
}
