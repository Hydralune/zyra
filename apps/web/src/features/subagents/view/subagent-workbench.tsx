import {
  useMemo,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type ReactNode,
} from "react"
import type {
  BudgetMetric,
  SubagentControlOperation,
  SubagentLifecycleIncident,
  SubagentPanelFilter,
  SubagentRow,
} from "../contracts.ts"
import type { SubagentPanelController } from "../controller.ts"

export function SubagentWorkbench(props: {
  controller: SubagentPanelController
  compact?: boolean
}): ReactNode {
  const snapshot = useSyncExternalStore(
    props.controller.subscribe,
    props.controller.getSnapshot,
    props.controller.getSnapshot,
  )
  const [instruction, setInstruction] = useState("")
  const [reason, setReason] = useState("")
  const [controlError, setControlError] = useState<string>()
  const [submitting, setSubmitting] = useState<"kill" | "steer">()
  const selectedIncidents = useMemo(
    () => snapshot.incidents.filter((incident) =>
      incident.childId === snapshot.selectedId),
    [snapshot.incidents, snapshot.selectedId],
  )
  const submit = async (
    event: FormEvent,
    action: "kill" | "steer",
  ) => {
    event.preventDefault()
    setControlError(undefined)
    setSubmitting(action)
    try {
      if (action === "kill") {
        await props.controller.kill({
          reason: reason || "Operator requested a scoped subagent kill.",
        })
      } else {
        await props.controller.steer({
          instruction,
          reason: reason || undefined,
        })
        setInstruction("")
      }
    } catch (error) {
      setControlError(error instanceof Error ? error.message : String(error))
    } finally {
      setSubmitting(undefined)
    }
  }
  if (!snapshot.viewerOpen) {
    return (
      <section className="subagent-workbench subagent-workbench--closed" aria-label="Subagent workbench">
        <header className="subagent-workbench__header">
          <div>
            <p className="eyebrow">AgentTool</p>
            <h2>Subagent viewer closed</h2>
          </div>
          <button type="button" onClick={() => props.controller.openViewer()}>
            Restore from canonical state
          </button>
        </header>
        <p>
          Closing this viewer detached browser subscriptions only. Child runs continue under
          their canonical runtime owner.
        </p>
      </section>
    )
  }
  const projection = snapshot.projection
  return (
    <section
      className={`subagent-workbench${props.compact ? " subagent-workbench--compact" : ""}`}
      aria-label="Subagent workbench"
      data-connected={snapshot.connected}
      data-enabled={snapshot.enabled}
      data-sealed={snapshot.sealed}
    >
      <header className="subagent-workbench__header">
        <div>
          <p className="eyebrow">AgentTool · canonical projection</p>
          <h2>子代理</h2>
          <p role="status" aria-live="polite">
            {projection
              ? `${projection.activeIds.length} active · ${projection.settledIds.length} settled · revision ${projection.canonicalRevision}`
              : "Select a task to inspect canonical child runs."}
          </p>
        </div>
        <div className="subagent-workbench__header-actions">
          <StatusBadge
            tone={!snapshot.enabled ? "danger" : snapshot.connected ? "success" : "warning"}
          >
            {!snapshot.enabled ? "disabled" : snapshot.connected ? "connected" : "disconnected"}
          </StatusBadge>
          {snapshot.sealed ? <StatusBadge tone="warning">sealed · read only</StatusBadge> : null}
          <button type="button" onClick={() => props.controller.closeViewer()}>
            Close viewer
          </button>
        </div>
      </header>

      {snapshot.disabledReason ? (
        <div className="request-state request-state--error" role="alert">
          {snapshot.disabledReason}
        </div>
      ) : null}
      {!projection?.ready && projection ? (
        <div className="request-state request-state--warning" role="status">
          Strict admission has unresolved errors. Mutating controls remain fail-closed.
        </div>
      ) : null}

      <div className="subagent-workbench__toolbar" role="toolbar" aria-label="Subagent filters">
        <label>
          Scope
          <select
            value={snapshot.filter}
            onChange={(event) =>
              props.controller.setFilter(event.currentTarget.value as SubagentPanelFilter)}
          >
            <option value="active">Active</option>
            <option value="settled">Settled</option>
            <option value="failed">Failed</option>
            <option value="quarantined">Quarantined</option>
            <option value="all">All</option>
          </select>
        </label>
        <label>
          Search
          <input
            type="search"
            value={snapshot.query}
            onChange={(event) => props.controller.setQuery(event.currentTarget.value)}
            placeholder="identity, owner, scope, result…"
          />
        </label>
        <button type="button" onClick={() => props.controller.expandAll()}>
          Expand all
        </button>
        <button type="button" onClick={() => props.controller.collapseAll()}>
          Collapse all
        </button>
      </div>

      <div className="subagent-workbench__body">
        <div className="subagent-workbench__tree" role="tree" aria-label="Subagent hierarchy">
          {snapshot.visibleRows.length === 0 ? (
            <EmptyState
              title="No admitted subagents in this view"
              detail={
                projection?.rejectedWorkerIds.length
                  ? `${projection.rejectedWorkerIds.length} worker rows failed strict admission.`
                  : "Canonical worker and AgentTool events will appear here."
              }
            />
          ) : (
            snapshot.visibleRows.map((row) => {
              const node = projection?.hierarchy.nodes[row.id]
              const expanded = snapshot.expandedIds.includes(row.id)
              const selected = snapshot.selectedId === row.id
              return (
                <article
                  key={row.id}
                  role="treeitem"
                  aria-level={(node?.depth ?? row.depth) + 1}
                  aria-expanded={node?.childIds.length ? expanded : undefined}
                  aria-selected={selected}
                  className={`subagent-tree-row${selected ? " is-selected" : ""}`}
                  style={{ paddingInlineStart: `${Math.min(12, node?.depth ?? row.depth) * 1.1}rem` }}
                >
                  <div className="subagent-tree-row__main">
                    {node?.childIds.length ? (
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={expanded ? `Collapse ${row.id}` : `Expand ${row.id}`}
                        onClick={() => props.controller.toggleExpanded(row.id)}
                      >
                        {expanded ? "▾" : "▸"}
                      </button>
                    ) : (
                      <span className="subagent-tree-row__spacer" aria-hidden="true">·</span>
                    )}
                    <button
                      type="button"
                      className="subagent-tree-row__select"
                      onClick={() => props.controller.select(row.id)}
                    >
                      <strong>{row.definitionName ?? row.id}</strong>
                      <span>{row.id}</span>
                    </button>
                    <LifecycleBadge row={row} />
                  </div>
                  <div className="subagent-tree-row__facts">
                    <span>depth {node?.depth ?? row.depth}</span>
                    <span>attempt {row.attempt}</span>
                    <span>{heartbeatLabel(row)}</span>
                    <span>{budgetLabel(row)}</span>
                  </div>
                </article>
              )
            })
          )}
        </div>

        <div className="subagent-workbench__detail">
          {snapshot.selected ? (
            <>
              <SubagentDetail row={snapshot.selected} />
              <SubagentControlForm
                row={snapshot.selected}
                sealed={snapshot.sealed}
                connected={snapshot.connected}
                instruction={instruction}
                reason={reason}
                submitting={submitting}
                error={controlError}
                onInstruction={setInstruction}
                onReason={setReason}
                onSubmit={submit}
              />
              <IncidentList
                incidents={selectedIncidents}
                onAcknowledge={(id) => props.controller.acknowledgeIncident(id)}
              />
            </>
          ) : (
            <EmptyState
              title="Select a subagent"
              detail="Inspect exact parent, scope, budget, heartbeat, checkpoint, result, error, and canonical evidence."
            />
          )}
        </div>
      </div>

      <ControlHistory operations={snapshot.controls.operations} />
      {projection?.issues.length ? (
        <details className="subagent-workbench__admission">
          <summary>
            Projection admission · {projection.issues.length} finding
            {projection.issues.length === 1 ? "" : "s"}
          </summary>
          <ul>
            {projection.issues.map((issue, index) => (
              <li key={`${issue.code}:${issue.childId ?? ""}:${issue.eventId ?? ""}:${index}`}>
                <StatusBadge tone={issue.severity === "error" ? "danger" : "warning"}>
                  {issue.severity}
                </StatusBadge>{" "}
                <strong>{issue.code}</strong> {issue.childId ? `· ${issue.childId}` : ""} —{" "}
                {issue.message}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  )
}

function SubagentDetail(props: { row: SubagentRow }): ReactNode {
  const row = props.row
  return (
    <section className="subagent-detail" aria-label={`Subagent ${row.id}`}>
      <header className="subagent-detail__header">
        <div>
          <p className="eyebrow">{row.executionMode ?? "child run"}</p>
          <h3>{row.definitionName ?? row.id}</h3>
          <code>{row.id}</code>
        </div>
        <LifecycleBadge row={row} />
      </header>
      <dl className="subagent-detail__identity">
        <Fact term="Parent" value={row.parentId} />
        <Fact term="Owner" value={row.ownerId ?? "missing"} />
        <Fact term="Run" value={row.runId} />
        <Fact term="Session" value={row.sessionId ?? "none"} />
        <Fact term="Attempt" value={String(row.attempt)} />
        <Fact term="Revision" value={String(row.revision)} />
        <Fact term="Route" value={row.routeId ?? "unassigned"} />
        <Fact term="Placement" value={row.placementId ?? "unassigned"} />
        <Fact term="Lease" value={row.leaseId ?? "none"} />
        <Fact term="Checkpoint" value={row.checkpointId ?? "none"} />
      </dl>

      <details open>
        <summary>Scope and custody</summary>
        <dl className="subagent-detail__identity">
          <Fact term="Scope digest" value={row.scope.digest ?? "missing"} />
          <Fact term="Permission mode" value={row.scope.permissionMode ?? "missing"} />
          <Fact term="Permission ceiling" value={row.scope.permissionCeilingDigest ?? "missing"} />
          <Fact term="Isolation" value={row.scope.isolation ?? "none"} />
          <Fact term="Memory scope" value={row.scope.memoryScope ?? "none"} />
        </dl>
        <TokenList title="Allowed child tools" values={row.scope.childTools} />
        <TokenList title="Denied tools" values={row.scope.deniedTools} />
        <TokenList title="Skills" values={row.scope.skills} />
        <TokenList title="MCP servers" values={row.scope.mcpServers} />
        <TokenList title="Lineage" values={row.scope.lineage} />
      </details>

      <details open>
        <summary>Budget</summary>
        <div className="subagent-budget-grid">
          {row.budget.metrics.map((metric) => (
            <BudgetGauge key={metric.key} metric={metric} />
          ))}
        </div>
        {row.budget.warnings.length ? (
          <ul>
            {row.budget.warnings.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        ) : null}
      </details>

      <details open>
        <summary>Heartbeat and recovery</summary>
        <dl className="subagent-detail__identity">
          <Fact term="Heartbeat" value={row.heartbeat.phase} />
          <Fact term="Last observed" value={formatTime(row.heartbeat.lastAt)} />
          <Fact term="Age" value={formatDuration(row.heartbeat.ageMs)} />
          <Fact term="Due" value={formatTime(row.heartbeat.dueAt)} />
          <Fact term="Timeout" value={formatTime(row.heartbeat.timeoutAt)} />
          <Fact term="Reconnect" value={row.reconnected ? `attempt ${row.attempt}` : "not observed"} />
          <Fact term="Crash" value={row.crashed ? "observed" : "not observed"} />
        </dl>
      </details>

      <details open={Boolean(row.result || row.error)}>
        <summary>Settlement</summary>
        {row.result ? (
          <div className="subagent-result">
            <h4>Result</h4>
            <p>{row.result.summary ?? "Canonical result committed without a display summary."}</p>
            <Fact term="Digest" value={row.result.digest ?? "none"} />
            <TokenList title="Artifacts" values={row.result.artifactIds} />
          </div>
        ) : null}
        {row.error ? (
          <div className="request-state request-state--error" role="alert">
            <strong>{row.error.code ?? "subagent_error"}</strong>
            <p>{row.error.message ?? "Canonical runtime reported a child-run failure."}</p>
          </div>
        ) : null}
        {row.lateResults.length ? (
          <div className="request-state request-state--warning" role="alert">
            <strong>Late result quarantine</strong>
            <ul>
              {row.lateResults.map((late) => (
                <li key={late.id}>
                  Event {late.resultEventId} arrived after terminal event {late.terminalEventId}.
                  It is permanently fenced.
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </details>
    </section>
  )
}

function SubagentControlForm(props: {
  row: SubagentRow
  sealed: boolean
  connected: boolean
  instruction: string
  reason: string
  submitting?: "kill" | "steer"
  error?: string
  onInstruction(value: string): void
  onReason(value: string): void
  onSubmit(event: FormEvent, action: "kill" | "steer"): void
}): ReactNode {
  const disabled =
    !props.connected ||
    !props.row.controlEligible ||
    Boolean(props.submitting)
  return (
    <form className="subagent-controls">
      <header>
        <h3>Scoped control</h3>
        <p>
          Bound to revision {props.row.revision}, attempt {props.row.attempt}, owner{" "}
          <code>{props.row.ownerId ?? "missing"}</code>.
        </p>
      </header>
      {props.sealed ? (
        <div className="request-state request-state--warning" role="status">
          Sealed mode rejects kill and steer before mutation, records the operator attempt,
          never enters approval wait, and keeps human_intervention_count at zero.
        </div>
      ) : null}
      <label>
        Steering instruction
        <textarea
          value={props.instruction}
          onChange={(event) => props.onInstruction(event.currentTarget.value)}
          rows={4}
          maxLength={60_000}
          disabled={disabled}
        />
      </label>
      <label>
        Audit reason
        <input
          value={props.reason}
          onChange={(event) => props.onReason(event.currentTarget.value)}
          maxLength={7_000}
          disabled={disabled}
        />
      </label>
      <div className="subagent-controls__actions">
        <button
          type="submit"
          disabled={disabled || !props.instruction.trim()}
          onClick={(event) => props.onSubmit(event, "steer")}
        >
          {props.submitting === "steer" ? "Submitting…" : props.sealed ? "Record denied steer" : "Steer"}
        </button>
        <button
          type="submit"
          className="danger"
          disabled={disabled || !props.reason.trim()}
          onClick={(event) => props.onSubmit(event, "kill")}
        >
          {props.submitting === "kill" ? "Submitting…" : props.sealed ? "Record denied kill" : "Kill"}
        </button>
      </div>
      {!props.row.controlEligible ? (
        <p role="status">
          Control is unavailable because strict scope, owner, terminal, quarantine, or
          connection admission failed.
        </p>
      ) : null}
      {props.error ? <p className="request-state request-state--error" role="alert">{props.error}</p> : null}
    </form>
  )
}

function IncidentList(props: {
  incidents: readonly SubagentLifecycleIncident[]
  onAcknowledge(id: string): void
}): ReactNode {
  if (!props.incidents.length) return null
  return (
    <section className="subagent-incidents">
      <h3>Lifecycle incidents</h3>
      <ul>
        {props.incidents.map((incident) => (
          <li key={incident.id} data-severity={incident.severity}>
            <div>
              <StatusBadge tone={incident.severity === "error" ? "danger" : "warning"}>
                {incident.kind}
              </StatusBadge>
              <p>{incident.message}</p>
              <small>
                attempt {incident.attempt} · {incident.occurrences} observation
                {incident.occurrences === 1 ? "" : "s"}
              </small>
            </div>
            {!incident.acknowledged ? (
              <button type="button" onClick={() => props.onAcknowledge(incident.id)}>
                Acknowledge
              </button>
            ) : (
              <span>acknowledged</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function ControlHistory(props: {
  operations: readonly SubagentControlOperation[]
}): ReactNode {
  if (!props.operations.length) return null
  return (
    <details className="subagent-control-history">
      <summary>Control reconciliation · {props.operations.length}</summary>
      <ol>
        {props.operations.map((operation) => (
          <li key={operation.id}>
            <div>
              <strong>{operation.action}</strong>{" "}
              <code>{operation.binding.childId}</code>{" "}
              <StatusBadge
                tone={
                  operation.phase === "committed"
                    ? "success"
                    : ["failed", "denied", "quarantined"].includes(operation.phase)
                      ? "danger"
                      : "warning"
                }
              >
                {operation.phase}
              </StatusBadge>
            </div>
            <dl>
              <Fact term="Nonce" value={operation.nonce} />
              <Fact term="Idempotency" value={operation.idempotencyKey} />
              <Fact term="Command" value={operation.commandId ?? "pending"} />
              <Fact term="Started" value={formatTime(operation.startedAt)} />
            </dl>
            {operation.effect ? (
              <p>
                {operation.effect.reasons.join("; ")} · evidence{" "}
                {operation.effect.evidenceEventIds.join(", ") || "pending"}
              </p>
            ) : null}
            {operation.error ? <p role="alert">{operation.error}</p> : null}
          </li>
        ))}
      </ol>
    </details>
  )
}

function BudgetGauge(props: { metric: BudgetMetric }): ReactNode {
  const percentage = props.metric.limit > 0
    ? Math.min(100, Math.round(props.metric.ratio * 100))
    : 0
  return (
    <div
      className="subagent-budget"
      data-exceeded={props.metric.exceeded}
      data-near-limit={props.metric.nearLimit}
    >
      <div>
        <strong>{props.metric.key.replaceAll("_", " ")}</strong>
        <span>{props.metric.limit > 0 ? `${props.metric.used} / ${props.metric.limit}` : "unreported"}</span>
      </div>
      <progress
        value={props.metric.limit > 0 ? Math.min(props.metric.used, props.metric.limit) : 0}
        max={props.metric.limit > 0 ? props.metric.limit : 1}
        aria-label={`${props.metric.key} budget ${percentage}%`}
      />
    </div>
  )
}

function LifecycleBadge(props: { row: SubagentRow }): ReactNode {
  const tone =
    props.row.lifecycle === "completed"
      ? "success"
      : ["failed", "killed", "rejected"].includes(props.row.lifecycle)
        ? "danger"
        : ["cancelled", "paused", "waiting", "recovering"].includes(props.row.lifecycle)
          ? "warning"
          : "neutral"
  return <StatusBadge tone={tone}>{props.row.lifecycle}</StatusBadge>
}

function StatusBadge(props: {
  children: ReactNode
  tone: "success" | "warning" | "danger" | "neutral"
}): ReactNode {
  return <span className={`status-badge status-badge--${props.tone}`}>{props.children}</span>
}

function Fact(props: { term: string; value: string }): ReactNode {
  return (
    <>
      <dt>{props.term}</dt>
      <dd>{props.value}</dd>
    </>
  )
}

function TokenList(props: {
  title: string
  values: readonly string[]
}): ReactNode {
  return (
    <div className="subagent-token-list">
      <h4>{props.title}</h4>
      {props.values.length ? (
        <ul>
          {props.values.map((value) => <li key={value}><code>{value}</code></li>)}
        </ul>
      ) : (
        <span>none</span>
      )}
    </div>
  )
}

function EmptyState(props: {
  title: string
  detail: string
}): ReactNode {
  return (
    <div className="empty-state">
      <h3>{props.title}</h3>
      <p>{props.detail}</p>
    </div>
  )
}

function heartbeatLabel(row: SubagentRow): string {
  if (row.heartbeat.phase === "terminal") return "heartbeat terminal"
  if (row.heartbeat.ageMs === undefined) return `heartbeat ${row.heartbeat.phase}`
  return `heartbeat ${row.heartbeat.phase} · ${formatDuration(row.heartbeat.ageMs)}`
}

function budgetLabel(row: SubagentRow): string {
  if (row.budget.exceeded) return "budget exceeded"
  if (row.budget.highestRatio <= 0) return "budget unreported"
  return `budget ${Math.round(row.budget.highestRatio * 100)}%`
}

function formatTime(value: string | undefined): string {
  if (!value) return "none"
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) return value
  return new Date(parsed).toLocaleString()
}

function formatDuration(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return "unknown"
  if (value < 1_000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${Math.round(value / 1_000)} s`
  if (value < 3_600_000) return `${Math.round(value / 60_000)} min`
  return `${Math.round(value / 3_600_000)} h`
}
