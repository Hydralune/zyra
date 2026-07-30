import { useMemo, useSyncExternalStore } from "react"
import type { PolicyApi } from "../../../api/policy-api.ts"
import {
  PolicyEvidenceRuntime,
  PolicyEvidenceView,
} from "../../topology/policy/index.ts"

function record(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

const HEADLINE_METRICS = [
  "communication.normalized_entropy",
  "communication.cost_per_effective_transition",
  "continuity.critical_fact_recall",
  "continuity.unresolved_obligation_retention",
  "symbolic.unsafe_commit_count",
  "symbolic.projector_bypass_reachable",
  "operator.early_exit_true_positive_ratio",
  "dispatch.causal_chain_completeness",
  "dispatch.local_real_receipt_completeness",
  "dispatch.edge_real_receipt_completeness",
  "dispatch.cloud_real_receipt_completeness",
] as const

function metricValue(value: unknown): string {
  const selected = Number(value)
  if (!Number.isFinite(selected)) return "—"
  return Number.isInteger(selected)
    ? selected.toLocaleString()
    : selected.toFixed(4)
}

function PolicyMetricProjection({
  runtime,
}: {
  runtime: PolicyEvidenceRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const report = record(snapshot.metricReport)
  const aggregate = record(report.aggregate_report)
  const metrics = record(aggregate.metrics)
  const mechanismReports = Array.isArray(report.mechanism_reports)
    ? report.mechanism_reports
    : []
  const antiGaming = record(report.anti_gaming)
  const realDispatch = snapshot.transitions.filter(
    (item) =>
      item.contract_kind === "physical_dispatch_receipt"
      && item.execution === "real"
      && item.integrity === "verified",
  ).length
  const simulatedDispatch = snapshot.transitions.filter(
    (item) =>
      item.contract_kind === "physical_dispatch_receipt"
      && item.execution === "simulated",
  ).length
  const continuity = snapshot.transitions.filter(
    (item) => item.contract_kind === "memory_continuity_receipt",
  ).length
  const loopx = snapshot.transitions.filter(
    (item) => item.contract_kind === "loopx_control_receipt",
  ).length

  return (
    <section
      className="policy-metric-projection"
      aria-labelledby="policy-value-heading"
      data-policy-metric-status={String(report.status || "missing")}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Receipt-derived value metrics</p>
          <h3 id="policy-value-heading">Baseline, actual and counterfactual evidence</h3>
        </div>
        <span className="tag">{String(report.status || "missing")}</span>
      </div>
      <p className="muted-copy">
        Every observed value retains its source receipt digest. Missing,
        failed and simulated evidence stays separate from successful actuals.
      </p>
      <dl className="fact-grid">
        <div><dt>Mechanism groups</dt><dd>{mechanismReports.length}</dd></div>
        <div><dt>Real dispatch receipts</dt><dd>{realDispatch}</dd></div>
        <div><dt>Simulated dispatch receipts</dt><dd>{simulatedDispatch}</dd></div>
        <div><dt>Continuity receipts</dt><dd>{continuity}</dd></div>
        <div><dt>LoopX receipts</dt><dd>{loopx}</dd></div>
        <div><dt>Report digest</dt><dd>{String(report.digest || "—").slice(0, 20)}</dd></div>
      </dl>
      {report.status !== "verified" ? (
        <div className="plan-warning">
          A verified metric report is not available for this evidence filter.
          No value is promoted to observed success.
        </div>
      ) : null}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Metric</th>
              <th>Status</th>
              <th>Value</th>
              <th>Samples</th>
              <th>Source receipts</th>
            </tr>
          </thead>
          <tbody>
            {HEADLINE_METRICS.map((metricId) => {
              const metric = record(metrics[metricId])
              const sourceRefs = Array.isArray(metric.source_refs)
                ? metric.source_refs
                : []
              return (
                <tr key={metricId} data-policy-metric={metricId}>
                  <th>{metricId}</th>
                  <td>{String(metric.status || "missing")}</td>
                  <td>
                    {metric.status === "observed"
                      ? metricValue(metric.value)
                      : "—"}
                  </td>
                  <td>{Number(metric.sample_count || 0)}</td>
                  <td>{sourceRefs.length}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="settings-grid">
        {Object.entries(antiGaming).map(([key, value]) => (
          <article key={key} data-anti-gaming-pass={value === true}>
            <strong>{key}</strong>
            <p>{value === true ? "enforced" : "not proven"}</p>
          </article>
        ))}
      </div>
    </section>
  )
}

export function ExperimentPolicyEvidence({
  api,
  taskId,
  runId,
  reportId,
}: {
  api: PolicyApi
  taskId?: string
  runId?: string
  reportId?: string
}) {
  const runtime = useMemo(
    () => new PolicyEvidenceRuntime({
      api,
      query: {
        taskId,
        runId,
        reportId,
      },
      pageLimit: 100,
      maximumTransitions: 5_000,
    }),
    [api, reportId, runId, taskId],
  )
  return (
    <>
      <PolicyMetricProjection runtime={runtime} />
      <PolicyEvidenceView
        runtime={runtime}
        title="Run evidence and physical execution chain"
      />
    </>
  )
}
