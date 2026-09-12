import {
  canRequestInline,
  isActiveContentFamily,
  isMediaFamily,
  isTextualFamily,
  type ArtifactContract,
} from "./contracts.ts"
import type {
  ArtifactSearchMatch,
  ArtifactTextLine,
  ArtifactTextLineIndex,
} from "./content.ts"
import {
  assertArtifactMediaSafe,
  safeExternalLink,
  type ArtifactSafeText,
  type SafeExternalLink,
} from "./security.ts"

export type ArtifactViewerKind =
  | "text"
  | "markdown"
  | "json"
  | "image"
  | "audio"
  | "video"
  | "binary"
  | "metadata"
  | "refused"

export interface ArtifactViewerDecision {
  kind: ArtifactViewerKind
  allowed: boolean
  reason?: string
  capabilities: ArtifactViewerCapabilities
}

export interface ArtifactViewerCapabilities {
  search: boolean
  virtualRows: boolean
  zoom: boolean
  pan: boolean
  download: boolean
  structuralNavigation: boolean
  lineSelection: boolean
}

export interface ArtifactTextViewerModel {
  kind: "text"
  artifact: ArtifactContract
  lines: readonly ArtifactTextLine[]
  highlighted: ReadonlyMap<number, readonly ArtifactTextHighlight[]>
  encodingLabel: string
  lineEndingLabel: string
  partial: boolean
  quarantined: boolean
  notices: readonly string[]
}

export interface ArtifactTextHighlight {
  startColumn: number
  endColumn: number
  matchId: string
}

export type MarkdownBlock =
  | MarkdownHeadingBlock
  | MarkdownParagraphBlock
  | MarkdownCodeBlock
  | MarkdownListBlock
  | MarkdownQuoteBlock
  | MarkdownRuleBlock
  | MarkdownTableBlock
  | MarkdownNoticeBlock

export interface MarkdownHeadingBlock {
  kind: "heading"
  level: 1 | 2 | 3 | 4 | 5 | 6
  id: string
  children: readonly MarkdownInline[]
  sourceLine: number
}

export interface MarkdownParagraphBlock {
  kind: "paragraph"
  children: readonly MarkdownInline[]
  sourceLine: number
}

export interface MarkdownCodeBlock {
  kind: "code"
  language?: string
  text: string
  sourceLine: number
  closed: boolean
}

export interface MarkdownListBlock {
  kind: "list"
  ordered: boolean
  start: number
  items: readonly {
    checked?: boolean
    children: readonly MarkdownInline[]
    sourceLine: number
  }[]
  sourceLine: number
}

export interface MarkdownQuoteBlock {
  kind: "quote"
  children: readonly MarkdownInline[]
  sourceLine: number
}

export interface MarkdownRuleBlock {
  kind: "rule"
  sourceLine: number
}

export interface MarkdownTableBlock {
  kind: "table"
  header: readonly (readonly MarkdownInline[])[]
  alignments: readonly ("left" | "center" | "right" | undefined)[]
  rows: readonly (readonly (readonly MarkdownInline[])[])[]
  sourceLine: number
}

export interface MarkdownNoticeBlock {
  kind: "notice"
  tone: "warning" | "danger"
  text: string
  sourceLine: number
}

export type MarkdownInline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "emphasis"; children: readonly MarkdownInline[] }
  | { kind: "strong"; children: readonly MarkdownInline[] }
  | { kind: "strike"; children: readonly MarkdownInline[] }
  | { kind: "link"; link: SafeExternalLink; children: readonly MarkdownInline[] }
  | { kind: "break" }

export interface ArtifactMarkdownViewerModel {
  kind: "markdown"
  artifact: ArtifactContract
  blocks: readonly MarkdownBlock[]
  truncated: boolean
  quarantined: boolean
  notices: readonly string[]
}

export type JsonNodeKind =
  | "object"
  | "array"
  | "string"
  | "number"
  | "boolean"
  | "null"
  | "truncated"
  | "error"

export interface JsonTreeNode {
  id: string
  path: string
  key: string
  kind: JsonNodeKind
  depth: number
  value?: string | number | boolean | null
  childCount: number
  expandable: boolean
  expanded: boolean
  children: readonly JsonTreeNode[]
  sourceBytes?: number
}

export interface JsonFlatRow {
  node: JsonTreeNode
  index: number
  path: string
  depth: number
  selected: boolean
  matched: boolean
}

export interface ArtifactJsonViewerModel {
  kind: "json"
  artifact: ArtifactContract
  root: JsonTreeNode
  rows: readonly JsonFlatRow[]
  valid: boolean
  complete: boolean
  error?: string
  quarantined: boolean
}

export interface ArtifactBinaryRow {
  offset: number
  address: string
  bytes: readonly number[]
  hex: string
  ascii: string
  complete: boolean
}

export interface ArtifactBinaryViewerModel {
  kind: "binary"
  artifact: ArtifactContract
  rangeOffset: number
  rangeEndExclusive: number
  rows: readonly ArtifactBinaryRow[]
  bytesPerRow: number
  partial: boolean
  executableWarning: boolean
}

export interface ArtifactMediaViewerModel {
  kind: "image" | "audio" | "video"
  artifact: ArtifactContract
  sourceUrl: string
  alt: string
  autoplay: false
  controls: true
  zoom: number
  panX: number
  panY: number
  quarantined: boolean
}

export interface ArtifactMetadataViewerModel {
  kind: "metadata" | "refused"
  artifact: ArtifactContract
  reason: string
  facts: readonly { label: string; value: string }[]
}

const DEFAULT_CAPABILITIES: ArtifactViewerCapabilities = Object.freeze({
  search: false,
  virtualRows: false,
  zoom: false,
  pan: false,
  download: false,
  structuralNavigation: false,
  lineSelection: false,
})

const markdownMaximumBytes = 2 * 1024 * 1024
export const jsonStructuralMaximumBytes = 4 * 1024 * 1024
const maximumJsonDepth = 128
const maximumJsonNodes = 100_000

export function chooseArtifactViewer(
  artifact: ArtifactContract,
): ArtifactViewerDecision {
  if (artifact.security.label === "secret") {
    return viewerDecision(
      "refused",
      false,
      "Secret artifact content is metadata-only.",
    )
  }
  if (!artifact.status.exists) {
    return viewerDecision(
      "refused",
      false,
      "Artifact content is missing.",
    )
  }
  if (!artifact.status.isFile) {
    return viewerDecision(
      "refused",
      false,
      "Artifact reference does not resolve to a regular file.",
    )
  }
  if (
    artifact.status.integrity === "size_mismatch"
    || artifact.status.integrity === "digest_mismatch"
    || artifact.status.integrity === "revision_mismatch"
    || artifact.status.integrity === "path_refused"
  ) {
    return viewerDecision(
      "refused",
      false,
      `Artifact integrity failed: ${artifact.status.integrity}.`,
    )
  }
  if (artifact.contentFamily === "text") {
    return viewerDecision("text", true, undefined, {
      search: true,
      virtualRows: true,
      lineSelection: true,
      download: artifact.security.downloadPolicy !== "deny",
    })
  }
  if (artifact.contentFamily === "markdown") {
    return viewerDecision("markdown", true, undefined, {
      search: true,
      virtualRows: artifact.sizeBytes > markdownMaximumBytes,
      structuralNavigation: true,
      lineSelection: true,
      download: artifact.security.downloadPolicy !== "deny",
    })
  }
  if (artifact.contentFamily === "json") {
    return viewerDecision("json", true, undefined, {
      search: true,
      virtualRows: true,
      structuralNavigation: true,
      lineSelection: true,
      download: artifact.security.downloadPolicy !== "deny",
    })
  }
  if (artifact.contentFamily === "image") {
    try {
      assertArtifactMediaSafe(artifact)
      return viewerDecision("image", true, undefined, {
        zoom: true,
        pan: true,
        download: artifact.security.downloadPolicy !== "deny",
      })
    } catch (error) {
      return viewerDecision(
        "metadata",
        false,
        error instanceof Error ? error.message : String(error),
      )
    }
  }
  if (artifact.contentFamily === "audio") {
    try {
      assertArtifactMediaSafe(artifact)
      return viewerDecision("audio", true, undefined, {
        download: artifact.security.downloadPolicy !== "deny",
      })
    } catch (error) {
      return viewerDecision(
        "metadata",
        false,
        error instanceof Error ? error.message : String(error),
      )
    }
  }
  if (artifact.contentFamily === "video") {
    try {
      assertArtifactMediaSafe(artifact)
      return viewerDecision("video", true, undefined, {
        download: artifact.security.downloadPolicy !== "deny",
      })
    } catch (error) {
      return viewerDecision(
        "metadata",
        false,
        error instanceof Error ? error.message : String(error),
      )
    }
  }
  if (
    artifact.contentFamily === "binary"
    || artifact.contentFamily === "document"
    || artifact.contentFamily === "archive"
    || artifact.contentFamily === "html"
    || artifact.contentFamily === "svg"
    || artifact.contentFamily === "executable"
  ) {
    return viewerDecision(
      "binary",
      canRequestInline(artifact),
      isActiveContentFamily(artifact.contentFamily)
        ? `Active ${artifact.contentFamily} content is shown only as bytes.`
        : undefined,
      {
        search: false,
        virtualRows: true,
        download:
          !artifact.status.executableRisk
          && artifact.security.downloadPolicy !== "deny",
      },
    )
  }
  return viewerDecision(
    "metadata",
    false,
    "No safe artifact viewer is registered for this content.",
  )
}

export function buildTextViewerModel(input: {
  artifact: ArtifactContract
  index: ArtifactTextLineIndex
  matches?: readonly ArtifactSearchMatch[]
  partial: boolean
  safeRanges: readonly ArtifactSafeText[]
}): ArtifactTextViewerModel {
  const highlighted = new Map<number, ArtifactTextHighlight[]>()
  for (const match of input.matches ?? []) {
    const list = highlighted.get(match.lineNumber) ?? []
    list.push(
      Object.freeze({
        startColumn: match.startColumn,
        endColumn: match.endColumn,
        matchId: match.id,
      }),
    )
    highlighted.set(match.lineNumber, list)
  }
  for (const list of highlighted.values()) {
    list.sort(
      (left, right) =>
        left.startColumn - right.startColumn
        || left.endColumn - right.endColumn,
    )
  }
  const notices = securityNotices(input.safeRanges)
  return Object.freeze({
    kind: "text",
    artifact: input.artifact,
    lines: input.index.lines(),
    highlighted,
    encodingLabel: encodingLabel(input.artifact),
    lineEndingLabel: lineEndingLabel(input.artifact),
    partial: input.partial,
    quarantined: input.safeRanges.some((range) => range.quarantined),
    notices,
  })
}

export function buildMarkdownViewerModel(input: {
  artifact: ArtifactContract
  text: ArtifactSafeText
  complete: boolean
}): ArtifactMarkdownViewerModel {
  const bytes = new TextEncoder().encode(input.text.text).byteLength
  if (bytes > markdownMaximumBytes) {
    return Object.freeze({
      kind: "markdown",
      artifact: input.artifact,
      blocks: Object.freeze([
        Object.freeze({
          kind: "notice",
          tone: "warning",
          text:
            "Markdown exceeds the structural rendering bound. "
            + "Use the virtual text viewer for bounded navigation.",
          sourceLine: 1,
        }),
      ]),
      truncated: true,
      quarantined: input.text.quarantined,
      notices: securityNotices([input.text]),
    })
  }
  const blocks = parseSafeMarkdown(input.text.text, {
    quarantined: input.text.quarantined,
  })
  return Object.freeze({
    kind: "markdown",
    artifact: input.artifact,
    blocks,
    truncated: !input.complete,
    quarantined: input.text.quarantined,
    notices: securityNotices([input.text]),
  })
}

export function parseSafeMarkdown(
  value: string,
  options: { quarantined?: boolean } = {},
): readonly MarkdownBlock[] {
  const text = normalizeLineEndings(value)
  const lines = text.split("\n")
  const blocks: MarkdownBlock[] = []
  let index = 0
  while (index < lines.length && blocks.length < 50_000) {
    const line = lines[index] ?? ""
    const sourceLine = index + 1
    if (!line.trim()) {
      index += 1
      continue
    }
    if (/^\s*<[\s\S]*>/.test(line)) {
      blocks.push(
        Object.freeze({
          kind: "notice",
          tone: "danger",
          text: "Raw HTML was refused by the artifact Markdown viewer.",
          sourceLine,
        }),
      )
      index += 1
      continue
    }
    const fence = /^\s*(```+|~~~+)\s*([A-Za-z0-9_+.-]*)\s*$/.exec(line)
    if (fence) {
      const marker = fence[1]!
      const content: string[] = []
      index += 1
      let closed = false
      while (index < lines.length) {
        const candidate = lines[index] ?? ""
        if (new RegExp(`^\\s*${escapeRegExp(marker[0]!)}{${marker.length},}\\s*$`).test(candidate)) {
          closed = true
          index += 1
          break
        }
        content.push(candidate)
        index += 1
      }
      blocks.push(
        Object.freeze({
          kind: "code",
          language: fence[2] || undefined,
          text: content.join("\n"),
          sourceLine,
          closed,
        }),
      )
      continue
    }
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line)
    if (heading) {
      const level = heading[1]!.length as 1 | 2 | 3 | 4 | 5 | 6
      const headingText = heading[2]!
      blocks.push(
        Object.freeze({
          kind: "heading",
          level,
          id: headingId(headingText, sourceLine),
          children: parseMarkdownInline(headingText),
          sourceLine,
        }),
      )
      index += 1
      continue
    }
    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
      blocks.push(Object.freeze({ kind: "rule", sourceLine }))
      index += 1
      continue
    }
    if (/^\s*>/.test(line)) {
      const quoteLines: string[] = []
      while (index < lines.length && /^\s*>/.test(lines[index] ?? "")) {
        quoteLines.push((lines[index] ?? "").replace(/^\s*>\s?/, ""))
        index += 1
      }
      blocks.push(
        Object.freeze({
          kind: "quote",
          children: parseMarkdownInline(quoteLines.join("\n")),
          sourceLine,
        }),
      )
      continue
    }
    const listMatch = /^\s*(?:(\d+)[.)]|[-+*])\s+(.+)$/.exec(line)
    if (listMatch) {
      const ordered = listMatch[1] !== undefined
      const start = ordered ? Number(listMatch[1]) : 1
      const items: {
        checked?: boolean
        children: readonly MarkdownInline[]
        sourceLine: number
      }[] = []
      while (index < lines.length) {
        const candidate = lines[index] ?? ""
        const match = /^\s*(?:(\d+)[.)]|[-+*])\s+(.+)$/.exec(candidate)
        if (!match || (match[1] !== undefined) !== ordered) break
        let body = match[2]!
        let checked: boolean | undefined
        const task = /^\[([ xX])]\s+(.+)$/.exec(body)
        if (task) {
          checked = task[1]!.toLowerCase() === "x"
          body = task[2]!
        }
        items.push(
          Object.freeze({
            checked,
            children: parseMarkdownInline(body),
            sourceLine: index + 1,
          }),
        )
        index += 1
      }
      blocks.push(
        Object.freeze({
          kind: "list",
          ordered,
          start,
          items: Object.freeze(items),
          sourceLine,
        }),
      )
      continue
    }
    if (
      index + 1 < lines.length
      && line.includes("|")
      && isMarkdownTableDelimiter(lines[index + 1] ?? "")
    ) {
      const header = splitMarkdownTableRow(line)
      const delimiters = splitMarkdownTableRow(lines[index + 1] ?? "")
      const alignments = delimiters.map(tableAlignment)
      const rows: (readonly (readonly MarkdownInline[])[])[] = []
      index += 2
      while (
        index < lines.length
        && (lines[index] ?? "").includes("|")
        && (lines[index] ?? "").trim()
      ) {
        rows.push(
          Object.freeze(
            splitMarkdownTableRow(lines[index] ?? "").map((cell) =>
              parseMarkdownInline(cell),
            ),
          ),
        )
        index += 1
      }
      blocks.push(
        Object.freeze({
          kind: "table",
          header: Object.freeze(header.map((cell) => parseMarkdownInline(cell))),
          alignments: Object.freeze(alignments),
          rows: Object.freeze(rows),
          sourceLine,
        }),
      )
      continue
    }
    const paragraph: string[] = [line]
    index += 1
    while (index < lines.length) {
      const candidate = lines[index] ?? ""
      if (!candidate.trim() || startsMarkdownBlock(candidate)) break
      paragraph.push(candidate)
      index += 1
    }
    blocks.push(
      Object.freeze({
        kind: "paragraph",
        children: parseMarkdownInline(paragraph.join("\n")),
        sourceLine,
      }),
    )
  }
  if (options.quarantined) {
    blocks.unshift(
      Object.freeze({
        kind: "notice",
        tone: "warning",
        text:
          "This untrusted Markdown is rendered as inert data. "
          + "Links require an explicit click; raw HTML and embedded media are disabled.",
        sourceLine: 1,
      }),
    )
  }
  return Object.freeze(blocks)
}

export function parseMarkdownInline(value: string): readonly MarkdownInline[] {
  const result: MarkdownInline[] = []
  let cursor = 0
  const expression =
    /(`+)([\s\S]*?)\1|\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|(\*\*|(?<![\p{L}\p{N}_])__)([\s\S]+?)\5|(~~)([\s\S]+?)\7|(\*|(?<![\p{L}\p{N}_])_)([^*_][\s\S]*?)\9|(\n)/gu
  for (const match of value.matchAll(expression)) {
    if (match.index === undefined) continue
    if (match.index > cursor) {
      result.push(Object.freeze({ kind: "text", text: value.slice(cursor, match.index) }))
    }
    if (match[1]) {
      result.push(Object.freeze({ kind: "code", text: match[2] ?? "" }))
    } else if (match[3] !== undefined && match[4]) {
      result.push(
        Object.freeze({
          kind: "link",
          link: safeExternalLink(match[4]),
          children: parseMarkdownInline(match[3]),
        }),
      )
    } else if (match[5]) {
      result.push(
        Object.freeze({
          kind: "strong",
          children: parseMarkdownInline(match[6] ?? ""),
        }),
      )
    } else if (match[7]) {
      result.push(
        Object.freeze({
          kind: "strike",
          children: parseMarkdownInline(match[8] ?? ""),
        }),
      )
    } else if (match[9]) {
      result.push(
        Object.freeze({
          kind: "emphasis",
          children: parseMarkdownInline(match[10] ?? ""),
        }),
      )
    } else if (match[11]) {
      result.push(Object.freeze({ kind: "break" }))
    }
    cursor = match.index + match[0].length
  }
  if (cursor < value.length) {
    result.push(Object.freeze({ kind: "text", text: value.slice(cursor) }))
  }
  return Object.freeze(result)
}

export function buildJsonViewerModel(input: {
  artifact: ArtifactContract
  text: ArtifactSafeText
  complete: boolean
  expandedPaths?: ReadonlySet<string>
  search?: string
  selectedPath?: string
}): ArtifactJsonViewerModel {
  const bytes = new TextEncoder().encode(input.text.text).byteLength
  if (!input.complete || bytes > jsonStructuralMaximumBytes) {
    const root = Object.freeze({
      id: "$",
      path: "$",
      key: "$",
      kind: "truncated" as const,
      depth: 0,
      value:
        bytes > jsonStructuralMaximumBytes
          ? "JSON exceeds the structural parse bound; use virtual raw text."
          : `JSON is only partially loaded (${bytes.toLocaleString()} of `
            + `${input.artifact.sizeBytes.toLocaleString()} bytes); load the `
            + "remaining bytes before structural parsing.",
      childCount: 0,
      expandable: false,
      expanded: false,
      children: Object.freeze([]),
      sourceBytes: bytes,
    })
    return Object.freeze({
      kind: "json",
      artifact: input.artifact,
      root,
      rows: Object.freeze([
        Object.freeze({
          node: root,
          index: 0,
          path: "$",
          depth: 0,
          selected: input.selectedPath === "$",
          matched: false,
        }),
      ]),
      valid: false,
      complete: input.complete,
      error: String(root.value),
      quarantined: input.text.quarantined,
    })
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(input.text.text)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    const root = Object.freeze({
      id: "$",
      path: "$",
      key: "$",
      kind: "error" as const,
      depth: 0,
      value: message,
      childCount: 0,
      expandable: false,
      expanded: false,
      children: Object.freeze([]),
      sourceBytes: bytes,
    })
    return Object.freeze({
      kind: "json",
      artifact: input.artifact,
      root,
      rows: Object.freeze([
        Object.freeze({
          node: root,
          index: 0,
          path: "$",
          depth: 0,
          selected: input.selectedPath === "$",
          matched: false,
        }),
      ]),
      valid: false,
      complete: true,
      error: message,
      quarantined: input.text.quarantined,
    })
  }
  const counter = { value: 0 }
  const expanded = input.expandedPaths ?? new Set(["$"])
  const root = buildJsonNode(parsed, "$", "$", 0, expanded, counter)
  const rows = flattenJsonTree(root, {
    selectedPath: input.selectedPath,
    search: input.search,
  })
  return Object.freeze({
    kind: "json",
    artifact: input.artifact,
    root,
    rows,
    valid: true,
    complete: true,
    quarantined: input.text.quarantined,
  })
}

export function buildJsonNode(
  value: unknown,
  key: string,
  path: string,
  depth: number,
  expandedPaths: ReadonlySet<string>,
  counter: { value: number },
): JsonTreeNode {
  counter.value += 1
  if (counter.value > maximumJsonNodes) {
    return Object.freeze({
      id: `${path}:truncated`,
      path,
      key,
      kind: "truncated",
      depth,
      value: "Maximum JSON node count reached.",
      childCount: 0,
      expandable: false,
      expanded: false,
      children: Object.freeze([]),
    })
  }
  if (depth > maximumJsonDepth) {
    return Object.freeze({
      id: `${path}:depth`,
      path,
      key,
      kind: "truncated",
      depth,
      value: "Maximum JSON depth reached.",
      childCount: 0,
      expandable: false,
      expanded: false,
      children: Object.freeze([]),
    })
  }
  if (value === null) {
    return primitiveJsonNode("null", null, key, path, depth)
  }
  if (typeof value === "string") {
    return primitiveJsonNode("string", value, key, path, depth)
  }
  if (typeof value === "number") {
    return primitiveJsonNode("number", value, key, path, depth)
  }
  if (typeof value === "boolean") {
    return primitiveJsonNode("boolean", value, key, path, depth)
  }
  if (Array.isArray(value)) {
    const expanded = expandedPaths.has(path)
    const children = expanded
      ? value.map((item, index) =>
          buildJsonNode(
            item,
            String(index),
            `${path}[${index}]`,
            depth + 1,
            expandedPaths,
            counter,
          ),
        )
      : []
    return Object.freeze({
      id: path,
      path,
      key,
      kind: "array",
      depth,
      childCount: value.length,
      expandable: value.length > 0,
      expanded,
      children: Object.freeze(children),
    })
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
    const expanded = expandedPaths.has(path)
    const children = expanded
      ? entries.map(([childKey, child]) =>
          buildJsonNode(
            child,
            childKey,
            jsonChildPath(path, childKey),
            depth + 1,
            expandedPaths,
            counter,
          ),
        )
      : []
    return Object.freeze({
      id: path,
      path,
      key,
      kind: "object",
      depth,
      childCount: entries.length,
      expandable: entries.length > 0,
      expanded,
      children: Object.freeze(children),
    })
  }
  return Object.freeze({
    id: `${path}:unsupported`,
    path,
    key,
    kind: "error",
    depth,
    value: `Unsupported JSON value type: ${typeof value}`,
    childCount: 0,
    expandable: false,
    expanded: false,
    children: Object.freeze([]),
  })
}

export function flattenJsonTree(
  root: JsonTreeNode,
  options: { selectedPath?: string; search?: string } = {},
): readonly JsonFlatRow[] {
  const rows: JsonFlatRow[] = []
  const query = String(options.search || "").trim().toLocaleLowerCase()
  const visit = (node: JsonTreeNode) => {
    const searchable = `${node.path}\n${node.key}\n${String(node.value ?? "")}`.toLocaleLowerCase()
    rows.push(
      Object.freeze({
        node,
        index: rows.length,
        path: node.path,
        depth: node.depth,
        selected: options.selectedPath === node.path,
        matched: Boolean(query && searchable.includes(query)),
      }),
    )
    if (node.expanded) node.children.forEach(visit)
  }
  visit(root)
  return Object.freeze(rows)
}

export function buildBinaryViewerModel(input: {
  artifact: ArtifactContract
  bytes: Uint8Array
  offset: number
  complete: boolean
  bytesPerRow?: number
}): ArtifactBinaryViewerModel {
  const bytesPerRow = clampInteger(input.bytesPerRow ?? 16, 8, 64)
  const rows: ArtifactBinaryRow[] = []
  for (let index = 0; index < input.bytes.length; index += bytesPerRow) {
    const selected = input.bytes.slice(index, index + bytesPerRow)
    const absolute = input.offset + index
    rows.push(
      Object.freeze({
        offset: absolute,
        address: absolute.toString(16).padStart(8, "0"),
        bytes: Object.freeze([...selected]),
        hex: [...selected]
          .map((value) => value.toString(16).padStart(2, "0"))
          .join(" "),
        ascii: [...selected]
          .map((value) => (value >= 32 && value <= 126 ? String.fromCharCode(value) : "·"))
          .join(""),
        complete: selected.length === bytesPerRow || input.complete,
      }),
    )
  }
  return Object.freeze({
    kind: "binary",
    artifact: input.artifact,
    rangeOffset: input.offset,
    rangeEndExclusive: input.offset + input.bytes.length,
    rows: Object.freeze(rows),
    bytesPerRow,
    partial:
      input.offset > 0
      || input.offset + input.bytes.length < input.artifact.sizeBytes,
    executableWarning:
      input.artifact.status.executableRisk
      || isActiveContentFamily(input.artifact.contentFamily),
  })
}

export function buildMediaViewerModel(input: {
  artifact: ArtifactContract
  sourceUrl: string
  zoom?: number
  panX?: number
  panY?: number
}): ArtifactMediaViewerModel {
  assertArtifactMediaSafe(input.artifact)
  if (!isMediaFamily(input.artifact.contentFamily)) {
    throw new ArtifactViewerError(
      "media_family_invalid",
      "Artifact does not belong to a media viewer family.",
    )
  }
  const kind: "image" | "audio" | "video" =
    input.artifact.contentFamily === "image"
      ? "image"
      : input.artifact.contentFamily === "audio"
        ? "audio"
        : "video"
  const sourceUrl = safeMediaSource(input.sourceUrl)
  return Object.freeze({
    kind,
    artifact: input.artifact,
    sourceUrl,
    alt: input.artifact.title || input.artifact.artifactId,
    autoplay: false,
    controls: true,
    zoom: clampNumber(input.zoom ?? 1, 0.1, 16),
    panX: clampNumber(input.panX ?? 0, -100_000, 100_000),
    panY: clampNumber(input.panY ?? 0, -100_000, 100_000),
    quarantined: input.artifact.security.trust !== "trusted",
  })
}

export function buildMetadataViewerModel(
  artifact: ArtifactContract,
  reason?: string,
): ArtifactMetadataViewerModel {
  const decision = chooseArtifactViewer(artifact)
  return Object.freeze({
    kind: decision.kind === "refused" ? "refused" : "metadata",
    artifact,
    reason: reason ?? decision.reason ?? "Metadata-only artifact.",
    facts: Object.freeze([
      { label: "Artifact", value: artifact.artifactId },
      { label: "Revision", value: artifact.revision },
      { label: "SHA-256", value: artifact.sha256 },
      { label: "Size", value: `${artifact.sizeBytes.toLocaleString()} bytes` },
      { label: "Media type", value: artifact.mediaType },
      { label: "Family", value: artifact.contentFamily },
      { label: "Encoding", value: artifact.encoding ?? "binary / unspecified" },
      { label: "Integrity", value: artifact.status.integrity },
      { label: "Security", value: artifact.security.label },
      { label: "Trust", value: artifact.security.trust },
      { label: "Producer node", value: artifact.producer.nodeId ?? "unknown" },
      { label: "Producer worker", value: artifact.producer.workerId ?? "unknown" },
      { label: "Producer span", value: artifact.producer.spanId ?? "unknown" },
      { label: "Producer tool call", value: artifact.producer.toolCallId ?? "unknown" },
      { label: "Retention", value: artifact.retention.policy },
    ].map((fact) => Object.freeze(fact))),
  })
}

export class ArtifactViewerRegistry {
  readonly #decisions = new Map<string, ArtifactViewerDecision>()
  #disabled = false

  resolve(artifact: ArtifactContract): ArtifactViewerDecision {
    if (this.#disabled) {
      throw new ArtifactViewerError(
        "viewer_registry_disabled",
        "Artifact viewer registry is disabled.",
      )
    }
    const key = `${artifact.artifactId}@${artifact.revision}`
    const existing = this.#decisions.get(key)
    if (existing) return existing
    const decision = chooseArtifactViewer(artifact)
    this.#decisions.set(key, decision)
    return decision
  }

  invalidate(artifactId?: string): void {
    if (!artifactId) {
      this.#decisions.clear()
      return
    }
    for (const key of [...this.#decisions.keys()]) {
      if (key.startsWith(`${artifactId}@`)) this.#decisions.delete(key)
    }
  }

  disable(): void {
    this.#disabled = true
    this.#decisions.clear()
  }

  snapshot(): {
    cachedDecisions: number
    disabled: boolean
    kinds: Readonly<Record<ArtifactViewerKind, number>>
  } {
    const kinds: Record<ArtifactViewerKind, number> = {
      text: 0,
      markdown: 0,
      json: 0,
      image: 0,
      audio: 0,
      video: 0,
      binary: 0,
      metadata: 0,
      refused: 0,
    }
    for (const decision of this.#decisions.values()) kinds[decision.kind] += 1
    return Object.freeze({
      cachedDecisions: this.#decisions.size,
      disabled: this.#disabled,
      kinds: Object.freeze(kinds),
    })
  }
}

export class ArtifactViewerError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactViewerError"
    this.code = code
  }
}

function viewerDecision(
  kind: ArtifactViewerKind,
  allowed: boolean,
  reason?: string,
  capabilities: Partial<ArtifactViewerCapabilities> = {},
): ArtifactViewerDecision {
  return Object.freeze({
    kind,
    allowed,
    reason,
    capabilities: Object.freeze({
      ...DEFAULT_CAPABILITIES,
      ...capabilities,
    }),
  })
}

function primitiveJsonNode(
  kind: "string" | "number" | "boolean" | "null",
  value: string | number | boolean | null,
  key: string,
  path: string,
  depth: number,
): JsonTreeNode {
  return Object.freeze({
    id: path,
    path,
    key,
    kind,
    depth,
    value,
    childCount: 0,
    expandable: false,
    expanded: false,
    children: Object.freeze([]),
  })
}

function jsonChildPath(parent: string, key: string): string {
  if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)) return `${parent}.${key}`
  return `${parent}[${JSON.stringify(key)}]`
}

function securityNotices(
  safeRanges: readonly ArtifactSafeText[],
): readonly string[] {
  const notices = new Set<string>()
  for (const range of safeRanges) {
    if (range.serverRedacted) notices.add("Server credential redaction applied.")
    if (range.clientRedacted) notices.add("Browser credential redaction applied.")
    if (range.serverPromptFindings.length) {
      notices.add("Server prompt-injection quarantine applied.")
    }
    if (range.promptFindings.length) {
      notices.add("Browser prompt-injection quarantine applied.")
    }
    if (range.quarantined) {
      notices.add("Content is inert, untrusted data and cannot invoke actions.")
    }
  }
  return Object.freeze([...notices])
}

function encodingLabel(artifact: ArtifactContract): string {
  const encoding = artifact.encoding ?? "unknown / binary"
  const bom = artifact.byteOrderMark ? ` · BOM ${artifact.byteOrderMark}` : ""
  return `${encoding}${bom}`
}

function lineEndingLabel(artifact: ArtifactContract): string {
  if (!artifact.lineEndings.length) return "no detected line endings"
  if (artifact.lineEndings.length === 1) return artifact.lineEndings[0]!.toUpperCase()
  return `mixed ${artifact.lineEndings.map((value) => value.toUpperCase()).join(" / ")}`
}

function startsMarkdownBlock(value: string): boolean {
  return (
    /^\s*(?:#{1,6}\s|```|~~~|>|(?:\d+[.)]|[-+*])\s|(?:---+|\*\*\*+|___+)\s*$)/.test(value)
    || /^\s*</.test(value)
  )
}

function isMarkdownTableDelimiter(value: string): boolean {
  const cells = splitMarkdownTableRow(value)
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
}

function splitMarkdownTableRow(value: string): string[] {
  let selected = value.trim()
  if (selected.startsWith("|")) selected = selected.slice(1)
  if (selected.endsWith("|")) selected = selected.slice(0, -1)
  const cells: string[] = []
  let current = ""
  let escaped = false
  for (const character of selected) {
    if (escaped) {
      current += character
      escaped = false
      continue
    }
    if (character === "\\") {
      escaped = true
      continue
    }
    if (character === "|") {
      cells.push(current.trim())
      current = ""
      continue
    }
    current += character
  }
  cells.push(current.trim())
  return cells
}

function tableAlignment(
  value: string,
): "left" | "center" | "right" | undefined {
  const selected = value.trim()
  if (selected.startsWith(":") && selected.endsWith(":")) return "center"
  if (selected.endsWith(":")) return "right"
  if (selected.startsWith(":")) return "left"
  return undefined
}

function headingId(value: string, sourceLine: number): string {
  const selected = value
    .toLocaleLowerCase()
    .replace(/[^a-z0-9\u00c0-\u024f\u4e00-\u9fff]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return selected ? `${selected}-${sourceLine}` : `heading-${sourceLine}`
}

function safeMediaSource(value: string): string {
  const selected = String(value || "").trim()
  if (selected.startsWith("blob:")) return selected
  if (selected.startsWith("/") && !selected.startsWith("//") && !selected.includes("\\")) {
    if (selected.split("/").some((segment) => segment === "." || segment === "..")) {
      throw new ArtifactViewerError(
        "media_source_invalid",
        "Artifact media source path is unsafe.",
      )
    }
    return selected
  }
  throw new ArtifactViewerError(
    "media_source_invalid",
    "Artifact media source must be a managed blob or root-relative URL.",
  )
}

function normalizeLineEndings(value: string): string {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n")
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function clampNumber(value: number, minimum: number, maximum: number): number {
  return Math.max(
    minimum,
    Math.min(maximum, Number.isFinite(value) ? value : minimum),
  )
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  return Math.floor(clampNumber(value, minimum, maximum))
}
