import { phaseLabel } from "../../../shell/product-copy.ts"
import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react"
import { formalActionPolicy } from "../admission.ts"
import type { ScenarioWorkbenchRuntime } from "../runtime.ts"

function duration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "—"
  if (value < 1_000) return `${Math.floor(value)} ms`
  const seconds = value / 1_000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${Math.floor(seconds % 60)}s`
}

function shortDigest(value: unknown): string {
  const selected = String(value || "")
  return selected.length > 18
    ? `${selected.slice(0, 10)}…${selected.slice(-6)}`
    : selected || "—"
}

export function ScenarioWorkbench({
  runtime,
}: {
  runtime: ScenarioWorkbenchRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const [input, setInput] = useState("")
  const [seed, setSeed] = useState("0")
  const [scenarioId, setScenarioId] = useState("live.software-delivery")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const [mode, setMode] = useState<"sealed" | "interactive">("interactive")
  const selected = snapshot.selected
  const actions = selected
    ? formalActionPolicy(selected.run)
    : undefined
  const definition = snapshot.definitions.find(
    (item) => item.scenario_id === scenarioId,
  ) ?? snapshot.definitions[0]
  const liveEvidence = selected?.evidence.live
  const effects = useMemo(
    () => Object.entries(selected?.evidence.effects ?? {}),
    [selected?.evidence.effects],
  )

  useEffect(() => {
    void runtime.open().catch((reason) => {
      setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => runtime.detach("Scenario panel unmounted.")
  }, [runtime])

  const create = async (event: FormEvent) => {
    event.preventDefault()
    setBusy("create")
    setError("")
    try {
      const run = await runtime.create({
        scenarioId: definition?.scenario_id,
        definitionVersion: definition?.version,
        mode,
        input,
        seed: Number(seed),
        labels: {
          surface: "zyra-workbench",
          slice: "M2-S05-02",
        },
      })
      setInput("")
      await runtime.select(run.scenario_run_id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy("")
    }
  }

  const mutate = async (
    operation: "start" | "archive" | "verify",
  ) => {
    if (!selected) return
    setBusy(operation)
    setError("")
    try {
      if (operation === "start") {
        await runtime.start(selected.run.scenario_run_id)
      } else if (operation === "archive") {
        await runtime.archive(
          selected.run.scenario_run_id,
          "Archived from the scenario workbench.",
        )
      } else {
        await runtime.verify(selected.run.scenario_run_id)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy("")
    }
  }

  return (
    <section
      className="detail-section scenario-tool"
      aria-labelledby="scenario-workbench-heading"
      data-scenario-connection={snapshot.connection}
      data-scenario-active-count={snapshot.activeCount}
      data-scenario-evidence-valid-count={snapshot.evidenceValidCount}
    >
      <div className="section-heading">
        <div>
          <h2 id="scenario-workbench-heading">任务试运行</h2>
        </div>
        <span className="tag" data-phase={snapshot.connection}>
          {phaseLabel(snapshot.connection)}
        </span>
      </div>
      <p className="muted-copy">
        输入一个目标，创建后再启动，检查 Zyra 是否能完成它。启动后关闭此页面也会继续执行。
      </p>

      <form className="scenario-create-form" onSubmit={create}>
        <label>
          任务类型
          <select
            value={definition?.scenario_id ?? ""}
            onChange={(event) => setScenarioId(event.currentTarget.value)}
            disabled={Boolean(busy) || !snapshot.definitions.length}
          >
            {!snapshot.definitions.length ? <option value="">{snapshot.connection === "online" ? "暂无可用任务类型" : error ? "任务类型加载失败" : "正在加载任务类型…"}</option> : null}
            {snapshot.definitions.map((item) => (
              <option key={`${item.scenario_id}:${item.version}`} value={item.scenario_id}>
                {({ "foundation.short-owner-chain": "简短流程检查", "live.software-delivery": "软件交付", "live.cross-source-research": "跨源研究" } as Record<string, string>)[item.scenario_id] ?? item.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          你希望完成什么？
          <textarea
            value={input}
            onChange={(event) => setInput(event.currentTarget.value)}
            placeholder={
              definition?.scenario_id === "live.cross-source-research"
                ? "描述研究问题，以及论据、矛盾检查和引用要求。"
                : "描述软件改动、预期行为和交付要求。"
            }
            required
            rows={4}
            disabled={Boolean(busy)}
          />
        </label>
        <details className="scenario-options">
          <summary>验收与复现选项 <span>{mode === "sealed" ? "正式验收" : "日常试运行"}</span></summary>
          <label>
            检查方式
            <select value={mode} disabled={Boolean(busy)} onChange={(event) => setMode(event.currentTarget.value as "sealed" | "interactive")}>
              <option value="interactive">日常试运行</option>
              <option value="sealed">正式验收</option>
            </select>
          </label>
          <p className="muted-copy">{mode === "sealed" ? "用于正式评测：要求干净的初始状态和新输入，并检查故障恢复、执行位置与结果证据。" : "允许使用已有状态，适合日常检查。结果不作为正式验收证据。"}</p>
          <label>
          随机种子（用于复现）
          <input
            type="number"
            min="0"
            step="1"
            value={seed}
            onChange={(event) => setSeed(event.currentTarget.value)}
            disabled={Boolean(busy)}
          />
          </label>
          <p className="muted-copy">相同种子用于复现相同的随机配置；一般保留 0 即可。</p>
        </details>
        <div className="settings-actions">
          <button
            className="button button-primary"
            type="submit"
            disabled={Boolean(busy) || !input.trim() || !definition}
          >
            {busy === "create" ? "正在创建…" : mode === "sealed" ? "创建正式验收" : "创建试运行"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            disabled={Boolean(busy)}
            onClick={() => void runtime.refresh("manual")}
          >
            刷新记录
          </button>
        </div>
        <p className="muted-copy">创建只保存配置。准备好后，在记录中点击“启动试运行”。</p>
      </form>

      {error ? (
        <div className="plan-warning" role="alert">{error}</div>
      ) : null}

      <div className={`settings-grid scenario-records${snapshot.rows.length ? "" : " tool-records-empty"}`}>
        <article>
          <h3>试运行记录</h3>
          {snapshot.rows.length ? (
            <ol className="plan-list">
              {snapshot.rows.map((row) => (
                <li
                  className="plan-node"
                  key={row.run.scenario_run_id}
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
                    type="button"
                    className="button button-secondary"
                    onClick={() => void runtime.select(row.run.scenario_run_id)}
                  >
                    <strong>{({ "foundation.short-owner-chain": "简短流程检查", "live.software-delivery": "软件交付", "live.cross-source-research": "跨源研究" } as Record<string, string>)[row.run.configuration.scenario_id] ?? row.run.configuration.scenario_id}</strong>
                    <span>{phaseLabel(row.run.phase)}</span>
                    <small>{row.run.scenario_run_id}</small>
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted-copy">
              还没有试运行记录。填写上方目标并创建后，记录会显示在这里。
            </p>
          )}
        </article>

        {snapshot.rows.length ? <article>
          <h3>执行情况</h3>
          {selected ? (
            <>
              <dl className="fact-grid">
                <div>
                  <dt>运行模式</dt>
                  <dd>{selected.admission.formal ? "正式验收" : "日常试运行"}</dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>{phaseLabel(selected.run.phase)}</dd>
                </div>
                <div>
                  <dt>耗时</dt>
                  <dd>{duration(selected.elapsedMs)}</dd>
                </div>
                <div>
                  <dt>初始状态检查</dt>
                  <dd>{selected.admission.clean ? "已验证" : selected.admission.formal ? "未通过" : "已有状态"}</dd>
                </div>
                <div>
                  <dt>新输入检查</dt>
                  <dd>{selected.admission.newInput ? "已验证" : "复用输入"}</dd>
                </div>
                <div>
                  <dt>策略</dt>
                  <dd>{shortDigest(
                    selected.run.configuration.policy?.policy_digest,
                  )}</dd>
                </div>
                <div>
                  <dt>人工干预次数</dt>
                  <dd>{selected.admission.humanInterventionCount}</dd>
                </div>
                <div>
                  <dt>有效步骤</dt>
                  <dd>{selected.evidence.effectiveStepCount}</dd>
                </div>
                <div>
                  <dt>无效步骤</dt>
                  <dd>{selected.evidence.invalidStepCount}</dd>
                </div>
                <div>
                  <dt>产物</dt>
                  <dd>{selected.evidence.artifactCount}</dd>
                </div>
                <div>
                  <dt>证据</dt>
                  <dd>{selected.evidence.valid ? "已验证" : "待验证"}</dd>
                </div>
              </dl>
              <div className="task-actions">
                <button
                  className="button button-primary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayStart}
                  onClick={() => void mutate("start")}
                >
                  {busy === "start" ? "正在启动…" : "启动试运行"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayVerify}
                  onClick={() => void mutate("verify")}
                >
                  {busy === "verify" ? "正在验证…" : "验证证据"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayArchive}
                  onClick={() => void mutate("archive")}
                >
                  归档
                </button>
              </div>
              {actions?.warning ? (
                <p className="plan-warning">{actions.warning}</p>
              ) : null}
              {selected.run.failure ? (
                <div className="plan-warning" role="alert">
                  <strong>场景执行未完成</strong>
                  <p>{String(selected.run.failure.message ?? selected.run.failure.code ?? "后端未提供失败说明。")}</p>
                </div>
              ) : null}
              {selected.admission.findings.map((item) => (
                <p className="plan-warning" key={item.code}>
                  {item.message}
                </p>
              ))}
            </>
          ) : (
            <p className="muted-copy">选择一条记录，查看执行情况与结果证据。</p>
          )}
        </article> : null}
      </div>

      {selected?.evidence.valid ? (
        <section aria-label="Effective step dimensions">
          <div className="section-heading">
            <h3>步骤效果</h3>
            <span>{selected.evidence.effectiveStepCount}</span>
          </div>
          <dl className="fact-grid">
            {effects.map(([effect, count]) => (
              <div key={effect}>
                <dt>{effect}</dt>
                <dd>{count}</dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      {liveEvidence ? (
        <section aria-label="Formal live scenario evidence">
          <div className="section-heading">
            <div>
              <p className="eyebrow">验收检查</p>
              <h3>故障恢复、执行位置与结果验证</h3>
            </div>
            <span className="tag" data-phase={liveEvidence.complete ? "succeeded" : "running"}>
              {liveEvidence.complete ? "已完成验收" : "验收未完成"}
            </span>
          </div>
          <dl className="fact-grid">
            <div>
              <dt>领域</dt>
              <dd>{liveEvidence.domain ?? "—"}</dd>
            </div>
            <div>
              <dt>有效状态转换</dt>
              <dd>{liveEvidence.effectiveTransitionCount}</dd>
            </div>
            <div>
              <dt>已恢复故障</dt>
              <dd>
                {liveEvidence.recoveredFaultCount}/{liveEvidence.faultCount}
              </dd>
            </div>
            <div>
              <dt>执行层级</dt>
              <dd>{liveEvidence.tierCount}/3</dd>
            </div>
            <div>
              <dt>模型与服务能力</dt>
              <dd>{liveEvidence.providerModelCapabilityCount}/2</dd>
            </div>
            <div>
              <dt>结果验证</dt>
              <dd>{liveEvidence.verificationValid ? "已验证" : "未通过"}</dd>
            </div>
            <div>
              <dt>执行位置验证</dt>
              <dd>{liveEvidence.placementValid ? "已验证" : "未通过"}</dd>
            </div>
            <div>
              <dt>事件归档</dt>
              <dd>{shortDigest(liveEvidence.archiveDigest)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      {selected?.sourceAudit ? (
        <section aria-label="M2 source role audit">
          <div className="section-heading">
            <h3>角色与来源审计</h3>
            <span>{selected.sourceAudit.valid ? "有效" : "无效"}</span>
          </div>
          <p className="muted-copy">
            {selected.sourceAudit.active.length} active owner rows ·{" "}
            {selected.sourceAudit.inactiveNotMissing.length} inactive roles
            (not missing capabilities) · OpenClaw{" "}
            {selected.sourceAudit.openclaw}
          </p>
        </section>
      ) : null}
    </section>
  )
}
