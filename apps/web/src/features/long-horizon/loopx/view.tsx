import { recordLabel } from "../../evidence/record-copy.ts"
import { useEffect, useMemo, useState } from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import type {
  LoopXControlAction,
  LoopXControlState,
} from "../../../api/loopx-api.ts"
import { loopxWorkbenchView } from "./contracts.ts"
import { LoopXActionCoordinator } from "./runtime.ts"

function metric(value: unknown): string {
  return value === undefined || value === null || value === ""
    ? "—"
    : String(value)
}

export function LoopXWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const coordinator = useMemo(() => new LoopXActionCoordinator(), [task.taskId])
  const [state, setState] = useState<LoopXControlState | null>(null)
  const [failure, setFailure] = useState("")
  const [busy, setBusy] = useState(false)

  const refresh = async (signal?: AbortSignal) => {
    try {
      setState(await runtime.api.loopx.state(task.taskId, { signal }))
      setFailure("")
    } catch (error) {
      if (!signal?.aborted) {
        setFailure(error instanceof Error ? error.message : String(error))
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [runtime.api.loopx, task.taskId])

  const command = async (
    action: LoopXControlAction,
    argumentsValue: Readonly<Record<string, unknown>> = {},
  ) => {
    if (busy) return
    setBusy(true)
    setFailure("")
    try {
      const result = await coordinator.execute(
        runtime.api.loopx,
        {
          taskId: task.taskId,
          runId: task.runId,
          action,
          arguments: argumentsValue,
        },
        state?.sync.cursor ?? 0,
      )
      setState(result.state)
      if (!result.ok) {
        setFailure(
          String(result.receipt.degraded_reason || "LoopX command conflicted."),
        )
      }
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  if (!state) {
    return (
      <section className="detail-section" aria-labelledby="loopx-heading">
        <div className="section-heading">
          <h3 id="loopx-heading">长程任务控制</h3>
          <span>{failure ? "连接异常" : "加载中"}</span>
        </div>
        <p className={failure ? "plan-warning" : "muted-copy"}>
          {failure || "正在读取长程目标与同步状态…"}
        </p>
      </section>
    )
  }

  const view = loopxWorkbenchView(state)
  return (
    <section
      className="detail-section"
      aria-labelledby="loopx-heading"
      data-loopx-lifecycle={view.lifecycle}
    >
      <div className="section-heading">
        <h3 id="loopx-heading">长程任务控制</h3>
        <span className={`tag ${view.lifecycle === "degraded" ? "tag-danger" : ""}`}>
          {recordLabel(view.lifecycle)}
        </span>
      </div>

      {failure || state.error ? (
        <div className="plan-warning" role="status">
          {failure || metric(state.error?.message)}
        </div>
      ) : null}

      <dl className="fact-grid">
        <div>
          <dt>运行时</dt>
          <dd>v{view.runtimeVersion} · {view.runtimeSource}</dd>
        </div>
        <div>
          <dt>长程目标</dt>
          <dd>{view.goalId}</dd>
        </div>
        <div>
          <dt>继续执行</dt>
          <dd>{view.continuationAllowed ? "可继续" : "受限"}</dd>
        </div>
        <div>
          <dt>长程任务配额</dt>
          <dd>
            {metric(view.quota.spent_slots)}/{metric(view.quota.limit_slots)}
          </dd>
        </div>
        <div>
          <dt>同步状态</dt>
          <dd>
            {view.sync.pending} 条待同步 · {view.sync.acked} 条已确认 ·{" "}
            {view.sync.dead_letter} 条同步失败
          </dd>
        </div>
        <div>
          <dt>关联任务</dt>
          <dd>{view.canonicalTaskStatus}</dd>
        </div>
        <div>
          <dt>执行租约</dt>
          <dd>{view.workerLeaseStatus}</dd>
        </div>
        <div>
          <dt>任务执行预算</dt>
          <dd>{metric(view.executionBudget.tool_calls)}</dd>
        </div>
        <div>
          <dt>执行归属</dt>
          <dd>任务关联与执行租约分别记录</dd>
        </div>
      </dl>

      <div className="task-actions">
        {!state.connected ? (
          <button
            className="button button-primary"
            type="button"
            disabled={busy}
            onClick={() => void command("connect", {
              objective: task.userGoal,
              todo_title: task.userGoal,
              limit_slots: 8,
            })}
          >
            连接目标
          </button>
        ) : (
          <>
            <button
              className="button button-secondary"
              type="button"
              disabled={busy}
              onClick={() => void command("interaction_submit", {
                input_ref: `ui:${task.taskId}:${view.sync.cursor}`,
                continuation_hint: "Continue the next bounded verified todo",
              })}
            >
              提交后续指令
            </button>
            <button
              className="button button-secondary"
              type="button"
              disabled={busy || view.sync.dead_letter === 0}
              onClick={() => void command("sync_retry")}
            >
              重新同步
            </button>
            <button
              className="button button-danger"
              type="button"
              disabled={busy}
              onClick={() => void command("disconnect")}
            >
              断开关联
            </button>
          </>
        )}
        <button
          className="button button-secondary"
          type="button"
          disabled={busy}
          onClick={() => void refresh()}
        >
          刷新
        </button>
      </div>

      {view.todos.length ? (
        <ol className="plan-list" aria-label="长程执行待办">
          {view.todos.map((todo) => (
            <li className="plan-node" key={todo.todo_id}>
              <span className="status-marker status-running" aria-hidden="true" />
              <div>
                <div className="plan-node-heading">
                  <strong>{todo.title || todo.text || todo.todo_id}</strong>
                  <span className="tag tag-muted">{todo.priority || "P2"}</span>
                  {todo.claimed_by ? <span className="tag">{todo.claimed_by}</span> : null}
                </div>
                <p>{todo.todo_id}</p>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => void command(
                    todo.claimed_by ? "release" : "claim",
                    {
                      todo_id: todo.todo_id,
                      claimant: todo.claimed_by || "zyra-web-controller",
                    },
                  )}
                >
                  {todo.claimed_by ? "Release claim" : "Claim todo"}
                </button>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted-copy">暂无长程任务待办。</p>
      )}
    </section>
  )
}
