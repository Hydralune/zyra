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
import { TopologyWorkbenchController } from "./controller.ts"
import { topologyControlTransport } from "./controls.ts"
import { TopologyGraphCanvas } from "./graph-canvas.tsx"
import { TopologyInspector } from "./inspector.tsx"
import { TopologyMinimap } from "./minimap.tsx"
import { TopologyToolbar } from "./toolbar.tsx"
import { safeDomId } from "./accessibility.ts"

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
    resolved = scrollToTarget(
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
          ? "Topology projection integrity error"
          : reconnecting
            ? "Reconnecting canonical event stream"
            : "Restoring topology snapshot"}
      </strong>
      {error ? <p>{error}</p> : null}
      {warnings.length > 0 ? <p>{warnings.join(" · ")}</p> : null}
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
      <span>{matches.length} matches</span>
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
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
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
    >
      <header className="topology-workbench-heading">
        <div>
          <p className="eyebrow">Dynamic topology</p>
          <h3 id="topology-workbench-heading">Live graph and recovery control</h3>
        </div>
        <dl className="topology-revision-facts">
          <div>
            <dt>Projection</dt>
            <dd>{snapshot.model.projectionRevision}</dd>
          </div>
          <div>
            <dt>Graph</dt>
            <dd>{snapshot.model.graphRevision}</dd>
          </div>
          <div>
            <dt>Commit</dt>
            <dd>{snapshot.model.commitRevision}</dd>
          </div>
          <div>
            <dt>Lag</dt>
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
              <strong>No topology entities</strong>
              <p>The canonical projection has not committed graph nodes for this task.</p>
            </div>
          ) : null}
        </div>
        <TopologyInspector
          controller={controller}
          snapshot={snapshot}
          task={task}
          onNavigate={(intent) => handleNavigation(runtime, intent)}
        />
      </div>
      <div className="visually-hidden" aria-live="polite" aria-atomic="true">
        {controller.announcements().at(-1) ?? ""}
      </div>
    </section>
  )
}
