import type {
  DiffFileContract,
  DiffHunkContract,
  DiffLayoutMode,
  DiffLineContract,
  DiffReviewLineSelection,
  DiffViewMode,
} from "./contracts.ts"

export type DiffVirtualRowKind =
  | "file-header"
  | "hunk-header"
  | "line"
  | "split-line"
  | "fold"
  | "binary"
  | "loading"
  | "error"

export interface DiffVirtualRowBase {
  rowId: string
  kind: DiffVirtualRowKind
  fileId: string
  hunkId?: string
  height: number
  top: number
  index: number
  focusable: boolean
}

export interface DiffVirtualFileHeaderRow extends DiffVirtualRowBase {
  kind: "file-header"
  file: DiffFileContract
}

export interface DiffVirtualHunkHeaderRow extends DiffVirtualRowBase {
  kind: "hunk-header"
  hunk: DiffHunkContract
}

export interface DiffVirtualLineRow extends DiffVirtualRowBase {
  kind: "line"
  line: DiffLineContract
  selected: boolean
  commented: boolean
}

export interface DiffVirtualSplitRow extends DiffVirtualRowBase {
  kind: "split-line"
  oldLine?: DiffLineContract
  newLine?: DiffLineContract
  selectedOld: boolean
  selectedNew: boolean
  commentedOld: boolean
  commentedNew: boolean
}

export interface DiffVirtualFoldRow extends DiffVirtualRowBase {
  kind: "fold"
  firstHiddenIndex: number
  lastHiddenIndex: number
  hiddenCount: number
  beforeLine?: number
  afterLine?: number
}

export interface DiffVirtualBinaryRow extends DiffVirtualRowBase {
  kind: "binary"
  file: DiffFileContract
  message: string
}

export interface DiffVirtualStatusRow extends DiffVirtualRowBase {
  kind: "loading" | "error"
  message: string
}

export type DiffVirtualRow =
  | DiffVirtualFileHeaderRow
  | DiffVirtualHunkHeaderRow
  | DiffVirtualLineRow
  | DiffVirtualSplitRow
  | DiffVirtualFoldRow
  | DiffVirtualBinaryRow
  | DiffVirtualStatusRow

type WithoutVirtualPosition<T> = T extends unknown
  ? Omit<T, "top" | "index">
  : never

export type DiffProjectedRow = WithoutVirtualPosition<DiffVirtualRow>

export interface DiffVirtualWindow {
  rows: readonly DiffVirtualRow[]
  totalRows: number
  totalHeight: number
  startIndex: number
  endIndex: number
  offsetTop: number
  offsetBottom: number
  firstFocusableRowId?: string
  lastFocusableRowId?: string
}

export interface DiffVirtualOptions {
  scrollTop: number
  viewportHeight: number
  overscanPixels?: number
  lineHeight?: number
  hunkHeaderHeight?: number
  fileHeaderHeight?: number
  foldHeight?: number
  statusHeight?: number
}

export interface DiffProjectionOptions {
  layout: DiffLayoutMode
  viewMode: DiffViewMode
  collapsedHunks?: ReadonlySet<string>
  expandedFolds?: ReadonlySet<string>
  selection?: DiffReviewLineSelection
  commentedLineIds?: ReadonlySet<string>
  contextCollapseThreshold?: number
  contextEdgeLines?: number
  includeFileHeader?: boolean
  loading?: boolean
  error?: string
}

export interface DiffRowProjection {
  rows: readonly DiffProjectedRow[]
  lineToRow: ReadonlyMap<string, string>
  hunkToRow: ReadonlyMap<string, string>
  focusableRowIds: readonly string[]
}

export interface DiffNavigationResult {
  rowId?: string
  index: number
  scrollTop: number
  changed: boolean
}

export class DiffVirtualizationError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffVirtualizationError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

const defaultLineHeight = 24
const defaultHunkHeaderHeight = 34
const defaultFileHeaderHeight = 46
const defaultFoldHeight = 30
const defaultStatusHeight = 44
const defaultContextThreshold = 16
const defaultContextEdgeLines = 3

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isFinite(value) || value < minimum || value > maximum) {
    throw new DiffVirtualizationError(
      "diff_virtual_option_invalid",
      `${name} must be between ${minimum} and ${maximum}.`,
      { actual: value },
    )
  }
  return Math.floor(value)
}

function lineVisibleInMode(
  line: DiffLineContract,
  mode: DiffViewMode,
): boolean {
  if (mode === "diff") return true
  if (mode === "old") return line.kind !== "added"
  return line.kind !== "deleted"
}

function selectedLine(
  line: DiffLineContract | undefined,
  side: "old" | "new",
  selection: DiffReviewLineSelection | undefined,
): boolean {
  if (!line || !selection || selection.side !== side) return false
  return selection.anchorLineIds.includes(line.lineId)
}

function rowId(
  kind: string,
  fileId: string,
  hunkId = "",
  suffix = "",
): string {
  return `diff-row:${kind}:${fileId}:${hunkId}:${suffix}`
}

function contextRuns(lines: readonly DiffLineContract[]): readonly {
  start: number
  end: number
}[] {
  const result: { start: number; end: number }[] = []
  let start = -1
  for (let index = 0; index <= lines.length; index += 1) {
    const context = index < lines.length && lines[index]!.kind === "context"
    if (context && start < 0) start = index
    if (!context && start >= 0) {
      result.push({ start, end: index })
      start = -1
    }
  }
  return Object.freeze(result)
}

interface FoldDecision {
  start: number
  end: number
  foldId: string
}

function foldDecisions(
  hunk: DiffHunkContract,
  expandedFolds: ReadonlySet<string>,
  threshold: number,
  edge: number,
): readonly FoldDecision[] {
  const result: FoldDecision[] = []
  for (const run of contextRuns(hunk.lines)) {
    const length = run.end - run.start
    if (length <= threshold) continue
    let hiddenStart = run.start + edge
    let hiddenEnd = run.end - edge
    if (run.start === 0) hiddenStart = run.start
    if (run.end === hunk.lines.length) hiddenEnd = run.end
    if (hiddenEnd <= hiddenStart) continue
    const foldId = rowId(
      "fold",
      hunk.fileId,
      hunk.hunkId,
      `${hiddenStart}-${hiddenEnd}`,
    )
    if (expandedFolds.has(foldId)) continue
    result.push({ start: hiddenStart, end: hiddenEnd, foldId })
  }
  return Object.freeze(result)
}

function splitRows(
  hunk: DiffHunkContract,
): readonly {
  oldLine?: DiffLineContract
  newLine?: DiffLineContract
}[] {
  const result: {
    oldLine?: DiffLineContract
    newLine?: DiffLineContract
  }[] = []
  let index = 0
  while (index < hunk.lines.length) {
    const line = hunk.lines[index]!
    if (line.kind === "context" || line.kind === "notice") {
      result.push({ oldLine: line, newLine: line })
      index += 1
      continue
    }
    if (line.kind === "added") {
      result.push({ newLine: line })
      index += 1
      continue
    }
    const deleted: DiffLineContract[] = []
    const added: DiffLineContract[] = []
    while (index < hunk.lines.length && hunk.lines[index]!.kind === "deleted") {
      deleted.push(hunk.lines[index]!)
      index += 1
    }
    while (index < hunk.lines.length && hunk.lines[index]!.kind === "added") {
      added.push(hunk.lines[index]!)
      index += 1
    }
    const length = Math.max(deleted.length, added.length)
    for (let offset = 0; offset < length; offset += 1) {
      result.push({
        oldLine: deleted[offset],
        newLine: added[offset],
      })
    }
  }
  return Object.freeze(result)
}

function projectUnifiedHunk(
  file: DiffFileContract,
  hunk: DiffHunkContract,
  options: Required<Pick<
    DiffProjectionOptions,
    "viewMode" | "contextCollapseThreshold" | "contextEdgeLines"
  >> & DiffProjectionOptions,
): DiffProjectedRow[] {
  const rows: DiffProjectedRow[] = []
  const folds = foldDecisions(
    hunk,
    options.expandedFolds ?? new Set(),
    options.contextCollapseThreshold,
    options.contextEdgeLines,
  )
  let index = 0
  for (const fold of folds) {
    while (index < fold.start) {
      const line = hunk.lines[index]!
      if (lineVisibleInMode(line, options.viewMode)) {
        rows.push({
          rowId: rowId("line", file.fileId, hunk.hunkId, line.lineId),
          kind: "line",
          fileId: file.fileId,
          hunkId: hunk.hunkId,
          height: defaultLineHeight,
          focusable: line.kind !== "notice",
          line,
          selected:
            selectedLine(line, "old", options.selection)
            || selectedLine(line, "new", options.selection),
          commented: options.commentedLineIds?.has(line.lineId) ?? false,
        })
      }
      index += 1
    }
    const first = hunk.lines[fold.start]
    const last = hunk.lines[fold.end - 1]
    rows.push({
      rowId: fold.foldId,
      kind: "fold",
      fileId: file.fileId,
      hunkId: hunk.hunkId,
      height: defaultFoldHeight,
      focusable: true,
      firstHiddenIndex: fold.start,
      lastHiddenIndex: fold.end - 1,
      hiddenCount: fold.end - fold.start,
      beforeLine: first?.newLine ?? first?.oldLine,
      afterLine: last?.newLine ?? last?.oldLine,
    })
    index = fold.end
  }
  while (index < hunk.lines.length) {
    const line = hunk.lines[index]!
    if (lineVisibleInMode(line, options.viewMode)) {
      rows.push({
        rowId: rowId("line", file.fileId, hunk.hunkId, line.lineId),
        kind: "line",
        fileId: file.fileId,
        hunkId: hunk.hunkId,
        height: defaultLineHeight,
        focusable: line.kind !== "notice",
        line,
        selected:
          selectedLine(line, "old", options.selection)
          || selectedLine(line, "new", options.selection),
        commented: options.commentedLineIds?.has(line.lineId) ?? false,
      })
    }
    index += 1
  }
  return rows
}

function projectSplitHunk(
  file: DiffFileContract,
  hunk: DiffHunkContract,
  options: DiffProjectionOptions,
): DiffProjectedRow[] {
  const rows: DiffProjectedRow[] = []
  for (const [index, pair] of splitRows(hunk).entries()) {
    const oldVisible =
      options.viewMode !== "new"
      && pair.oldLine?.kind !== "added"
    const newVisible =
      options.viewMode !== "old"
      && pair.newLine?.kind !== "deleted"
    const oldLine = oldVisible ? pair.oldLine : undefined
    const newLine = newVisible ? pair.newLine : undefined
    if (!oldLine && !newLine) continue
    rows.push({
      rowId: rowId(
        "split",
        file.fileId,
        hunk.hunkId,
        `${oldLine?.lineId ?? "empty"}:${newLine?.lineId ?? "empty"}:${index}`,
      ),
      kind: "split-line",
      fileId: file.fileId,
      hunkId: hunk.hunkId,
      height: defaultLineHeight,
      focusable: Boolean(oldLine || newLine),
      oldLine,
      newLine,
      selectedOld: selectedLine(oldLine, "old", options.selection),
      selectedNew: selectedLine(newLine, "new", options.selection),
      commentedOld:
        Boolean(oldLine)
        && (options.commentedLineIds?.has(oldLine!.lineId) ?? false),
      commentedNew:
        Boolean(newLine)
        && (options.commentedLineIds?.has(newLine!.lineId) ?? false),
    })
  }
  return rows
}

export function projectDiffRows(
  file: DiffFileContract,
  hunks: readonly DiffHunkContract[],
  options: DiffProjectionOptions,
): DiffRowProjection {
  const contextCollapseThreshold = bounded(
    options.contextCollapseThreshold,
    defaultContextThreshold,
    2,
    10_000,
    "contextCollapseThreshold",
  )
  const contextEdgeLines = bounded(
    options.contextEdgeLines,
    defaultContextEdgeLines,
    0,
    Math.max(0, contextCollapseThreshold - 1),
    "contextEdgeLines",
  )
  const collapsed = options.collapsedHunks ?? new Set<string>()
  const rows: DiffProjectedRow[] = []
  const lineToRow = new Map<string, string>()
  const hunkToRow = new Map<string, string>()
  const focusableRowIds: string[] = []
  if (options.includeFileHeader !== false) {
    rows.push({
      rowId: rowId("file", file.fileId),
      kind: "file-header",
      fileId: file.fileId,
      height: defaultFileHeaderHeight,
      focusable: true,
      file,
    })
  }
  if (file.binary) {
    rows.push({
      rowId: rowId("binary", file.fileId),
      kind: "binary",
      fileId: file.fileId,
      height: defaultStatusHeight,
      focusable: true,
      file,
      message:
        "Binary content is metadata-only. Text patch application is disabled.",
    })
  } else {
    let previousHunkIndex = -1
    for (const hunk of hunks) {
      if (hunk.fileId !== file.fileId) {
        throw new DiffVirtualizationError(
          "diff_virtual_hunk_file_mismatch",
          "Cannot project a hunk from another file.",
          { expected: file.fileId, actual: hunk.fileId },
        )
      }
      if (hunk.index <= previousHunkIndex) {
        throw new DiffVirtualizationError(
          "diff_virtual_hunk_order",
          "Cannot project duplicate or out-of-order hunks.",
          { previous: previousHunkIndex, current: hunk.index },
        )
      }
      previousHunkIndex = hunk.index
      const headerRowId = rowId("hunk", file.fileId, hunk.hunkId)
      rows.push({
        rowId: headerRowId,
        kind: "hunk-header",
        fileId: file.fileId,
        hunkId: hunk.hunkId,
        height: defaultHunkHeaderHeight,
        focusable: true,
        hunk,
      })
      hunkToRow.set(hunk.hunkId, headerRowId)
      if (collapsed.has(hunk.hunkId)) continue
      const hunkRows =
        options.layout === "split"
          ? projectSplitHunk(file, hunk, options)
          : projectUnifiedHunk(file, hunk, {
            ...options,
            viewMode: options.viewMode,
            contextCollapseThreshold,
            contextEdgeLines,
          })
      for (const row of hunkRows) {
        rows.push(row)
        if (row.kind === "line") {
          lineToRow.set(row.line.lineId, row.rowId)
        } else if (row.kind === "split-line") {
          if (row.oldLine) lineToRow.set(row.oldLine.lineId, row.rowId)
          if (row.newLine) lineToRow.set(row.newLine.lineId, row.rowId)
        }
      }
    }
  }
  if (options.loading) {
    rows.push({
      rowId: rowId("loading", file.fileId),
      kind: "loading",
      fileId: file.fileId,
      height: defaultStatusHeight,
      focusable: false,
      message: "Loading more verified hunks…",
    })
  }
  if (options.error) {
    rows.push({
      rowId: rowId("error", file.fileId),
      kind: "error",
      fileId: file.fileId,
      height: defaultStatusHeight,
      focusable: true,
      message: options.error,
    })
  }
  for (const row of rows) {
    if (row.focusable) focusableRowIds.push(row.rowId)
  }
  return Object.freeze({
    rows: Object.freeze(rows),
    lineToRow,
    hunkToRow,
    focusableRowIds: Object.freeze(focusableRowIds),
  })
}

function cumulativeOffsets(
  rows: readonly DiffProjectedRow[],
  heights: {
    line: number
    hunk: number
    file: number
    fold: number
    status: number
  },
): { rows: DiffVirtualRow[]; offsets: number[]; total: number } {
  const projected: DiffVirtualRow[] = []
  const offsets = new Array<number>(rows.length + 1)
  let top = 0
  offsets[0] = 0
  for (const [index, row] of rows.entries()) {
    const height =
      row.kind === "line" || row.kind === "split-line"
        ? heights.line
        : row.kind === "hunk-header"
          ? heights.hunk
          : row.kind === "file-header"
            ? heights.file
            : row.kind === "fold"
              ? heights.fold
              : heights.status
    projected.push({ ...row, height, top, index } as DiffVirtualRow)
    top += height
    offsets[index + 1] = top
  }
  return { rows: projected, offsets, total: top }
}

function lowerBound(offsets: readonly number[], value: number): number {
  let low = 0
  let high = Math.max(0, offsets.length - 1)
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if (offsets[middle]! < value) low = middle + 1
    else high = middle
  }
  return low
}

export function diffVirtualWindow(
  projection: DiffRowProjection,
  options: DiffVirtualOptions,
): DiffVirtualWindow {
  const scrollTop = bounded(
    options.scrollTop,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "scrollTop",
  )
  const viewportHeight = bounded(
    options.viewportHeight,
    1,
    1,
    1_000_000,
    "viewportHeight",
  )
  const overscan = bounded(
    options.overscanPixels,
    viewportHeight,
    0,
    10_000_000,
    "overscanPixels",
  )
  const layout = cumulativeOffsets(projection.rows, {
    line: bounded(
      options.lineHeight,
      defaultLineHeight,
      12,
      400,
      "lineHeight",
    ),
    hunk: bounded(
      options.hunkHeaderHeight,
      defaultHunkHeaderHeight,
      18,
      400,
      "hunkHeaderHeight",
    ),
    file: bounded(
      options.fileHeaderHeight,
      defaultFileHeaderHeight,
      18,
      600,
      "fileHeaderHeight",
    ),
    fold: bounded(
      options.foldHeight,
      defaultFoldHeight,
      18,
      400,
      "foldHeight",
    ),
    status: bounded(
      options.statusHeight,
      defaultStatusHeight,
      18,
      600,
      "statusHeight",
    ),
  })
  const startPixel = Math.max(0, scrollTop - overscan)
  const endPixel = Math.min(
    layout.total,
    scrollTop + viewportHeight + overscan,
  )
  const startIndex = Math.max(
    0,
    Math.min(layout.rows.length, lowerBound(layout.offsets, startPixel) - 1),
  )
  const endIndex = Math.max(
    startIndex,
    Math.min(
      layout.rows.length,
      lowerBound(layout.offsets, endPixel) + 1,
    ),
  )
  const rows = layout.rows.slice(startIndex, endIndex)
  const focusable = rows.filter((row) => row.focusable)
  return Object.freeze({
    rows: Object.freeze(rows),
    totalRows: layout.rows.length,
    totalHeight: layout.total,
    startIndex,
    endIndex,
    offsetTop: layout.offsets[startIndex] ?? 0,
    offsetBottom: Math.max(0, layout.total - (layout.offsets[endIndex] ?? 0)),
    firstFocusableRowId: focusable[0]?.rowId,
    lastFocusableRowId: focusable.at(-1)?.rowId,
  })
}

function rowTop(
  rows: readonly Omit<DiffVirtualRow, "top" | "index">[],
  index: number,
  lineHeight = defaultLineHeight,
): number {
  let top = 0
  for (let cursor = 0; cursor < index; cursor += 1) {
    const row = rows[cursor]!
    top +=
      row.kind === "line" || row.kind === "split-line"
        ? lineHeight
        : row.height
  }
  return top
}

export function navigateDiffRows(
  projection: DiffRowProjection,
  currentRowId: string | undefined,
  action:
    | "next"
    | "previous"
    | "page-next"
    | "page-previous"
    | "first"
    | "last"
    | "next-hunk"
    | "previous-hunk",
  options: {
    viewportHeight: number
    lineHeight?: number
  },
): DiffNavigationResult {
  const focusable = projection.focusableRowIds
  if (!focusable.length) {
    return Object.freeze({
      rowId: undefined,
      index: -1,
      scrollTop: 0,
      changed: false,
    })
  }
  const current = currentRowId ? focusable.indexOf(currentRowId) : -1
  const pageSize = Math.max(
    1,
    Math.floor(
      bounded(
        options.viewportHeight,
        defaultLineHeight,
        1,
        1_000_000,
        "viewportHeight",
      )
      / bounded(
        options.lineHeight,
        defaultLineHeight,
        12,
        400,
        "lineHeight",
      ),
    ),
  )
  let next: number
  if (action === "first") next = 0
  else if (action === "last") next = focusable.length - 1
  else if (action === "next") next = Math.min(focusable.length - 1, current + 1)
  else if (action === "previous") next = Math.max(0, current < 0 ? 0 : current - 1)
  else if (action === "page-next") next = Math.min(
    focusable.length - 1,
    Math.max(0, current) + pageSize,
  )
  else if (action === "page-previous") next = Math.max(
    0,
    (current < 0 ? 0 : current) - pageSize,
  )
  else {
    const currentProjectionIndex = currentRowId
      ? projection.rows.findIndex((row) => row.rowId === currentRowId)
      : -1
    const step = action === "next-hunk" ? 1 : -1
    let cursor = currentProjectionIndex < 0
      ? (step > 0 ? 0 : projection.rows.length - 1)
      : currentProjectionIndex + step
    let found: string | undefined
    while (cursor >= 0 && cursor < projection.rows.length) {
      const row = projection.rows[cursor]!
      if (row.kind === "hunk-header") {
        found = row.rowId
        break
      }
      cursor += step
    }
    if (!found) {
      next = step > 0 ? focusable.length - 1 : 0
    } else {
      next = focusable.indexOf(found)
    }
  }
  const selected = focusable[Math.max(0, next)]!
  const projectionIndex = projection.rows.findIndex(
    (row) => row.rowId === selected,
  )
  const scrollTop = rowTop(
    projection.rows,
    Math.max(0, projectionIndex),
    options.lineHeight,
  )
  return Object.freeze({
    rowId: selected,
    index: Math.max(0, next),
    scrollTop,
    changed: selected !== currentRowId,
  })
}

export function selectionFromRows(
  fileId: string,
  hunk: DiffHunkContract,
  startLineId: string,
  endLineId: string,
  side: "old" | "new",
): DiffReviewLineSelection {
  const candidates = hunk.lines.filter((line) =>
    side === "old"
      ? line.oldLine !== undefined
      : line.newLine !== undefined)
  const firstIndex = candidates.findIndex((line) => line.lineId === startLineId)
  const lastIndex = candidates.findIndex((line) => line.lineId === endLineId)
  if (firstIndex < 0 || lastIndex < 0) {
    throw new DiffVirtualizationError(
      "diff_selection_anchor_missing",
      "Selection anchor does not belong to the selected hunk side.",
    )
  }
  const start = Math.min(firstIndex, lastIndex)
  const end = Math.max(firstIndex, lastIndex)
  const selected = candidates.slice(start, end + 1)
  const startCoordinate =
    side === "old"
      ? selected[0]!.oldLine
      : selected[0]!.newLine
  const endCoordinate =
    side === "old"
      ? selected.at(-1)!.oldLine
      : selected.at(-1)!.newLine
  if (startCoordinate === undefined || endCoordinate === undefined) {
    throw new DiffVirtualizationError(
      "diff_selection_coordinate_missing",
      "Selection side does not have line coordinates.",
    )
  }
  return Object.freeze({
    fileId,
    hunkId: hunk.hunkId,
    side,
    startLine: startCoordinate,
    endLine: endCoordinate,
    anchorLineIds: Object.freeze(selected.map((line) => line.lineId)),
  })
}

export function revealSelection(
  projection: DiffRowProjection,
  selection: DiffReviewLineSelection,
): string | undefined {
  for (const lineId of selection.anchorLineIds) {
    const row = projection.lineToRow.get(lineId)
    if (row) return row
  }
  return projection.hunkToRow.get(selection.hunkId)
}
