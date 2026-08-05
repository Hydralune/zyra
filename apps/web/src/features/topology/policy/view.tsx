import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react"
import type {
  PolicyCausalReference,
  PolicyEvidenceTransition,
} from "../../../api/policy-api.ts"
import type { PolicyEvidenceRuntime } from "./runtime.ts"

const VIRTUAL_WINDOW = 80

type PhysicalLocation = "LOCAL" | "EDGE" | "CLOUD" | "UNKNOWN"
type EvidenceTruth = "real" | "simulated" | "degraded" | "missing" | "stale"

export interface PhysicalDispatchViewModel {
  location: PhysicalLocation
  lane: "terminal" | "local" | "edge" | "cloud" | "unknown"
  label: string
  truth: EvidenceTruth
  realGateClosed: boolean
  backendId?: string
  terminalId?: string
  attemptId?: string
  leaseId?: string
  permissionRef?: string
  failoverCount: number
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function optionalText(value: unknown): string | undefined {
  const selected = typeof value === "string" ? value.trim() : ""
  return selected || undefined
}

export function physicalDispatchViewModel(
  transition: PolicyEvidenceTransition,
): PhysicalDispatchViewModel | undefined {
  if (transition.contract_kind !== "physical_dispatch_receipt") return undefined
  const details = record(transition.details)
  const identity = record(details.physical_identity)
  const validation = record(details.physical_validation)
  const rawLocation = optionalText(identity.location)?.toLowerCase()
  const location: PhysicalLocation =
    rawLocation === "local"
      ? "LOCAL"
      : rawLocation === "edge"
        ? "EDGE"
        : rawLocation === "cloud"
          ? "CLOUD"
          : "UNKNOWN"
  const terminalId = optionalText(identity.terminal_id)
  const lane =
    location === "LOCAL" && terminalId
      ? "terminal"
      : location === "LOCAL"
        ? "local"
        : location === "EDGE"
          ? "edge"
          : location === "CLOUD"
            ? "cloud"
            : "unknown"
  const realGateClosed = validation.real_gate_closed === true
  const truth: EvidenceTruth =
    transition.integrity === "missing"
      ? "missing"
      : transition.integrity === "stale"
        ? "stale"
        : transition.execution === "real"
          && transition.integrity === "verified"
          && realGateClosed
          ? "real"
          : transition.execution === "simulated"
            ? "simulated"
            : "degraded"
  const recoveryEvidence = Array.isArray(details.recovery_evidence)
    ? details.recovery_evidence
    : []
  return Object.freeze({
    location,
    lane,
    label:
      lane === "terminal"
        ? "TERMINAL · LOCAL"
        : lane === "local"
          ? "LOCAL PROCESS"
          : lane === "edge"
            ? "EDGE RUNTIME"
            : lane === "cloud"
              ? "CLOUD PROVIDER"
              : "UNVERIFIED LOCATION",
    truth,
    realGateClosed,
    backendId: optionalText(identity.backend_id),
    terminalId,
    attemptId: optionalText(details.physical_attempt_id),
    leaseId: optionalText(details.lease_id),
    permissionRef: optionalText(details.permission_ref),
    failoverCount: recoveryEvidence.length,
  })
}

function compact(value: unknown, maximum = 32): string {
  const selected = String(value || "")
  if (!selected) return "—"
  return selected.length > maximum
    ? `${selected.slice(0, maximum - 9)}…${selected.slice(-8)}`
    : selected
}

function downloadEvidence(runtime: PolicyEvidenceRuntime): void {
  if (typeof document === "undefined" || typeof URL === "undefined") return
  const content = runtime.exportLoaded()
  const url = URL.createObjectURL(
    new Blob([content], { type: "application/json;charset=utf-8" }),
  )
  const link = document.createElement("a")
  link.href = url
  link.download = `zyra-policy-evidence-${runtime.getSnapshot().snapshotDigest || "degraded"}.json`
  link.click()
  URL.revokeObjectURL(url)
}

function EvidenceReference({
  reference,
  transition,
  onNavigate,
}: {
  reference: PolicyCausalReference
  transition: PolicyEvidenceTransition
  onNavigate?: (
    reference: PolicyCausalReference,
    transition: PolicyEvidenceTransition,
  ) => void
}) {
  return (
    <button
      className="button button-secondary policy-evidence-ref"
      type="button"
      data-evidence-ref-kind={reference.kind}
      data-evidence-ref-id={reference.id}
      onClick={() => onNavigate?.(reference, transition)}
    >
      <span>{reference.kind}</span>
      <strong>{compact(reference.id, 24)}</strong>
    </button>
  )
}

function PhysicalDispatchSummary({
  transition,
}: {
  transition: PolicyEvidenceTransition
}) {
  const dispatch = physicalDispatchViewModel(transition)
  if (!dispatch) return null
  return (
    <div
      className="physical-dispatch-summary"
      data-physical-location={dispatch.location}
      data-physical-lane={dispatch.lane}
      data-evidence-truth={dispatch.truth}
      data-real-gate-closed={dispatch.realGateClosed || undefined}
    >
      <div>
        <strong>{dispatch.label}</strong>
        <span className="tag">{dispatch.truth}</span>
        <span className="tag">
          {dispatch.realGateClosed ? "real gate closed" : "real gate open"}
        </span>
      </div>
      <dl>
        <div><dt>Backend</dt><dd>{dispatch.backendId ?? "not supplied"}</dd></div>
        <div><dt>Attempt</dt><dd>{dispatch.attemptId ?? "missing"}</dd></div>
        <div><dt>Lease</dt><dd>{dispatch.leaseId ?? "missing"}</dd></div>
        <div><dt>Permission</dt><dd>{dispatch.permissionRef ?? "missing"}</dd></div>
        <div><dt>Failover</dt><dd>{dispatch.failoverCount}</dd></div>
        {dispatch.terminalId ? (
          <div><dt>Terminal</dt><dd>{dispatch.terminalId}</dd></div>
        ) : null}
      </dl>
      {dispatch.truth !== "real" ? (
        <p>
          This receipt is visible for diagnosis but is not evidence of successful
          physical execution.
        </p>
      ) : null}
    </div>
  )
}

export function PolicyEvidenceView({
  runtime,
  title = "Mechanism evidence chain",
  onNavigate,
}: {
  runtime: PolicyEvidenceRuntime
  title?: string
  onNavigate?: (
    reference: PolicyCausalReference,
    transition: PolicyEvidenceTransition,
  ) => void
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const [mechanismVersion, setMechanismVersion] = useState(
    snapshot.query.mechanismVersion ?? "",
  )
  const [receiptId, setReceiptId] = useState(snapshot.query.receiptId ?? "")
  const [windowStart, setWindowStart] = useState(0)
  const selected = useMemo(
    () => snapshot.transitions.find(
      (item) => item.transition_id === snapshot.selectedTransitionId,
    ),
    [snapshot.transitions, snapshot.selectedTransitionId],
  )
  const maximumStart = Math.max(
    0,
    snapshot.transitions.length - VIRTUAL_WINDOW,
  )
  const boundedStart = Math.min(windowStart, maximumStart)
  const visible = snapshot.transitions.slice(
    boundedStart,
    boundedStart + VIRTUAL_WINDOW,
  )

  useEffect(() => {
    void runtime.open()
    return () => runtime.close("Policy evidence view unmounted.")
  }, [runtime])

  useEffect(() => {
    if (snapshot.transitions.length > VIRTUAL_WINDOW && windowStart === 0) {
      setWindowStart(maximumStart)
    }
  }, [maximumStart, snapshot.transitions.length, windowStart])

  return (
    <section
      className="policy-evidence-view"
      aria-labelledby="policy-evidence-heading"
      data-policy-connection={snapshot.connection}
      data-policy-snapshot={snapshot.snapshotDigest}
      data-policy-transition-count={snapshot.transitions.length}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">P2 causal projection</p>
          <h3 id="policy-evidence-heading">{title}</h3>
        </div>
        <span
          className="tag"
          data-phase={
            snapshot.connection === "ready"
              ? "succeeded"
              : snapshot.connection === "loading"
                ? "running"
                : "failed"
          }
        >
          {snapshot.connection}
        </span>
      </div>
      <p className="muted-copy">
        Read-only projection of canonical proposal, symbolic verdict, graph
        commit, continuity, operator/lease/attempt, LoopX, artifact and verifier
        evidence. Simulated execution never receives a real badge.
      </p>

      {snapshot.error ? (
        <div className="plan-warning" role="alert">
          Evidence adapter degraded: {snapshot.error}
        </div>
      ) : null}
      {snapshot.issues.map((issue, index) => (
        <div
          className="plan-warning"
          key={`${issue.code}:${issue.event_id}:${index}`}
          data-evidence-integrity={issue.integrity}
        >
          {issue.code}: {issue.message}
        </div>
      ))}

      <div className="settings-grid policy-evidence-filters">
        <label>
          Mechanism version
          <input
            value={mechanismVersion}
            onChange={(event) => setMechanismVersion(event.currentTarget.value)}
            placeholder="all versions"
          />
        </label>
        <label>
          Receipt id
          <input
            value={receiptId}
            onChange={(event) => setReceiptId(event.currentTarget.value)}
            placeholder="drill down"
          />
        </label>
        <div className="task-actions">
          <button
            className="button button-primary"
            type="button"
            onClick={() => void runtime.setFilters({
              mechanismVersion: mechanismVersion || undefined,
              receiptId: receiptId || undefined,
            })}
          >
            Apply
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => downloadEvidence(runtime)}
            disabled={snapshot.transitions.length === 0}
          >
            Export with digest
          </button>
        </div>
      </div>

      <dl className="fact-grid policy-evidence-summary">
        <div><dt>Snapshot</dt><dd>{compact(snapshot.snapshotDigest)}</dd></div>
        <div><dt>High watermark</dt><dd>{snapshot.highWatermark}</dd></div>
        <div><dt>Loaded</dt><dd>{snapshot.transitions.length}</dd></div>
        <div><dt>More pages</dt><dd>{snapshot.hasMore ? "yes" : "no"}</dd></div>
        <div><dt>Dropped locally</dt><dd>{snapshot.droppedTransitions}</dd></div>
        <div><dt>Evidence digests</dt><dd>{snapshot.evidenceDigests.length}</dd></div>
      </dl>

      <div className="policy-evidence-window-controls">
        <button
          className="button button-secondary"
          type="button"
          disabled={boundedStart === 0}
          onClick={() => setWindowStart(Math.max(0, boundedStart - VIRTUAL_WINDOW))}
        >
          Previous {VIRTUAL_WINDOW}
        </button>
        <span>
          {snapshot.transitions.length
            ? `${boundedStart + 1}–${boundedStart + visible.length}`
            : "0"} of {snapshot.transitions.length}
        </span>
        <button
          className="button button-secondary"
          type="button"
          disabled={boundedStart >= maximumStart}
          onClick={() => setWindowStart(
            Math.min(maximumStart, boundedStart + VIRTUAL_WINDOW),
          )}
        >
          Next {VIRTUAL_WINDOW}
        </button>
        <button
          className="button button-secondary"
          type="button"
          disabled={!snapshot.hasMore || snapshot.connection === "loading"}
          onClick={() => void runtime.loadNext()}
        >
          Load canonical next page
        </button>
      </div>

      <ol className="policy-evidence-timeline" aria-label="Policy evidence timeline">
        {visible.map((item) => (
          <li
            key={item.transition_id}
            data-policy-transition-id={item.transition_id}
            data-policy-contract-kind={item.contract_kind}
            data-policy-execution={item.execution}
            data-policy-integrity={item.integrity}
          >
            <button
              className="policy-evidence-transition"
              type="button"
              onClick={() => runtime.select(item.transition_id)}
              aria-expanded={selected?.transition_id === item.transition_id}
            >
              <span className="policy-evidence-sequence">#{item.sequence}</span>
              <span>
                <strong>{item.contract_kind}</strong>
                <small>
                  {item.mechanism.id}@{item.mechanism.version}
                </small>
              </span>
              <span className="tag">{item.mechanism.lifecycle}</span>
              <span className="tag">{item.mechanism.readiness}</span>
              <span className="tag">{item.execution}</span>
              <span className="tag">{item.integrity}</span>
            </button>
            <div className="policy-evidence-links">
              {item.causal_refs.slice(0, 12).map((reference) => (
                <EvidenceReference
                  key={`${reference.kind}:${reference.id}`}
                  reference={reference}
                  transition={item}
                  onNavigate={onNavigate}
                />
              ))}
            </div>
            <PhysicalDispatchSummary transition={item} />
          </li>
        ))}
      </ol>

      {snapshot.transitions.length === 0 && snapshot.connection !== "loading" ? (
        <div className="empty-state">
          <strong>No admitted evidence for this filter.</strong>
          <p>Missing evidence is not displayed as a successful mechanism run.</p>
        </div>
      ) : null}

      {selected ? (
        <aside
          className="policy-evidence-inspector"
          aria-label="Selected policy transition"
        >
          <div className="section-heading">
            <h4>{selected.contract_kind}</h4>
            <span>{compact(selected.receipt_id)}</span>
          </div>
          <dl className="fact-grid">
            <div><dt>Disposition</dt><dd>{selected.disposition || "—"}</dd></div>
            <div><dt>Run</dt><dd>{compact(selected.run_id)}</dd></div>
            <div><dt>Task</dt><dd>{compact(selected.task_id)}</dd></div>
            <div><dt>Digest</dt><dd>{compact(selected.contract_digest)}</dd></div>
            <div><dt>Constraints</dt><dd>{selected.constraints.length}</dd></div>
            <div><dt>Graph operations</dt><dd>{selected.graph_diff.length}</dd></div>
          </dl>
          {selected.constraints.length ? (
            <ul>
              {selected.constraints.slice(0, 40).map((item, index) => (
                <li key={`${String(item.constraint_id || "")}:${index}`}>
                  <strong>{String(item.constraint_id || "constraint")}</strong>
                  {" · "}{item.passed === true ? "pass" : "reject"}
                  {" · "}{String(item.reason_code || "")}
                </li>
              ))}
            </ul>
          ) : null}
          {selected.graph_diff.length ? (
            <ul>
              {selected.graph_diff.slice(0, 40).map((item, index) => (
                <li key={`${String(item.entity_id || "")}:${index}`}>
                  <strong>{String(item.kind || "operation")}</strong>
                  {" · "}{String(item.entity_id || "")}
                </li>
              ))}
            </ul>
          ) : null}
        </aside>
      ) : null}
    </section>
  )
}
