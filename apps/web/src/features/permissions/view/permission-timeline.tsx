import { recordLabel } from "../../evidence/record-copy.ts"
import type { PermissionTimelineEntry } from "../contracts.ts"

export function PermissionTimeline(input: {
  entries: readonly PermissionTimelineEntry[]
  selectedRequestId?: string
  maximum?: number
}): React.JSX.Element {
  const maximum = Math.min(
    500,
    Math.max(8, Math.floor(input.maximum ?? 80)),
  )
  const visible = input.entries
    .filter(
      (entry) =>
        !input.selectedRequestId
        || !entry.requestId
        || entry.requestId === input.selectedRequestId,
    )
    .slice(-maximum)
    .reverse()
  return (
    <section
      className="permission-timeline"
      aria-labelledby="permission-timeline-heading"
      data-permission-timeline
    >
      <div className="section-heading">
        <h4 id="permission-timeline-heading">权限记录</h4>
        <span>{visible.length} 条记录</span>
      </div>
      {visible.length ? (
        <ol className="permission-timeline-list">
          {visible.map((entry) => (
            <li
              key={entry.entryId}
              data-permission-id={entry.requestId}
              data-event-request-id={entry.requestId}
              data-permission-event-kind={entry.kind}
              data-permission-event-phase={entry.phase}
              data-permission-event-canonical={
                entry.canonical ? "true" : "false"
              }
              className={`permission-timeline-entry permission-timeline-${entry.severity}`}
              tabIndex={-1}
            >
              <span
                className="permission-timeline-marker"
                aria-hidden="true"
              />
              <div>
                <div className="permission-timeline-title">
                  <strong>{entry.phase === "allow" ? "允许执行" : entry.phase === "deny" ? "拒绝执行" : entry.title}</strong>
                  <span className={`tag tag-${entry.phase === "allow" ? "success" : entry.phase === "deny" ? "danger" : "muted"}`}>{recordLabel(entry.phase)}</span>
                  {entry.canonical ? (
                    <span className="tag">后端记录</span>
                  ) : null}
                </div>
                <details><summary>查看说明与来源</summary><p>{entry.title}</p><p>{entry.detail}</p>
                  <PermissionTimelineRefs refs={entry.refs} />
                </details>
                <time dateTime={entry.createdAt}>
                  {formatTimestamp(entry.createdAt)}
                </time>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted-copy">
          暂无对应权限记录。
        </p>
      )}
    </section>
  )
}

function PermissionTimelineRefs(input: {
  refs: Readonly<Record<string, string>>
}): React.JSX.Element | null {
  const refs = Object.entries(input.refs).slice(0, 5)
  if (!refs.length) return null
  return (
    <dl className="permission-timeline-refs">
      {refs.map(([name, value]) => (
        <div key={name}>
          <dt>{{ decision_id: "审批编号", tool_call_id: "工具调用", request_id: "请求编号", rule_id: "规则编号" }[name] ?? name.replaceAll("_", " ")}</dt>
          <dd className="mono" title={value}>{short(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function short(value: string): string {
  return value.length > 32
    ? `${value.slice(0, 18)}…${value.slice(-10)}`
    : value
}

function formatTimestamp(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(timestamp)
}
