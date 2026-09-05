import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import { selectMemoryConsole, type MemoryLayer } from "../../memory/projection.ts"
import { selectProviderConsole } from "../../providers/projection.ts"
import { selectPlacementConsole } from "../../placement/projection.ts"
import { humanTokens } from "../value.ts"

function useSessionRuntime(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.sessionConsole.subscribe,
    runtime.sessionConsole.getSnapshot,
    runtime.sessionConsole.getSnapshot,
  )
}

function phaseTone(phase: string): string {
  if (phase === "committed") return "success"
  if (phase === "failed" || phase === "disabled" || phase === "disconnected") return "danger"
  if (phase === "queued" || phase === "reconciling") return "warning"
  return "muted"
}

export function SessionConsoleWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const snapshot = useSessionRuntime(runtime)
  const [memoryQuery, setMemoryQuery] = useState("")
  const [memoryLayer, setMemoryLayer] = useState<MemoryLayer | "all">("all")
  const [targetTokens, setTargetTokens] = useState<number | undefined>()
  const [requiredCapability, setRequiredCapability] = useState("")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const memorySelector = useMemo(
    () => selectMemoryConsole(task.taskId, {
      text: memoryQuery,
      layers: memoryLayer === "all" ? [] : [memoryLayer],
      limit: 50,
    }),
    [memoryLayer, memoryQuery, task.taskId],
  )
  const providerSelector = useMemo(
    () => selectProviderConsole(task.taskId, {
      requiredCapabilities: requiredCapability ? [requiredCapability] : [],
    }),
    [requiredCapability, task.taskId],
  )
  const placementSelector = useMemo(
    () => selectPlacementConsole(task.taskId),
    [task.taskId],
  )
  const memory = useProjectionSelector(runtime, memorySelector)
  const providers = useProjectionSelector(runtime, providerSelector)
  const placement = useProjectionSelector(runtime, placementSelector)

  useEffect(() => {
    runtime.sessionConsole.bind(task.taskId, task.runId, snapshot.activeSessionId)
  }, [runtime, task.runId, task.taskId])

  useEffect(() => {
    const offline = () => runtime.sessionConsole.disconnected()
    const online = () => runtime.sessionConsole.reconnected()
    window.addEventListener("offline", offline)
    window.addEventListener("online", online)
    return () => {
      window.removeEventListener("offline", offline)
      window.removeEventListener("online", online)
    }
  }, [runtime])

  const act = async (name: string, action: () => Promise<unknown>) => {
    setBusy(name)
    setError("")
    try {
      await action()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy("")
    }
  }

  const projection = snapshot.projection
  const context = projection?.context
  const preview = projection?.preview
  const operation = snapshot.active

  return (
    <section className="detail-section session-console" aria-labelledby="session-console-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Canonical backend custody</p>
          <h3 id="session-console-heading">会话、记忆与执行位置</h3>
        </div>
        <span className={`tag tag-${projection?.connected ? "success" : "danger"}`}>
          {projection?.connected ? "live" : "disconnected"}
        </span>
      </div>

      {error ? <p className="plan-warning" role="alert">{error}</p> : null}
      {operation ? (
        <div className="console-operation" data-operation-id={operation.id}>
          <span className={`tag tag-${phaseTone(operation.phase)}`}>{operation.phase}</span>
          <strong>{operation.name}</strong>
          <span>rev {operation.startedRevision}</span>
          {operation.receipt ? <span>{operation.receipt.summary}</span> : null}
          {operation.error ? <span className="danger-copy">{operation.error}</span> : null}
        </div>
      ) : null}

      <div className="settings-grid">
        <article aria-labelledby="session-lineage-heading">
          <div className="section-heading">
            <h4 id="session-lineage-heading">会话关系</h4>
            <span>{projection?.lineage.length ?? 0}</span>
          </div>
          <label>
            Active session
            <select
              value={projection?.activeSessionId ?? ""}
              onChange={(event) => {
                try {
                  runtime.sessionConsole.setSession(event.currentTarget.value)
                } catch (cause) {
                  setError(cause instanceof Error ? cause.message : String(cause))
                }
              }}
              disabled={!snapshot.enabled || !snapshot.connected}
            >
              {(projection?.lineage ?? []).map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {"· ".repeat(entry.depth)}{entry.id} · {entry.lifecycle}
                </option>
              ))}
            </select>
          </label>
          <ol className="compact-list">
            {(projection?.lineage ?? []).map((entry) => (
              <li key={entry.id} data-session-id={entry.id} data-active={entry.active}>
                <button type="button" onClick={() => runtime.sessionConsole.setSession(entry.id)}>
                  <strong>{entry.id}</strong>
                  <span>{entry.forked ? "fork" : "root"} · epoch {entry.compactEpoch}</span>
                </button>
                <small>
                  {humanTokens(entry.contextTokens)}/{humanTokens(entry.contextLimit)} tokens ·
                  {entry.workerIds.length} workers · {entry.childIds.length} children
                </small>
              </li>
            ))}
          </ol>
          {projection?.orphans.length ? (
            <p className="plan-warning">{projection.orphans.length} orphan lineage node(s)</p>
          ) : null}
          {projection?.cycles.length ? (
            <p className="plan-warning">{projection.cycles.length} cyclic lineage node(s)</p>
          ) : null}
        </article>

        <article aria-labelledby="context-budget-heading">
          <div className="section-heading">
            <h4 id="context-budget-heading">上下文与压缩</h4>
            <span>{context?.pressure ?? "unavailable"}</span>
          </div>
          <dl className="fact-grid">
            <div><dt>已使用</dt><dd>{humanTokens(context?.used ?? 0)}</dd></div>
            <div><dt>上限</dt><dd>{humanTokens(context?.limit ?? 0)}</dd></div>
            <div><dt>预留</dt><dd>{humanTokens(context?.reserve ?? 0)}</dd></div>
            <div><dt>压缩轮次</dt><dd>{context?.compactEpoch ?? 0}</dd></div>
            <div><dt>恢复来源</dt><dd>{projection?.restoreSource ?? "none"}</dd></div>
            <div><dt>待写入</dt><dd>{projection?.pendingWriteCount ?? 0}</dd></div>
          </dl>
          <div className="context-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={(context?.percentage ?? 0) * 100}>
            <span style={{ width: `${Math.min(100, (context?.percentage ?? 0) * 100)}%` }} />
          </div>
          <ol className="compact-list">
            {(context?.categories ?? []).map((category) => (
              <li key={category.id}>
                <strong>{category.label}</strong>
                <span>{humanTokens(category.tokens)} · {(category.percentage * 100).toFixed(1)}%</span>
              </li>
            ))}
          </ol>
          <label>
            Post-compact target
            <input
              type="number"
              min={512}
              max={context?.limit ?? 1000000}
              value={targetTokens ?? preview?.targetTokens ?? ""}
              onChange={(event) => setTargetTokens(Number(event.currentTarget.value) || undefined)}
            />
          </label>
          <div className="task-actions">
            <button
              className="button button-secondary"
              type="button"
              disabled={Boolean(busy) || !snapshot.connected}
              onClick={() => {
                try {
                  runtime.sessionConsole.previewCompact({ targetTokens })
                } catch (cause) {
                  setError(cause instanceof Error ? cause.message : String(cause))
                }
              }}
            >
              Preview compact
            </button>
            <button
              className="button button-primary"
              type="button"
              disabled={Boolean(busy) || !preview?.eligible || !snapshot.connected}
              onClick={() => void act("compact", () => runtime.sessionConsole.executeCompact({
                targetTokens,
                previewFingerprint: preview?.fingerprint,
              }))}
            >
              {busy === "compact" ? "Compacting…" : "Execute compact"}
            </button>
            <button
              className="button button-secondary"
              type="button"
              disabled={Boolean(busy) || !snapshot.connected}
              onClick={() => void act("context", () => runtime.sessionConsole.inspectContext("all"))}
            >
              查看上下文
            </button>
          </div>
          {preview ? (
            <dl className="fact-grid">
              <div><dt>Projected</dt><dd>{humanTokens(preview.projectedTokens)}</dd></div>
              <div><dt>Saved</dt><dd>{humanTokens(preview.savedTokens)}</dd></div>
              <div><dt>Retained refs</dt><dd>{preview.retainedSourceIds.length}</dd></div>
              <div><dt>Dropped refs</dt><dd>{preview.droppedSourceIds.length}</dd></div>
            </dl>
          ) : null}
        </article>
      </div>

      <div className="settings-grid">
        <article aria-labelledby="checkpoint-heading">
          <div className="section-heading">
            <h4 id="checkpoint-heading">检查点与恢复</h4>
            <span>{projection?.checkpoints.length ?? 0}</span>
          </div>
          <ol className="compact-list">
            {(projection?.checkpoints ?? []).slice(0, 20).map((checkpoint) => (
              <li key={checkpoint.id} data-disposition={checkpoint.disposition}>
                <div>
                  <strong>{checkpoint.id}</strong>
                  <span className={`tag tag-${checkpoint.disposition === "conflict" || checkpoint.disposition === "stale" ? "danger" : "muted"}`}>
                    {checkpoint.disposition}
                  </span>
                </div>
                <small>
                  {checkpoint.pendingWrites} pending / {checkpoint.committedWrites} committed · {checkpoint.source}
                </small>
                <div className="task-actions">
                  <button
                    className="button button-secondary"
                    type="button"
                    disabled={!checkpoint.exactResumeEligible || Boolean(busy) || !snapshot.connected}
                    onClick={() => void act("resume", () => runtime.sessionConsole.resume(checkpoint.id))}
                  >
                    Resume
                  </button>
                  <button
                    className="button button-secondary"
                    type="button"
                    disabled={!checkpoint.exactResumeEligible || Boolean(busy) || !snapshot.connected}
                    onClick={() => void act("rewind", () => runtime.sessionConsole.rewind(checkpoint.id))}
                  >
                    Rewind
                  </button>
                </div>
                {checkpoint.conflictReason || checkpoint.staleReason ? (
                  <p className="danger-copy">{checkpoint.conflictReason ?? checkpoint.staleReason}</p>
                ) : null}
              </li>
            ))}
          </ol>
          <button
            className="button button-secondary"
            type="button"
            disabled={Boolean(busy) || !snapshot.connected}
            onClick={() => void act("export", () => runtime.sessionConsole.exportSession({ include: "all" }))}
          >
            Export run
          </button>
        </article>

        <article aria-labelledby="memory-heading">
          <div className="section-heading">
            <h4 id="memory-heading">记忆管理</h4>
            <span>{memory.totalRows}</span>
          </div>
          <div className="filter-row">
            <input
              value={memoryQuery}
              placeholder="搜索记忆记录"
              onChange={(event) => setMemoryQuery(event.currentTarget.value)}
            />
            <select value={memoryLayer} onChange={(event) => setMemoryLayer(event.currentTarget.value as MemoryLayer | "all")}>
              <option value="all">全部记忆层</option>
              <option value="working">Working</option>
              <option value="episodic">Episodic</option>
              <option value="semantic">Semantic</option>
              <option value="skill">Skill</option>
            </select>
            <button
              className="button button-secondary"
              type="button"
              disabled={Boolean(busy) || !snapshot.connected}
              onClick={() => void act("memory", () => runtime.sessionConsole.queryMemory({
                query: memoryQuery,
                layer: memoryLayer,
              }))}
            >
              Query backend
            </button>
          </div>
          <dl className="fact-grid">
            {memory.layerSummaries.map((layer) => (
              <div key={layer.layer}>
                <dt>{layer.layer}</dt>
                <dd>{layer.count} · {humanTokens(layer.tokens)} tokens</dd>
              </div>
            ))}
          </dl>
          <ol className="compact-list">
            {memory.rows.slice(0, 20).map((row) => (
              <li key={row.id} data-memory-layer={row.layer}>
                <div>
                  <strong>{row.title}</strong>
                  <span className="tag tag-muted">{row.layer}</span>
                  <span className="tag tag-muted">{row.veracity}</span>
                </div>
                <p>{row.summary}</p>
                <small>
                  score {(row.combinedScore * 100).toFixed(0)} · provenance {(row.provenanceScore * 100).toFixed(0)} ·
                  {row.sourceEventIds.length} events / {row.sourceArtifactIds.length} artifacts
                </small>
              </li>
            ))}
          </ol>
          {memory.latestCuratorRun ? (
            <div className="console-operation">
              <strong>Curator {memory.latestCuratorRun.id}</strong>
              <span>{memory.latestCuratorRun.active ? "active" : "settled"}</span>
              <span>{memory.latestCuratorRun.candidateCount} candidates · {memory.latestCuratorRun.committedCount} committed</span>
            </div>
          ) : null}
          <button
            className="button button-secondary"
            type="button"
            disabled={Boolean(busy) || !snapshot.connected || !task.active}
            onClick={() => void act("curator", () => runtime.sessionConsole.curateMemory({ action: "curate" }))}
            title={task.active ? undefined : "任务已结束；继续执行任务后可整理记忆"}
          >
            Run curator
          </button>
        </article>
      </div>

      <div className="settings-grid">
        <article aria-labelledby="provider-heading">
          <div className="section-heading">
            <h4 id="provider-heading">服务商与模型</h4>
            <span>{providers.providers.length} / {providers.models.length}</span>
          </div>
          <label>
            Required capability
            <input
              value={requiredCapability}
              placeholder="tools, vision, reasoning…"
              onChange={(event) => setRequiredCapability(event.currentTarget.value.trim())}
            />
          </label>
          {providers.selectedRoute ? (
            <div className="console-operation">
              <strong>{providers.selectedRoute.providerId} / {providers.selectedRoute.modelId}</strong>
              <span>{providers.selectedRoute.routeId}</span>
              {providers.selectedRoute.degraded ? <span className="tag tag-warning">fallback</span> : null}
            </div>
          ) : null}
          <ol className="compact-list">
            {providers.providers.map((provider) => (
              <li key={provider.id} data-provider-health={provider.health}>
                <div>
                  <strong>{provider.name}</strong>
                  <span className="tag tag-muted">{provider.health}</span>
                  <span className="tag tag-muted">
                    credentials {provider.credentialPresent ? "present" : "absent"}
                  </span>
                </div>
                <small>
                  {provider.models.length} models · {provider.capabilities.length} capabilities ·
                  quota {provider.quota ? `${provider.quota.remaining}/${provider.quota.limit}` : "unknown"}
                </small>
              </li>
            ))}
          </ol>
          <ol className="compact-list">
            {providers.candidates.slice(0, 12).map((candidate) => (
              <li key={candidate.id}>
                <div>
                  <strong>#{candidate.rank} {candidate.providerId}/{candidate.modelId}</strong>
                  <span className="tag tag-muted">{candidate.admission}</span>
                </div>
                <small>score {(candidate.score * 100).toFixed(0)} · {candidate.reasons.join("; ") || "eligible"}</small>
                {candidate.admission === "eligible" ? (
                  <button
                    className="button button-secondary"
                    type="button"
                    disabled={Boolean(busy) || !snapshot.connected}
                    onClick={() => void act("model", () => runtime.sessionConsole.selectModel({
                      action: "select",
                      providerId: candidate.providerId,
                      modelId: candidate.modelId,
                      purpose: requiredCapability || "general",
                    }))}
                  >
                    Select route
                  </button>
                ) : null}
              </li>
            ))}
          </ol>
          <dl className="fact-grid">
            <div><dt>Input</dt><dd>{humanTokens(providers.totalInputTokens)}</dd></div>
            <div><dt>Output</dt><dd>{humanTokens(providers.totalOutputTokens)}</dd></div>
            <div><dt>Cost</dt><dd>${providers.totalCostUsd.toFixed(4)}</dd></div>
            <div><dt>Failures</dt><dd>{providers.totalFailures}</dd></div>
          </dl>
        </article>

        <article aria-labelledby="placement-heading">
          <div className="section-heading">
            <h4 id="placement-heading">设备、边缘与云端执行</h4>
            <span>{placement.selectedTier ?? "unplaced"}</span>
          </div>
          <dl className="fact-grid">
            <div><dt>Sensitivity</dt><dd>{placement.constraints.sensitivity}</dd></div>
            <div><dt>Privacy</dt><dd>{placement.privacyCompliant ? "compliant" : "violation"}</dd></div>
            <div><dt>SLA</dt><dd>{placement.slaCompliant ? "compliant" : "violation"}</dd></div>
            <div><dt>Cost</dt><dd>{placement.costCompliant ? "compliant" : "violation"}</dd></div>
          </dl>
          <ol className="compact-list">
            {placement.profiles.map((profile) => (
              <li key={profile.id}>
                <div>
                  <strong>{profile.label}</strong>
                  <span className="tag tag-muted">{profile.tier}</span>
                  <span className="tag tag-muted">{profile.online ? "online" : "offline"}</span>
                </div>
                <small>
                  CPU {profile.capacity.cpuCores} · GPU {profile.capacity.gpuCount}/{profile.capacity.gpuMemoryMb} MiB ·
                  RAM {profile.capacity.memoryUsedMb}/{profile.capacity.memoryMb} MiB ·
                  {profile.capacity.networkLatencyMs} ms
                </small>
              </li>
            ))}
          </ol>
          <ol className="compact-list">
            {placement.candidates.map((candidate) => (
              <li key={candidate.id}>
                <div>
                  <strong>#{candidate.rank} {candidate.profileId}</strong>
                  <span className="tag tag-muted">{candidate.admission}</span>
                </div>
                <small>
                  score {(candidate.score * 100).toFixed(0)} · privacy {(candidate.privacyScore * 100).toFixed(0)} ·
                  latency {candidate.estimatedLatencyMs} ms · ${candidate.estimatedCostUsd.toFixed(3)}
                </small>
                {candidate.violations.length ? <p className="danger-copy">{candidate.violations.join("; ")}</p> : null}
              </li>
            ))}
          </ol>
          {placement.modelSplit.length ? (
            <div>
              <strong>Model split</strong>
              <ol className="compact-list">
                {placement.modelSplit.map((leg) => (
                  <li key={leg.id}>
                    <span>{leg.role}</span>
                    <span>{(leg.fraction * 100).toFixed(0)}% · {leg.tier} · {leg.providerId}/{leg.modelId}</span>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          {placement.migrations.length ? (
            <div>
              <strong>Migration history</strong>
              <ol className="compact-list">
                {placement.migrations.slice(0, 10).map((migration) => (
                  <li key={migration.id}>
                    <span>{migration.fromTier} → {migration.toTier}</span>
                    <span>{migration.phase} · {migration.reason}</span>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          {placement.violations.length ? (
            <div className="plan-warning" role="status">
              {placement.violations.map((violation) => (
                <p key={violation.id}>{violation.severity}: {violation.message}</p>
              ))}
            </div>
          ) : null}
        </article>
      </div>
    </section>
  )
}
