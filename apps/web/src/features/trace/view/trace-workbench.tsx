import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
  type KeyboardEvent,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import {
  TraceCompleteness,
  TraceNodeKind,
  TraceSemanticKind,
  TraceViewKind,
  type TraceNavigationTarget,
  type TraceNode,
  type TraceSemanticKindValue,
} from "../contracts.ts"
import { selectCausalTrace } from "../projector.ts"
import { TraceWorkbenchController } from "./controller.ts"

function formatDuration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 ms"
  if (value < 1_000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1_000)}s`
}

function targetLabel(target: TraceNavigationTarget): string {
  switch (target.view) {
    case TraceViewKind.TIMELINE:
      return "Timeline"
    case TraceViewKind.TOPOLOGY:
      return "Topology"
    case TraceViewKind.TERMINAL:
      return "Terminal"
    case TraceViewKind.BROWSER:
      return "Browser"
    case TraceViewKind.ARTIFACT:
      return "Artifact"
    case TraceViewKind.DIFF:
      return "Diff"
    default:
      return "Trace"
  }
}

function TraceMetrics({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const projection = snapshot.projection
  const diagnostics = projection.diagnostics
  return (
    <dl className="fact-grid trace-metrics">
      <div><dt>Canonical events</dt><dd>{diagnostics.eventCount}</dd></div>
      <div><dt>Trace nodes</dt><dd>{diagnostics.nodeCount}</dd></div>
      <div><dt>Typed edges</dt><dd>{diagnostics.edgeCount}</dd></div>
      <div><dt>Critical duration</dt><dd>{formatDuration(projection.criticalPath.durationMs)}</dd></div>
      <div><dt>Projection lag</dt><dd>{diagnostics.lag}</dd></div>
      <div><dt>Partial / late</dt><dd>{diagnostics.partialCount} / {diagnostics.lateCount}</dd></div>
      <div><dt>Orphan / quarantine</dt><dd>{diagnostics.orphanCount} / {diagnostics.quarantineCount}</dd></div>
      <div><dt>Visible / hidden</dt><dd>{snapshot.folded.nodes.length} / {snapshot.folded.hiddenNodeKeys.length}</dd></div>
    </dl>
  )
}

function SemanticToggle({
  controller,
  semantic,
  label,
}: {
  controller: TraceWorkbenchController
  semantic: TraceSemanticKindValue
  label: string
}) {
  const selected = controller.getSnapshot().filter.semantics?.includes(semantic) ?? false
  return (
    <button
      type="button"
      className={`button button-secondary${selected ? " is-active" : ""}`}
      aria-pressed={selected}
      onClick={() => {
        const current = controller.getSnapshot().filter.semantics ?? []
        controller.patchFilter({
          semantics: selected
            ? current.filter((value) => value !== semantic)
            : [...current, semantic],
        })
      }}
    >
      {label}
    </button>
  )
}

function TraceToolbar({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const onSearch = (event: ChangeEvent<HTMLInputElement>) => {
    controller.patchFilter({ search: event.currentTarget.value })
  }
  return (
    <div className="trace-toolbar" role="group" aria-label="Causal trace filters">
      <label className="trace-search">
        <span>Search trace</span>
        <input
          type="search"
          value={snapshot.filter.search ?? ""}
          placeholder="event, span:, worker:, artifact:"
          onChange={onSearch}
        />
      </label>
      <div className="trace-toolbar-actions">
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.PERMISSION} label="Permission" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.PLACEMENT} label="Placement" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.FAULT} label="Fault" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.RECOVERY} label="Recovery" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.MCP} label="MCP" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.SKILL} label="Skill" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.SUBAGENT} label="Subagent" />
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.filter.criticalOnly === true}
          onClick={() => controller.patchFilter({ criticalOnly: !snapshot.filter.criticalOnly })}
        >
          Critical path
        </button>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.filter.failuresOnly === true}
          onClick={() => controller.patchFilter({ failuresOnly: !snapshot.filter.failuresOnly })}
        >
          Failures
        </button>
        <button type="button" className="button button-secondary" onClick={() => controller.resetFilter()}>
          Reset
        </button>
      </div>
      <div className="trace-toolbar-actions">
        <label>
          <span>Fold by </span>
          <select
            value={snapshot.fold.mode}
            onChange={(event) => controller.patchFold({
              mode: event.currentTarget.value as "manual" | "span" | "worker" | "semantic",
            })}
          >
            <option value="span">Span</option>
            <option value="worker">Worker</option>
            <option value="semantic">Semantic</option>
            <option value="manual">Hierarchy</option>
          </select>
        </label>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.fold.preserveCritical}
          onClick={() => controller.patchFold({ preserveCritical: !snapshot.fold.preserveCritical })}
        >
          Preserve critical
        </button>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.fold.preserveFailures}
          onClick={() => controller.patchFold({ preserveFailures: !snapshot.fold.preserveFailures })}
        >
          Preserve faults
        </button>
      </div>
    </div>
  )
}

function TraceIntegrity({ controller }: { controller: TraceWorkbenchController }) {
  const projection = controller.getSnapshot().projection
  const diagnostics = projection.diagnostics
  const reconciliation = controller.reconciliation()
  if (!diagnostics.warnings.length && !reconciliation?.changes.length) return null
  return (
    <aside className="trace-integrity" aria-label="Trace integrity and reconciliation">
      <div className="section-heading">
        <h4>Integrity and late reconciliation</h4>
        <span>{diagnostics.quarantineCount} quarantined</span>
      </div>
      {diagnostics.warnings.length ? (
        <ul>
          {diagnostics.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}
      {reconciliation?.changes.length ? (
        <details>
          <summary>{reconciliation.changes.length} change(s) in revision {reconciliation.currentRevision}</summary>
          <ol>
            {reconciliation.changes.slice(-20).map((change) => (
              <li key={change.id} data-trace-reconciliation-kind={change.kind}>
                <strong>{change.kind}</strong> · {change.message}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      {projection.quarantine.length ? (
        <details>
          <summary>Quarantined records</summary>
          <ol>
            {projection.quarantine.slice(0, 40).map((record) => (
              <li key={record.id} data-trace-quarantine-code={record.code}>
                <strong>{record.code}</strong> · {record.message}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </aside>
  )
}

function CriticalPathSummary({ controller }: { controller: TraceWorkbenchController }) {
  const path = controller.getSnapshot().projection.criticalPath
  if (!path.nodeKeys.length) return null
  return (
    <aside className="trace-critical-path" aria-label="Critical causal path">
      <div className="section-heading">
        <h4>Critical path</h4>
        <span>{path.nodeKeys.length} nodes · {formatDuration(path.durationMs)}</span>
      </div>
      <div className="trace-path-strip">
        {path.nodeKeys.slice(0, 24).map((key, index) => {
          const node = controller.getSnapshot().projection.nodesByKey[key]
          if (!node) return null
          return (
            <button
              type="button"
              key={key}
              className="trace-path-step"
              title={node.summary}
              onClick={() => controller.reveal(key)}
            >
              <span>{index + 1}</span>
              {node.title}
            </button>
          )
        })}
      </div>
      {path.bottlenecks.length ? (
        <ul className="trace-bottlenecks">
          {path.bottlenecks.slice(0, 5).map((bottleneck) => (
            <li key={bottleneck.id}>
              <button type="button" onClick={() => controller.reveal(bottleneck.nodeKey)}>
                {bottleneck.kind}: {bottleneck.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </aside>
  )
}

function TraceEvidence({ node }: { node: TraceNode }) {
  return (
    <dl className="inline-facts trace-evidence">
      <dt>Event</dt><dd>{node.primaryEventId ?? "—"}</dd>
      {node.refs.spanId ? <><dt>Span</dt><dd>{node.refs.spanId}</dd></> : null}
      {node.refs.workerId ? <><dt>Worker</dt><dd>{node.refs.workerId}</dd></> : null}
      {node.refs.toolCallId ? <><dt>Tool call</dt><dd>{node.refs.toolCallId}</dd></> : null}
      {node.refs.providerId ? <><dt>Provider</dt><dd>{node.refs.providerId}</dd></> : null}
      {node.refs.permissionId ? <><dt>Permission</dt><dd>{node.refs.permissionId}</dd></> : null}
      {node.refs.failureId ? <><dt>Failure</dt><dd>{node.refs.failureId}</dd></> : null}
      {node.refs.recoveryId ? <><dt>Recovery</dt><dd>{node.refs.recoveryId}</dd></> : null}
      {node.refs.mutationId ? <><dt>Mutation</dt><dd>{node.refs.mutationId}</dd></> : null}
      {node.refs.artifactIds.length ? <><dt>Artifacts</dt><dd>{node.refs.artifactIds.join(", ")}</dd></> : null}
    </dl>
  )
}

function TraceRow({
  controller,
  node,
  selected,
  expanded,
}: {
  controller: TraceWorkbenchController
  node: TraceNode
  selected: boolean
  expanded: boolean
}) {
  const ref = useRef<HTMLElement | null>(null)
  const targets = controller.navigationTargets(node.key).filter((target) => target.view !== TraceViewKind.TRACE)
  const pinned = controller.getSnapshot().pins.some((reference) => reference.nodeKey === node.key)
  useEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.borderBoxSize?.[0]?.blockSize
      controller.measure(node.key, measured ?? element.getBoundingClientRect().height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [controller, node.key])
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      controller.toggleExpanded(node.key)
    } else if (event.key === "p" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      controller.togglePin(node.key)
    }
  }
  return (
    <article
      ref={ref}
      className={`trace-row${selected ? " is-selected" : ""}${node.critical ? " is-critical" : ""}`}
      tabIndex={0}
      aria-expanded={expanded}
      data-trace-node-key={node.key}
      data-trace-node-kind={node.identity.kind}
      data-trace-completeness={node.completeness}
      data-event-id={node.primaryEventId}
      data-span-id={node.refs.spanId}
      data-worker-id={node.refs.workerId}
      data-tool-call-id={node.refs.toolCallId}
      data-artifact-id={node.refs.artifactIds[0]}
      data-mutation-id={node.refs.mutationId}
      onFocus={() => controller.select(node.key, { expand: false, anchor: false })}
      onClick={() => controller.select(node.key, { expand: false, anchor: false })}
      onKeyDown={onKeyDown}
    >
      <header className="trace-row-heading">
        <div>
          <span className={`status-marker status-${node.terminal ? "completed" : "running"}`} aria-hidden="true" />
          <strong>{node.title}</strong>
          <span className="tag tag-muted">{node.identity.kind}</span>
          <span className={`tag${node.completeness === TraceCompleteness.COMPLETE ? " tag-muted" : " tag-danger"}`}>
            {node.completeness}
          </span>
          {node.critical ? <span className="tag">critical</span> : null}
          {node.retryCount ? <span className="tag tag-danger">retry {node.retryCount}</span> : null}
        </div>
        <span>#{node.sequence}{node.endSequence !== node.sequence ? `–${node.endSequence}` : ""}</span>
      </header>
      <p>{node.summary}</p>
      <div className="trace-tags">
        {node.semantics.map((semantic) => <span className="tag tag-muted" key={semantic}>{semantic}</span>)}
      </div>
      <div className="trace-row-actions">
        <button type="button" onClick={(event) => { event.stopPropagation(); controller.toggleExpanded(node.key) }}>
          {expanded ? "Less" : "Inspect"}
        </button>
        <button type="button" onClick={(event) => { event.stopPropagation(); controller.togglePin(node.key) }}>
          {pinned ? "Unpin" : "Pin for report"}
        </button>
        {targets.slice(0, 8).map((target) => (
          <button
            type="button"
            key={target.id}
            title={target.label}
            onClick={(event) => {
              event.stopPropagation()
              controller.navigate(target)
            }}
          >
            {targetLabel(target)}
          </button>
        ))}
      </div>
      {expanded ? (
        <div className="trace-row-details">
          <TraceEvidence node={node} />
          <dl className="inline-facts">
            <dt>Duration</dt><dd>{formatDuration(node.durationMs)}</dd>
            <dt>Contribution</dt><dd>{node.contributionScore.toFixed(1)}</dd>
            <dt>Incoming / outgoing</dt><dd>{node.incomingEdgeIds.length} / {node.outgoingEdgeIds.length}</dd>
            <dt>Events</dt><dd>{node.eventIds.length}</dd>
          </dl>
          {node.diagnostics.length ? (
            <ul className="trace-diagnostics">
              {node.diagnostics.map((diagnostic) => <li key={diagnostic}>{diagnostic}</li>)}
            </ul>
          ) : null}
          {Object.keys(node.safeAttributes).length ? (
            <details>
              <summary>Safe canonical attributes</summary>
              <dl className="inline-facts">
                {Object.entries(node.safeAttributes).map(([key, value]) => (
                  <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>
                ))}
              </dl>
            </details>
          ) : null}
        </div>
      ) : null}
    </article>
  )
}

function VirtualTraceList({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const container = useRef<HTMLDivElement | null>(null)
  const onScroll = () => {
    const element = container.current
    if (element) controller.setViewport(element.scrollTop, element.clientHeight)
  }
  if (!snapshot.folded.nodes.length) {
    return <p className="muted-copy">No trace nodes match the active filters.</p>
  }
  return (
    <div
      ref={container}
      className="trace-virtual-scroll"
      data-trace-virtual-total={snapshot.virtualWindow.total}
      data-trace-virtual-mounted={snapshot.virtualWindow.items.length}
      onScroll={onScroll}
    >
      <div className="trace-virtual-space" style={{ height: `${snapshot.virtualWindow.totalHeight}px`, position: "relative" }}>
        {snapshot.virtualWindow.items.map((item) => {
          const node = snapshot.folded.nodes[item.index]
          if (!node || node.key !== item.key) return null
          return (
            <div
              key={item.key}
              className="trace-virtual-item"
              style={{ position: "absolute", top: `${item.top}px`, left: 0, right: 0 }}
            >
              <TraceRow
                controller={controller}
                node={node}
                selected={snapshot.selectedNodeKey === node.key}
                expanded={snapshot.expandedNodeKeys.has(node.key)}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PinnedReferences({ controller }: { controller: TraceWorkbenchController }) {
  const pins = controller.getSnapshot().pins
  if (!pins.length) return null
  return (
    <aside className="trace-pins" aria-label="Pinned final report references">
      <div className="section-heading">
        <h4>Final report references</h4>
        <span>{pins.length}</span>
      </div>
      <ol>
        {pins.map((reference) => (
          <li key={reference.id} data-trace-report-reference={reference.id}>
            <button type="button" onClick={() => controller.reveal(reference.nodeKey)}>
              {reference.label}{reference.stale ? " · stale" : ""}
            </button>
            <button type="button" onClick={() => controller.unpin(reference.nodeKey)}>Remove</button>
          </li>
        ))}
      </ol>
      <details>
        <summary>Report-ready Markdown</summary>
        <pre>{controller.exportReport().markdown}</pre>
      </details>
    </aside>
  )
}

export function CausalTraceWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const selector = useMemo(() => selectCausalTrace(task.taskId), [task.taskId])
  const projection = useProjectionSelector(runtime, selector)
  const controller = useMemo(
    () => new TraceWorkbenchController(task.taskId, projection),
    [task.taskId],
  )
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  )
  const [collapsed, setCollapsed] = useState(false)
  useEffect(() => {
    controller.updateProjection(projection)
  }, [controller, projection])
  useEffect(() => () => controller.close("Trace workbench unmounted."), [controller])
  return (
    <section
      className="detail-section causal-trace-workbench"
      aria-labelledby="causal-trace-heading"
      data-task-id={task.taskId}
      data-trace-state-owner="CanonicalProjectionStore"
      data-trace-second-replay-store="false"
      data-trace-close-stops-task="false"
      data-trace-projection-revision={snapshot.projection.projectionRevision}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Canonical cross-view evidence</p>
          <h3 id="causal-trace-heading">Causal trace</h3>
        </div>
        <div className="trace-heading-actions">
          {snapshot.activeNavigation ? (
            <span role="status" data-trace-navigation-phase={snapshot.activeNavigation.phase}>
              {snapshot.activeNavigation.phase}: {snapshot.activeNavigation.target.label}
            </span>
          ) : null}
          <button type="button" className="button button-secondary" onClick={() => controller.navigateBack()}>
            Back across views
          </button>
          <button type="button" className="button button-secondary" onClick={() => setCollapsed((value) => !value)}>
            {collapsed ? "Open trace" : "Close viewer"}
          </button>
        </div>
      </div>
      {collapsed ? (
        <p className="muted-copy" data-trace-viewer-closed="true">
          The trace viewer is closed locally. Task {task.taskId} and all worker sessions continue unchanged.
        </p>
      ) : (
        <>
          <TraceMetrics controller={controller} />
          <TraceToolbar controller={controller} />
          <TraceIntegrity controller={controller} />
          <CriticalPathSummary controller={controller} />
          <p className="muted-copy">
            Cross-view joins use canonical event, span, worker, tool, artifact, mutation, PTY, browser, and provider IDs. Focus a typed row in another view and press Alt+Enter to return here.
          </p>
          <VirtualTraceList controller={controller} />
          <PinnedReferences controller={controller} />
        </>
      )}
    </section>
  )
}
