import { useCallback, useEffect, useState, useSyncExternalStore } from "react"
import { phaseLabel } from "../../../shell/product-copy.ts"
import type { ScenarioWorkbenchRuntime } from "../runtime.ts"

/**
 * One-click launchers for the two long-horizon demonstration tasks.
 *
 * Each button admits and starts a *sealed* scenario: the formal path that
 * requires a clean starting state, injects the frozen fault schedule and
 * records recoverable, independently verifiable evidence.  The scenario
 * console already owns the transport, admission checks and polling, so this
 * panel only supplies the frozen task profiles and renders live progress.
 */

interface DemoTask {
  key: string
  scenarioId: string
  title: string
  detail: string
  seed: number
  /** Builds the run input; the nonce keeps every launch a genuinely new input. */
  buildInput: (nonce: string) => string
}

const SOFTWARE_BRIEF =
  "Add an input-digest-bound sealed delivery marker to a clean SDK workspace, "
  + "preserve a requirement-change failure-path test, execute syntax and unit "
  + "verification, and publish a checksum-bound patch, command receipts, causal "
  + "transition index, and final verifier."

const RESEARCH_QUESTION =
  "Using live primary web sources, produce a checksum-bound technical "
  + "intelligence report that explains HTTP semantics and status-code "
  + "governance, distinguishes normative claims from implementation guidance, "
  + "surfaces contradictions and uncertainty after the frozen requirement "
  + "change, and binds every material claim to exact acquired bytes."

const DEMO_TASKS: readonly DemoTask[] = [
  {
    key: "software-delivery",
    scenarioId: "live.software-delivery",
    title: "软件工程交付",
    detail: "隔离工作区完成代码发现、规划、补丁、Git、测试、故障恢复与再验证",
    seed: 60201,
    buildInput: (nonce) =>
      `${SOFTWARE_BRIEF} Delivery run marker: ${nonce}.`,
  },
  {
    key: "cross-source-research",
    scenarioId: "live.cross-source-research",
    title: "跨源技术研究",
    detail: "实时采集权威来源，做校验和绑定、断言抽取、引用核验与结构化交付",
    seed: 60202,
    buildInput: (nonce) =>
      JSON.stringify({
        question: `${RESEARCH_QUESTION} Research run marker: ${nonce}.`,
        source_urls: [
          "https://www.rfc-editor.org/rfc/rfc9110.txt",
          "https://www.iana.org/assignments/http-status-codes/http-status-codes-1.csv",
          "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status",
        ],
        requirements: [
          "Every material claim must resolve to exact acquired source bytes.",
          "At least one claim must be corroborated across independent authorities.",
          "Normative protocol facts, registry facts, and implementation guidance must remain separately attributed.",
          "Source checksums, request identities, acquisition times, uncertainty, and contradictions must be retained.",
        ],
      }),
  },
] as const

function launchNonce(): string {
  const now = Date.now().toString(36)
  const random = Math.random().toString(36).slice(2, 8)
  return `${now}-${random}`
}

function duration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "—"
  const seconds = value / 1_000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${Math.floor(seconds % 60)}s`
}

/** Launch progress, shown while the console is admitting and starting a run. */
interface LaunchProgress {
  key: string
  stage: "connecting" | "submitting"
}

export function LongRunDemoPanel({
  runtime,
}: {
  runtime: ScenarioWorkbenchRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const [busy, setBusy] = useState<LaunchProgress>()
  const [error, setError] = useState("")
  const [launched, setLaunched] = useState<Record<string, string>>({})

  useEffect(() => {
    void runtime.open().catch(() => {
      // The panel reports connection state via the snapshot; a failed initial
      // open is not fatal and the user can retry through the buttons.
    })
    return () => runtime.detach("Long-run demo panel unmounted.")
  }, [runtime])

  const launch = useCallback(
    async (task: DemoTask) => {
      setError("")
      try {
        // Bringing the console up is the slow part of a cold launch, so name
        // it in the UI: a button that merely greys out for several seconds
        // reads as "the click did nothing".
        if (runtime.getSnapshot().connection !== "online") {
          setBusy({ key: task.key, stage: "connecting" })
          await runtime.open()
        }
        setBusy({ key: task.key, stage: "submitting" })
        const run = await runtime.create({
          scenarioId: task.scenarioId,
          mode: "sealed",
          input: task.buildInput(launchNonce()),
          seed: task.seed,
          labels: { surface: "zyra-workbench", demo: "long-horizon" },
        })
        await runtime.start(run.scenario_run_id)
        setLaunched((current) => ({ ...current, [task.key]: run.scenario_run_id }))
        await runtime.select(run.scenario_run_id)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        setBusy(undefined)
      }
    },
    [runtime],
  )

  const runFor = useCallback(
    (task: DemoTask) => {
      const launchedId = launched[task.key]
      // Only show a run this panel launched.  Falling back to "the newest run
      // of this scenario type" would display a previous run's id and status,
      // which reads as though the click did nothing.
      if (!launchedId) return undefined
      const row = snapshot.rows.find(
        (item) => item.run.scenario_run_id === launchedId,
      )
      return row?.run
    },
    [launched, snapshot.rows],
  )

  const connecting = busy?.stage === "connecting"
    || (Boolean(busy) && snapshot.connection !== "online")

  return (
    <section className="long-run-demo" aria-labelledby="long-run-demo-heading">
      <div className="long-run-demo-heading">
        <h2 id="long-run-demo-heading">长程任务演示</h2>
        <span className="tag" data-phase={snapshot.connection}>
          {phaseLabel(snapshot.connection)}
          {snapshot.connectionReason ? ` · ${snapshot.connectionReason}` : ""}
        </span>
      </div>
      <p className="long-run-demo-copy">
        点击按钮即可运行一个封存（sealed）长程任务：系统会在干净状态与冻结故障计划下自主执行，
        全程零人工干预，并留下可独立复核的证据。
      </p>
      {busy ? (
        <p className="long-run-demo-progress" data-stage={busy.stage} role="status">
          {connecting
            ? `正在连接场景服务（当前：${phaseLabel(snapshot.connection)}）…首次连接通常需要几秒，请不要重复点击。`
            : "已连接，正在提交并启动运行…"}
        </p>
      ) : null}
      <div className="long-run-demo-grid">
        {DEMO_TASKS.map((task) => {
          const run = runFor(task)
          const phase = run?.phase ?? ""
          const stalled = Boolean(
            run
            && run.phase === "running"
            && !busy
            && Date.now() - Date.parse(run.updated_at) > 30_000,
          )
          return (
            <article key={task.key} className="long-run-demo-card" data-phase={phase || "idle"}>
              <strong>{task.title}</strong>
              <p>{task.detail}</p>
              {run ? (
                <dl className="long-run-demo-facts">
                  <div>
                    <dt>状态</dt>
                    <dd>{phaseLabel(run.phase)}</dd>
                  </div>
                  <div>
                    <dt>运行 ID</dt>
                    <dd>{run.scenario_run_id}</dd>
                  </div>
                </dl>
              ) : null}
              {run && run.phase === "running" ? (
                <p className="long-run-demo-hint" role="status">
                  {stalled
                    ? "已在后台运行，状态暂时没有更新——可以点「刷新状态」查看最新进度。"
                    : "正在后台执行，进度会自动刷新。"}
                </p>
              ) : null}
              <button
                type="button"
                className="button button-primary"
                // Deliberately not `disabled` while busy.  The launch pauses on
                // the connection handshake for several seconds; a greyed-out
                // button during that window is what made the panel look broken.
                // Re-clicks are harmless — `runtime.refresh`/`create` dedupe.
                aria-busy={busy?.key === task.key}
                data-launching={busy?.key === task.key ? busy?.stage : undefined}
                onClick={() => void launch(task)}
              >
                {busy?.key === task.key ? (connecting ? "正在连接…" : "正在启动…") : run ? "重新运行" : "运行此任务"}
              </button>
            </article>
          )
        })}
      </div>
      {error ? (
        <div className="plan-warning" role="alert">{error}</div>
      ) : null}
      {Object.keys(launched).length || snapshot.rows.length ? (
        <p className="long-run-demo-footnote">
          运行在后台继续，即使关闭页面也不会中断。
          {" "}
          <button
            type="button"
            className="settings-text-button"
            onClick={() => runtime.refresh("manual")}
          >
            刷新状态
          </button>
        </p>
      ) : null}
    </section>
  )
}
