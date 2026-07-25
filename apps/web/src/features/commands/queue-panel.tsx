import { useSyncExternalStore } from "react"
import type { CommandQueueItem } from "../../../../../packages/commands/src/index.ts"
import type { CommandSurfaceRuntime } from "./runtime.ts"

function QueueRow(input: {
  item: CommandQueueItem
  runtime: CommandSurfaceRuntime
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
            void input.runtime.retryQueueItem(input.item)
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
}): React.JSX.Element | null {
  const snapshot = useSyncExternalStore(
    input.runtime.subscribe,
    input.runtime.getSnapshot,
    input.runtime.getSnapshot,
  )
  const queue = snapshot.queue
  if (!queue?.items.length) return null
  return (
    <section
      aria-label="Backend command queue"
      data-command-queue-owner="PromptQueueRuntime+CanonicalProjectionStore"
      data-command-queue-restored={queue.restored ? "true" : "false"}
    >
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
          />
        ))}
      </ol>
    </section>
  )
}
