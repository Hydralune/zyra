import { useCallback, useState, useSyncExternalStore } from "react"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"

/**
 * One-click launcher for a real long-horizon benchmark task.
 *
 * Unlike the sealed scenario panel, this starts an ordinary task against the
 * bound benchmark container: the agent reads a real repository, edits real
 * production code and runs the project's own test suite.  It is the run the
 * execution stream renders.
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

const DEMO_GOAL = [
  "Work on the repository in the supplied benchmark container. A Flask",
  "Blueprint with an empty name is accepted but behaves incorrectly.",
  "Diagnose the issue, implement the smallest correct production fix, and add",
  "or adapt a focused regression test. Run relevant tests. Preserve unrelated",
  "behavior and deliver the code change in the repository; do not only",
  "describe a patch.",
].join(" ")

/** Launch progress, named so a multi-second wait does not read as a dead click. */
type LaunchStage = "creating" | "starting"

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
  const [stage, setStage] = useState<LaunchStage>()
  const [error, setError] = useState("")

  // `bound` comes from the API's health report.  An older API that predates
  // the report normalizes to `bound: false`, which disables the button rather
  // than promising a launch the backend would refuse.
  const benchmark = snapshot.runtime.health?.benchmark
  const bound = benchmark?.bound === true
  const transportEnabled = snapshot.transportEnabled
  const busy = stage !== undefined
  const disabled = !bound || !transportEnabled

  const launch = useCallback(async () => {
    setError("")
    setStage("creating")
    try {
      // The idempotency keys are derived from the goal and a launch nonce, not
      // from a shared record id, because this panel does not own a submission
      // record.  Re-clicking while busy is prevented by `busy`; a genuine
      // retry after a failure is a new launch and must get fresh keys.
      const nonce = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
      const created = await runtime.api.lifecycle.create({
        goal: `${DEMO_GOAL} Delivery run marker: ${nonce}.`,
        autoRun: false,
        // A demo is meant to be reviewed and replayed, so this task opts into
        // retaining its bounded presentation text.  Every other task keeps the
        // low-entropy default.
        persistPresentationText: true,
        idempotencyKey: `long-horizon:${nonce}:create`,
      })
      const task = created.mutation.task
      runtime.workbench.applyMutation(task)
      // Navigate before the blocking run request so the execution stream binds
      // and the stop control is reachable while the task is still executing.
      runtime.router.openTask(task.taskId, { focus: "task-detail" })
      setStage("starting")
      const settled = await runtime.api.lifecycle.resume({
        taskId: task.taskId,
        runId: task.runId,
        idempotencyKey: `long-horizon:${nonce}:run`,
      })
      runtime.workbench.applyMutation(settled.mutation.task)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setStage(undefined)
    }
  }, [runtime])

  return (
    <section className="long-horizon-launch" aria-labelledby="long-horizon-launch-heading">
      <div className="long-horizon-launch-heading">
        <h2 id="long-horizon-launch-heading">运行真实长程任务</h2>
        <span className="tag" data-phase={bound ? "ready" : "idle"}>
          {bound ? "基准容器已绑定" : "未绑定基准容器"}
        </span>
      </div>
      <p className="long-horizon-launch-copy">
        在真实代码仓库中执行：读取工程、修改生产代码、运行项目自带测试，并在轮次与
        token 上限内自主推进。运行中可随时停止，之后可从断点继续。
      </p>
      {!bound ? (
        <p className="long-horizon-launch-hint" role="status">
          当前 API 没有绑定基准容器，启动会被拒绝。请用
          {" "}<code>python scripts/dev_up_swebench.py</code>{" "}
          启动工作台，再回到这里运行。
        </p>
      ) : null}
      <button
        type="button"
        className="button button-primary"
        // Deliberately not `disabled` while busy: the launch pauses for seconds
        // on two network calls, and a greyed-out button during that window is
        // what made the sealed panel look broken.  Re-clicks are guarded by
        // `busy`.
        aria-busy={busy}
        data-launching={stage}
        disabled={disabled}
        onClick={() => {
          if (busy) return
          void launch()
        }}
      >
        {busy
          ? stage === "creating"
            ? "正在创建任务…"
            : "正在启动执行…"
          : "运行演示任务"}
      </button>
      {busy ? (
        <p className="long-horizon-launch-progress" role="status">
          {stage === "creating"
            ? "已提交目标，正在创建任务并准备执行资源…"
            : "任务已创建，正在启动执行；可以打开任务页查看实时执行流。"}
        </p>
      ) : null}
      {error ? (
        <div className="plan-warning" role="alert">{error}</div>
      ) : null}
    </section>
  )
}
