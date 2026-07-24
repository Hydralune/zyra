import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import {
  TimelineEventKind,
  TimelinePhase,
  availableTimelineKinds,
  availableTimelinePhases,
  availableTimelineWorkers,
  selectWorkerCausalTimeline,
  type TimelineDrilldownTarget,
  type TimelineHiddenGap,
  type TimelineRow,
} from "../projection/index.ts"
import {
  ScaledTimelineRuntime,
  type TimelineMeasuredRow,
  type TimelineRowOverlay,
} from "../scale/index.ts"
import {
  TimelineWorkbenchController,
  type TimelineWorkbenchSnapshot,
} from "./controller.ts"
import { RecoveryControlPanel } from "./recovery-control-panel.tsx"
import { dispatchArtifactNavigation } from "../../artifacts/catalog.ts"

function formatTime(value: string): string {
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) return value
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(parsed)
}

function formatDuration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 ms"
  if (value < 1_000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1_000).toFixed(1)} s`
  const minutes = Math.floor(value / 60_000)
  const seconds = Math.round((value % 60_000) / 1_000)
  return `${minutes}m ${seconds}s`
}

function phaseTone(phase: TimelineRow["phase"]): string {
  switch (phase) {
    case TimelinePhase.FAILED:
      return "danger"
    case TimelinePhase.CANCELLED:
      return "muted"
    case TimelinePhase.RECOVERING:
      return "warning"
    case TimelinePhase.WAITING_POLICY:
      return "policy"
    case TimelinePhase.WAITING_TOOL:
      return "tool"
    case TimelinePhase.COMPLETED:
      return "success"
    default:
      return "active"
  }
}

function kindSymbol(kind: TimelineRow["rowKind"]): string {
  switch (kind) {
    case TimelineEventKind.FAILURE:
      return "!"
    case TimelineEventKind.RECOVERY:
      return "↻"
    case TimelineEventKind.TOOL:
      return "⌁"
    case TimelineEventKind.POLICY:
      return "◇"
    case TimelineEventKind.ARTIFACT:
      return "◆"
    case TimelineEventKind.BROWSER_STEP:
      return "◎"
    case TimelineEventKind.BACKGROUND:
      return "◌"
    case TimelineEventKind.ROUTE:
    case TimelineEventKind.PLACEMENT:
      return "⇢"
    case TimelineEventKind.CHECKPOINT:
      return "▣"
    case TimelineEventKind.TOPOLOGY:
      return "⌘"
    case TimelineEventKind.WORKER:
      return "●"
    default:
      return "·"
  }
}

function StatusBanner({ snapshot }: { snapshot: TimelineWorkbenchSnapshot }) {
  if (
    !snapshot.loading &&
    !snapshot.reconnecting &&
    !snapshot.error &&
    snapshot.projection.diagnostics.warnings.length === 0
  ) {
    return null
  }
  const warnings = snapshot.projection.diagnostics.warnings
  return (
    <div
      className={[
        "timeline-status",
        snapshot.error ? "is-error" : snapshot.reconnecting ? "is-warning" : "is-loading",
      ].join(" ")}
      role={snapshot.error ? "alert" : "status"}
    >
      <strong>
        {snapshot.error
          ? "Timeline projection integrity error"
          : snapshot.loading
            ? "Restoring canonical worker history"
            : snapshot.reconnecting
              ? "Canonical stream is catching up"
              : "Timeline contains partial evidence"}
      </strong>
      {snapshot.error ? <p>{snapshot.error}</p> : null}
      {warnings.length ? <p>{warnings.join(" · ")}</p> : null}
    </div>
  )
}

function ToggleChip({
  active,
  children,
  onClick,
  count,
  title,
}: {
  active: boolean
  children: ReactNode
  onClick(): void
  count?: number
  title?: string
}) {
  return (
    <button
      type="button"
      className={`timeline-filter-chip${active ? " is-active" : ""}`}
      aria-pressed={active}
      onClick={onClick}
      title={title}
    >
      <span>{children}</span>
      {count !== undefined ? <span className="timeline-filter-count">{count}</span> : null}
    </button>
  )
}

function TimelineToolbar({
  controller,
  snapshot,
}: {
  controller: TimelineWorkbenchController
  snapshot: TimelineWorkbenchSnapshot
}) {
  const workers = useMemo(
    () => availableTimelineWorkers(snapshot.projection.rows),
    [snapshot.projection.rows],
  )
  const phases = useMemo(
    () => availableTimelinePhases(snapshot.projection.rows),
    [snapshot.projection.rows],
  )
  const kinds = useMemo(
    () => availableTimelineKinds(snapshot.projection.rows),
    [snapshot.projection.rows],
  )
  const filter = snapshot.view.filter
  return (
    <div className="timeline-toolbar" aria-label="Timeline filters and navigation">
      <label className="timeline-search">
        <span>Search timeline</span>
        <input
          type="search"
          value={filter.search ?? ""}
          placeholder="worker, event, tool, failure…"
          onChange={(event) => controller.setSearch(event.currentTarget.value)}
        />
      </label>
      <div className="timeline-filter-group" aria-label="Worker filters">
        <span className="timeline-filter-label">Workers</span>
        <div>
          {workers.slice(0, 12).map((worker) => (
            <ToggleChip
              key={worker.workerId}
              active={filter.workerIds?.includes(worker.workerId) ?? false}
              count={worker.rowCount}
              title={`${worker.failureCount} failure rows · ${worker.recoveryCount} recovery rows`}
              onClick={() => controller.toggleWorker(worker.workerId)}
            >
              {worker.workerId}
            </ToggleChip>
          ))}
        </div>
      </div>
      <div className="timeline-filter-group" aria-label="Worker phase filters">
        <span className="timeline-filter-label">Phase</span>
        <div>
          {phases.map(({ phase, count }) => (
            <ToggleChip
              key={phase}
              active={filter.phases?.includes(phase as never) ?? false}
              count={count}
              onClick={() => controller.togglePhase(phase as never)}
            >
              {phase}
            </ToggleChip>
          ))}
        </div>
      </div>
      <details className="timeline-more-filters">
        <summary>Event types</summary>
        <div className="timeline-filter-group">
          <div>
            {kinds.map(({ kind, count }) => (
              <ToggleChip
                key={kind}
                active={filter.kinds?.includes(kind as never) ?? false}
                count={count}
                onClick={() => controller.toggleKind(kind as never)}
              >
                {kind}
              </ToggleChip>
            ))}
          </div>
        </div>
      </details>
      <div className="timeline-quick-filters">
        <ToggleChip
          active={filter.criticalOnly === true}
          onClick={() => controller.toggleCritical()}
        >
          Critical path
        </ToggleChip>
        <ToggleChip
          active={filter.failuresOnly === true}
          onClick={() => controller.toggleFailures()}
        >
          Failure + recovery
        </ToggleChip>
        <ToggleChip
          active={filter.includePartial === false}
          onClick={() => controller.togglePartial()}
          title="Hide rows whose canonical evidence is incomplete"
        >
          Complete evidence only
        </ToggleChip>
        {snapshot.view.filtered.activeFilterCount ? (
          <button
            type="button"
            className="button button-secondary"
            onClick={() => controller.resetFilters()}
          >
            Clear {snapshot.view.filtered.activeFilterCount}
          </button>
        ) : null}
      </div>
      <div className="timeline-window-controls">
        <button
          type="button"
          className="button button-secondary"
          disabled={snapshot.view.window.beforeCount === 0}
          onClick={() => controller.showEarlier()}
        >
          Earlier
        </button>
        <span>
          {snapshot.view.window.total
            ? `${snapshot.view.window.firstIndex + 1}–${snapshot.view.window.lastIndex + 1} of ${snapshot.view.window.total}`
            : "0 rows"}
        </span>
        <button
          type="button"
          className="button button-secondary"
          disabled={snapshot.view.window.afterCount === 0}
          onClick={() => controller.showLater()}
        >
          Later
        </button>
        <button
          type="button"
          className={`button ${snapshot.view.followLatest ? "button-primary" : "button-secondary"}`}
          onClick={() => controller.followLatest()}
        >
          Follow latest
        </button>
      </div>
    </div>
  )
}

function HiddenGapRow({ gap }: { gap: TimelineHiddenGap }) {
  return (
    <li
      className={`timeline-gap${gap.causalBridge ? " is-causal" : ""}`}
      data-timeline-gap-id={gap.id}
    >
      <span aria-hidden="true">⋯</span>
      <div>
        <strong>{gap.count} hidden row{gap.count === 1 ? "" : "s"}</strong>
        <p>
          sequences {gap.startSequence}–{gap.endSequence}
          {gap.causalBridge ? " · preserves a visible causal bridge" : ""}
          {gap.containsFailure ? " · includes failure" : ""}
          {gap.containsRecovery ? " · includes recovery" : ""}
          {gap.containsCritical ? " · includes critical path" : ""}
        </p>
        <span>
          {Object.entries(gap.phaseCounts)
            .map(([phase, count]) => `${phase} ${count}`)
            .join(" · ")}
        </span>
      </div>
    </li>
  )
}

function TimelineRowDetails({ row }: { row: TimelineRow }) {
  return (
    <div className="timeline-row-details">
      <dl>
        <div>
          <dt>Event range</dt>
          <dd>{row.sequence === row.endSequence ? row.sequence : `${row.sequence}–${row.endSequence}`}</dd>
        </div>
        <div>
          <dt>Correlation</dt>
          <dd><code>{row.correlationId}</code></dd>
        </div>
        {row.causationId ? (
          <div>
            <dt>Cause</dt>
            <dd><code>{row.causationId}</code></dd>
          </div>
        ) : null}
        {row.spanId ? (
          <div>
            <dt>Span</dt>
            <dd><code>{row.spanId}</code></dd>
          </div>
        ) : null}
        {row.leaseId ? (
          <div>
            <dt>Lease</dt>
            <dd><code>{row.leaseId}</code></dd>
          </div>
        ) : null}
        {row.routeId ? (
          <div>
            <dt>Route</dt>
            <dd><code>{row.routeId}</code></dd>
          </div>
        ) : null}
      </dl>
      <div className="timeline-evidence-list">
        <strong>Canonical evidence</strong>
        <ul>
          {row.evidence.map((item) => (
            <li key={`${item.kind}:${item.id}`} className={item.missing ? "is-missing" : ""}>
              <span>{item.kind}</span>
              <code>{item.id}</code>
              <small>
                {item.missing
                  ? "missing"
                  : `${item.eventIds.length} event${item.eventIds.length === 1 ? "" : "s"}`}
              </small>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function TimelineRowItem({
  controller,
  row,
  selected,
  expanded,
  measured,
  overlay,
  onMeasure,
}: {
  controller: TimelineWorkbenchController
  row: TimelineRow
  selected: boolean
  expanded: boolean
  measured?: TimelineMeasuredRow
  overlay?: TimelineRowOverlay
  onMeasure?(rowKey: string, height: number): void
}) {
  return (
    <li
      ref={(element) => {
        if (element && onMeasure) {
          onMeasure(row.key, element.getBoundingClientRect().height)
        }
      }}
      id={`timeline-row-${row.key.replace(/[^\w-]+/g, "-")}`}
      className={[
        "timeline-row",
        selected ? "is-selected" : "",
        row.critical ? "is-critical" : "",
        row.partial ? "is-partial" : "",
        row.late ? "is-late" : "",
      ].filter(Boolean).join(" ")}
      data-timeline-row-key={row.key}
      data-event-id={row.primaryEventId}
      data-correlation-id={row.correlationId}
      data-worker-id={row.workerId}
      data-failure-id={row.failureId}
      data-recovery-id={row.recoveryId}
      data-tool-call-id={row.toolCallId}
      data-span-id={row.spanId}
      data-effective-step={overlay?.effectiveStep ? "true" : "false"}
      style={
        measured
          ? {
              position: "absolute",
              transform: `translateY(${measured.offset}px)`,
              width: "100%",
              minHeight: `${measured.size}px`,
            }
          : undefined
      }
      tabIndex={selected ? 0 : -1}
      onClick={() => controller.selectRow(row.key)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          controller.selectRow(row.key)
        }
      }}
    >
      <div className="timeline-rail" aria-hidden="true">
        <span className={`timeline-kind-symbol tone-${phaseTone(row.phase)}`}>
          {kindSymbol(row.rowKind)}
        </span>
        <span className="timeline-rail-line" />
      </div>
      <article>
        <header>
          <time dateTime={row.committedAt}>{formatTime(row.committedAt)}</time>
          <span className="tag tag-muted">#{row.sequence}</span>
          <span className={`timeline-phase tone-${phaseTone(row.phase)}`}>{row.phase}</span>
          <span className="timeline-kind">{row.rowKind}</span>
          {row.critical ? <span className="tag">critical</span> : null}
          {row.partial ? <span className="tag tag-danger">partial</span> : null}
          {row.late ? <span className="tag tag-muted">late</span> : null}
          {row.duplicateCount ? <span className="tag tag-muted">deduped {row.duplicateCount}</span> : null}
          {overlay?.labels.slice(0, 4).map((label) => (
            <span className="tag tag-muted" key={label}>{label}</span>
          ))}
        </header>
        <div className="timeline-row-heading">
          <div>
            <strong>{row.title}</strong>
            <p>{row.summary}</p>
          </div>
          <button
            type="button"
            className="timeline-expand"
            aria-expanded={expanded}
            onClick={(event) => {
              event.stopPropagation()
              controller.toggleExpanded(row.key)
            }}
          >
            {expanded ? "Hide evidence" : "Evidence"}
          </button>
        </div>
        <div className="timeline-row-entities">
          {row.workerId ? <span>worker <code>{row.workerId}</code></span> : null}
          {row.nodeId ? <span>node <code>{row.nodeId}</code></span> : null}
          {row.toolCallId ? <span>tool <code>{row.toolCallId}</code></span> : null}
          {row.failureId ? <span>failure <code>{row.failureId}</code></span> : null}
          {row.recoveryId ? <span>recovery <code>{row.recoveryId}</code></span> : null}
          {row.artifactIds.map((artifactId) => (
            <button
              key={artifactId}
              type="button"
              className="timeline-artifact-link"
              data-artifact-id={artifactId}
              onClick={(event) => {
                event.stopPropagation()
                if (typeof window !== "undefined") {
                  dispatchArtifactNavigation(
                    {
                      artifactId,
                      source: "timeline",
                      sourceEventId: row.primaryEventId,
                      focus: true,
                    },
                    window,
                  )
                  document
                    .querySelector<HTMLElement>(".artifact-workbench")
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
              }}
            >
              artifact <code>{artifactId}</code>
            </button>
          ))}
          <span>{row.eventIds.length} event{row.eventIds.length === 1 ? "" : "s"}</span>
        </div>
        {expanded ? <TimelineRowDetails row={row} /> : null}
      </article>
    </li>
  )
}

function TimelineRows({
  controller,
  snapshot,
}: {
  controller: TimelineWorkbenchController
  snapshot: TimelineWorkbenchSnapshot
}) {
  const scale = useMemo(
    () =>
      new ScaledTimelineRuntime({
        viewportHeight: 720,
        virtualizer: {
          estimatedRowHeight: 138,
          overscanPixels: 1000,
          maximumRenderedRows: 180,
          measurementCacheLimit: 50_000,
        },
      }),
    [snapshot.taskId],
  )
  const [scaled, setScaled] = useState(() =>
    scale.setProjection(snapshot.projection),
  )
  const listRef = useRef<HTMLOListElement | null>(null)
  useEffect(() => {
    scale.setOptions({
      selectedRowKey: snapshot.view.selectedRowKey,
      criticalOnly: snapshot.view.filter.criticalOnly,
      search: {
        text: snapshot.view.filter.search ?? "",
        workerIds: snapshot.view.filter.workerIds,
        phases: snapshot.view.filter.phases,
        kinds:
          snapshot.view.filter.failuresOnly
            ? [TimelineEventKind.FAILURE]
            : snapshot.view.filter.kinds,
        effectiveOnly: snapshot.view.filter.includeNonEffective === false,
        limit: 10_000,
      },
    })
    setScaled(scale.setProjection(snapshot.projection))
  }, [
    scale,
    snapshot.projection,
    snapshot.view.filter,
    snapshot.view.selectedRowKey,
  ])
  useEffect(() => {
    const element = listRef.current
    if (!element) return
    const update = () => {
      const next = scale.setViewport(element.scrollTop, element.clientHeight)
      if (next) setScaled(next)
    }
    update()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [scale])
  useEffect(() => () => scale.close(), [scale])
  const measure = (rowKey: string, height: number) => {
    const next = scale.measureRow(rowKey, height)
    if (next && next.revision !== scaled.revision) setScaled(next)
  }
  const rows = scaled.virtual.rows
  if (!scaled.rows.length) {
    return (
      <div className="timeline-empty">
        <strong>No timeline rows match the current filters.</strong>
        <p>
          {snapshot.projection.rows.length
            ? `${snapshot.projection.rows.length} canonical row(s) remain available.`
            : "The canonical event projection has not committed worker activity yet."}
        </p>
      </div>
    )
  }
  return (
    <>
      <div className="timeline-scale-summary" aria-label="Timeline scale summary">
        <span>{scaled.totalRows.toLocaleString()} canonical rows</span>
        <span>{scaled.effectiveSteps.toLocaleString()} effective steps</span>
        <span>{scaled.hiddenByFolds.toLocaleString()} safely folded</span>
        <span>{scaled.renderedRows} mounted</span>
        {scaled.search ? <span>{scaled.search.totalMatches} search matches</span> : null}
        {scaled.goalDrift.currentEpoch ? (
          <span>
            goal epoch {scaled.goalDrift.currentEpoch.ordinal + 1}
            {" · "}
            drift {(scaled.goalDrift.currentEpoch.driftScore * 100).toFixed(0)}%
          </span>
        ) : null}
      </div>
      {scaled.folds.folds.length ? (
        <div className="timeline-fold-controls" aria-label="Causal fold controls">
          {scaled.folds.folds.slice(0, 16).map((fold) => (
            <button
              key={fold.id}
              type="button"
              disabled={!fold.safeToCollapse}
              aria-expanded={fold.expanded}
              title={fold.reason}
              onClick={() => {
                const next = scale.toggleFold(fold.id)
                if (next) setScaled(next)
              }}
            >
              {fold.expanded ? "collapse" : "expand"} {fold.label}
            </button>
          ))}
          {scaled.folds.folds.length > 16 ? (
            <span>+{scaled.folds.folds.length - 16} more folds</span>
          ) : null}
        </div>
      ) : null}
      <ol
        ref={listRef}
        className="timeline-row-list timeline-virtual-list"
        aria-label="Worker causal timeline"
        onScroll={(event) => {
          const target = event.currentTarget
          const next = scale.setViewport(target.scrollTop, target.clientHeight)
          if (next) setScaled(next)
        }}
      >
        <li
          className="timeline-virtual-spacer"
          aria-hidden="true"
          style={{ height: `${scaled.virtual.totalHeight}px` }}
        />
        {rows.map((measured) => {
          const row = measured.row
          return (
            <TimelineRowItem
              key={row.key}
              controller={controller}
              row={row}
              measured={measured}
              overlay={scaled.overlays.byRowKey.get(row.key)}
              onMeasure={measure}
              selected={snapshot.view.selectedRowKey === row.key}
              expanded={snapshot.view.expandedRowKeys.has(row.key)}
            />
          )
        })}
      </ol>
    </>
  )
}

function navigateToTarget(
  runtime: WorkbenchRuntime,
  taskId: string,
  target: TimelineDrilldownTarget,
): void {
  if (typeof document === "undefined") return
  let element: HTMLElement | null = null
  try {
    element = document.querySelector<HTMLElement>(target.selector)
  } catch {
    element = null
  }
  if (element) {
    element.scrollIntoView({ behavior: "smooth", block: "center" })
    element.focus({ preventScroll: true })
    runtime.announcer.announce(`Opened ${target.label}.`)
    return
  }
  runtime.notifications.push({
    id: `timeline-target-${target.kind}-${target.entityId}`,
    title: "Canonical target is outside the current detail view",
    message: `${target.label} remains linked by canonical evidence.`,
    tone: target.available ? "warning" : "error",
    durationMs: 8_000,
    taskId,
  })
}

function TimelineInspector({
  runtime,
  controller,
  snapshot,
}: {
  runtime: WorkbenchRuntime
  controller: TimelineWorkbenchController
  snapshot: TimelineWorkbenchSnapshot
}) {
  const row = snapshot.selectedRow
  if (!row) {
    const path = snapshot.projection.criticalPath
    return (
      <aside className="timeline-inspector" aria-label="Timeline critical path summary">
        <p className="eyebrow">Causal analysis</p>
        <h4>Critical path</h4>
        <dl>
          <div><dt>Rows</dt><dd>{path.rowKeys.length}</dd></div>
          <div><dt>Events</dt><dd>{path.eventIds.length}</dd></div>
          <div><dt>Workers</dt><dd>{path.workerIds.length}</dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(path.durationMs)}</dd></div>
          <div><dt>Complete</dt><dd>{path.complete ? "yes" : "partial"}</dd></div>
        </dl>
        <p>
          Select a timeline row to inspect its spans, tool calls, artifacts,
          failures, recoveries, mutations and topology targets.
        </p>
        {path.rowKeys.length ? (
          <button
            type="button"
            className="button button-secondary"
            onClick={() => {
              const key = path.rowKeys[0]
              if (key) controller.selectRow(key)
            }}
          >
            Inspect first critical row
          </button>
        ) : null}
      </aside>
    )
  }
  const incoming = row.incomingEdgeIds
    .map((id) => snapshot.projection.graph.edges.find((edge) => edge.id === id))
    .filter(Boolean)
  const outgoing = row.outgoingEdgeIds
    .map((id) => snapshot.projection.graph.edges.find((edge) => edge.id === id))
    .filter(Boolean)
  return (
    <aside
      className="timeline-inspector"
      aria-label={`Timeline row ${row.title}`}
      data-selected-timeline-row={row.key}
    >
      <div className="timeline-inspector-heading">
        <div>
          <p className="eyebrow">Selected evidence</p>
          <h4>{row.title}</h4>
        </div>
        <button type="button" onClick={() => controller.clearSelection()}>
          close
        </button>
      </div>
      <p>{row.summary}</p>
      <dl>
        <div><dt>Phase</dt><dd>{row.phase}</dd></div>
        <div><dt>Sequence</dt><dd>{row.sequence}–{row.endSequence}</dd></div>
        <div><dt>Causal depth</dt><dd>{row.depth}</dd></div>
        <div><dt>Incoming</dt><dd>{incoming.length}</dd></div>
        <div><dt>Outgoing</dt><dd>{outgoing.length}</dd></div>
        <div><dt>Effective</dt><dd>{row.effective ? "yes" : "no"}</dd></div>
      </dl>
      <section>
        <h5>Causal neighbors</h5>
        <div className="timeline-neighbor-list">
          {[...incoming, ...outgoing].map((edge) => {
            if (!edge) return null
            const neighborEventId =
              edge.targetEventId === row.primaryEventId
                ? edge.sourceEventId
                : edge.targetEventId
            return (
              <button
                type="button"
                key={edge.id}
                onClick={() => {
                  if (controller.selectEvent(neighborEventId)) {
                    const element = document.querySelector<HTMLElement>(
                      `[data-event-id="${neighborEventId}"]`,
                    )
                    element?.scrollIntoView({ behavior: "smooth", block: "center" })
                  }
                }}
              >
                <strong>{edge.kind}</strong>
                <code>{neighborEventId}</code>
              </button>
            )
          })}
        </div>
      </section>
      <section>
        <h5>Open canonical target</h5>
        <div className="timeline-target-list">
          {row.drilldowns.map((target) => (
            <button
              type="button"
              key={`${target.kind}:${target.entityId}`}
              disabled={!target.available}
              onClick={() => navigateToTarget(runtime, snapshot.taskId, target)}
            >
              <span>{target.kind}</span>
              <strong>{target.label}</strong>
            </button>
          ))}
        </div>
      </section>
    </aside>
  )
}

export function WorkerCausalTimelineWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const selector = useMemo(
    () =>
      selectWorkerCausalTimeline(task.taskId, {
        includeNonEffective: true,
        includePartial: true,
        maximumEvents: 250_000,
        maximumClosureDepth: 512,
      }),
    [task.taskId],
  )
  const projection = useProjectionSelector(runtime, selector)
  const controller = useMemo(
    () =>
      new TimelineWorkbenchController(task.taskId, projection, {
        windowCount: 120,
        overscan: 24,
        followLatest: true,
      }),
    [task.taskId],
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
      controller.setError(error instanceof Error ? error.message : String(error))
    }
  }, [controller, projection])
  useEffect(
    () => () => controller.close("Worker timeline route unmounted."),
    [controller],
  )
  useEffect(() => {
    for (const message of controller.takeAnnouncements()) {
      runtime.announcer.announce(message)
    }
  }, [controller, runtime, snapshot.revision])
  return (
    <section
      className="worker-timeline-workbench"
      aria-labelledby="worker-timeline-heading"
      data-timeline-projection-revision={projection.projectionRevision}
      data-timeline-committed-sequence={projection.diagnostics.committedSequence}
    >
      <header className="worker-timeline-heading">
        <div>
          <p className="eyebrow">Worker causal timeline</p>
          <h3 id="worker-timeline-heading">Execution, failure and recovery evidence</h3>
          <p>
            Canonical worker phases joined with spans, tools, artifacts, routes,
            failures, recovery attempts and state mutations.
          </p>
        </div>
        <dl className="timeline-summary">
          <div><dt>Rows</dt><dd>{projection.rows.length}</dd></div>
          <div><dt>Workers</dt><dd>{projection.workerEpochs.length}</dd></div>
          <div><dt>Recoveries</dt><dd>{projection.recoveryChains.length}</dd></div>
          <div><dt>Lag</dt><dd>{projection.diagnostics.lag}</dd></div>
        </dl>
      </header>
      <StatusBanner snapshot={snapshot} />
      <TimelineToolbar controller={controller} snapshot={snapshot} />
      <RecoveryControlPanel
        runtime={runtime}
        task={task}
        projection={projection}
      />
      <div className="timeline-workspace">
        <div className="timeline-list-region">
          <TimelineRows controller={controller} snapshot={snapshot} />
        </div>
        <TimelineInspector
          runtime={runtime}
          controller={controller}
          snapshot={snapshot}
        />
      </div>
      <div className="visually-hidden" aria-live="polite" aria-atomic="true">
        {controller.announcements().at(-1) ?? ""}
      </div>
    </section>
  )
}
