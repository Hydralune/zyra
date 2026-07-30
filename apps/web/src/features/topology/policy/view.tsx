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
