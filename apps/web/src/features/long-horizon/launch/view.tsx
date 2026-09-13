import { useCallback, useState, useSyncExternalStore } from "react"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"

/**
 * One-click launchers for real long-horizon benchmark tasks.
 *
 * Unlike the sealed scenario console, these start an ordinary task against the
 * bound benchmark container: the agent reads a real repository, edits real
 * production code and runs the project's own test suite.  It is the run the
 * execution stream renders.
 *
 * A *profile* names one such task.  They deliberately share a container and a
 * repository and differ only in the method they demand, because that is what
 * the workbench can actually demonstrate today: the benchmark container is
 * resolved per dispatch from the API process's environment, so one API owns
 * exactly one container.  Offering two repositories would need a per-task
 * container binding on the backend, which does not exist.
 *
 * Two properties of the launch shape this component:
 *
 * * The API binds its benchmark container at process start.  With none bound,
 *   `/tasks/{id}/run` fails closed, so the button reports "unavailable" up
 *   front instead of looking broken when clicked.
 * * A launch is two calls.  `create` returns the task immediately; `run` then
 *   blocks for the whole execution.  Routing to the task *between* them is what
 *   lets the stream attach and the stop control appear before the run ends.
 */

const DELIVERY_TAIL =
  "Preserve unrelated behavior and deliver the code change in the repository; "
  + "do not only describe a patch."

export interface LongHorizonTaskProfile {
  key: string
  /** Card heading. */
  title: string
  /** One line on what this run demands of the agent. */
  detail: string
  /**
   * Builds the goal text.
   *
   * The nonce makes every launch a genuinely new input: two runs of the same
   * profile must not be deduplicated into one by the lifecycle's idempotency
   * keys or by a provider's prompt cache.
   */
  goal: (nonce: string) => string
}

const FLASK_BRIEF =
  "Work on the repository in the supplied benchmark container. A Flask "
  + "Blueprint with an empty name is accepted but behaves incorrectly."

export const LONG_HORIZON_TASK_PROFILES: readonly LongHorizonTaskProfile[] = [
  {
    key: "fix-first",
    title: "缺陷修复",
    detail: "定位缺陷、改动生产代码、补上回归测试，并跑通项目自带测试",
    goal: (nonce) =>
      `${FLASK_BRIEF} Diagnose the issue, implement the smallest correct `
      + `production fix, and add or adapt a focused regression test. Run `
      + `relevant tests. ${DELIVERY_TAIL} Delivery run marker: ${nonce}.`,
  },
  {
    key: "test-first",
    title: "测试先行",
    detail: "先写一个能复现缺陷、确实失败的测试，再改代码让它通过",
    goal: (nonce) =>
      `${FLASK_BRIEF} Work test-first: reproduce the defect with a focused `
      + `failing test before changing any production code, confirm that test `
      + `fails for the stated reason, then implement the smallest correct `
      + `production fix that makes it pass. Re-run the relevant tests `
      + `afterwards. ${DELIVERY_TAIL} Delivery run marker: ${nonce}.`,
  },
] as const

/** The profile the in-task launcher starts, where a single button is wanted. */
export const DEFAULT_LONG_HORIZON_PROFILE = LONG_HORIZON_TASK_PROFILES[0]!

/** Launch progress, named so a multi-second wait does not read as a dead click. */
export type LaunchStage = "creating" | "starting"

function launchNonce(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/**
 * Start one real long-horizon run against the bound benchmark container.
 *
 * Extracted so the home panel and the task page share one implementation: the
 * ordering below is load-bearing (create, navigate, then run), and a second
 * copy would drift from it.
 */
export async function launchLongHorizonRun(
  runtime: WorkbenchRuntime,
  onStage: (stage: LaunchStage | undefined) => void,
  profile: LongHorizonTaskProfile = DEFAULT_LONG_HORIZON_PROFILE,
): Promise<void> {
  onStage("creating")
  try {
    // The idempotency keys are derived from a launch nonce, not from a shared
    // record id, because the caller owns no submission record.  A genuine retry
    // after a failure is a new launch and must get fresh keys.
    const nonce = launchNonce()
    const created = await runtime.api.lifecycle.create({
      goal: profile.goal(nonce),
      autoRun: false,
      // A demo is meant to be reviewed and replayed, so this task opts into
      // retaining its bounded presentation text.  Every other task keeps the
      // low-entropy default.
      persistPresentationText: true,
      idempotencyKey: `long-horizon:${profile.key}:${nonce}:create`,
    })
    const task = created.mutation.task
    runtime.workbench.applyMutation(task)
    // Navigate before the blocking run request so the execution stream binds
    // and the stop control is reachable while the task is still executing.
    runtime.router.openTask(task.taskId, { focus: "task-detail" })
    onStage("starting")
    const settled = await runtime.api.lifecycle.resume({
      taskId: task.taskId,
      runId: task.runId,
      idempotencyKey: `long-horizon:${profile.key}:${nonce}:run`,
    })
    runtime.workbench.applyMutation(settled.mutation.task)
  } finally {
    onStage(undefined)
  }
}

export function LongHorizonLaunchPanel({
  runtime,
}: {
  runtime: WorkbenchRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.workbench.subscribe,
    runtime.workbench.getSnapshot,
    runtime.workbench.getSnapshot,
  )
  const [busy, setBusy] = useState<{ key: string; stage: LaunchStage }>()
  const [error, setError] = useState("")

  // `bound` comes from the API's health report.  An older API that predates
  // the report normalizes to `bound: false`, which disables the button rather
  // than promising a launch the backend would refuse.
  //
  // "Not yet known" is a third state, not the same as "unbound": before health
  // answers, saying the container is missing is a false statement about the
  // backend, and it is the state a reader sees first on every page load.
  const benchmark = snapshot.runtime.health?.benchmark
  const bound = benchmark?.bound === true
  const unknown = benchmark === undefined
  const transportEnabled = snapshot.transportEnabled
  const disabled = unknown || !bound || !transportEnabled

  const launch = useCallback(
    async (profile: LongHorizonTaskProfile) => {
      setError("")
      try {
        await launchLongHorizonRun(
          runtime,
          (stage) =>
            setBusy(stage ? { key: profile.key, stage } : undefined),
          profile,
        )
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    },
    [runtime],
  )

  return (
    <section className="long-horizon-launch" aria-labelledby="long-horizon-launch-heading">
      <div className="long-horizon-launch-heading">
        <h2 id="long-horizon-launch-heading">运行真实长程任务</h2>
        <span className="tag" data-phase={unknown ? "idle" : bound ? "ready" : "idle"}>
          {unknown ? "正在确认基准容器" : bound ? "基准容器已绑定" : "未绑定基准容器"}
        </span>
      </div>
      <p className="long-horizon-launch-copy">
        在真实代码仓库中执行：读取工程、修改生产代码、运行项目自带测试，并在轮次与
        token 上限内自主推进。运行中可随时停止，之后可从断点继续。
      </p>
      {unknown ? (
        <p className="long-horizon-launch-hint" role="status">
          正在读取运行时状态，确认基准容器是否已绑定…
        </p>
      ) : null}
      {!unknown && !bound ? (
        <p className="long-horizon-launch-hint" role="status">
          当前 API 没有绑定基准容器，启动会被拒绝。请用
          {" "}<code>python scripts/dev_up_swebench.py</code>{" "}
          启动工作台，再回到这里运行。
        </p>
      ) : null}
      <div className="long-horizon-launch-grid">
        {LONG_HORIZON_TASK_PROFILES.map((profile) => {
          const stage = busy?.key === profile.key ? busy.stage : undefined
          return (
            <article
              key={profile.key}
              className="long-horizon-launch-card"
              data-launching={stage}
            >
              <strong>{profile.title}</strong>
              <p>{profile.detail}</p>
              <button
                type="button"
                className="button button-primary"
                // Deliberately not `disabled` while busy: the launch pauses for
                // seconds on two network calls, and a greyed-out button during
                // that window is what made the sealed panel look broken.
                // Re-clicks are guarded by the stage state.
                aria-busy={stage !== undefined}
                data-launching={stage}
                disabled={disabled}
                onClick={() => {
                  if (busy) return
                  void launch(profile)
                }}
              >
                {stage === "creating"
                  ? "正在创建任务…"
                  : stage === "starting"
                    ? "正在启动执行…"
                    : "运行此任务"}
              </button>
              {stage ? (
                <p className="long-horizon-launch-progress" role="status">
                  {stage === "creating"
                    ? "已提交目标，正在创建任务并准备执行资源…"
                    : "任务已创建，正在启动执行；可以打开任务页查看实时执行流。"}
                </p>
              ) : null}
            </article>
          )
        })}
      </div>
      {error ? (
        <div className="plan-warning" role="alert">{error}</div>
      ) : null}
    </section>
  )
}
