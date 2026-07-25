import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { ExperimentWorkbenchRuntime } from "../runtime.ts"

function shortDigest(value: unknown): string {
  const selected = String(value || "")
  return selected.length > 20
    ? `${selected.slice(0, 12)}…${selected.slice(-6)}`
    : selected || "—"
}

function number(value: number | undefined, digits = 3): string {
  if (value === undefined || !Number.isFinite(value)) return "—"
  if (Number.isInteger(value)) return value.toLocaleString()
  return value.toFixed(digits)
}

function percent(value: number): string {
  return `${Math.max(0, Math.min(100, value * 100)).toFixed(0)}%`
}

export function ExperimentWorkbench({
  runtime,
}: {
  runtime: ExperimentWorkbenchRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const [metricFilter, setMetricFilter] = useState("")
  const [variantFilter, setVariantFilter] = useState("")
  const selected = snapshot.selected
  const metrics = useMemo(
    () => snapshot.metrics.filter(
      (row) =>
        (!metricFilter || row.metric === metricFilter)
        && (!variantFilter || row.variantId === variantFilter),
    ),
    [snapshot.metrics, metricFilter, variantFilter],
  )
  const metricNames = useMemo(
    () => [...new Set(snapshot.metrics.map((item) => item.metric))].sort(),
    [snapshot.metrics],
  )
  const variantNames = useMemo(
    () => [...new Set(snapshot.metrics.map((item) => item.variantId))].sort(),
    [snapshot.metrics],
  )

  useEffect(() => {
    void runtime.open().catch((reason) => {
      setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => runtime.detach("Experiment evidence panel unmounted.")
  }, [runtime])

  const mutate = async (operation: "start" | "verify" | "archive") => {
    if (!selected) return
    setBusy(operation)
    setError("")
    try {
      if (operation === "start") {
        await runtime.start(selected.run.experiment_id)
      } else if (operation === "verify") {
        await runtime.verify(selected.run.experiment_id)
      } else {
        await runtime.archive(selected.run.experiment_id)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy("")
    }
  }

  return (
    <section
      className="detail-section"
      aria-labelledby="experiment-workbench-heading"
      data-experiment-connection={snapshot.connection}
      data-experiment-active-count={snapshot.activeCount}
      data-report-valid={snapshot.reportAdmission?.valid ?? false}
      data-bundle-valid={snapshot.bundleAssessment?.valid ?? false}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">M2 exit evidence</p>
          <h2 id="experiment-workbench-heading">Ablation and metrics workbench</h2>
        </div>
        <span className="tag" data-phase={snapshot.connection}>
          {snapshot.connection}
        </span>
      </div>
      <p className="muted-copy">
        The backend owns every matrix cell and continues after this browser
        disconnects. Reviewers can move from a 100-point requirement row to
        controlled variants, P50/P95 and dispersion, raw samples, canonical
        source events, verifier receipts and the tamper-evident bundle without
        reading backend logs.
      </p>

      {error ? <div className="plan-warning" role="alert">{error}</div> : null}
      {snapshot.registryAdmission?.findings.map((item) => (
        <div className="plan-warning" key={`${item.code}:${item.message}`}>
          {item.message}
        </div>
      ))}

      <div className="settings-grid">
        <article>
          <div className="section-heading">
            <h3>Durable experiment runs</h3>
            <button
              className="button button-secondary"
              type="button"
              disabled={Boolean(busy)}
              onClick={() => void runtime.refresh("manual")}
            >
              Refresh
            </button>
          </div>
          {snapshot.rows.length ? (
            <ol className="plan-list">
              {snapshot.rows.map((row) => (
                <li
                  className="plan-node"
                  key={row.run.experiment_id}
                  data-selected={row.selected}
                >
                  <span
                    className={`status-marker status-${
                      row.run.phase === "succeeded"
                        ? "completed"
                        : row.run.phase === "failed"
                          ? "failed"
                          : "running"
                    }`}
                    aria-hidden="true"
                  />
                  <button
                    className="button button-secondary"
                    type="button"
                    onClick={() => void runtime.select(row.run.experiment_id)}
                  >
                    <strong>{row.run.title}</strong>
                    <span>
                      {row.run.phase} · {percent(row.progressRatio)}
                    </span>
                    <small>{row.run.experiment_id}</small>
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted-copy">
              No formal experiment is present in this clean state.
            </p>
          )}
        </article>

        <article>
          <h3>Selected envelope and custody</h3>
          {selected ? (
            <>
              <dl className="fact-grid">
                <div><dt>Phase</dt><dd>{selected.run.phase}</dd></div>
                <div><dt>Matrix cells</dt><dd>
                  {selected.run.progress.terminal}/{selected.run.progress.planned}
                </dd></div>
                <div><dt>Repetitions</dt><dd>{selected.run.repetitions}</dd></div>
                <div><dt>Domain</dt><dd>{selected.run.envelope.task_domain}</dd></div>
                <div><dt>Commit</dt><dd>{shortDigest(selected.run.envelope.commit_sha)}</dd></div>
                <div><dt>Envelope</dt><dd>{shortDigest(selected.run.envelope.envelope_digest)}</dd></div>
                <div><dt>Source</dt><dd>{shortDigest(selected.run.envelope.source_evidence_digest)}</dd></div>
                <div><dt>Browser required</dt><dd>no</dd></div>
              </dl>
              <div className="task-actions">
                <button
                  className="button button-primary"
                  type="button"
                  disabled={
                    Boolean(busy)
                    || !["created", "admitted"].includes(selected.run.phase)
                  }
                  onClick={() => void mutate("start")}
                >
                  {busy === "start" ? "Starting…" : "Start matrix"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || selected.run.phase !== "succeeded"}
                  onClick={() => void mutate("verify")}
                >
                  {busy === "verify" ? "Verifying…" : "Verify and load bundle"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !selected.run.terminal}
                  onClick={() => void mutate("archive")}
                >
                  Archive
                </button>
              </div>
              {selected.admission.findings.map((item) => (
                <p className="plan-warning" key={`${item.code}:${item.message}`}>
                  {item.message}
                </p>
              ))}
            </>
          ) : (
            <p className="muted-copy">Select an experiment to inspect it.</p>
          )}
        </article>
      </div>

      {snapshot.report ? (
        <>
          <section aria-labelledby="experiment-score-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Authoritative requirement matrix</p>
                <h3 id="experiment-score-heading">Exit score and integrity</h3>
              </div>
              <span
                className="tag"
                data-phase={
                  snapshot.reportAdmission?.valid
                    && snapshot.bundleAssessment?.valid
                    ? "succeeded"
                    : "failed"
                }
              >
                {snapshot.headline.scoreVerified}/{snapshot.headline.scoreTotal}
              </span>
            </div>
            <dl className="fact-grid">
              <div><dt>Raw samples</dt><dd>{snapshot.headline.rawSamples.toLocaleString()}</dd></div>
              <div><dt>Metric summaries</dt><dd>{snapshot.headline.summaries.toLocaleString()}</dd></div>
              <div><dt>Comparisons</dt><dd>{snapshot.headline.comparisons.toLocaleString()}</dd></div>
              <div><dt>Requirements</dt><dd>
                {snapshot.headline.requirementsVerified}/{snapshot.headline.requirementsTotal}
              </dd></div>
              <div><dt>Bundle members</dt><dd>{snapshot.bundleAssessment?.memberCount ?? 0}</dd></div>
              <div><dt>Merkle root</dt><dd>{shortDigest(snapshot.bundleAssessment?.merkleRoot)}</dd></div>
              <div><dt>Backend logs required</dt><dd>
                {snapshot.reviewerGraph.backendLogRequired ? "yes" : "no"}
              </dd></div>
              <div><dt>Bundle path</dt><dd>{snapshot.bundleAssessment?.path || "—"}</dd></div>
            </dl>
            {snapshot.reportAdmission?.findings.map((item) => (
              <p className="plan-warning" key={`${item.code}:${item.message}`}>
                {item.message}
              </p>
            ))}
            {snapshot.bundleAssessment?.findings.map((item) => (
              <p className="plan-warning" key={item}>{item}</p>
            ))}
            <div className="settings-grid">
              {snapshot.requirementRows.map((row) => (
                <article
                  key={row.requirementId}
                  id={`experiment-requirement-${row.requirementId}`}
                  data-requirement-verified={row.verified}
                >
                  <div className="section-heading">
                    <h4>{row.requirementId}</h4>
                    <span>{row.score} pt</span>
                  </div>
                  <p>{row.title}</p>
                  <p className="muted-copy">
                    {row.verified ? "verified" : "not verified"} ·
                    {" "}{row.metrics.length} metrics · {row.variants.length} variants ·
                    {" "}{row.evidenceIds.length} evidence links
                  </p>
                </article>
              ))}
            </div>
          </section>

          <section aria-labelledby="experiment-metrics-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Controlled matrix statistics</p>
                <h3 id="experiment-metrics-heading">P50, P95, dispersion and confidence</h3>
              </div>
              <span>{metrics.length} rows</span>
            </div>
            <div className="settings-grid">
              <label>
                Metric
                <select
                  value={metricFilter}
                  onChange={(event) => setMetricFilter(event.currentTarget.value)}
                >
                  <option value="">All metrics</option>
                  {metricNames.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
              <label>
                Variant
                <select
                  value={variantFilter}
                  onChange={(event) => setVariantFilter(event.currentTarget.value)}
                >
                  <option value="">All variants</option>
                  {variantNames.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Metric</th>
                    <th>Variant</th>
                    <th>n</th>
                    <th>P50</th>
                    <th>P95</th>
                    <th>Std dev</th>
                    <th>MAD / IQR</th>
                    <th>Mean confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.slice(0, 500).map((row) => (
                    <tr key={`${row.metric}:${row.variantId}`}>
                      <th>{row.metric}</th>
                      <td>{row.variantId}</td>
                      <td>{row.count}</td>
                      <td>{number(row.p50)}</td>
                      <td>{number(row.p95)}</td>
                      <td>{number(row.standardDeviation)}</td>
                      <td>
                        {number(row.medianAbsoluteDeviation)} / {number(row.interquartileRange)}
                      </td>
                      <td>
                        [{number(row.confidenceLower)}, {number(row.confidenceUpper)}]
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section aria-labelledby="experiment-comparison-heading">
            <div className="section-heading">
              <h3 id="experiment-comparison-heading">Baseline and ablation effects</h3>
              <span>{snapshot.comparisons.length}</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Metric</th>
                    <th>Anchor</th>
                    <th>Compared</th>
                    <th>Absolute Δ</th>
                    <th>Relative Δ</th>
                    <th>Direction</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.comparisons.slice(0, 500).map((row) => (
                    <tr
                      key={`${row.metric}:${row.baselineVariantId}:${row.comparedVariantId}`}
                    >
                      <th>{row.metric}</th>
                      <td>{row.baselineVariantId}</td>
                      <td>{row.comparedVariantId}</td>
                      <td>{number(row.absoluteDelta)}</td>
                      <td>{number(row.relativeDelta)}</td>
                      <td>{row.effectDirection || "descriptive"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section aria-labelledby="experiment-navigation-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Reviewer navigation</p>
                <h3 id="experiment-navigation-heading">Requirement → metric → sample → source</h3>
              </div>
              <span>
                {snapshot.reviewerGraph.nodes.length} nodes /
                {" "}{snapshot.reviewerGraph.edges.length} edges
              </span>
            </div>
            <ol className="plan-list">
              {snapshot.reviewerGraph.nodes.slice(0, 80).map((node) => (
                <li className="plan-node" key={node.nodeId}>
                  <span className="status-marker status-completed" aria-hidden="true" />
                  <div>
                    <strong>{node.label}</strong>
                    <span>{node.kind} · {shortDigest(node.digest)}</span>
                    <small>{node.route}</small>
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </>
      ) : null}
    </section>
  )
}
