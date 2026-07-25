import { useSyncExternalStore } from "react"
import type { PermissionConsoleRuntime } from "../runtime.ts"
import type { PermissionSourceSurface } from "../contracts.ts"

export function PermissionSurfaceStatus(input: {
  runtime: PermissionConsoleRuntime
  surface: Extract<
    PermissionSourceSurface,
    "browser" | "terminal" | "command" | "tool"
  >
  label?: string
}): React.JSX.Element | null {
  const snapshot = useSyncExternalStore(
    input.runtime.subscribe,
    input.runtime.getSnapshot,
    input.runtime.getSnapshot,
  )
  const requests = snapshot.requests.filter(
    (request) =>
      !request.terminal
      && (
        request.sourceSurface === input.surface
        || (
          input.surface === "tool"
          && !["browser", "terminal", "command"].includes(
            request.sourceSurface,
          )
        )
      ),
  )
  if (!requests.length) return null
  return (
    <aside
      className="permission-surface-status"
      aria-label={`${input.label ?? input.surface} pending permissions`}
      data-permission-surface={input.surface}
    >
      <strong>
        {requests.length} {input.label ?? input.surface} action
        {requests.length === 1 ? "" : "s"} awaiting permission
      </strong>
      <ul>
        {requests.map((request) => (
          <li
            key={request.requestId}
            {...surfaceData(input.surface, request.requestId)}
          >
            <button
              type="button"
              onClick={() => focusPermission(request.requestId)}
            >
              <span>{request.toolName}</span>
              <span className={`tag permission-risk-${request.riskLevel}`}>
                {request.riskLevel}
              </span>
              <span className="mono">
                {short(request.argumentsDigest)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  )
}

function surfaceData(
  surface: "browser" | "terminal" | "command" | "tool",
  requestId: string,
): Record<string, string> {
  if (surface === "browser") {
    return {
      "data-browser-permission-request-id": requestId,
      "data-browser-step": "permission",
    }
  }
  if (surface === "terminal") {
    return {
      "data-terminal-permission-request-id": requestId,
      "data-terminal-entry": "permission",
    }
  }
  if (surface === "command") {
    return {
      "data-command-permission-id": requestId,
      "data-control-request-id": requestId,
    }
  }
  return { "data-tool-permission-request-id": requestId }
}

function focusPermission(requestId: string): void {
  if (typeof document === "undefined") return
  const escaped = escapeAttribute(requestId)
  const target = document.querySelector<HTMLElement>(
    `[data-permission-detail][data-permission-request-id="${escaped}"],`
      + `[data-permission-request-id="${escaped}"]`,
  )
  target?.scrollIntoView({ behavior: "smooth", block: "center" })
  target?.focus({ preventScroll: true })
}

function short(value: string): string {
  return value.length > 14
    ? `${value.slice(0, 8)}…${value.slice(-4)}`
    : value
}

function escapeAttribute(value: string): string {
  return value.replace(/["\\\n\r\f]/g, (character) => {
    return `\\${character.charCodeAt(0).toString(16)} `
  })
}
