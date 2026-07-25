import type {
  DiffFileKind,
  DiffHunkContract,
  DiffLineContract,
  DiffLineKind,
} from "./contracts.ts"

export interface ParsedDiffFile {
  fileId: string
  path: string
  previousPath?: string
  kind: DiffFileKind
  binary: boolean
  oldMode: number
  newMode: number
  oldSha256: string
  newSha256: string
  additions: number
  deletions: number
  patchStart: number
  patchEnd: number
  patchBytes: number
  headers: readonly string[]
  hunks: readonly DiffHunkContract[]
}

export interface ParsedDiffDocument {
  diffId: string
  files: readonly ParsedDiffFile[]
  additions: number
  deletions: number
  hunkCount: number
  lineCount: number
  patchBytes: number
  encoding: string
  lineEnding: "lf" | "crlf" | "cr" | "mixed" | "none"
  bom: boolean
}

export interface UnifiedDiffParseOptions {
  maximumFiles?: number
  maximumHunks?: number
  maximumLines?: number
  maximumBytes?: number
  allowPreamble?: boolean
  expectedDiffId?: string
}

export interface DiffDecodeResult {
  text: string
  encoding: "utf-8" | "utf-16le" | "utf-16be"
  bom: boolean
  binary: boolean
  nulBytes: number
  invalidSequences: number
  lineEnding: "lf" | "crlf" | "cr" | "mixed" | "none"
}

interface MutableFile {
  fileId: string
  path: string
  previousPath?: string
  kind: DiffFileKind
  binary: boolean
  oldMode: number
  newMode: number
  oldSha256: string
  newSha256: string
  additions: number
  deletions: number
  patchStart: number
  patchEnd: number
  headers: string[]
  hunks: DiffHunkContract[]
  oldHeaderPath?: string
  newHeaderPath?: string
  renameFrom?: string
  renameTo?: string
  newFile: boolean
  deletedFile: boolean
}

interface HunkHeader {
  oldStart: number
  oldCount: number
  newStart: number
  newCount: number
  section: string
}

export class UnifiedDiffError extends Error {
  readonly code: string
  readonly patchLine: number
  readonly filePath?: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    patchLine = 0,
    filePath?: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "UnifiedDiffError"
    this.code = code
    this.patchLine = patchLine
    this.filePath = filePath
    this.details = Object.freeze({ ...details })
  }
}

const encoder = new TextEncoder()
const fileHeaderPattern = /^diff --git (.+) (.+)$/
const hunkHeaderPattern =
  /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$/
const indexPattern = /^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: ([0-7]{6}))?$/i
const modePattern = /^[0-7]{6}$/
const defaultOptions = Object.freeze({
  maximumFiles: 10_000,
  maximumHunks: 100_000,
  maximumLines: 5_000_000,
  maximumBytes: 256 * 1024 * 1024,
  allowPreamble: true,
})

function boundedOption(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new UnifiedDiffError(
      "diff_parse_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
    )
  }
  return value
}

function normalizedOptions(options: UnifiedDiffParseOptions) {
  return Object.freeze({
    maximumFiles: boundedOption(
      options.maximumFiles,
      defaultOptions.maximumFiles,
      1,
      100_000,
      "maximumFiles",
    ),
    maximumHunks: boundedOption(
      options.maximumHunks,
      defaultOptions.maximumHunks,
      1,
      1_000_000,
      "maximumHunks",
    ),
    maximumLines: boundedOption(
      options.maximumLines,
      defaultOptions.maximumLines,
      1,
      20_000_000,
      "maximumLines",
    ),
    maximumBytes: boundedOption(
      options.maximumBytes,
      defaultOptions.maximumBytes,
      1_024,
      1024 * 1024 * 1024,
      "maximumBytes",
    ),
    allowPreamble: options.allowPreamble !== false,
    expectedDiffId: options.expectedDiffId?.trim(),
  })
}

function lineEndingOf(text: string): "lf" | "crlf" | "cr" | "mixed" | "none" {
  let lf = 0
  let crlf = 0
  let cr = 0
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index)
    if (code === 13) {
      if (text.charCodeAt(index + 1) === 10) {
        crlf += 1
        index += 1
      } else {
        cr += 1
      }
    } else if (code === 10) {
      lf += 1
    }
  }
  const kinds = Number(lf > 0) + Number(crlf > 0) + Number(cr > 0)
  if (!kinds) return "none"
  if (kinds > 1) return "mixed"
  if (crlf) return "crlf"
  if (cr) return "cr"
  return "lf"
}

function countNuls(bytes: Uint8Array): number {
  let count = 0
  for (const byte of bytes) if (byte === 0) count += 1
  return count
}

function swapPairs(bytes: Uint8Array): Uint8Array {
  const result = new Uint8Array(bytes.length)
  for (let index = 0; index < bytes.length; index += 2) {
    result[index] = bytes[index + 1] ?? 0
    result[index + 1] = bytes[index] ?? 0
  }
  return result
}

function decodeUtf8(bytes: Uint8Array): {
  text: string
  invalidSequences: number
} {
  try {
    return {
      text: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      invalidSequences: 0,
    }
  } catch {
    const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes)
    let invalidSequences = 0
    for (const character of text) {
      if (character === "\uFFFD") invalidSequences += 1
    }
    return { text, invalidSequences }
  }
}

export function decodeDiffBytes(bytes: Uint8Array): DiffDecodeResult {
  const nulBytes = countNuls(bytes)
  let text: string
  let encoding: DiffDecodeResult["encoding"] = "utf-8"
  let bom = false
  let invalidSequences = 0
  if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    bom = true
    const decoded = decodeUtf8(bytes.subarray(3))
    text = decoded.text
    invalidSequences = decoded.invalidSequences
  } else if (bytes[0] === 0xff && bytes[1] === 0xfe) {
    bom = true
    encoding = "utf-16le"
    text = new TextDecoder("utf-16le").decode(bytes.subarray(2))
  } else if (bytes[0] === 0xfe && bytes[1] === 0xff) {
    bom = true
    encoding = "utf-16be"
    text = new TextDecoder("utf-16le").decode(swapPairs(bytes.subarray(2)))
  } else {
    const decoded = decodeUtf8(bytes)
    text = decoded.text
    invalidSequences = decoded.invalidSequences
  }
  const nulRatio = bytes.length ? nulBytes / bytes.length : 0
  const invalidRatio = text.length ? invalidSequences / text.length : 0
  const binary =
    encoding === "utf-8"
    && (nulRatio > 0.01 || invalidRatio > 0.01)
  return Object.freeze({
    text,
    encoding,
    bom,
    binary,
    nulBytes,
    invalidSequences,
    lineEnding: lineEndingOf(text),
  })
}

function normalizeLineEndings(text: string): string {
  return text.replaceAll("\r\n", "\n").replaceAll("\r", "\n")
}

function splitPatchLines(text: string): string[] {
  const normalized = normalizeLineEndings(text)
  const lines = normalized.split("\n")
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop()
  return lines
}

function stripGitPrefix(value: string, prefix: "a/" | "b/"): string {
  const parsed = parseGitToken(value)
  if (parsed === "/dev/null") return parsed
  if (parsed.startsWith(prefix)) return parsed.slice(prefix.length)
  return parsed
}

function parseGitToken(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) {
    throw new UnifiedDiffError(
      "diff_parse_path_missing",
      "Git diff header is missing a path.",
    )
  }
  if (!trimmed.startsWith('"')) return decodeGitEscapes(trimmed)
  if (!trimmed.endsWith('"') || trimmed.length < 2) {
    throw new UnifiedDiffError(
      "diff_parse_path_quote",
      "Git diff path has an unmatched quote.",
    )
  }
  return decodeGitEscapes(trimmed.slice(1, -1))
}

function decodeGitEscapes(value: string): string {
  let result = ""
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index]!
    if (character !== "\\") {
      result += character
      continue
    }
    const next = value[index + 1]
    if (next === undefined) {
      throw new UnifiedDiffError(
        "diff_parse_path_escape",
        "Git diff path ends with an escape.",
      )
    }
    if (next === "n") {
      result += "\n"
      index += 1
      continue
    }
    if (next === "t") {
      result += "\t"
      index += 1
      continue
    }
    if (next === "r") {
      result += "\r"
      index += 1
      continue
    }
    if (next === '"' || next === "\\") {
      result += next
      index += 1
      continue
    }
    if (/[0-7]/.test(next)) {
      const octal = value.slice(index + 1, index + 4)
      if (!/^[0-7]{3}$/.test(octal)) {
        throw new UnifiedDiffError(
          "diff_parse_path_octal",
          "Git diff path contains an invalid octal escape.",
        )
      }
      result += String.fromCharCode(Number.parseInt(octal, 8))
      index += 3
      continue
    }
    result += next
    index += 1
  }
  return result
}

function safeLogicalPath(value: string, patchLine: number): string {
  const path = value.replaceAll("\\", "/")
  if (
    !path
    || path === "/dev/null"
    || path.startsWith("/")
    || /^[A-Za-z]:/.test(path)
    || path.includes("\u0000")
    || path.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    if (path === "/dev/null") return path
    throw new UnifiedDiffError(
      "diff_parse_path_unsafe",
      `Diff contains an unsafe logical path: ${JSON.stringify(path)}.`,
      patchLine,
      path,
    )
  }
  return path
}

function parseHeaderPaths(
  line: string,
  patchLine: number,
): { oldPath: string; newPath: string } | undefined {
  const match = line.match(fileHeaderPattern)
  if (!match) return undefined
  const oldPath = safeLogicalPath(
    stripGitPrefix(match[1]!, "a/"),
    patchLine,
  )
  const newPath = safeLogicalPath(
    stripGitPrefix(match[2]!, "b/"),
    patchLine,
  )
  if (oldPath === "/dev/null" || newPath === "/dev/null") {
    throw new UnifiedDiffError(
      "diff_parse_git_null_path",
      "diff --git header cannot use /dev/null.",
      patchLine,
    )
  }
  return { oldPath, newPath }
}

function parseMarkerPath(
  line: string,
  marker: "--- " | "+++ ",
  patchLine: number,
): string {
  let value = line.slice(marker.length)
  const tab = value.indexOf("\t")
  if (tab >= 0) value = value.slice(0, tab)
  const parsed = parseGitToken(value)
  if (parsed === "/dev/null") return parsed
  return safeLogicalPath(
    stripGitPrefix(parsed, marker === "--- " ? "a/" : "b/"),
    patchLine,
  )
}

function parseMode(value: string, patchLine: number): number {
  if (!modePattern.test(value)) {
    throw new UnifiedDiffError(
      "diff_parse_mode_invalid",
      `Invalid Git file mode ${value}.`,
      patchLine,
    )
  }
  return Number.parseInt(value.slice(-4), 8)
}

function parseHunkHeader(line: string, patchLine: number): HunkHeader {
  const match = line.match(hunkHeaderPattern)
  if (!match) {
    throw new UnifiedDiffError(
      "diff_parse_hunk_header",
      `Invalid unified diff hunk header: ${line}.`,
      patchLine,
    )
  }
  const oldStart = Number.parseInt(match[1]!, 10)
  const oldCount = match[2] === undefined
    ? 1
    : Number.parseInt(match[2], 10)
  const newStart = Number.parseInt(match[3]!, 10)
  const newCount = match[4] === undefined
    ? 1
    : Number.parseInt(match[4], 10)
  if (
    !Number.isSafeInteger(oldStart)
    || !Number.isSafeInteger(oldCount)
    || !Number.isSafeInteger(newStart)
    || !Number.isSafeInteger(newCount)
    || oldStart < 0
    || newStart < 0
    || oldCount < 0
    || newCount < 0
  ) {
    throw new UnifiedDiffError(
      "diff_parse_hunk_coordinate",
      "Hunk coordinates exceed supported integer bounds.",
      patchLine,
    )
  }
  if (oldCount > 0 && oldStart < 1) {
    throw new UnifiedDiffError(
      "diff_parse_old_start",
      "Non-empty old hunk range must start at line one or later.",
      patchLine,
    )
  }
  if (newCount > 0 && newStart < 1) {
    throw new UnifiedDiffError(
      "diff_parse_new_start",
      "Non-empty new hunk range must start at line one or later.",
      patchLine,
    )
  }
  return {
    oldStart,
    oldCount,
    newStart,
    newCount,
    section: match[5] ?? "",
  }
}

function fnv128(value: string): string {
  let high = 0x6c62272e07bb0142n
  let low = 0x62b821756295c58dn
  const prime = 0x0000000001000000000000000000013bn
  const mask = (1n << 128n) - 1n
  for (const byte of encoder.encode(value)) {
    const combined = ((high << 64n) | low) ^ BigInt(byte)
    const next = (combined * prime) & mask
    high = next >> 64n
    low = next & ((1n << 64n) - 1n)
  }
  return `${high.toString(16).padStart(16, "0")}${low
    .toString(16)
    .padStart(16, "0")}`
}

function stableId(kind: string, ...parts: readonly unknown[]): string {
  return `${kind}:${fnv128(JSON.stringify(parts))}`
}

function fileFromHeader(
  oldPath: string,
  newPath: string,
  patchStart: number,
): MutableFile {
  const path = newPath
  return {
    fileId: stableId("diff-file", oldPath, newPath, patchStart),
    path,
    previousPath: oldPath === newPath ? undefined : oldPath,
    kind: oldPath === newPath ? "modified" : "renamed",
    binary: false,
    oldMode: 0,
    newMode: 0,
    oldSha256: "",
    newSha256: "",
    additions: 0,
    deletions: 0,
    patchStart,
    patchEnd: patchStart,
    headers: [],
    hunks: [],
    renameFrom: oldPath === newPath ? undefined : oldPath,
    renameTo: oldPath === newPath ? undefined : newPath,
    newFile: false,
    deletedFile: false,
  }
}

function finalizeFile(file: MutableFile, patchEnd: number): ParsedDiffFile {
  file.patchEnd = Math.max(file.patchStart, patchEnd)
  const oldPath = file.oldHeaderPath
  const newPath = file.newHeaderPath
  if (file.renameFrom || file.renameTo) {
    if (!file.renameFrom || !file.renameTo) {
      throw new UnifiedDiffError(
        "diff_parse_rename_incomplete",
        "Rename metadata requires both rename from and rename to.",
        file.patchStart + 1,
        file.path,
      )
    }
    file.previousPath = safeLogicalPath(file.renameFrom, file.patchStart + 1)
    file.path = safeLogicalPath(file.renameTo, file.patchStart + 1)
    file.kind = "renamed"
  } else if (file.newFile || oldPath === "/dev/null") {
    file.kind = "added"
    file.previousPath = undefined
  } else if (file.deletedFile || newPath === "/dev/null") {
    file.kind = "deleted"
    file.path = file.previousPath ?? file.path
    file.previousPath = undefined
  } else if (file.binary) {
    file.kind = "binary"
  } else if (file.previousPath && file.previousPath !== file.path) {
    file.kind = "renamed"
  } else {
    file.kind = "modified"
    file.previousPath = undefined
  }
  if (
    file.oldHeaderPath
    && file.oldHeaderPath !== "/dev/null"
    && file.previousPath
    && file.oldHeaderPath !== file.previousPath
  ) {
    throw new UnifiedDiffError(
      "diff_parse_old_path_mismatch",
      "Old marker path disagrees with rename or git header.",
      file.patchStart + 1,
      file.path,
      {
        marker: file.oldHeaderPath,
        header: file.previousPath,
      },
    )
  }
  if (
    file.newHeaderPath
    && file.newHeaderPath !== "/dev/null"
    && file.newHeaderPath !== file.path
  ) {
    throw new UnifiedDiffError(
      "diff_parse_new_path_mismatch",
      "New marker path disagrees with rename or git header.",
      file.patchStart + 1,
      file.path,
      {
        marker: file.newHeaderPath,
        header: file.path,
      },
    )
  }
  if (file.binary && file.hunks.length) {
    throw new UnifiedDiffError(
      "diff_parse_binary_hunks",
      "Binary diff cannot contain text hunks.",
      file.patchStart + 1,
      file.path,
    )
  }
  if (!file.binary && !file.hunks.length && !file.renameFrom && !file.newFile && !file.deletedFile) {
    throw new UnifiedDiffError(
      "diff_parse_empty_file_section",
      "Diff file section contains no hunks or file operation.",
      file.patchStart + 1,
      file.path,
    )
  }
  return Object.freeze({
    fileId: file.fileId,
    path: file.path,
    previousPath: file.previousPath,
    kind: file.kind,
    binary: file.binary,
    oldMode: file.oldMode,
    newMode: file.newMode,
    oldSha256: file.oldSha256,
    newSha256: file.newSha256,
    additions: file.additions,
    deletions: file.deletions,
    patchStart: file.patchStart,
    patchEnd: file.patchEnd,
    patchBytes: Math.max(0, file.patchEnd - file.patchStart),
    headers: Object.freeze([...file.headers]),
    hunks: Object.freeze([...file.hunks]),
  })
}

function parseHunkLines(
  lines: readonly string[],
  start: number,
  file: MutableFile,
  header: HunkHeader,
): { hunk: DiffHunkContract; next: number } {
  let index = start + 1
  let oldLine = header.oldStart
  let newLine = header.newStart
  let oldConsumed = 0
  let newConsumed = 0
  let additions = 0
  let deletions = 0
  let contexts = 0
  const parsedLines: DiffLineContract[] = []
  const hunkIndex = file.hunks.length
  const hunkId = stableId(
    "diff-hunk",
    file.fileId,
    hunkIndex,
    header.oldStart,
    header.oldCount,
    header.newStart,
    header.newCount,
  )
  while (index < lines.length) {
    const line = lines[index]!
    if (
      line.startsWith("diff --git ")
      || line.startsWith("@@ ")
      || (
        (line.startsWith("--- ") || line.startsWith("Binary files "))
        && oldConsumed === header.oldCount
        && newConsumed === header.newCount
      )
    ) {
      break
    }
    if (oldConsumed === header.oldCount && newConsumed === header.newCount) {
      if (line === "\\ No newline at end of file") {
        const previous = parsedLines[parsedLines.length - 1]
        if (!previous || previous.kind === "notice") {
          throw new UnifiedDiffError(
            "diff_parse_newline_notice_orphan",
            "No-newline marker has no preceding diff line.",
            index + 1,
            file.path,
          )
        }
        parsedLines[parsedLines.length - 1] = Object.freeze({
          ...previous,
          noNewline: true,
        })
        index += 1
      }
      break
    }
    if (line === "\\ No newline at end of file") {
      const previous = parsedLines[parsedLines.length - 1]
      if (!previous || previous.kind === "notice") {
        throw new UnifiedDiffError(
          "diff_parse_newline_notice_orphan",
          "No-newline marker has no preceding diff line.",
          index + 1,
          file.path,
        )
      }
      parsedLines[parsedLines.length - 1] = Object.freeze({
        ...previous,
        noNewline: true,
      })
      index += 1
      continue
    }
    const prefix = line[0]
    let kind: DiffLineKind
    let oldCoordinate: number | undefined
    let newCoordinate: number | undefined
    if (prefix === " ") {
      kind = "context"
      oldCoordinate = oldLine
      newCoordinate = newLine
      oldLine += 1
      newLine += 1
      oldConsumed += 1
      newConsumed += 1
      contexts += 1
    } else if (prefix === "+") {
      kind = "added"
      newCoordinate = newLine
      newLine += 1
      newConsumed += 1
      additions += 1
      file.additions += 1
    } else if (prefix === "-") {
      kind = "deleted"
      oldCoordinate = oldLine
      oldLine += 1
      oldConsumed += 1
      deletions += 1
      file.deletions += 1
    } else {
      throw new UnifiedDiffError(
        "diff_parse_hunk_line_prefix",
        `Hunk line must begin with space, +, or -: ${line}.`,
        index + 1,
        file.path,
      )
    }
    if (oldConsumed > header.oldCount || newConsumed > header.newCount) {
      throw new UnifiedDiffError(
        "diff_parse_hunk_overflow",
        "Hunk contains more old or new lines than its header declares.",
        index + 1,
        file.path,
      )
    }
    const text = line.slice(1)
    const byteLength = encoder.encode(line).byteLength
    parsedLines.push(
      Object.freeze({
        lineId: stableId(
          "diff-line",
          hunkId,
          index + 1,
          kind,
          oldCoordinate,
          newCoordinate,
          text,
        ),
        kind,
        text,
        oldLine: oldCoordinate,
        newLine: newCoordinate,
        patchLine: index + 1,
        byteOffset: 0,
        byteLength,
        noNewline: false,
      }),
    )
    index += 1
  }
  if (oldConsumed !== header.oldCount || newConsumed !== header.newCount) {
    throw new UnifiedDiffError(
      "diff_parse_hunk_underflow",
      "Hunk ended before consuming the ranges declared by its header.",
      index + 1,
      file.path,
      {
        expectedOld: header.oldCount,
        actualOld: oldConsumed,
        expectedNew: header.newCount,
        actualNew: newConsumed,
      },
    )
  }
  const hunk: DiffHunkContract = Object.freeze({
    hunkId,
    fileId: file.fileId,
    index: hunkIndex,
    header: lines[start]!,
    section: header.section,
    oldStart: header.oldStart,
    oldCount: header.oldCount,
    newStart: header.newStart,
    newCount: header.newCount,
    additions,
    deletions,
    contextLines: contexts,
    patchStart: start,
    patchEnd: index,
    lines: Object.freeze(parsedLines),
  })
  return { hunk, next: index }
}

function ensureFile(
  file: MutableFile | undefined,
  patchLine: number,
): MutableFile {
  if (!file) {
    throw new UnifiedDiffError(
      "diff_parse_file_header_required",
      "Diff metadata or hunk appeared before a diff --git header.",
      patchLine,
    )
  }
  return file
}

export function parseUnifiedDiff(
  value: string | Uint8Array,
  options: UnifiedDiffParseOptions = {},
): ParsedDiffDocument {
  const policy = normalizedOptions(options)
  const decoded = typeof value === "string"
    ? Object.freeze({
      text: value,
      encoding: "utf-8" as const,
      bom: value.charCodeAt(0) === 0xfeff,
      binary: false,
      nulBytes: 0,
      invalidSequences: 0,
      lineEnding: lineEndingOf(value),
    })
    : decodeDiffBytes(value)
  if (decoded.binary) {
    throw new UnifiedDiffError(
      "diff_parse_binary_document",
      "Unified diff artifact is binary or undecodable.",
    )
  }
  const text = decoded.bom && decoded.text.charCodeAt(0) === 0xfeff
    ? decoded.text.slice(1)
    : decoded.text
  const patchBytes = encoder.encode(text).byteLength
  if (patchBytes > policy.maximumBytes) {
    throw new UnifiedDiffError(
      "diff_parse_byte_budget",
      `Diff artifact exceeds ${policy.maximumBytes} UTF-8 bytes.`,
      0,
      undefined,
      { actual: patchBytes, maximum: policy.maximumBytes },
    )
  }
  const lines = splitPatchLines(text)
  if (lines.length > policy.maximumLines) {
    throw new UnifiedDiffError(
      "diff_parse_line_budget",
      `Diff artifact exceeds ${policy.maximumLines} lines.`,
      0,
      undefined,
      { actual: lines.length, maximum: policy.maximumLines },
    )
  }
  const files: ParsedDiffFile[] = []
  let current: MutableFile | undefined
  let index = 0
  let sawFile = false
  let hunkCount = 0
  while (index < lines.length) {
    const line = lines[index]!
    const headerPaths = parseHeaderPaths(line, index + 1)
    if (headerPaths) {
      if (current) files.push(finalizeFile(current, index))
      if (files.length + 1 > policy.maximumFiles) {
        throw new UnifiedDiffError(
          "diff_parse_file_budget",
          `Diff artifact exceeds ${policy.maximumFiles} files.`,
          index + 1,
        )
      }
      current = fileFromHeader(headerPaths.oldPath, headerPaths.newPath, index)
      current.headers.push(line)
      sawFile = true
      index += 1
      continue
    }
    if (!sawFile) {
      if (!line.trim()) {
        index += 1
        continue
      }
      if (!policy.allowPreamble) {
        throw new UnifiedDiffError(
          "diff_parse_preamble_rejected",
          "Text before the first diff --git header is not allowed.",
          index + 1,
        )
      }
      index += 1
      continue
    }
    const file = ensureFile(current, index + 1)
    if (line.startsWith("new file mode ")) {
      file.newFile = true
      file.newMode = parseMode(line.slice("new file mode ".length), index + 1)
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("deleted file mode ")) {
      file.deletedFile = true
      file.oldMode = parseMode(
        line.slice("deleted file mode ".length),
        index + 1,
      )
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("old mode ")) {
      file.oldMode = parseMode(line.slice("old mode ".length), index + 1)
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("new mode ")) {
      file.newMode = parseMode(line.slice("new mode ".length), index + 1)
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("similarity index ") || line.startsWith("dissimilarity index ")) {
      const percentage = Number.parseInt(line.slice(line.lastIndexOf(" ") + 1), 10)
      if (!Number.isInteger(percentage) || percentage < 0 || percentage > 100) {
        throw new UnifiedDiffError(
          "diff_parse_similarity_invalid",
          "Rename similarity percentage is invalid.",
          index + 1,
          file.path,
        )
      }
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("rename from ")) {
      file.renameFrom = safeLogicalPath(
        parseGitToken(line.slice("rename from ".length)),
        index + 1,
      )
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("rename to ")) {
      file.renameTo = safeLogicalPath(
        parseGitToken(line.slice("rename to ".length)),
        index + 1,
      )
      file.headers.push(line)
      index += 1
      continue
    }
    const indexMatch = line.match(indexPattern)
    if (indexMatch) {
      file.oldSha256 = indexMatch[1]!.toLowerCase()
      file.newSha256 = indexMatch[2]!.toLowerCase()
      if (indexMatch[3]) {
        const mode = parseMode(indexMatch[3], index + 1)
        if (!file.oldMode) file.oldMode = mode
        if (!file.newMode) file.newMode = mode
      }
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("--- ")) {
      file.oldHeaderPath = parseMarkerPath(line, "--- ", index + 1)
      file.headers.push(line)
      index += 1
      if (index >= lines.length || !lines[index]!.startsWith("+++ ")) {
        throw new UnifiedDiffError(
          "diff_parse_new_marker_missing",
          "Old file marker must be followed by a new file marker.",
          index + 1,
          file.path,
        )
      }
      file.newHeaderPath = parseMarkerPath(lines[index]!, "+++ ", index + 1)
      file.headers.push(lines[index]!)
      index += 1
      continue
    }
    if (line.startsWith("Binary files ") && line.endsWith(" differ")) {
      file.binary = true
      file.headers.push(line)
      index += 1
      continue
    }
    if (line.startsWith("GIT binary patch")) {
      file.binary = true
      file.headers.push(line)
      index += 1
      while (index < lines.length && !lines[index]!.startsWith("diff --git ")) {
        file.headers.push(lines[index]!)
        index += 1
      }
      continue
    }
    if (line.startsWith("@@ ")) {
      hunkCount += 1
      if (hunkCount > policy.maximumHunks) {
        throw new UnifiedDiffError(
          "diff_parse_hunk_budget",
          `Diff artifact exceeds ${policy.maximumHunks} hunks.`,
          index + 1,
          file.path,
        )
      }
      const header = parseHunkHeader(line, index + 1)
      const result = parseHunkLines(lines, index, file, header)
      const previous = file.hunks[file.hunks.length - 1]
      if (previous) {
        const previousOldEnd = previous.oldStart + previous.oldCount
        const previousNewEnd = previous.newStart + previous.newCount
        if (
          result.hunk.oldStart < previousOldEnd
          || result.hunk.newStart < previousNewEnd
        ) {
          throw new UnifiedDiffError(
            "diff_parse_hunk_overlap",
            "Hunks overlap or are out of order.",
            index + 1,
            file.path,
            {
              previous: previous.header,
              current: result.hunk.header,
            },
          )
        }
      }
      file.hunks.push(result.hunk)
      index = result.next
      continue
    }
    if (!line.trim()) {
      file.headers.push(line)
      index += 1
      continue
    }
    throw new UnifiedDiffError(
      "diff_parse_unknown_metadata",
      `Unsupported line in diff file section: ${line}.`,
      index + 1,
      file.path,
    )
  }
  if (current) files.push(finalizeFile(current, lines.length))
  if (!files.length) {
    throw new UnifiedDiffError(
      "diff_parse_empty",
      "Unified diff contains no file sections.",
    )
  }
  const seenPaths = new Set<string>()
  for (const file of files) {
    const key = file.path.toLowerCase()
    if (seenPaths.has(key)) {
      throw new UnifiedDiffError(
        "diff_parse_duplicate_file",
        `Unified diff repeats file ${file.path}.`,
        file.patchStart + 1,
        file.path,
      )
    }
    seenPaths.add(key)
  }
  const diffId = stableId(
    "diff",
    files.map((file) => ({
      path: file.path,
      previousPath: file.previousPath,
      kind: file.kind,
      hunks: file.hunks.map((hunk) => hunk.hunkId),
    })),
    patchBytes,
  )
  if (policy.expectedDiffId && policy.expectedDiffId !== diffId) {
    throw new UnifiedDiffError(
      "diff_parse_identity_mismatch",
      "Parsed diff identity does not match the expected receipt.",
      0,
      undefined,
      { expected: policy.expectedDiffId, actual: diffId },
    )
  }
  let additions = 0
  let deletions = 0
  for (const file of files) {
    additions += file.additions
    deletions += file.deletions
  }
  return Object.freeze({
    diffId,
    files: Object.freeze(files),
    additions,
    deletions,
    hunkCount,
    lineCount: lines.length,
    patchBytes,
    encoding: decoded.encoding,
    lineEnding: decoded.lineEnding,
    bom: decoded.bom,
  })
}

export interface PatchApplication {
  text: string
  appliedHunks: readonly string[]
  additions: number
  deletions: number
  noNewlineAtEnd: boolean
}

function textLines(value: string): { lines: string[]; finalNewline: boolean } {
  const normalized = normalizeLineEndings(value)
  const finalNewline = normalized.endsWith("\n")
  const lines = normalized.split("\n")
  if (finalNewline) lines.pop()
  return { lines, finalNewline }
}

function assertContext(
  actual: string | undefined,
  expected: string,
  hunk: DiffHunkContract,
  line: DiffLineContract,
): void {
  if (actual !== expected) {
    throw new UnifiedDiffError(
      "diff_apply_context_mismatch",
      `Patch context no longer matches at old line ${line.oldLine}.`,
      line.patchLine,
      undefined,
      {
        hunkId: hunk.hunkId,
        expected,
        actual,
        oldLine: line.oldLine,
      },
    )
  }
}

export function applyFileHunks(
  baseText: string,
  hunks: readonly DiffHunkContract[],
): PatchApplication {
  const source = textLines(baseText)
  const output: string[] = []
  const applied: string[] = []
  let sourceIndex = 0
  let additions = 0
  let deletions = 0
  let noNewlineAtEnd = !source.finalNewline
  let previousOldEnd = 0
  for (const hunk of hunks) {
    const hunkStart = hunk.oldStart === 0 ? 0 : hunk.oldStart - 1
    if (hunkStart < sourceIndex || hunk.oldStart < previousOldEnd) {
      throw new UnifiedDiffError(
        "diff_apply_hunk_overlap",
        "Cannot apply overlapping or out-of-order hunks.",
        hunk.patchStart + 1,
      )
    }
    while (sourceIndex < hunkStart) {
      output.push(source.lines[sourceIndex]!)
      sourceIndex += 1
    }
    for (const line of hunk.lines) {
      if (line.kind === "context") {
        assertContext(source.lines[sourceIndex], line.text, hunk, line)
        output.push(line.text)
        sourceIndex += 1
      } else if (line.kind === "deleted") {
        assertContext(source.lines[sourceIndex], line.text, hunk, line)
        sourceIndex += 1
        deletions += 1
      } else if (line.kind === "added") {
        output.push(line.text)
        additions += 1
      }
      if (line.noNewline) {
        // The marker describes the immediately preceding side. A deleted EOF
        // line belongs only to the old file, so replacing/removing it clears
        // the inherited no-newline state unless the new/context side carries
        // its own marker.
        noNewlineAtEnd = line.kind !== "deleted"
      }
    }
    previousOldEnd = hunk.oldStart + hunk.oldCount
    applied.push(hunk.hunkId)
  }
  while (sourceIndex < source.lines.length) {
    output.push(source.lines[sourceIndex]!)
    sourceIndex += 1
  }
  const text = output.join("\n") + (noNewlineAtEnd ? "" : "\n")
  return Object.freeze({
    text,
    appliedHunks: Object.freeze(applied),
    additions,
    deletions,
    noNewlineAtEnd,
  })
}

export function invertHunk(hunk: DiffHunkContract): DiffHunkContract {
  const lines = hunk.lines.map((line): DiffLineContract => {
    const kind: DiffLineKind =
      line.kind === "added"
        ? "deleted"
        : line.kind === "deleted"
          ? "added"
          : line.kind
    return Object.freeze({
      ...line,
      lineId: stableId("inverse-line", line.lineId),
      kind,
      oldLine: line.newLine,
      newLine: line.oldLine,
    })
  })
  return Object.freeze({
    ...hunk,
    hunkId: stableId("inverse-hunk", hunk.hunkId),
    header:
      `@@ -${hunk.newStart},${hunk.newCount}`
      + ` +${hunk.oldStart},${hunk.oldCount} @@`
      + (hunk.section ? ` ${hunk.section}` : ""),
    oldStart: hunk.newStart,
    oldCount: hunk.newCount,
    newStart: hunk.oldStart,
    newCount: hunk.oldCount,
    additions: hunk.deletions,
    deletions: hunk.additions,
    lines: Object.freeze(lines),
  })
}

export function invertFileHunks(
  hunks: readonly DiffHunkContract[],
): readonly DiffHunkContract[] {
  return Object.freeze(
    [...hunks]
      .reverse()
      .map(invertHunk)
      .map((hunk, index) => Object.freeze({ ...hunk, index })),
  )
}

export function compactHunkPreview(
  hunk: DiffHunkContract,
  options: { context?: number; maximumAddedRun?: number } = {},
): readonly string[] {
  const context = boundedOption(options.context, 2, 0, 100, "context")
  const maximumAddedRun = boundedOption(
    options.maximumAddedRun,
    8,
    1,
    1_000,
    "maximumAddedRun",
  )
  const changed = hunk.lines
    .map((line, index) => line.kind === "context" ? -1 : index)
    .filter((index) => index >= 0)
  if (!changed.length) return Object.freeze([])
  const included = new Set<number>()
  for (const index of changed) {
    for (
      let cursor = Math.max(0, index - context);
      cursor <= Math.min(hunk.lines.length - 1, index + context);
      cursor += 1
    ) {
      included.add(cursor)
    }
  }
  const result: string[] = []
  let previous = -2
  let addedRun = 0
  for (const index of [...included].sort((left, right) => left - right)) {
    if (index > previous + 1) result.push("…")
    const line = hunk.lines[index]!
    if (line.kind === "added") {
      addedRun += 1
      if (addedRun > maximumAddedRun) {
        if (addedRun === maximumAddedRun + 1) result.push("+…")
        previous = index
        continue
      }
    } else {
      addedRun = 0
    }
    const prefix =
      line.kind === "added"
        ? "+"
        : line.kind === "deleted"
          ? "-"
          : line.kind === "context"
            ? " "
            : "\\"
    const coordinate = line.newLine ?? line.oldLine ?? 0
    result.push(`${prefix}${coordinate}|${line.text}`)
    previous = index
  }
  return Object.freeze(result)
}
