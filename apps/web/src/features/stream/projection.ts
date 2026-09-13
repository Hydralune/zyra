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
 * One narration block: what the model said in a single provider round, with the
 * work that round then performed.
 *
 * A round is the natural unit of an agent transcript -- the model says what it
 * is about to do, then does it -- so the stream is grouped this way rather than
 * flattened into one list.  Narration text comes from the durable presentation
 * frames that the round's own `assistant_message_id` identifies.
 */
export interface StreamRound {
  key: string
  /** Model narration for this round, when it said anything. */
  text?: string
  /** What the round did. */
  entries: readonly StreamEntry[]
  /** Position of the round's first event, for stable ordering. */
  sequence: number
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

/** The tool's own name, which the summary leads with ("shell: called"). */
function toolName(summary: string): string {
  return summary.split(":")[0]?.trim() || "工具"
}

/**
 * The line worth showing beneath a tool row.
 *
 * A call's summary is `<tool>: <phase>`, so "shell: called" only repeats the
 * title.  What a reader wants is the outcome, which arrives when the call
 * settles -- "shell: Sandbox command completed" -> "Sandbox command completed".
 */
function settledToolDetail(type: string, summary: string): string | undefined {
  if (type !== "tool.succeeded" && type !== "tool.failed" && type !== "tool.cancelled") {
    return undefined
  }
  if (isBookkeeping(summary)) return undefined
  const separator = summary.indexOf(":")
  const detail = separator === -1 ? summary : summary.slice(separator + 1)
  return detail.trim() || undefined
}

/**
 * Collapse one round's parallel tool calls into a single row.
 *
 * A round issues its calls as a batch, so a task that runs two commands at once
 * produced two rows reading "shell · 6s · 2 条记录" side by side -- identical to
 * a reader, because the per-call arguments are digested away and both share the
 * batch.  Showing the batch once, with the count, is what the round actually
 * did; the individual rows stay available through the entry's `toolCallId` set.
 */
function collapseToolBatches(entries: readonly StreamEntry[]): StreamEntry[] {
  const out: StreamEntry[] = []
  const batch: StreamEntry[] = []

  const flush = (): void => {
    if (batch.length === 0) return
    if (batch.length === 1) {
      out.push(batch[0]!)
    } else {
      const first = batch[0]!
      const last = batch[batch.length - 1]!
      const failed = batch.filter((item) => item.status === "failed").length
      const running = batch.some((item) => item.status === "running")
      // The calls run concurrently, so the batch takes as long as its slowest
      // member -- not the span from the first opening to the last settling,
      // which collapses to near zero because they finish together.
      const slowest = batch.reduce<number | undefined>(
        (longest, item) =>
          item.durationMs === undefined
            ? longest
            : longest === undefined
              ? item.durationMs
              : Math.max(longest, item.durationMs),
        undefined,
      )
      out.push({
        ...first,
        key: `batch:${first.key}`,
        title: `${toolName(first.title)} ×${batch.length}`,
        detail: failed > 0
          ? `${failed} 个失败`
          : running
            ? undefined
            : last.detail,
        status: failed > 0 ? "failed" : running ? "running" : "completed",
        durationMs: slowest,
        toolCallId: undefined,
        // The count of spine events folded in is not something a reader acts
        // on, and on a batch it reads as "4 条记录" for what is one row.
        eventCount: 1,
      })
    }
    batch.length = 0
  }

  for (const entry of entries) {
    if (entry.kind !== "tool") {
      flush()
      out.push(entry)
      continue
    }
    // Calls issued together arrive together; a break in the run means a new one.
    const previous = batch[batch.length - 1]
    if (previous && entry.sequence - previous.sequence > 1) flush()
    batch.push(entry)
  }
  flush()
  return out
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

/**
 * A block's opening event, held until its ending event arrives.
 *
 * Reasoning and narration each emit `started` then `ended` around one block of
 * text, adjacent in the spine.  The gap between them is how long the model
 * spent on that block -- the figure a reader wants ("thought for 7s"), and the
 * only source for it, since the ending event carries the text but not the
 * duration.
 */
interface BlockOpen {
  at: string
}

/** Opening event type -> the kind its ending event should produce. */
const BLOCK_STARTS: Readonly<Record<string, "thinking" | "message">> = {
  "reasoning.started": "thinking",
  "text.started": "message",
}

export function projectExecutionStream(
  events: readonly CausalEventProjection[],
): StreamEntry[] {
  const ordered = [...events].sort((left, right) => left.sequence - right.sequence)
  const entries: StreamEntry[] = []
  const tools = new Map<string, ToolAccumulator>()
  // The last opening event per block kind.  A block's start and end are
  // adjacent in the spine, so keeping only the most recent open is enough and
  // avoids depending on a stream identity the projection does not carry.
  const blocks = new Map<"thinking" | "message", BlockOpen>()

  for (const event of ordered) {
    const type = event.eventType.replace(/^runtime\./, "")

    const blockKind = BLOCK_STARTS[type]
    if (blockKind) {
      blocks.set(blockKind, { at: event.createdAt })
      continue
    }

    if (TOOL_EVENT_TYPES.has(type)) {
      const toolCallId = event.toolCallId
      if (!toolCallId) continue
      const existing = tools.get(toolCallId)
      if (!existing) {
        const entry: StreamEntry = {
          key: `tool:${toolCallId}`,
          kind: "tool",
          status: toolStatus(type, false),
          title: toolName(event.summary),
          // The summary echoes the tool name ("shell: called"), which the title
          // already carries; the settled summary is the only part worth a line,
          // and it is attached when the call settles.
          detail: settledToolDetail(type, event.summary),
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
      const detail = settledToolDetail(type, event.summary)
      if (detail) existing.entry.detail = detail
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
      // The end of a narration block.  Its text arrives on the event's inline
      // presentation payload; without it this row would say only "an answer was
      // produced", which is why multi-round runs used to look empty.  Emitted
      // once per round so the stream reads as think -> act -> think -> act.
      const text = event.presentationText?.trim()
      if (!text) continue
      const opened = blocks.get("message")
      blocks.delete("message")
      entries.push({
        key: `narration:${event.eventId}`,
        kind: "message",
        status: "completed",
        title: text,
        durationMs: opened ? millisBetween(opened.at, event.createdAt) : undefined,
        at: event.createdAt,
        sequence: event.sequence,
        nodeId: event.nodeId,
        artifactIds: event.artifactIds,
        eventCount: 1,
      })
    } else if (type === "reasoning.ended") {
      // Deliberation for the round, kept alongside the narration it produced.
      const text = event.presentationText?.trim()
      if (!text) continue
      const opened = blocks.get("thinking")
      blocks.delete("thinking")
      entries.push({
        key: `thinking:${event.eventId}`,
        kind: "thinking",
        status: "completed",
        title: text,
        durationMs: opened ? millisBetween(opened.at, event.createdAt) : undefined,
        at: event.createdAt,
        sequence: event.sequence,
        nodeId: event.nodeId,
        artifactIds: event.artifactIds,
        eventCount: 1,
      })
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

  return collapseToolBatches(
    entries.sort((left, right) => left.sequence - right.sequence),
  )
}
