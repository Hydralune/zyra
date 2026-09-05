import { phaseLabel } from "../../../shell/product-copy.ts"
import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { ExperimentWorkbenchRuntime } from "../runtime.ts"
import { ExperimentPolicyEvidence } from "../policy/index.ts"

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
  const policyCell = selected?.run.cells.find((item) => Boolean(item.task_id))
  const policyReportId = selected
    ? String(
        selected.run.metadata.policy_metric_report_id
        ?? selected.run.metadata.policy_report_id
        ?? "",
      )
    : ""
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
          <p className="eyebrow">实验分析</p>
          <h2 id="experiment-workbench-heading">实验与指标</h2>
        </div>
        <span className="tag" data-phase={snapshot.connection}>
          {phaseLabel(snapshot.connection)}
        </span>
      </div>
      <p className="muted-copy">
        集中查看实验对比、统计指标、原始样本和验收证据。实验由后端持续执行，关闭浏览器不会中断。
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
            <h3>实验记录</h3>
            <button
              className="button button-secondary"
              type="button"
              disabled={Boolean(busy)}
              onClick={() => void runtime.refresh("manual")}
            >
              刷新
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
                      {phaseLabel(row.run.phase)} · {percent(row.progressRatio)}
                    </span>
                    <small>{row.run.experiment_id}</small>
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted-copy">
              还没有实验记录。
            </p>
          )}
        </article>

        <article>
          <h3>实验配置与存档</h3>
          {selected ? (
            <>
              <dl className="fact-grid">
                <div><dt>状态</dt><dd>{phaseLabel(selected.run.phase)}</dd></div>
                <div><dt>已完成实验单元</dt><dd>
                  {selected.run.progress.terminal}/{selected.run.progress.planned}
                </dd></div>
                <div><dt>重复次数</dt><dd>{selected.run.repetitions}</dd></div>
                <div><dt>领域</dt><dd>{selected.run.envelope.task_domain}</dd></div>
                <div><dt>代码版本</dt><dd>{shortDigest(selected.run.envelope.commit_sha)}</dd></div>
                <div><dt>配置摘要</dt><dd>{shortDigest(selected.run.envelope.envelope_digest)}</dd></div>
                <div><dt>来源</dt><dd>{shortDigest(selected.run.envelope.source_evidence_digest)}</dd></div>
                <div><dt>需要保持浏览器打开</dt><dd>否</dd></div>
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
                  {busy === "start" ? "正在启动…" : "启动实验"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || selected.run.phase !== "succeeded"}
                  onClick={() => void mutate("verify")}
                >
                  {busy === "verify" ? "正在验证…" : "验证并加载归档"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !selected.run.terminal}
                  onClick={() => void mutate("archive")}
                >
                  归档
                </button>
              </div>
              {selected.admission.findings.map((item) => (
                <p className="plan-warning" key={`${item.code}:${item.message}`}>
                  {item.message}
                </p>
              ))}
            </>
          ) : (
            <p className="muted-copy">选择实验后查看配置与结果。</p>
          )}
        </article>
      </div>

      {snapshot.report ? (
        <>
          <section aria-labelledby="experiment-score-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">验收指标</p>
                <h3 id="experiment-score-heading">评分与完整性</h3>
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
              <div><dt>原始样本</dt><dd>{snapshot.headline.rawSamples.toLocaleString()}</dd></div>
              <div><dt>指标汇总</dt><dd>{snapshot.headline.summaries.toLocaleString()}</dd></div>
              <div><dt>对比数量</dt><dd>{snapshot.headline.comparisons.toLocaleString()}</dd></div>
              <div><dt>验收要求</dt><dd>
                {snapshot.headline.requirementsVerified}/{snapshot.headline.requirementsTotal}
              </dd></div>
              <div><dt>归档条目</dt><dd>{snapshot.bundleAssessment?.memberCount ?? 0}</dd></div>
              <div><dt>Merkle 根摘要</dt><dd>{shortDigest(snapshot.bundleAssessment?.merkleRoot)}</dd></div>
              <div><dt>需要后端日志</dt><dd>
                {snapshot.reviewerGraph.backendLogRequired ? "yes" : "no"}
              </dd></div>
              <div><dt>归档路径</dt><dd>{snapshot.bundleAssessment?.path || "—"}</dd></div>
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
                    {row.verified ? "已验证" : "not verified"} ·
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
                <p className="eyebrow">实验统计</p>
                <h3 id="experiment-metrics-heading">分位数、离散度与置信区间</h3>
              </div>
              <span>{metrics.length} rows</span>
            </div>
            <div className="settings-grid">
              <label>
                指标
                <select
                  value={metricFilter}
                  onChange={(event) => setMetricFilter(event.currentTarget.value)}
                >
                  <option value="">全部指标</option>
                  {metricNames.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
              <label>
                变体
                <select
                  value={variantFilter}
                  onChange={(event) => setVariantFilter(event.currentTarget.value)}
                >
                  <option value="">全部变体</option>
                  {variantNames.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>变体</th>
                    <th>n</th>
                    <th>P50</th>
                    <th>P95</th>
                    <th>标准差</th>
                    <th>MAD / IQR</th>
                    <th>均值置信区间</th>
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
              <h3 id="experiment-comparison-heading">基线与消融对比</h3>
              <span>{snapshot.comparisons.length}</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>基线</th>
                    <th>对比变体</th>
                    <th>绝对变化</th>
                    <th>相对变化</th>
                    <th>变化方向</th>
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
                <p className="eyebrow">证据导航</p>
                <h3 id="experiment-navigation-heading">要求 → 指标 → 样本 → 来源</h3>
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
      {selected
        && runtime.policyApi
        && (
          policyCell?.task_id
          || policyCell?.owner_run_id
          || policyCell?.scenario_run_id
        ) ? (
        <ExperimentPolicyEvidence
          api={runtime.policyApi}
          taskId={policyCell?.task_id || undefined}
          runId={
            policyCell?.owner_run_id
            || policyCell?.scenario_run_id
            || undefined
          }
          reportId={policyReportId || undefined}
        />
      ) : null}
    </section>
  )
}
