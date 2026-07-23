import { useSyncExternalStore } from "react"
import type { WorkbenchRuntime } from "../../app/runtime.ts"

export function NotificationTray({ runtime }: { runtime: WorkbenchRuntime }) {
  const snapshot = useSyncExternalStore(
    runtime.notifications.subscribe,
    runtime.notifications.getSnapshot,
    runtime.notifications.getSnapshot,
  )
  if (!snapshot.notifications.length) return null
  return (
    <aside className="notification-tray" aria-label="Workbench notifications">
      {snapshot.notifications.slice(0, 4).map((notification) => (
        <article
          key={notification.id}
          className="notification"
          data-tone={notification.tone}
          role={notification.tone === "error" ? "alert" : "status"}
          onMouseEnter={() => runtime.notifications.acknowledge(notification.id)}
        >
          <div>
            <strong>{notification.title}</strong>
            <p>{notification.message}</p>
          </div>
          <button
            type="button"
            aria-label={`Dismiss ${notification.title}`}
            onClick={() => runtime.notifications.dismiss(notification.id)}
          >
            ×
          </button>
        </article>
      ))}
      {snapshot.notifications.length > 4 ? (
        <button type="button" onClick={() => runtime.notifications.clear()}>
          Clear {snapshot.notifications.length - 4} more
        </button>
      ) : null}
    </aside>
  )
}
