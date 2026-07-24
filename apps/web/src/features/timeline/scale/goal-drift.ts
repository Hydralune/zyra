import type {
  TimelineEventFacts,
  TimelineRow,
  WorkerCausalTimelineProjection,
} from "../projection/index.ts"
import type {
  TimelineDriftKind,
  TimelineGoalDrift,
  TimelineGoalEpoch,
} from "./contracts.ts"

const TERM_PATTERN = /[\p{L}\p{N}_-]{2,}/gu
const STOP_TERMS = new Set([
  "the",
  "and",
  "for",
  "with",
  "from",
  "that",
  "this",
  "into",
  "then",
  "task",
  "run",
  "event",
  "worker",
  "node",
  "true",
  "false",
])

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function normalize(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .trim()
}

function terms(value: string): ReadonlySet<string> {
  return new Set(
    normalize(value)
      .match(TERM_PATTERN)
      ?.filter((term) => !STOP_TERMS.has(term) && !/^\d+$/.test(term)) ?? [],
  )
}

function union<T>(...values: readonly ReadonlySet<T>[]): Set<T> {
  const result = new Set<T>()
  for (const collection of values) {
    for (const value of collection) result.add(value)
  }
  return result
}

function jaccard(
  previous: ReadonlySet<string>,
  current: ReadonlySet<string>,
): number {
  if (!previous.size && !current.size) return 0
  const all = union(previous, current)
  let common = 0
  for (const term of previous) {
    if (current.has(term)) common += 1
  }
  return 1 - common / Math.max(1, all.size)
}

function explicitKind(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): TimelineDriftKind | undefined {
  const haystack = normalize(
    [
      row.title,
      row.summary,
      ...row.tags,
      ...facts.flatMap((fact) => [
        fact.event.eventType,
        fact.title,
        fact.summary,
        ...Object.keys(fact.safeAttributes),
        ...Object.values(fact.safeAttributes)
          .filter(
            (value): value is string | number =>
              typeof value === "string" || typeof value === "number",
          )
          .map(String),
      ]),
    ].join(" "),
  )
  if (/\bsteer(?:ed|ing)?\b|\/steer\b/.test(haystack)) return "steer"
  if (/\breplan(?:ned|ning)?\b/.test(haystack)) return "replan"
  if (
    /\brequirement(?:s)?[-_ ]?(?:change|changed|update|updated)\b/.test(
      haystack,
    )
  ) {
    return "requirement-change"
  }
  if (/\bconstraint(?:s)?[-_ ]?(?:change|changed|update|updated)\b/.test(haystack)) {
    return "constraint-change"
  }
  if (/\bgoal[-_ ]?(?:change|changed|update|updated)\b/.test(haystack)) {
    return "goal-update"
  }
  return undefined
}

function stringAttribute(
  facts: readonly TimelineEventFacts[],
  names: readonly string[],
): string | undefined {
  for (const fact of facts) {
    for (const name of names) {
      const value = fact.safeAttributes[name]
      if (
        (typeof value === "string" || typeof value === "number") &&
        String(value).trim()
      ) {
        return String(value).trim()
      }
    }
  }
  return undefined
}

function instruction(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): string {
  return (
    stringAttribute(facts, [
      "instruction",
      "reason",
      "goal",
      "objective",
      "requirement",
      "new_requirement",
      "steer",
      "message",
    ]) ??
    row.summary ??
    row.title
  )
}

function digest(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): string | undefined {
  return stringAttribute(facts, [
    "goal_digest",
    "goalDigest",
    "requirement_digest",
    "requirementDigest",
    "objective_digest",
    "objectiveDigest",
  ]) ?? (row.mutationIds.at(-1) || undefined)
}

function affectedNodeIds(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): readonly string[] {
  const result = new Set<string>()
  if (row.nodeId) result.add(row.nodeId)
  for (const fact of facts) {
    for (const name of [
      "node_id",
      "nodeId",
      "target_node_id",
      "targetNodeId",
      "affected_node_id",
      "affectedNodeId",
    ]) {
      const value = fact.safeAttributes[name]
      if (typeof value === "string" && value.trim()) result.add(value.trim())
    }
  }
  return freeze(result)
}

function eventMap(
  projection: WorkerCausalTimelineProjection,
): ReadonlyMap<string, TimelineEventFacts> {
  return new Map(
    projection.events.map((facts) => [facts.event.eventId, facts]),
  )
}

interface DriftCandidate {
  row: TimelineRow
  facts: readonly TimelineEventFacts[]
  kind: TimelineDriftKind
  explicit: boolean
  instruction: string
  digest?: string
  terms: ReadonlySet<string>
}

function candidates(
  projection: WorkerCausalTimelineProjection,
): readonly DriftCandidate[] {
  const byEvent = eventMap(projection)
  const result: DriftCandidate[] = []
  for (const row of projection.rows) {
    const facts = row.eventIds
      .map((eventId) => byEvent.get(eventId))
      .filter((value): value is TimelineEventFacts => Boolean(value))
    const kind = explicitKind(row, facts)
    const rowInstruction = instruction(row, facts)
    const rowTerms = terms(rowInstruction)
    if (!kind && !result.length) {
      result.push({
        row,
        facts,
        kind: "initial",
        explicit: false,
        instruction: rowInstruction,
        digest: digest(row, facts),
        terms: rowTerms,
      })
      continue
    }
    if (!kind) continue
    result.push({
      row,
      facts,
      kind,
      explicit: true,
      instruction: rowInstruction,
      digest: digest(row, facts),
      terms: rowTerms,
    })
  }
  return freeze(result)
}

function epochEnd(
  projection: WorkerCausalTimelineProjection,
  currentIndex: number,
  all: readonly DriftCandidate[],
): TimelineRow {
  const next = all[currentIndex + 1]
  if (next) {
    const index = projection.rows.findIndex((row) => row.key === next.row.key)
    return projection.rows[Math.max(0, index - 1)] ?? next.row
  }
  return projection.rows.at(-1) ?? all[currentIndex].row
}

export function projectTimelineGoalDrift(
  projection: WorkerCausalTimelineProjection,
): TimelineGoalDrift {
  const all = candidates(projection)
  let previousTerms = new Set<string>()
  let previousDigest: string | undefined
  const epochs: TimelineGoalEpoch[] = []
  all.forEach((candidate, ordinal) => {
    const end = epochEnd(projection, ordinal, all)
    const changed = [...candidate.terms].filter(
      (term) => !previousTerms.has(term),
    )
    const retained = [...candidate.terms].filter((term) =>
      previousTerms.has(term),
    )
    const removed = [...previousTerms].filter(
      (term) => !candidate.terms.has(term),
    )
    const lexicalDrift =
      ordinal === 0 ? 0 : jaccard(previousTerms, candidate.terms)
    const digestChanged =
      Boolean(previousDigest) &&
      Boolean(candidate.digest) &&
      previousDigest !== candidate.digest
    const kindWeight =
      candidate.kind === "requirement-change"
        ? 0.2
        : candidate.kind === "constraint-change"
          ? 0.16
          : candidate.kind === "steer"
            ? 0.12
            : candidate.kind === "replan"
              ? 0.08
              : 0.05
    const driftScore =
      ordinal === 0
        ? 0
        : Math.min(
            1,
            lexicalDrift * 0.7 +
              (digestChanged ? 0.15 : 0) +
              kindWeight +
              Math.min(0.1, removed.length / 50),
          )
    const sourceEventIds = freeze(candidate.row.eventIds)
    const endEventId =
      end.eventIds.at(-1) ??
      end.primaryEventId
    const epoch: TimelineGoalEpoch = Object.freeze({
      id: `goal-epoch:${ordinal}:${candidate.row.primaryEventId}`,
      kind: candidate.kind,
      ordinal,
      startSequence: candidate.row.sequence,
      endSequence: end.endSequence,
      startEventId: candidate.row.primaryEventId,
      endEventId,
      startedAt: candidate.row.occurredAt,
      endedAt: end.committedAt,
      sourceRowKeys: Object.freeze([candidate.row.key]),
      sourceEventIds,
      goalDigest: candidate.digest,
      previousGoalDigest: previousDigest,
      instruction: candidate.instruction,
      affectedNodeIds: affectedNodeIds(candidate.row, candidate.facts),
      changedTerms: freeze(changed.sort()),
      retainedTerms: freeze(retained.sort()),
      driftScore,
      explicit: candidate.explicit,
      effective: candidate.row.effective || candidate.explicit,
    })
    epochs.push(epoch)
    previousTerms = new Set(candidate.terms)
    previousDigest = candidate.digest ?? previousDigest
  })
  const driftRowKeys = new Set(
    epochs
      .filter((epoch) => epoch.kind !== "initial")
      .flatMap((epoch) => epoch.sourceRowKeys),
  )
  return Object.freeze({
    epochs: freeze(epochs),
    currentEpoch: epochs.at(-1),
    maximumDriftScore: epochs.reduce(
      (maximum, epoch) => Math.max(maximum, epoch.driftScore),
      0,
    ),
    totalGoalChanges: epochs.filter((epoch) => epoch.kind === "goal-update")
      .length,
    totalRequirementChanges: epochs.filter(
      (epoch) => epoch.kind === "requirement-change",
    ).length,
    totalSteers: epochs.filter((epoch) => epoch.kind === "steer").length,
    driftRowKeys,
  })
}

export function goalEpochForSequence(
  drift: TimelineGoalDrift,
  sequence: number,
): TimelineGoalEpoch | undefined {
  return drift.epochs.find(
    (epoch) =>
      sequence >= epoch.startSequence && sequence <= epoch.endSequence,
  )
}

export function goalEpochForRow(
  drift: TimelineGoalDrift,
  row: TimelineRow,
): TimelineGoalEpoch | undefined {
  return goalEpochForSequence(drift, row.sequence)
}

export function goalChangedBetween(
  drift: TimelineGoalDrift,
  startSequence: number,
  endSequence: number,
): readonly TimelineGoalEpoch[] {
  return freeze(
    drift.epochs.filter(
      (epoch) =>
        epoch.kind !== "initial" &&
        epoch.startSequence >= startSequence &&
        epoch.startSequence <= endSequence,
    ),
  )
}
