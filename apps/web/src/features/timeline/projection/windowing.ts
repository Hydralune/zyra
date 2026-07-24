import type {
  TimelineRow,
  TimelineWindow,
  TimelineWindowOptions,
} from "./contracts.ts"

function integer(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isSafeInteger(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Number(value)))
}

function revisionKey(
  rows: readonly TimelineRow[],
  firstIndex: number,
  lastIndex: number,
  pins: readonly string[],
): string {
  const first = rows[firstIndex]
  const last = rows[lastIndex]
  return [
    rows.length,
    first?.key ?? "empty",
    first?.endSequence ?? 0,
    last?.key ?? "empty",
    last?.endSequence ?? 0,
    pins.join(","),
  ].join(":")
}

export function windowTimelineRows(
  rows: readonly TimelineRow[],
  options: TimelineWindowOptions = {},
): TimelineWindow {
  const total = rows.length
  if (total === 0) {
    return Object.freeze({
      rows: Object.freeze([]),
      firstIndex: 0,
      lastIndex: -1,
      total: 0,
      beforeCount: 0,
      afterCount: 0,
      pinnedRowKeys: Object.freeze([]),
      revisionKey: "0:empty",
    })
  }
  const count = integer(options.count, 100, 1, 5_000)
  const overscan = integer(options.overscan, 20, 0, 1_000)
  const anchorIndex = options.anchorRowKey
    ? rows.findIndex((row) => row.key === options.anchorRowKey)
    : undefined
  const requestedStart =
    anchorIndex !== undefined && anchorIndex >= 0
      ? anchorIndex - Math.floor(count / 2)
      : integer(options.start, 0, 0, Math.max(0, total - 1))
  const viewportStart = Math.max(
    0,
    Math.min(total - 1, requestedStart),
  )
  const viewportEnd = Math.min(total - 1, viewportStart + count - 1)
  const firstIndex = Math.max(0, viewportStart - overscan)
  const lastIndex = Math.min(total - 1, viewportEnd + overscan)
  const indexes = new Set<number>()
  for (let index = firstIndex; index <= lastIndex; index += 1) {
    indexes.add(index)
  }
  const pins: string[] = []
  for (const key of options.pinRowKeys ?? []) {
    const index = rows.findIndex((row) => row.key === key)
    if (index < 0) continue
    indexes.add(index)
    pins.push(key)
  }
  const selected = [...indexes]
    .sort((left, right) => left - right)
    .map((index) => rows[index]!)
  return Object.freeze({
    rows: Object.freeze(selected),
    firstIndex,
    lastIndex,
    total,
    beforeCount: firstIndex,
    afterCount: Math.max(0, total - lastIndex - 1),
    anchorIndex:
      anchorIndex !== undefined && anchorIndex >= 0
        ? anchorIndex
        : undefined,
    pinnedRowKeys: Object.freeze([...new Set(pins)]),
    revisionKey: revisionKey(rows, firstIndex, lastIndex, pins),
  })
}

export function revealTimelineRow(
  rows: readonly TimelineRow[],
  rowKey: string,
  count = 100,
  overscan = 20,
): TimelineWindow {
  return windowTimelineRows(rows, {
    anchorRowKey: rowKey,
    count,
    overscan,
    pinRowKeys: [rowKey],
  })
}

export function nextTimelineWindow(
  current: TimelineWindow,
  rows: readonly TimelineRow[],
  direction: "earlier" | "later",
  count = 100,
): TimelineWindow {
  const start =
    direction === "earlier"
      ? Math.max(0, current.firstIndex - count)
      : Math.min(
          Math.max(0, rows.length - 1),
          Math.max(current.lastIndex + 1, current.firstIndex + count),
        )
  return windowTimelineRows(rows, {
    start,
    count,
    overscan: 20,
    pinRowKeys: current.pinnedRowKeys,
  })
}

export function rowForTimelineEvent(
  rows: readonly TimelineRow[],
  eventId: string,
): TimelineRow | undefined {
  return rows.find((row) => row.eventIds.includes(eventId))
}
