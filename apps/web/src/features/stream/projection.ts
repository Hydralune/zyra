import type { CausalEventProjection } from "../../state/contracts.ts"

/**
 * Folds the canonical event spine into an ordered execution stream.
 *
 * The spine records one row per fact, but a reader wants one row per *thing
 * that happened*.  A tool call is three events (`called`, possibly `progress`,
 * then `succeeded`/`failed`); the stream shows one tool entry that settles in
 * place.  Bookkeeping that carries no new information is dropped rather than
 * rendered as noise.
 *
 * The stream deliberately excludes plan steps.  The whole plan is created
 * before any work starts, so interleaving steps by time would cluster all of
 * them at the top of the timeline.  Step state belongs to the plan view, and
 * every entry here still carries `nodeId` so work stays attributable to the
 * step it advanced.
 *
 * This is a pure function over the durable spine.  Live text arrives on a
 * separate channel and is merged by the caller, because the spine deliberately
 * stores only digests for stream content (see `code_worker_adapter.py`).
 */

export type StreamEntryKind =
  | "thinking"
  | "message"
  | "tool"
  | "artifact"
  | "recovery"
  | "notice"

export type StreamEntryStatus = "running" | "completed" | "failed" | "cancelled"

export interface StreamEntry {
  /** Stable across re-projections so React keys survive a refresh. */
  key: string
  kind: StreamEntryKind
  status: StreamEntryStatus
  /** One-line headline, already localized. */
  title: string
  /** Optional supporting line (tool name, node title, failure reason). */
  detail?: string
  /** Milliseconds between the opening and settling event, when both exist. */
  durationMs?: number
  at: string
  sequence: number
  toolCallId?: string
  nodeId?: string
  artifactIds: readonly string[]
  /** Number of spine events folded into this entry. */
  eventCount: number
}

/**
 * The spine normalizes legacy events into itself and stamps a fixed sentence on
 * each one.  Rendering that sentence would fill the stream with rows that say
 * nothing, so bookkeeping rows are dropped rather than shown.
 */
const BOOKKEEPING = /^Legacy .+ event normalized into the runtime event spine\.?$/

function isBookkeeping(summary: string): boolean {
  return BOOKKEEPING.test(summary.trim())
}

/**
 * Some coordination events carry a bare machine identifier instead of a
 * sentence ("running", "consume_completed_communication_window").  Showing
 * those tells a reader nothing they can act on.
 */
const MACHINE_TOKEN = /^[a-z][a-z0-9_]{0,47}$/

function isNoiseNotice(summary: string): boolean {
  const text = summary.trim()
  return !text || MACHINE_TOKEN.test(text)
}

/** Consecutive events of the same tool call arrive as a burst. */
const TOOL_EVENT_TYPES = new Set([
  "tool.called",
  "tool.progress",
  "tool.succeeded",
  "tool.failed",
  "tool.cancelled",
])

function toolStatus(type: string, hadFailure: boolean): StreamEntryStatus {
  if (type === "tool.cancelled") return "cancelled"
  if (type === "tool.failed") return "failed"
  if (type === "tool.succeeded") return hadFailure ? "failed" : "completed"
  return "running"
}

function millisBetween(start: string, end: string): number | undefined {
  const from = Date.parse(start)
  const to = Date.parse(end)
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return undefined
  return to - from
}

interface ToolAccumulator {
  entry: StreamEntry
  openedAt: string
  failed: boolean
}

export function projectExecutionStream(
  events: readonly CausalEventProjection[],
): StreamEntry[] {
  const ordered = [...events].sort((left, right) => left.sequence - right.sequence)
  const entries: StreamEntry[] = []
  const tools = new Map<string, ToolAccumulator>()

  for (const event of ordered) {
    const type = event.eventType.replace(/^runtime\./, "")

    if (TOOL_EVENT_TYPES.has(type)) {
      const toolCallId = event.toolCallId
      if (!toolCallId) continue
      const existing = tools.get(toolCallId)
      if (!existing) {
        const entry: StreamEntry = {
          key: `tool:${toolCallId}`,
          kind: "tool",
          status: toolStatus(type, false),
          title: event.summary.split(":")[0]?.trim() || "工具",
          detail: isBookkeeping(event.summary) ? undefined : event.summary,
          at: event.createdAt,
          sequence: event.sequence,
          toolCallId,
          nodeId: event.nodeId,
          artifactIds: event.artifactIds,
          eventCount: 1,
        }
        tools.set(toolCallId, { entry, openedAt: event.createdAt, failed: false })
        entries.push(entry)
        continue
      }
      if (type === "tool.failed") existing.failed = true
      // Settle in place: the opening row stays where it started.
      if (type !== "tool.progress") {
        existing.entry.status = toolStatus(type, existing.failed)
      }
      if (!isBookkeeping(event.summary)) existing.entry.detail = event.summary
      existing.entry.eventCount += 1
      if (existing.entry.status !== "running") {
        existing.entry.durationMs = millisBetween(existing.openedAt, event.createdAt)
      }
    } else if (type === "artifact.committed" || type === "artifact.created") {
      entries.push({
        key: `artifact:${event.eventId}`,
        kind: "artifact",
        status: "completed",
        title: "产物已保存",
        detail: isBookkeeping(event.summary) ? undefined : event.summary,
        at: event.createdAt,
        sequence: event.sequence,
        nodeId: event.nodeId,
        artifactIds: event.artifactIds,
        eventCount: 1,
      })
    } else if (type.startsWith("node.")) {
      // Step state is owned by the plan view, not this timeline (see the module
      // docstring).  Node events are still how a step's failure reaches the
      // stream, so a failed node becomes a recovery entry below.
      if (type === "node.failed") {
        entries.push({
          key: `step-failed:${event.eventId}`,
          kind: "recovery",
          status: "failed",
          title: "步骤执行失败",
          detail: isBookkeeping(event.summary) ? undefined : event.summary,
          at: event.createdAt,
          sequence: event.sequence,
          nodeId: event.nodeId,
          artifactIds: event.artifactIds,
          eventCount: 1,
        })
      }
    } else if (type.startsWith("recovery.") || type === "backend.failover") {
      // A run that keeps recovering emits one identical row per attempt -- a
      // real one produced 25 in a row.  Repeated identical recoveries collapse
      // into a single row that counts them, so the stream shows "this kept
      // happening" instead of repeating the same line.
      const summary = isBookkeeping(event.summary) ? undefined : event.summary
      const previous = entries[entries.length - 1]
      if (
        previous
        && previous.kind === "recovery"
        && previous.title === "故障恢复"
        && previous.detail === summary
        && previous.status === (type.endsWith("failed") || type.endsWith("requested") ? "running" : "completed")
      ) {
        previous.eventCount += 1
        previous.at = event.createdAt
        continue
      }
      entries.push({
        key: `recovery:${event.eventId}`,
        kind: "recovery",
        status: type.endsWith("failed") || type.endsWith("requested") ? "running" : "completed",
        title: "故障恢复",
        detail: summary,
        at: event.createdAt,
        sequence: event.sequence,
        nodeId: event.nodeId,
        artifactIds: event.artifactIds,
        eventCount: 1,
      })
    } else if (type === "text.ended") {
      // The durable spine clears stream content, so a replay carries no text.
      // Without live text this row is only "an answer was produced", which adds
      // nothing to a stream already showing how it was produced -- the final
      // answer is rendered from the task's own summary.  Emitted only when the
      // caller has live text to attach, which the merge step decides.
      continue
    } else if (type === "agent.message" && !isBookkeeping(event.summary)
      && !isNoiseNotice(event.summary)) {
      // A coordination note is narration, not a categorised object: showing it
      // as "记录 / 协作记录 / <text>" buries the one line that matters under two
      // labels.  The text is the row.
      entries.push({
        key: `notice:${event.eventId}`,
        kind: "notice",
        status: "completed",
        title: event.summary,
        at: event.createdAt,
        sequence: event.sequence,
        nodeId: event.nodeId,
        artifactIds: event.artifactIds,
        eventCount: 1,
      })
    }
  }

  return entries.sort((left, right) => left.sequence - right.sequence)
}
