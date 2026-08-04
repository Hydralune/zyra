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
  tone = "reconnecting",
  label,
  retryLabel = "立即重试",
}: {
  failure?: RequestFailure
  onRetry: () => void
  tone?: "offline" | "reconnecting" | "error"
  label?: string
  retryLabel?: string
}) {
  const defaultLabel =
    tone === "offline"
      ? "浏览器已离线，Zyra 暂停了自动更新"
      : tone === "error"
        ? "运行时不可用"
        : "正在重新连接 Zyra"
  return (
    <div className="connection-banner" data-tone={tone} role="status" aria-live="polite">
      <span className="connection-dot" aria-hidden="true" />
      <span>
        {label ?? defaultLabel}
        {failure?.attempt ? ` · 第 ${failure.attempt} 次尝试` : ""}
        {tone === "error" && failure?.message ? ` · ${failure.message}` : ""}
      </span>
      <button type="button" onClick={onRetry}>{retryLabel}</button>
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
