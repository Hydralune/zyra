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
      <span>{input.item.name ?? input.item.text ?? "Queued command"}</span>
      <span>{input.item.phase}</span>
      <span>{input.item.priority}</span>
      {input.item.error ? <span role="alert">{input.item.error}</span> : null}
      {input.item.cancellable ? (
        <button
          type="button"
          onClick={() => {
            void input.runtime.cancelQueueItem(
              input.item,
              "Cancelled from the command queue preview.",
            )
          }}
        >
          Cancel
        </button>
      ) : null}
      {input.item.retryable ? (
        <button
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
          Retry
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
      aria-label="Backend command queue"
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
      {queue ? (
        <>
      <header>
        <strong>Command queue</strong>
        <span>{queue.pending.length} pending</span>
        <button
          type="button"
          onClick={() => {
            void input.runtime.restoreQueue({ includeTerminal: true })
          }}
        >
          Refresh
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
      ) : null}
    </section>
  )
}
