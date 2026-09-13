import { useMemo } from "react"
import { formatDuration } from "../../../shell/task-metrics.ts"
import type { StreamEntry, StreamEntryKind } from "../projection.ts"

/**
 * Renders an execution stream: one row per thing that happened.
 *
 * Row type is carried by `data-kind` and coloured entirely through the shared
 * `--state-*` tri-state variables, so a new kind is one entry in the stylesheet
 * rather than a bespoke colour per element.
 *
 * Icons are inline SVG rather than text glyphs: the previous `◫ ◎ ◆ ▸ ▲ ·`
 * set rendered at inconsistent weights across platforms and read as noise.
 */

/** One stroked path per kind, drawn on a 16x16 grid at a uniform weight. */
const KIND_ICON: Record<StreamEntryKind, string> = {
  // A thinking dot pair — deliberation.
  thinking: "M6 8h.01M10 8h.01M8 3a5 5 0 1 0 0 10A5 5 0 0 0 8 3z",
  // A speech stroke — the answer.
  message: "M3 4h10v6H6l-3 3z",
  // A wrench-ish chevron — a tool ran.
  tool: "M4 8l2.5 2.5L12 5",
  // A document — an artifact was written.
  artifact: "M4 2h5l3 3v9H4zM9 2v3h3",
  // A warning triangle — something failed and recovered.
  recovery: "M8 3l5 9H3zM8 7v2M8 11h.01",
  // A small dot — a bookkeeping note.
  notice: "M8 8h.01",
}

function KindIcon({ kind }: { kind: StreamEntryKind }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={KIND_ICON[kind]} />
    </svg>
  )
}

/** The goal header icon: a chevron prompt. */
function GoalIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M5 4l4 4-4 4M9.5 12h3" />
    </svg>
  )
}

/** The result icon: a check inside a circle. */
function ResultIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zM5.5 8.2l1.8 1.8 3.4-3.8" />
    </svg>
  )
}

export function ExecutionStream({
  entries,
  goal,
  status,
  result,
  evidenceArtifactCount = 0,
  liveText,
  liveThinking,
}: {
  entries: readonly StreamEntry[]
  goal: string
  status: string
  /**
   * The task's final answer.  The durable spine stores only digests for stream
   * content, so the answer is read from the task projection rather than
   * reconstructed from the stream -- without this the stream shows how the
   * work was done but never what it produced.
   */
  result?: string
  /** Internal evidence artifacts folded into one summary row. */
  evidenceArtifactCount?: number
  /** Transient assistant text for the turn still in flight, if any. */
  liveText?: string
  /**
   * Transient model deliberation for the round still in flight.  It rides its
   * own stream and is rendered separately from `liveText` so the two texts are
   * never concatenated.
   */
  liveThinking?: string
}) {
  const rows = useMemo(() => entries, [entries])

  return (
    <div className="execution-stream" role="log" aria-label="执行流" data-task-status={status}>
      <article className="stream-entry stream-entry-goal" data-kind="goal">
        <span className="stream-entry-glyph"><GoalIcon /></span>
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
          <span className="stream-entry-glyph">
            <KindIcon kind={entry.kind} />
          </span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-title">{entry.title}</span>
              {entry.durationMs !== undefined ? (
                <span className="stream-entry-duration">
                  {entry.kind === "thinking"
                    ? `思考了 ${formatDuration(entry.durationMs)}`
                    : formatDuration(entry.durationMs)}
                </span>
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

      {evidenceArtifactCount > 0 ? (
        <article className="stream-entry" data-kind="notice">
          <span className="stream-entry-glyph"><KindIcon kind="notice" /></span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-title">运行证据已归档</span>
              <span className="stream-entry-count">{evidenceArtifactCount} 项</span>
            </div>
            <p className="stream-entry-detail">
              交付清单、会话快照、运行时转录、执行轨迹与记忆连续性结果，可在证据中心查看。
            </p>
          </div>
        </article>
      ) : null}

      {liveThinking ? (
        <article className="stream-entry" data-kind="thinking" data-status={status === "running" ? "running" : "completed"}>
          <span className="stream-entry-glyph"><KindIcon kind="thinking" /></span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-title">
                {status === "running" ? "正在推理…" : "思考"}
              </span>
            </div>
            <p className="stream-entry-detail stream-entry-live">{liveThinking}</p>
          </div>
        </article>
      ) : null}

      {liveText ? (
        <article className="stream-entry" data-kind="message" data-status="running">
          <span className="stream-entry-glyph"><KindIcon kind="message" /></span>
          <div className="stream-entry-body">
            <div className="stream-entry-head">
              <span className="stream-entry-title">正在生成…</span>
            </div>
            <p className="stream-entry-detail stream-entry-live">{liveText}</p>
          </div>
        </article>
      ) : null}

      {result ? (
        <article className="stream-entry stream-entry-result" data-kind="result">
          <span className="stream-entry-glyph"><ResultIcon /></span>
          <div className="stream-entry-body">
            <p className="stream-entry-result-text">{result}</p>
          </div>
        </article>
      ) : null}

      {!rows.length && !liveText && !liveThinking && !result ? (
        <p className="stream-empty" role="status">等待执行输出…</p>
      ) : null}
    </div>
  )
}
