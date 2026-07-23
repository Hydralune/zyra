import type { ReactNode } from "react"
import type { RequestFailure, RequestPhase } from "../../shell/workbench-controller.ts"

export function LoadingState({
  title,
  detail,
}: {
  title: string
  detail?: string
}) {
  return (
    <div className="state-panel state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        {detail ? <p>{detail}</p> : null}
      </div>
    </div>
  )
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string
  detail: string
  action?: ReactNode
}) {
  return (
    <div className="state-panel state-empty">
      <div className="state-icon" aria-hidden="true">◇</div>
      <strong>{title}</strong>
      <p>{detail}</p>
      {action}
    </div>
  )
}

export function ErrorState({
  title,
  failure,
  onRetry,
  retryLabel = "Try again",
}: {
  title: string
  failure?: RequestFailure
  onRetry?: () => void
  retryLabel?: string
}) {
  return (
    <div className="state-panel state-error" role="alert">
      <div className="state-icon" aria-hidden="true">!</div>
      <strong>{title}</strong>
      <p>{failure?.message ?? "The request could not be completed."}</p>
      {failure?.retryable && onRetry ? (
        <button className="button button-secondary" type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  )
}

export function ReconnectingState({
  failure,
  onRetry,
}: {
  failure?: RequestFailure
  onRetry: () => void
}) {
  return (
    <div className="connection-banner" role="status" aria-live="polite">
      <span className="connection-dot" aria-hidden="true" />
      <span>
        Reconnecting to Zyra
        {failure?.attempt ? ` · attempt ${failure.attempt}` : ""}
      </span>
      <button type="button" onClick={onRetry}>Retry now</button>
    </div>
  )
}

export function PhaseRegion({
  phase,
  loading,
  empty,
  error,
  children,
}: {
  phase: RequestPhase | "not-found"
  loading: ReactNode
  empty?: ReactNode
  error: ReactNode
  children: ReactNode
}) {
  if (phase === "loading" || phase === "reconnecting") return loading
  if (phase === "error" || phase === "not-found") return error
  if (phase === "empty") return empty ?? null
  return children
}
