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
        <h4 id="permission-timeline-heading">Permission causal timeline</h4>
        <span>{visible.length} event(s)</span>
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
                  <strong>{entry.title}</strong>
                  <span className="tag tag-muted">{entry.phase}</span>
                  {entry.canonical ? (
                    <span className="tag">canonical</span>
                  ) : null}
                </div>
                <p>{entry.detail}</p>
                <PermissionTimelineRefs refs={entry.refs} />
                <time dateTime={entry.createdAt}>
                  {formatTimestamp(entry.createdAt)}
                </time>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted-copy">
          No matching permission lifecycle events are projected.
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
          <dt>{name.replaceAll("_", " ")}</dt>
          <dd className="mono">{short(value)}</dd>
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
