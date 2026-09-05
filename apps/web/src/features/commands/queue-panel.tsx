import { useSyncExternalStore } from "react"
import type { CommandQueueItem } from "../../../../../packages/commands/src/index.ts"
import type { CommandSurfaceRuntime } from "./runtime.ts"
import type { PermissionConsoleRuntime } from "../permissions/index.ts"
import {
  PermissionSurfaceStatus,
  rejectPermissionSealedManualAction,
} from "../permissions/index.ts"

function QueueRow(input: {
  item: CommandQueueItem
  runtime: CommandSurfaceRuntime
  permissionRuntime?: PermissionConsoleRuntime
  actorId: string
}): React.JSX.Element {
  return (
    <li
      data-command-queue-id={input.item.queueId}
      data-command-phase={input.item.phase}
      data-command-source={input.item.source}
    >
      <span>{input.item.name ?? input.item.text ?? "排队命令"}</span>
      <span className="tag" data-phase={input.item.phase}>{{ queued: "排队中", reserved: "准备中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消", expired: "已过期" }[input.item.phase]}</span>
      <span title="执行优先级">{{ now: "优先", next: "正常", later: "稍后" }[input.item.priority] ?? input.item.priority}</span>
      {input.item.error ? <span role="alert">{input.item.error}</span> : null}
      {input.item.cancellable ? (
        <button
          className="button button-danger"
          type="button"
          onClick={() => {
            void input.runtime.cancelQueueItem(
              input.item,
              "Cancelled from the command queue preview.",
            )
          }}
        >
          取消
        </button>
      ) : null}
      {input.item.retryable ? (
        <button
          className="button button-secondary"
          type="button"
          onClick={() => {
            void (async () => {
              if (input.permissionRuntime) {
                const rejected = await rejectPermissionSealedManualAction({
                  runtime: input.permissionRuntime,
                  action: "retry",
                  actorId: input.actorId,
                  requestId: input.item.requestId ?? input.item.queueId,
                  reason:
                    "A manual command retry cannot advance a sealed run.",
                })
                if (rejected) return
              }
              await input.runtime.retryQueueItem(input.item)
            })()
          }}
        >
          重试
        </button>
      ) : null}
    </li>
  )
}

export function CommandQueuePanel(input: {
  runtime: CommandSurfaceRuntime
  permissionRuntime?: PermissionConsoleRuntime
  actorId?: string
}): React.JSX.Element | null {
  const snapshot = useSyncExternalStore(
    input.runtime.subscribe,
    input.runtime.getSnapshot,
    input.runtime.getSnapshot,
  )
  const queue = snapshot.queue
  if (!queue?.items.length && !input.permissionRuntime) return null
  return (
    <section
      aria-label="命令队列"
      className="command-queue-panel"
      data-command-queue-owner="PromptQueueRuntime+CanonicalProjectionStore"
      data-command-queue-restored={queue?.restored ? "true" : "false"}
    >
      {input.permissionRuntime ? (
        <PermissionSurfaceStatus
          runtime={input.permissionRuntime}
          surface="command"
          label="command"
        />
      ) : null}
      {queue?.items.length ? (
        <>
      <header>
        <strong>命令队列</strong>
        <span>{queue.pending.length} 条待处理</span>
        <button
          type="button"
          onClick={() => {
            void input.runtime.restoreQueue({ includeTerminal: true })
          }}
        >
          刷新
        </button>
      </header>
      <ol>
        {queue.items.map((item) => (
          <QueueRow
            key={item.queueId}
            item={item}
            runtime={input.runtime}
            permissionRuntime={input.permissionRuntime}
            actorId={input.actorId ?? "zyra-web-operator"}
          />
        ))}
      </ol>
        </>
      ) : <p className="muted-copy">暂无命令记录。</p>}
    </section>
  )
}
