import { useMemo } from "react"
import { formatDuration } from "../../../shell/task-metrics.ts"
import type { StreamEntry, StreamEntryKind } from "../projection.ts"

/**
 * Renders an execution stream: one row per thing that happened.
 *
 * Row type is carried by `data-kind` and coloured entirely through the shared
 * `--state-*` tri-state variables, so a new kind is one entry in the stylesheet
 * rather than a bespoke colour per element.
 */

/** Glyph per kind. Text, not an icon font, so it renders identically anywhere. */
const KIND_GLYPH: Record<StreamEntryKind, string> = {
  thinking: "◆",
  message: "▸",
  tool: "▸",
  artifact: "◫",
  recovery: "▲",
  notice: "·",
}

const KIND_LABEL: Record<StreamEntryKind, string> = {
  thinking: "思考",
  message: "回答",
  tool: "工具",
  artifact: "产物",
  recovery: "恢复",
  notice: "记录",
}

export function ExecutionStream({
  entries,
  goal,
  status,
  liveText,
}: {
  entries: readonly StreamEntry[]
  goal: string
  status: string
  /** Transient assistant text for the turn still in flight, if any. */
  liveText?: string
}) {
  const rows = useMemo(() => entries, [entries])

  return (
    <div className="execution-stream" role="log" aria-label="执行流" data-task-status={status}>
      <article className="stream-entry" data-kind="goal">
        <span className="stream-entry-glyph" aria-hidden="true">◎</span>
        <div className="stream-entry-body">
          <span className="stream-entry-title">{goal}</span>
        </div>
      </article>

      {rows.map((entry) => (
        <article
          key={entry.key}
          className="stream-entry"
          data-kind={entry.kind}
          data-status={entry.status}
          data-node={entry.nodeId}
        >
          <span className="stream-entry-glyph" aria-hidden="true">
            {KIND_GLYPH[entry.kind]}
          </span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-kind">{KIND_LABEL[entry.kind]}</span>
              <span className="stream-entry-title">{entry.title}</span>
              {entry.durationMs !== undefined ? (
                <span className="stream-entry-duration">{formatDuration(entry.durationMs)}</span>
              ) : null}
              {entry.eventCount > 1 ? (
                <span className="stream-entry-count">{entry.eventCount} 条记录</span>
              ) : null}
            </div>
            {entry.detail ? (
              <p className="stream-entry-detail">{entry.detail}</p>
            ) : null}
          </div>
        </article>
      ))}

      {liveText ? (
        <article className="stream-entry" data-kind="message" data-status="running">
          <span className="stream-entry-glyph" aria-hidden="true">▸</span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-kind">回答</span>
              <span className="stream-entry-title">正在生成…</span>
            </div>
            <p className="stream-entry-detail stream-entry-live">{liveText}</p>
          </div>
        </article>
      ) : null}

      {!rows.length && !liveText ? (
        <p className="stream-empty" role="status">等待执行输出…</p>
      ) : null}
    </div>
  )
}
