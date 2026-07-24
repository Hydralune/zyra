import { useMemo } from "react"
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
import { selectArtifactPanel } from "../../state/panel-selectors.ts"
import { TopologyWorkbench } from "../../features/topology/view/topology-workbench.tsx"
import { WorkerCausalTimelineWorkbench } from "../../features/timeline/view/timeline-workbench.tsx"
import { ArtifactWorkbench } from "../../features/artifacts/view/artifact-workbench.tsx"
import { DiffReviewWorkbench } from "../../features/diff-review/view/diff-review-workbench.tsx"
import { TerminalWorkbench } from "../../features/terminal/view/terminal-workbench.tsx"
import { BrowserWorkbench } from "../../features/browser/view/browser-workbench.tsx"

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
          <strong>{node.title || node.nodeId}</strong>
          {root ? <span className="tag">root</span> : null}
          <span className="tag tag-muted">{node.status}</span>
          {entry.dependencyBlocked ? <span className="tag tag-danger">waiting</span> : null}
        </div>
        {node.description ? <p>{node.description}</p> : null}
        <dl className="inline-facts">
          {node.assignedWorkerId ? (
            <>
              <dt>Worker</dt>
              <dd>{node.assignedWorkerId}</dd>
            </>
          ) : null}
          {node.dependsOn.length ? (
            <>
              <dt>Depends on</dt>
              <dd>{node.dependsOn.join(", ")}</dd>
            </>
          ) : null}
          {node.artifactIds.length ? (
            <>
              <dt>Artifacts</dt>
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
    lifecycleBusy: runtime.api.lifecycle.inFlight().length > 0,
    transportEnabled: runtime.workbench.getSnapshot().transportEnabled,
  })
  const cancel = () => {
    runtime.overlays.open({
      kind: "task-cancel",
      title: "Cancel task",
      replaceKind: true,
      payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal },
    })
  }
  const resume = () => {
    runtime.overlays.open({
      kind: "task-resume",
      title: "Resume task",
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
        Refresh
      </button>
      {actions.cancel.allowed ? (
        <button
          className="button button-danger"
          type="button"
          data-task-detail-action
          title={actions.cancel.reason}
          onClick={cancel}
        >
          Cancel
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
          Resume
        </button>
      ) : null}
    </div>
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
  const artifactPanel = useProjectionSelector(
    runtime,
    selectArtifactPanel(task.taskId),
  )
  return (
    <div className="task-detail-scroll">
      <header className="task-detail-header">
        <div>
          <div className="task-identity">
            <span className={`status-marker status-${task.status}`} aria-hidden="true" />
            <span>{task.status}</span>
            {task.active ? <span className="tag">active</span> : null}
            {task.terminal ? <span className="tag tag-muted">terminal</span> : null}
          </div>
          <h2 id="task-detail-heading" tabIndex={-1}>{task.userGoal || "Untitled task"}</h2>
          <p className="task-id">{task.taskId}</p>
        </div>
        <TaskActions runtime={runtime} task={task} />
      </header>

      <dl className="fact-grid">
        <div>
          <dt>Run</dt>
          <dd>{task.runId}</dd>
        </div>
        <div>
          <dt>Root node</dt>
          <dd>{task.rootNodeId}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{dateTime(task.createdAt)}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{dateTime(task.updatedAt)}</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{formatDuration(metrics.elapsedMs)}</dd>
        </div>
        <div>
          <dt>Health</dt>
          <dd>{taskHealthLabel(metrics)}</dd>
        </div>
        <div>
          <dt>Workers</dt>
          <dd>{metrics.workerCount}</dd>
        </div>
        <div>
          <dt>Dependencies</dt>
          <dd>{metrics.dependencyEdges}</dd>
        </div>
        <div>
          <dt>Projection revision</dt>
          <dd>{projectionRevision}</dd>
        </div>
        <div>
          <dt>Committed sequence</dt>
          <dd>{live.lastSequence}</dd>
        </div>
        <div>
          <dt>Live workers</dt>
          <dd>{live.activeWorkers}/{live.workers}</dd>
        </div>
        <div>
          <dt>Pending approvals</dt>
          <dd>{live.pendingPermissions}</dd>
        </div>
      </dl>

      <section className="detail-section" aria-labelledby="recent-events-heading">
        <div className="section-heading">
          <h3 id="recent-events-heading">Canonical events</h3>
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
            The canonical event projection is restoring or waiting for its first committed event.
          </p>
        )}
      </section>

      <TopologyWorkbench runtime={runtime} task={task} />

      <WorkerCausalTimelineWorkbench runtime={runtime} task={task} />

      <section className="detail-section" aria-labelledby="plan-heading">
        <div className="section-heading">
          <h3 id="plan-heading">Plan</h3>
          <span>{tree.completed}/{tree.total} complete · {tree.progress}%</span>
        </div>
        {tree.cycles.length || tree.orphans.length || Object.keys(tree.missingDependencies).length ? (
          <div className="plan-warning" role="status">
            {tree.cycles.length ? `${tree.cycles.length} cycle(s). ` : ""}
            {tree.orphans.length ? `${tree.orphans.length} orphan(s). ` : ""}
            {Object.keys(tree.missingDependencies).length
              ? `${Object.keys(tree.missingDependencies).length} node(s) with missing dependencies.`
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
          <p className="muted-copy">The runtime has not projected plan nodes yet.</p>
        )}
      </section>

      <section
        className="detail-section"
        aria-label="Artifact projection and viewer"
        data-artifact-selector-count={artifactPanel.rows.length}
        data-artifact-selector-missing-producers={artifactPanel.missingProducerIds.length}
      >
        <ArtifactWorkbench runtime={runtime} taskId={task.taskId} />
      </section>

      <DiffReviewWorkbench runtime={runtime} task={task} />

      <BrowserWorkbench runtime={runtime} task={task} />

      <TerminalWorkbench runtime={runtime} task={task} />
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
          title="Select a task"
          detail="Choose a task to inspect its canonical run, plan, and artifact projection."
        />
      </section>
    )
  }
  return (
    <section className="task-detail-panel" aria-labelledby="task-detail-heading">
      <PhaseRegion
        phase={state.phase}
        loading={<LoadingState title="Loading task" detail={state.taskId} />}
        error={
          state.phase === "not-found" ? (
            <EmptyState
              title="Task not found"
              detail="The route does not resolve to a task visible to this runtime."
              action={
                <button className="button button-secondary" type="button" onClick={() => runtime.router.openTasks()}>
                  Back to tasks
                </button>
              }
            />
          ) : (
            <ErrorState
              title="Task unavailable"
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
