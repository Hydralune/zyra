function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} is not an object`)
  return value as Record<string, unknown>
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value || value.length > 10_000) throw new TypeError(`${label} is invalid`)
  return value
}

function contentText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length > 10_000) throw new TypeError(`${label} is invalid`)
  return value
}

function integer(value: unknown, fallback = 0): number {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : fallback
}

export interface ProductDiffFile {
  fileId: string
  path: string
  previousPath?: string
  kind: string
  binary: boolean
  oversized: boolean
  truncated: boolean
  additions: number
  deletions: number
  hunkCount: number
  pageCount: number
}

export interface ProductDiffManifest {
  diffId: string
  artifactRevision: string
  files: readonly ProductDiffFile[]
}

export interface ProductDiffPage {
  pageIndex: number
  pageCount: number
  lines: readonly string[]
}

export function parseProductDiffManifest(value: unknown): ProductDiffManifest {
  const source = record(value, "diff manifest")
  if (source.schema !== "zyra.diff-review-manifest.v1" || source.physical_path_disclosed !== false) {
    throw new TypeError("diff manifest failed its schema or path-disclosure contract")
  }
  const binding = record(source.source, "diff source")
  const files = Array.isArray(source.files) ? source.files.map((item, index) => {
    const file = record(item, `diff file ${index}`)
    return Object.freeze({
      fileId: text(file.file_id ?? file.fileId, "diff file id"),
      path: text(file.path, "diff file path"),
      previousPath: typeof (file.previous_path ?? file.previousPath) === "string" ? String(file.previous_path ?? file.previousPath) : undefined,
      kind: text(file.kind, "diff file kind"),
      binary: file.binary === true,
      oversized: file.oversized === true,
      truncated: file.truncated === true,
      additions: integer(file.additions),
      deletions: integer(file.deletions),
      hunkCount: integer(file.hunk_count ?? file.hunkCount),
      pageCount: Math.max(1, integer(file.page_count ?? file.pageCount, 1)),
    })
  }) : []
  return Object.freeze({
    diffId: text(source.diff_id ?? source.diffId, "diff id"),
    artifactRevision: text(binding.artifact_revision ?? binding.artifactRevision, "artifact revision"),
    files: Object.freeze(files),
  })
}

export function parseProductDiffPage(value: unknown): ProductDiffPage {
  const source = record(value, "diff page")
  if (source.schema !== "zyra.diff-review-page.v1" || source.physical_path_disclosed !== false) {
    throw new TypeError("diff page failed its schema or path-disclosure contract")
  }
  const output: string[] = []
  const hunks = Array.isArray(source.hunks) ? source.hunks : []
  for (const [index, item] of hunks.entries()) {
    const hunk = record(item, `diff hunk ${index}`)
    output.push(text(hunk.header, "diff hunk header"))
    for (const lineItem of Array.isArray(hunk.lines) ? hunk.lines : []) {
      const line = record(lineItem, "diff line")
      const kind = String(line.kind ?? "context")
      const marker = kind === "added" ? "+" : kind === "deleted" ? "-" : kind === "notice" ? "\\" : " "
      output.push(`${marker}${contentText(line.text ?? "", "diff line text")}`)
    }
  }
  return Object.freeze({
    pageIndex: integer(source.page_index ?? source.pageIndex),
    pageCount: Math.max(1, integer(source.page_count ?? source.pageCount, 1)),
    lines: Object.freeze(output),
  })
}
