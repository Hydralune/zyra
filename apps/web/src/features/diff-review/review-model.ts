import type {
  DiffFileContract,
  DiffHunkContract,
  DiffLayoutMode,
  DiffReviewComment,
  DiffReviewLineSelection,
  DiffReviewReceipt,
  DiffViewMode,
} from "./contracts.ts"
import { selectionFromRows } from "./virtualization.ts"

export interface DiffReviewPreferences {
  layout: DiffLayoutMode
  viewMode: DiffViewMode
  sidebarOpen: boolean
  sidebarWidth: number
  expandedFiles: readonly string[]
  collapsedHunks: readonly string[]
  expandedFolds: readonly string[]
}

export interface DiffReviewModelState {
  diffId: string
  taskId: string
  files: readonly DiffFileContract[]
  filteredFiles: readonly DiffFileContract[]
  activeFileId?: string
  focusedRowId?: string
  filter: string
  preferences: DiffReviewPreferences
  selection?: DiffReviewLineSelection
  comments: readonly DiffReviewComment[]
  commentDraft: string
  commentBusy: boolean
  reviewRevision: number
  receipts: readonly DiffReviewReceipt[]
  selectedFileIds: readonly string[]
  dirty: boolean
  generation: number
  updatedAt: number
}

export interface DiffReviewPersistence {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export type DiffReviewModelListener = (state: DiffReviewModelState) => void

export class DiffReviewModelError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffReviewModelError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

const defaultPreferences: DiffReviewPreferences = Object.freeze({
  layout: "unified",
  viewMode: "diff",
  sidebarOpen: true,
  sidebarWidth: 320,
  expandedFiles: Object.freeze([]),
  collapsedHunks: Object.freeze([]),
  expandedFolds: Object.freeze([]),
})

function cleanIdentity(value: string, name: string): string {
  const selected = String(value || "").trim()
  if (!selected || selected.length > 512 || /[\u0000\r\n]/.test(selected)) {
    throw new DiffReviewModelError(
      "diff_review_identity_invalid",
      `${name} is not a valid identity.`,
    )
  }
  return selected
}

function uniqueIdentities(values: readonly string[]): readonly string[] {
  const result: string[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const selected = String(value || "").trim()
    if (!selected || seen.has(selected) || selected.length > 512) continue
    seen.add(selected)
    result.push(selected)
  }
  return Object.freeze(result)
}

function boundedWidth(value: number): number {
  if (!Number.isFinite(value)) return defaultPreferences.sidebarWidth
  return Math.min(720, Math.max(220, Math.round(value)))
}

function parsePreferences(value: unknown): DiffReviewPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return defaultPreferences
  }
  const source = value as Record<string, unknown>
  const layout =
    source.layout === "split" || source.layout === "unified"
      ? source.layout
      : defaultPreferences.layout
  const viewMode =
    source.viewMode === "old"
    || source.viewMode === "new"
    || source.viewMode === "diff"
      ? source.viewMode
      : defaultPreferences.viewMode
  return Object.freeze({
    layout,
    viewMode,
    sidebarOpen:
      typeof source.sidebarOpen === "boolean"
        ? source.sidebarOpen
        : defaultPreferences.sidebarOpen,
    sidebarWidth: boundedWidth(Number(source.sidebarWidth)),
    expandedFiles: uniqueIdentities(
      Array.isArray(source.expandedFiles)
        ? source.expandedFiles.map(String)
        : [],
    ),
    collapsedHunks: uniqueIdentities(
      Array.isArray(source.collapsedHunks)
        ? source.collapsedHunks.map(String)
        : [],
    ),
    expandedFolds: uniqueIdentities(
      Array.isArray(source.expandedFolds)
        ? source.expandedFolds.map(String)
        : [],
    ),
  })
}

function persistenceKey(taskId: string): string {
  return `zyra.diff-review.preferences.v1:${taskId}`
}

function browserPersistence(): DiffReviewPersistence | undefined {
  if (typeof window === "undefined" || !window.localStorage) return undefined
  return window.localStorage
}

function loadPreferences(
  persistence: DiffReviewPersistence | undefined,
  taskId: string,
): DiffReviewPreferences {
  if (!persistence) return defaultPreferences
  try {
    const value = persistence.getItem(persistenceKey(taskId))
    if (!value) return defaultPreferences
    return parsePreferences(JSON.parse(value))
  } catch {
    persistence.removeItem(persistenceKey(taskId))
    return defaultPreferences
  }
}

function filterFiles(
  files: readonly DiffFileContract[],
  filterValue: string,
): readonly DiffFileContract[] {
  const query = filterValue.trim().toLowerCase()
  if (!query) return Object.freeze([...files])
  const terms = query.split(/\s+/g).filter(Boolean)
  return Object.freeze(
    files.filter((file) => {
      const searchable = [
        file.path,
        file.previousPath ?? "",
        file.kind,
        file.language,
        file.mimeType,
        ...file.risk.reasonCodes,
      ].join(" ").toLowerCase()
      return terms.every((term) => searchable.includes(term))
    }),
  )
}

function initialFileId(
  files: readonly DiffFileContract[],
  filtered: readonly DiffFileContract[],
  requested?: string,
): string | undefined {
  if (requested && files.some((file) => file.fileId === requested)) {
    return requested
  }
  return filtered[0]?.fileId ?? files[0]?.fileId
}

function sameSelection(
  left: DiffReviewLineSelection | undefined,
  right: DiffReviewLineSelection | undefined,
): boolean {
  if (!left || !right) return left === right
  return (
    left.fileId === right.fileId
    && left.hunkId === right.hunkId
    && left.side === right.side
    && left.startLine === right.startLine
    && left.endLine === right.endLine
    && left.anchorLineIds.length === right.anchorLineIds.length
    && left.anchorLineIds.every(
      (lineId, index) => right.anchorLineIds[index] === lineId,
    )
  )
}

function freezeState(state: DiffReviewModelState): DiffReviewModelState {
  return Object.freeze({
    ...state,
    files: Object.freeze([...state.files]),
    filteredFiles: Object.freeze([...state.filteredFiles]),
    preferences: Object.freeze({
      ...state.preferences,
      expandedFiles: Object.freeze([...state.preferences.expandedFiles]),
      collapsedHunks: Object.freeze([...state.preferences.collapsedHunks]),
      expandedFolds: Object.freeze([...state.preferences.expandedFolds]),
    }),
    comments: Object.freeze([...state.comments]),
    receipts: Object.freeze([...state.receipts]),
    selectedFileIds: Object.freeze([...state.selectedFileIds]),
  })
}

export class DiffReviewModel {
  readonly #taskId: string
  readonly #diffId: string
  readonly #listeners = new Set<DiffReviewModelListener>()
  readonly #persistence?: DiffReviewPersistence
  #state: DiffReviewModelState
  #closed = false

  constructor(input: {
    taskId: string
    diffId: string
    files: readonly DiffFileContract[]
    persistence?: DiffReviewPersistence
    activeFileId?: string
  }) {
    this.#taskId = cleanIdentity(input.taskId, "taskId")
    this.#diffId = cleanIdentity(input.diffId, "diffId")
    this.#persistence = input.persistence ?? browserPersistence()
    const files = Object.freeze([...input.files])
    const filtered = filterFiles(files, "")
    this.#state = freezeState({
      diffId: this.#diffId,
      taskId: this.#taskId,
      files,
      filteredFiles: filtered,
      activeFileId: initialFileId(files, filtered, input.activeFileId),
      filter: "",
      preferences: loadPreferences(this.#persistence, this.#taskId),
      comments: [],
      commentDraft: "",
      commentBusy: false,
      reviewRevision: 0,
      receipts: [],
      selectedFileIds: files
        .filter((file) => !file.binary)
        .map((file) => file.fileId),
      dirty: false,
      generation: 1,
      updatedAt: Date.now(),
    })
  }

  get state(): DiffReviewModelState {
    return this.#state
  }

  listen(listener: DiffReviewModelListener): () => void {
    this.#requireOpen()
    this.#listeners.add(listener)
    listener(this.#state)
    return () => this.#listeners.delete(listener)
  }

  replaceFiles(
    filesValue: readonly DiffFileContract[],
  ): DiffReviewModelState {
    this.#requireOpen()
    const files = Object.freeze([...filesValue])
    const fileIds = new Set<string>()
    for (const file of files) {
      if (fileIds.has(file.fileId)) {
        throw new DiffReviewModelError(
          "diff_review_duplicate_file",
          `Diff review repeats file ${file.fileId}.`,
        )
      }
      fileIds.add(file.fileId)
    }
    const filteredFiles = filterFiles(files, this.#state.filter)
    const activeFileId = initialFileId(
      files,
      filteredFiles,
      this.#state.activeFileId,
    )
    const selectedFileIds = this.#state.selectedFileIds
      .filter((fileId) => fileIds.has(fileId))
    for (const file of files) {
      if (
        !file.binary
        && !selectedFileIds.includes(file.fileId)
        && !this.#state.files.some((prior) => prior.fileId === file.fileId)
      ) {
        selectedFileIds.push(file.fileId)
      }
    }
    return this.#commit({
      files,
      filteredFiles,
      activeFileId,
      selection:
        this.#state.selection
        && fileIds.has(this.#state.selection.fileId)
          ? this.#state.selection
          : undefined,
      comments: this.#state.comments.filter((comment) =>
        fileIds.has(comment.fileId)),
      selectedFileIds,
    })
  }

  setFilter(value: string): DiffReviewModelState {
    this.#requireOpen()
    const filter = String(value || "").slice(0, 8 * 1024)
    const filteredFiles = filterFiles(this.#state.files, filter)
    const activeFileId = initialFileId(
      this.#state.files,
      filteredFiles,
      this.#state.activeFileId,
    )
    return this.#commit({ filter, filteredFiles, activeFileId })
  }

  selectFile(fileIdValue: string): DiffReviewModelState {
    this.#requireOpen()
    const fileId = cleanIdentity(fileIdValue, "fileId")
    if (!this.#state.files.some((file) => file.fileId === fileId)) {
      throw new DiffReviewModelError(
        "diff_review_file_missing",
        `Diff review file ${fileId} does not exist.`,
      )
    }
    return this.#commit({
      activeFileId: fileId,
      selection:
        this.#state.selection?.fileId === fileId
          ? this.#state.selection
          : undefined,
      focusedRowId: undefined,
      commentDraft: "",
    })
  }

  toggleFileSelection(fileIdValue: string): DiffReviewModelState {
    this.#requireOpen()
    const fileId = cleanIdentity(fileIdValue, "fileId")
    const file = this.#state.files.find((candidate) => candidate.fileId === fileId)
    if (!file) {
      throw new DiffReviewModelError(
        "diff_review_file_missing",
        `Diff review file ${fileId} does not exist.`,
      )
    }
    if (file.binary) {
      throw new DiffReviewModelError(
        "diff_review_binary_apply_rejected",
        "Binary diff cannot be selected for text patch apply.",
      )
    }
    const selected = new Set(this.#state.selectedFileIds)
    if (selected.has(fileId)) selected.delete(fileId)
    else selected.add(fileId)
    return this.#commit({
      selectedFileIds: this.#state.files
        .map((candidate) => candidate.fileId)
        .filter((candidate) => selected.has(candidate)),
      dirty: true,
    })
  }

  selectAllFiles(selectedValue: boolean): DiffReviewModelState {
    this.#requireOpen()
    return this.#commit({
      selectedFileIds: selectedValue
        ? this.#state.files
          .filter((file) => !file.binary)
          .map((file) => file.fileId)
        : [],
      dirty: true,
    })
  }

  setLayout(layout: DiffLayoutMode): DiffReviewModelState {
    if (layout !== "unified" && layout !== "split") {
      throw new DiffReviewModelError(
        "diff_review_layout_invalid",
        `Unsupported diff layout ${layout}.`,
      )
    }
    return this.#preferences({ layout })
  }

  setViewMode(viewMode: DiffViewMode): DiffReviewModelState {
    if (!["diff", "old", "new"].includes(viewMode)) {
      throw new DiffReviewModelError(
        "diff_review_view_mode_invalid",
        `Unsupported diff view mode ${viewMode}.`,
      )
    }
    return this.#preferences({ viewMode })
  }

  toggleSidebar(): DiffReviewModelState {
    return this.#preferences({
      sidebarOpen: !this.#state.preferences.sidebarOpen,
    })
  }

  resizeSidebar(width: number): DiffReviewModelState {
    return this.#preferences({ sidebarWidth: boundedWidth(width) })
  }

  toggleHunk(hunkIdValue: string): DiffReviewModelState {
    const hunkId = cleanIdentity(hunkIdValue, "hunkId")
    const collapsed = new Set(this.#state.preferences.collapsedHunks)
    if (collapsed.has(hunkId)) collapsed.delete(hunkId)
    else collapsed.add(hunkId)
    return this.#preferences({
      collapsedHunks: [...collapsed],
    })
  }

  expandFold(foldIdValue: string): DiffReviewModelState {
    const foldId = cleanIdentity(foldIdValue, "foldId")
    const expanded = new Set(this.#state.preferences.expandedFolds)
    expanded.add(foldId)
    return this.#preferences({ expandedFolds: [...expanded] })
  }

  focusRow(rowIdValue: string | undefined): DiffReviewModelState {
    const focusedRowId = rowIdValue
      ? cleanIdentity(rowIdValue, "rowId")
      : undefined
    return this.#commit({ focusedRowId })
  }

  beginSelection(input: {
    hunk: DiffHunkContract
    startLineId: string
    endLineId: string
    side: "old" | "new"
  }): DiffReviewLineSelection {
    this.#requireOpen()
    if (input.hunk.fileId !== this.#state.activeFileId) {
      throw new DiffReviewModelError(
        "diff_review_selection_file_mismatch",
        "Selection hunk is not in the active file.",
      )
    }
    const selection = selectionFromRows(
      input.hunk.fileId,
      input.hunk,
      input.startLineId,
      input.endLineId,
      input.side,
    )
    if (!sameSelection(selection, this.#state.selection)) {
      this.#commit({ selection, commentDraft: "" })
    }
    return selection
  }

  clearSelection(): DiffReviewModelState {
    return this.#commit({ selection: undefined, commentDraft: "" })
  }

  setCommentDraft(value: string): DiffReviewModelState {
    this.#requireOpen()
    const body = String(value || "")
    if (new TextEncoder().encode(body).byteLength > 64 * 1024) {
      throw new DiffReviewModelError(
        "diff_review_comment_budget",
        "Review comment exceeds 64 KiB.",
      )
    }
    return this.#commit({ commentDraft: body })
  }

  setCommentBusy(value: boolean): DiffReviewModelState {
    return this.#commit({ commentBusy: Boolean(value) })
  }

  admitReceipt(receipt: DiffReviewReceipt): DiffReviewModelState {
    this.#requireOpen()
    if (
      receipt.diffId !== this.#diffId
      || receipt.taskId !== this.#taskId
    ) {
      throw new DiffReviewModelError(
        "diff_review_receipt_binding",
        "Review receipt belongs to another task or diff.",
      )
    }
    const existing = this.#state.receipts.find(
      (candidate) => candidate.receiptId === receipt.receiptId,
    )
    if (existing) {
      if (
        existing.revision !== receipt.revision
        || existing.causationId !== receipt.causationId
      ) {
        throw new DiffReviewModelError(
          "diff_review_receipt_conflict",
          "Review receipt identity was reused with different content.",
        )
      }
      return this.#state
    }
    if (receipt.revision <= this.#state.reviewRevision) {
      throw new DiffReviewModelError(
        "diff_review_receipt_stale",
        "Review receipt revision is stale.",
        {
          current: this.#state.reviewRevision,
          received: receipt.revision,
        },
      )
    }
    let comments = [...this.#state.comments]
    if (receipt.comment) {
      const index = comments.findIndex(
        (comment) => comment.commentId === receipt.comment!.commentId,
      )
      if (index >= 0) {
        if (comments[index]!.revision >= receipt.comment.revision) {
          throw new DiffReviewModelError(
            "diff_review_comment_stale",
            "Review comment receipt is stale.",
          )
        }
        comments[index] = receipt.comment
      } else {
        comments.push(receipt.comment)
      }
      comments = comments.sort((left, right) =>
        left.createdAt.localeCompare(right.createdAt)
        || left.commentId.localeCompare(right.commentId))
    }
    return this.#commit({
      receipts: [...this.#state.receipts, receipt].slice(-2_000),
      comments,
      selection: receipt.selection ?? receipt.comment?.selection ?? this.#state.selection,
      commentDraft: receipt.comment ? "" : this.#state.commentDraft,
      commentBusy: false,
      reviewRevision: receipt.revision,
      dirty: true,
    })
  }

  commentsForFile(fileIdValue: string): readonly DiffReviewComment[] {
    const fileId = cleanIdentity(fileIdValue, "fileId")
    return Object.freeze(
      this.#state.comments.filter((comment) => comment.fileId === fileId),
    )
  }

  commentsForHunk(hunkIdValue: string): readonly DiffReviewComment[] {
    const hunkId = cleanIdentity(hunkIdValue, "hunkId")
    return Object.freeze(
      this.#state.comments.filter((comment) => comment.hunkId === hunkId),
    )
  }

  commentedLineIds(): ReadonlySet<string> {
    const ids = new Set<string>()
    for (const comment of this.#state.comments) {
      if (comment.state === "resolved") continue
      for (const lineId of comment.selection.anchorLineIds) ids.add(lineId)
    }
    return ids
  }

  markApplied(): DiffReviewModelState {
    return this.#commit({ dirty: false })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#listeners.clear()
  }

  #preferences(
    patch: Partial<DiffReviewPreferences>,
  ): DiffReviewModelState {
    this.#requireOpen()
    const preferences = parsePreferences({
      ...this.#state.preferences,
      ...patch,
    })
    if (this.#persistence) {
      try {
        this.#persistence.setItem(
          persistenceKey(this.#taskId),
          JSON.stringify(preferences),
        )
      } catch {
        // Preference persistence is optional. Runtime state remains authoritative
        // for the mounted review and no file/patch truth is stored here.
      }
    }
    return this.#commit({ preferences })
  }

  #commit(patch: Partial<DiffReviewModelState>): DiffReviewModelState {
    this.#state = freezeState({
      ...this.#state,
      ...patch,
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
    return this.#state
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffReviewModelError(
        "diff_review_model_closed",
        "Diff review model is closed.",
      )
    }
  }
}

export function fileStatusLabel(file: DiffFileContract): string {
  if (file.kind === "added") return "Added"
  if (file.kind === "deleted") return "Deleted"
  if (file.kind === "renamed") return "Renamed"
  if (file.binary) return "Binary"
  return "Modified"
}

export function reviewRiskLabel(file: DiffFileContract): string {
  if (file.risk.conflictedPaths > 0) return "Repository conflict"
  if (file.risk.nestedRepository) return "Nested repository"
  if (file.risk.dirty) return "Dirty workspace"
  if (file.oversized) return "Large file"
  if (file.truncated) return "Partial diff"
  return "Bound snapshot"
}
