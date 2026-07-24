export interface MergeChange {
  changeId: string
  side: "current" | "proposed"
  baseStart: number
  baseEnd: number
  replacement: readonly string[]
  deleted: readonly string[]
}

export interface MergeConflict {
  conflictId: string
  baseStart: number
  baseEnd: number
  baseLines: readonly string[]
  currentLines: readonly string[]
  proposedLines: readonly string[]
  currentChangeIds: readonly string[]
  proposedChangeIds: readonly string[]
  reason:
    | "overlapping_edit"
    | "delete_modify"
    | "competing_insert"
    | "ambiguous_alignment"
  resolved: false
}

export interface ThreeWayMergeResult {
  state: "clean" | "conflicted" | "unchanged"
  text: string
  mergedLines: readonly string[]
  currentChanges: readonly MergeChange[]
  proposedChanges: readonly MergeChange[]
  appliedChangeIds: readonly string[]
  conflicts: readonly MergeConflict[]
  baseLineEnding: "\n" | "\r\n" | "\r" | ""
  finalNewline: boolean
  diagnostics: readonly string[]
}

export interface ThreeWayMergeOptions {
  maximumLines?: number
  maximumCells?: number
  conflictMarkers?: boolean
  markerCurrent?: string
  markerBase?: string
  markerProposed?: string
}

export class DiffMergeError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffMergeError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

interface TextLines {
  lines: string[]
  lineEnding: "\n" | "\r\n" | "\r" | ""
  finalNewline: boolean
}

interface EditStep {
  kind: "equal" | "delete" | "insert"
  line: string
  baseIndex: number
  targetIndex: number
}

interface MergeRegion {
  start: number
  end: number
  current: MergeChange[]
  proposed: MergeChange[]
}

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DiffMergeError(
      "diff_merge_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
      { actual: value },
    )
  }
  return value
}

function textLines(value: string): TextLines {
  let crlf = 0
  let lf = 0
  let cr = 0
  for (let index = 0; index < value.length; index += 1) {
    if (value[index] === "\r" && value[index + 1] === "\n") {
      crlf += 1
      index += 1
    } else if (value[index] === "\r") {
      cr += 1
    } else if (value[index] === "\n") {
      lf += 1
    }
  }
  const lineEnding =
    crlf >= lf && crlf >= cr && crlf > 0
      ? "\r\n"
      : lf >= cr && lf > 0
        ? "\n"
        : cr > 0
          ? "\r"
          : ""
  const finalNewline = /(?:\r\n|\r|\n)$/.test(value)
  const normalized = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n")
  const lines = normalized.split("\n")
  if (finalNewline) lines.pop()
  return { lines, lineEnding, finalNewline }
}

function sameLines(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  return left.every((line, index) => line === right[index])
}

function commonPrefix(
  left: readonly string[],
  right: readonly string[],
): number {
  let index = 0
  while (
    index < left.length
    && index < right.length
    && left[index] === right[index]
  ) {
    index += 1
  }
  return index
}

function commonSuffix(
  left: readonly string[],
  right: readonly string[],
  prefix: number,
): number {
  let count = 0
  while (
    count < left.length - prefix
    && count < right.length - prefix
    && left[left.length - count - 1] === right[right.length - count - 1]
  ) {
    count += 1
  }
  return count
}

function lcsSteps(
  base: readonly string[],
  target: readonly string[],
  maximumCells: number,
): EditStep[] {
  const prefix = commonPrefix(base, target)
  const suffix = commonSuffix(base, target, prefix)
  const baseMiddle = base.slice(prefix, base.length - suffix)
  const targetMiddle = target.slice(prefix, target.length - suffix)
  const cells = (baseMiddle.length + 1) * (targetMiddle.length + 1)
  if (cells > maximumCells) {
    return fallbackSteps(base, target, prefix, suffix)
  }
  const width = targetMiddle.length + 1
  const matrix = new Uint32Array((baseMiddle.length + 1) * width)
  for (let left = baseMiddle.length - 1; left >= 0; left -= 1) {
    for (let right = targetMiddle.length - 1; right >= 0; right -= 1) {
      const offset = left * width + right
      matrix[offset] =
        baseMiddle[left] === targetMiddle[right]
          ? matrix[(left + 1) * width + right + 1]! + 1
          : Math.max(
            matrix[(left + 1) * width + right]!,
            matrix[left * width + right + 1]!,
          )
    }
  }
  const steps: EditStep[] = []
  for (let index = 0; index < prefix; index += 1) {
    steps.push({
      kind: "equal",
      line: base[index]!,
      baseIndex: index,
      targetIndex: index,
    })
  }
  let left = 0
  let right = 0
  while (left < baseMiddle.length || right < targetMiddle.length) {
    if (
      left < baseMiddle.length
      && right < targetMiddle.length
      && baseMiddle[left] === targetMiddle[right]
    ) {
      steps.push({
        kind: "equal",
        line: baseMiddle[left]!,
        baseIndex: prefix + left,
        targetIndex: prefix + right,
      })
      left += 1
      right += 1
      continue
    }
    const deleteScore =
      left < baseMiddle.length
        ? matrix[(left + 1) * width + right]!
        : -1
    const insertScore =
      right < targetMiddle.length
        ? matrix[left * width + right + 1]!
        : -1
    if (right < targetMiddle.length && insertScore > deleteScore) {
      steps.push({
        kind: "insert",
        line: targetMiddle[right]!,
        baseIndex: prefix + left,
        targetIndex: prefix + right,
      })
      right += 1
    } else if (left < baseMiddle.length) {
      steps.push({
        kind: "delete",
        line: baseMiddle[left]!,
        baseIndex: prefix + left,
        targetIndex: prefix + right,
      })
      left += 1
    } else {
      steps.push({
        kind: "insert",
        line: targetMiddle[right]!,
        baseIndex: prefix + left,
        targetIndex: prefix + right,
      })
      right += 1
    }
  }
  for (let index = 0; index < suffix; index += 1) {
    steps.push({
      kind: "equal",
      line: base[base.length - suffix + index]!,
      baseIndex: base.length - suffix + index,
      targetIndex: target.length - suffix + index,
    })
  }
  return steps
}

function fallbackSteps(
  base: readonly string[],
  target: readonly string[],
  prefix: number,
  suffix: number,
): EditStep[] {
  const steps: EditStep[] = []
  for (let index = 0; index < prefix; index += 1) {
    steps.push({
      kind: "equal",
      line: base[index]!,
      baseIndex: index,
      targetIndex: index,
    })
  }
  const baseEnd = base.length - suffix
  const targetEnd = target.length - suffix
  for (let index = prefix; index < baseEnd; index += 1) {
    steps.push({
      kind: "delete",
      line: base[index]!,
      baseIndex: index,
      targetIndex: prefix,
    })
  }
  for (let index = prefix; index < targetEnd; index += 1) {
    steps.push({
      kind: "insert",
      line: target[index]!,
      baseIndex: baseEnd,
      targetIndex: index,
    })
  }
  for (let index = 0; index < suffix; index += 1) {
    steps.push({
      kind: "equal",
      line: base[baseEnd + index]!,
      baseIndex: baseEnd + index,
      targetIndex: targetEnd + index,
    })
  }
  return steps
}

function changeId(
  side: MergeChange["side"],
  start: number,
  end: number,
  replacement: readonly string[],
): string {
  let hash = 2166136261
  const value = `${side}:${start}:${end}:${replacement.join("\u0000")}`
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `merge-change:${side}:${start}-${end}:${(hash >>> 0).toString(16)}`
}

function changesFromSteps(
  base: readonly string[],
  steps: readonly EditStep[],
  side: MergeChange["side"],
): MergeChange[] {
  const changes: MergeChange[] = []
  let baseStart = -1
  let baseEnd = -1
  let replacement: string[] = []
  const flush = () => {
    if (baseStart < 0) return
    const deleted = base.slice(baseStart, baseEnd)
    changes.push(
      Object.freeze({
        changeId: changeId(side, baseStart, baseEnd, replacement),
        side,
        baseStart,
        baseEnd,
        replacement: Object.freeze([...replacement]),
        deleted: Object.freeze(deleted),
      }),
    )
    baseStart = -1
    baseEnd = -1
    replacement = []
  }
  for (const step of steps) {
    if (step.kind === "equal") {
      flush()
      continue
    }
    if (baseStart < 0) {
      baseStart = step.baseIndex
      baseEnd = step.baseIndex
    }
    if (step.kind === "delete") {
      baseEnd = Math.max(baseEnd, step.baseIndex + 1)
    } else {
      replacement.push(step.line)
    }
  }
  flush()
  return changes
}

export function diffChanges(
  base: readonly string[],
  target: readonly string[],
  side: MergeChange["side"],
  maximumCells = 4_000_000,
): readonly MergeChange[] {
  if (sameLines(base, target)) return Object.freeze([])
  const steps = lcsSteps(base, target, maximumCells)
  return Object.freeze(changesFromSteps(base, steps, side))
}

function touches(left: MergeChange, right: MergeChange): boolean {
  if (left.baseStart === left.baseEnd && right.baseStart === right.baseEnd) {
    return left.baseStart === right.baseStart
  }
  if (left.baseStart === left.baseEnd) {
    return left.baseStart >= right.baseStart && left.baseStart <= right.baseEnd
  }
  if (right.baseStart === right.baseEnd) {
    return right.baseStart >= left.baseStart && right.baseStart <= left.baseEnd
  }
  return left.baseStart < right.baseEnd && right.baseStart < left.baseEnd
}

function regions(
  current: readonly MergeChange[],
  proposed: readonly MergeChange[],
): MergeRegion[] {
  const all = [...current, ...proposed].sort((left, right) =>
    left.baseStart - right.baseStart
    || left.baseEnd - right.baseEnd
    || left.side.localeCompare(right.side))
  const result: MergeRegion[] = []
  for (const change of all) {
    const previous = result.at(-1)
    if (
      previous
      && (
        change.baseStart < previous.end
        || (change.baseStart === previous.end && change.baseStart === change.baseEnd)
        || [...previous.current, ...previous.proposed].some((item) =>
          touches(item, change))
      )
    ) {
      previous.start = Math.min(previous.start, change.baseStart)
      previous.end = Math.max(previous.end, change.baseEnd)
      previous[change.side].push(change)
    } else {
      result.push({
        start: change.baseStart,
        end: change.baseEnd,
        current: change.side === "current" ? [change] : [],
        proposed: change.side === "proposed" ? [change] : [],
      })
    }
  }
  return result
}

function applyRegion(
  base: readonly string[],
  start: number,
  end: number,
  changes: readonly MergeChange[],
): string[] {
  if (!changes.length) return base.slice(start, end)
  const output: string[] = []
  let cursor = start
  for (const change of [...changes].sort((left, right) =>
    left.baseStart - right.baseStart
    || left.baseEnd - right.baseEnd)) {
    if (change.baseStart < cursor) {
      throw new DiffMergeError(
        "diff_merge_overlapping_side_changes",
        "One merge side contains overlapping changes.",
        { start, end, change },
      )
    }
    output.push(...base.slice(cursor, change.baseStart))
    output.push(...change.replacement)
    cursor = change.baseEnd
  }
  output.push(...base.slice(cursor, end))
  return output
}

function conflictReason(
  current: readonly MergeChange[],
  proposed: readonly MergeChange[],
): MergeConflict["reason"] {
  const currentDelete = current.some(
    (change) => change.baseEnd > change.baseStart && !change.replacement.length,
  )
  const proposedDelete = proposed.some(
    (change) => change.baseEnd > change.baseStart && !change.replacement.length,
  )
  if (currentDelete !== proposedDelete) return "delete_modify"
  const currentInsert = current.every(
    (change) => change.baseStart === change.baseEnd,
  )
  const proposedInsert = proposed.every(
    (change) => change.baseStart === change.baseEnd,
  )
  if (currentInsert && proposedInsert) return "competing_insert"
  return "overlapping_edit"
}

function conflictId(
  start: number,
  end: number,
  current: readonly string[],
  proposed: readonly string[],
): string {
  let hash = 0x811c9dc5
  const value = `${start}:${end}:${current.join("\u0000")}:${proposed.join("\u0000")}`
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return `merge-conflict:${start}-${end}:${(hash >>> 0).toString(16)}`
}

function conflictMarkers(
  baseLines: readonly string[],
  currentLines: readonly string[],
  proposedLines: readonly string[],
  options: ThreeWayMergeOptions,
): string[] {
  return [
    `<<<<<<< ${options.markerCurrent ?? "CURRENT"}`,
    ...currentLines,
    `||||||| ${options.markerBase ?? "BASE"}`,
    ...baseLines,
    "=======",
    ...proposedLines,
    `>>>>>>> ${options.markerProposed ?? "PROPOSED"}`,
  ]
}

function chooseLineEnding(
  base: TextLines,
  current: TextLines,
  proposed: TextLines,
): "\n" | "\r\n" | "\r" | "" {
  if (base.lineEnding) return base.lineEnding
  if (current.lineEnding) return current.lineEnding
  if (proposed.lineEnding) return proposed.lineEnding
  return ""
}

function renderText(
  lines: readonly string[],
  lineEnding: "\n" | "\r\n" | "\r" | "",
  finalNewline: boolean,
): string {
  const separator = lineEnding || "\n"
  return lines.join(separator) + (finalNewline ? separator : "")
}

export function threeWayMerge(
  baseText: string,
  currentText: string,
  proposedText: string,
  options: ThreeWayMergeOptions = {},
): ThreeWayMergeResult {
  const maximumLines = bounded(
    options.maximumLines,
    200_000,
    1,
    2_000_000,
    "maximumLines",
  )
  const maximumCells = bounded(
    options.maximumCells,
    4_000_000,
    1_000,
    100_000_000,
    "maximumCells",
  )
  const base = textLines(baseText)
  const current = textLines(currentText)
  const proposed = textLines(proposedText)
  if (
    base.lines.length > maximumLines
    || current.lines.length > maximumLines
    || proposed.lines.length > maximumLines
  ) {
    throw new DiffMergeError(
      "diff_merge_line_budget",
      "Three-way merge exceeds its line budget.",
      {
        base: base.lines.length,
        current: current.lines.length,
        proposed: proposed.lines.length,
        maximumLines,
      },
    )
  }
  const lineEnding = chooseLineEnding(base, current, proposed)
  if (currentText === proposedText) {
    return Object.freeze({
      state: currentText === baseText ? "unchanged" : "clean",
      text: currentText,
      mergedLines: Object.freeze([...current.lines]),
      currentChanges: Object.freeze([]),
      proposedChanges: Object.freeze([]),
      appliedChangeIds: Object.freeze([]),
      conflicts: Object.freeze([]),
      baseLineEnding: lineEnding,
      finalNewline: current.finalNewline,
      diagnostics: Object.freeze(["current_and_proposed_identical"]),
    })
  }
  if (currentText === baseText) {
    return Object.freeze({
      state: "clean",
      text: proposedText,
      mergedLines: Object.freeze([...proposed.lines]),
      currentChanges: Object.freeze([]),
      proposedChanges: diffChanges(
        base.lines,
        proposed.lines,
        "proposed",
        maximumCells,
      ),
      appliedChangeIds: Object.freeze([]),
      conflicts: Object.freeze([]),
      baseLineEnding: lineEnding,
      finalNewline: proposed.finalNewline,
      diagnostics: Object.freeze(["current_unchanged"]),
    })
  }
  if (proposedText === baseText) {
    return Object.freeze({
      state: "unchanged",
      text: currentText,
      mergedLines: Object.freeze([...current.lines]),
      currentChanges: diffChanges(
        base.lines,
        current.lines,
        "current",
        maximumCells,
      ),
      proposedChanges: Object.freeze([]),
      appliedChangeIds: Object.freeze([]),
      conflicts: Object.freeze([]),
      baseLineEnding: lineEnding,
      finalNewline: current.finalNewline,
      diagnostics: Object.freeze(["proposal_has_no_change"]),
    })
  }
  const currentChanges = diffChanges(
    base.lines,
    current.lines,
    "current",
    maximumCells,
  )
  const proposedChanges = diffChanges(
    base.lines,
    proposed.lines,
    "proposed",
    maximumCells,
  )
  const merged: string[] = []
  const conflicts: MergeConflict[] = []
  const applied: string[] = []
  const diagnostics: string[] = []
  let cursor = 0
  for (const region of regions(currentChanges, proposedChanges)) {
    merged.push(...base.lines.slice(cursor, region.start))
    const baseRegion = base.lines.slice(region.start, region.end)
    const currentRegion = applyRegion(
      base.lines,
      region.start,
      region.end,
      region.current,
    )
    const proposedRegion = applyRegion(
      base.lines,
      region.start,
      region.end,
      region.proposed,
    )
    if (!region.current.length) {
      merged.push(...proposedRegion)
      applied.push(...region.proposed.map((change) => change.changeId))
    } else if (!region.proposed.length) {
      merged.push(...currentRegion)
      applied.push(...region.current.map((change) => change.changeId))
    } else if (sameLines(currentRegion, proposedRegion)) {
      merged.push(...currentRegion)
      applied.push(
        ...region.current.map((change) => change.changeId),
        ...region.proposed.map((change) => change.changeId),
      )
      diagnostics.push(`identical_overlap:${region.start}-${region.end}`)
    } else if (sameLines(currentRegion, baseRegion)) {
      merged.push(...proposedRegion)
      applied.push(...region.proposed.map((change) => change.changeId))
    } else if (sameLines(proposedRegion, baseRegion)) {
      merged.push(...currentRegion)
      applied.push(...region.current.map((change) => change.changeId))
    } else {
      const conflict: MergeConflict = Object.freeze({
        conflictId: conflictId(
          region.start,
          region.end,
          currentRegion,
          proposedRegion,
        ),
        baseStart: region.start,
        baseEnd: region.end,
        baseLines: Object.freeze(baseRegion),
        currentLines: Object.freeze(currentRegion),
        proposedLines: Object.freeze(proposedRegion),
        currentChangeIds: Object.freeze(
          region.current.map((change) => change.changeId),
        ),
        proposedChangeIds: Object.freeze(
          region.proposed.map((change) => change.changeId),
        ),
        reason: conflictReason(region.current, region.proposed),
        resolved: false,
      })
      conflicts.push(conflict)
      if (options.conflictMarkers) {
        merged.push(
          ...conflictMarkers(baseRegion, currentRegion, proposedRegion, options),
        )
      } else {
        merged.push(...currentRegion)
      }
    }
    cursor = region.end
  }
  merged.push(...base.lines.slice(cursor))
  const finalNewline =
    current.finalNewline === proposed.finalNewline
      ? current.finalNewline
      : base.finalNewline
  const text = renderText(merged, lineEnding, finalNewline)
  return Object.freeze({
    state: conflicts.length ? "conflicted" : "clean",
    text,
    mergedLines: Object.freeze(merged),
    currentChanges,
    proposedChanges,
    appliedChangeIds: Object.freeze(applied),
    conflicts: Object.freeze(conflicts),
    baseLineEnding: lineEnding,
    finalNewline,
    diagnostics: Object.freeze(diagnostics),
  })
}

export function resolveMergeConflict(
  result: ThreeWayMergeResult,
  conflictIdValue: string,
  choice: "current" | "proposed" | "base" | "both",
): ThreeWayMergeResult {
  const conflict = result.conflicts.find(
    (candidate) => candidate.conflictId === conflictIdValue,
  )
  if (!conflict) {
    throw new DiffMergeError(
      "diff_merge_conflict_missing",
      `Merge conflict ${conflictIdValue} does not exist.`,
    )
  }
  if (result.conflicts.length !== 1) {
    throw new DiffMergeError(
      "diff_merge_resolution_requires_remerge",
      "Individual resolution requires re-merging when multiple conflicts exist.",
      { conflicts: result.conflicts.length },
    )
  }
  const replacement =
    choice === "current"
      ? conflict.currentLines
      : choice === "proposed"
        ? conflict.proposedLines
        : choice === "base"
          ? conflict.baseLines
          : [...conflict.currentLines, ...conflict.proposedLines]
  const lines = [
    ...result.mergedLines.slice(0, conflict.baseStart),
    ...replacement,
    ...result.mergedLines.slice(conflict.baseEnd),
  ]
  const text = renderText(lines, result.baseLineEnding, result.finalNewline)
  return Object.freeze({
    ...result,
    state: "clean",
    text,
    mergedLines: Object.freeze(lines),
    conflicts: Object.freeze([]),
    diagnostics: Object.freeze([
      ...result.diagnostics,
      `resolved:${conflictIdValue}:${choice}`,
    ]),
  })
}
