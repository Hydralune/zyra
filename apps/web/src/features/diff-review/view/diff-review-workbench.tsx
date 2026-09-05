import {
  Fragment,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type {
  DiffFileContract,
  DiffHunkContract,
  DiffLineContract,
  DiffReviewLineSelection,
} from "../contracts.ts"
import {
  DiffReviewWorkbenchRuntime,
  type DiffReviewWorkbenchState,
} from "../runtime.ts"
import {
  fileStatusLabel,
  reviewRiskLabel,
} from "../review-model.ts"
import {
  diffVirtualWindow,
  navigateDiffRows,
  projectDiffRows,
  revealSelection,
  type DiffRowProjection,
  type DiffVirtualFoldRow,
  type DiffVirtualLineRow,
  type DiffVirtualRow,
  type DiffVirtualSplitRow,
} from "../virtualization.ts"

export interface DiffReviewWorkbenchProps {
  runtime: WorkbenchRuntime
  task: TaskProjection
  sealed?: boolean
}

const defaultViewportHeight = 620
const lineHeight = 24

function displayCount(value: number): string {
  return new Intl.NumberFormat().format(value)
}

function shortDigest(value: string): string {
  if (!value) return "—"
  const selected = value.replace(/^sha256:/, "")
  return selected.length > 16
    ? `${selected.slice(0, 8)}…${selected.slice(-6)}`
    : selected
}

function fileKindSymbol(file: DiffFileContract): string {
  if (file.kind === "added") return "A"
  if (file.kind === "deleted") return "D"
  if (file.kind === "renamed") return "R"
  if (file.binary) return "B"
  return "M"
}

function phaseMessage(state: DiffReviewWorkbenchState): string {
  if (state.phase === "catalog-loading") return "正在加载代码变更…"
  if (state.phase === "manifest-loading") return "Parsing the selected patch…"
  if (state.phase === "loading") return "Loading verified hunk pages…"
  if (state.phase === "preflighting") return "Checking file snapshots and merge safety…"
  if (state.phase === "applying") return "Applying through the workspace transaction owner…"
  if (state.phase === "permission-pending") return "Permission approval is pending."
  if (state.phase === "rolling-back") return "Rolling back the committed write set…"
  if (state.phase === "committed") return "Patch committed and verified."
  if (state.phase === "rolled-back") return "Patch write set rolled back."
  if (state.phase === "disconnected") return "Diff or patch owner is disconnected."
  if (state.phase === "failed") return state.error?.message ?? "Diff review failed."
  if (state.phase === "cancelled") return "Diff review operation cancelled."
  return ""
}

export function DiffReviewWorkbench({
  runtime,
  task,
  sealed = false,
}: DiffReviewWorkbenchProps) {
  const workbench = useMemo(
    () =>
      new DiffReviewWorkbenchRuntime({
        api: runtime.api.tasks,
        taskId: task.taskId,
        refresh: {
          refreshTask: async () => {
            await runtime.workbench.loadTask(task.taskId)
          },
          refreshArtifacts: async () => {
            await runtime.workbench.loadTask(task.taskId)
          },
          refreshEvents: async () => {
            await runtime.workbench.loadTask(task.taskId)
          },
        },
      }),
    [runtime, task.taskId],
  )
  const [state, setState] = useState<DiffReviewWorkbenchState>(workbench.state)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(defaultViewportHeight)
  const [searchQuery, setSearchQuery] = useState("")
  const [commentBody, setCommentBody] = useState("")
  const [selectionStart, setSelectionStart] = useState<{
    hunkId: string
    lineId: string
    side: "old" | "new"
  }>()
  const [permitId, setPermitId] = useState("")
  const viewportRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const unsubscribe = workbench.listen(setState)
    void workbench.loadCatalog().catch(() => { /* Load failures are published by the runtime; cancellation on unmount is expected. */ })
    return () => {
      unsubscribe()
      workbench.close("Diff review component unmounted.")
    }
  }, [workbench])

  useEffect(() => {
    const element = viewportRef.current
    if (!element || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height
      if (height && Number.isFinite(height)) setViewportHeight(height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [state.review?.activeFileId])

  useEffect(() => {
    setScrollTop(0)
    setSelectionStart(undefined)
    if (viewportRef.current) viewportRef.current.scrollTop = 0
  }, [state.review?.activeFileId, state.selectedArtifact?.artifactId])

  const activeFile = useMemo(() => {
    const active = state.review?.activeFileId
    return state.manifest?.files.find((file) => file.fileId === active)
  }, [state.manifest, state.review?.activeFileId])

  const activeLoad = activeFile
    ? state.fetch?.files[activeFile.fileId]
    : undefined

  const projection = useMemo(() => {
    if (!activeFile) return undefined
    return projectDiffRows(activeFile, activeLoad?.hunks ?? [], {
      layout: state.review?.preferences.layout ?? "unified",
      viewMode: state.review?.preferences.viewMode ?? "diff",
      collapsedHunks: new Set(
        state.review?.preferences.collapsedHunks ?? [],
      ),
      expandedFolds: new Set(
        state.review?.preferences.expandedFolds ?? [],
      ),
      selection: state.review?.selection,
      commentedLineIds: workbench.reviewModel?.commentedLineIds(),
      loading:
        activeLoad?.phase === "loading"
        || (Boolean(activeLoad) && !activeLoad?.complete),
      error: activeLoad?.error?.message,
    })
  }, [
    activeFile,
    activeLoad,
    state.review?.preferences,
    state.review?.selection,
    state.review?.comments,
    workbench,
  ])

  const windowed = useMemo(() => {
    if (!projection) return undefined
    return diffVirtualWindow(projection, {
      scrollTop,
      viewportHeight,
      overscanPixels: viewportHeight,
      lineHeight,
    })
  }, [projection, scrollTop, viewportHeight])

  useEffect(() => {
    const selection = state.review?.selection
    if (!selection || !projection || !viewportRef.current) return
    const rowId = revealSelection(projection, selection)
    if (!rowId) return
    const rowIndex = projection.rows.findIndex((row) => row.rowId === rowId)
    if (rowIndex < 0) return
    let top = 0
    for (let index = 0; index < rowIndex; index += 1) {
      top += projection.rows[index]!.height
    }
    const element = viewportRef.current
    if (top < element.scrollTop || top > element.scrollTop + element.clientHeight - lineHeight) {
      element.scrollTop = Math.max(0, top - lineHeight * 2)
      setScrollTop(element.scrollTop)
    }
  }, [projection, state.review?.selection])

  const selectArtifact = (artifactId: string, revision: string) => {
    void workbench.selectArtifact(artifactId, revision)
  }

  const selectFile = (fileId: string) => {
    void workbench.selectFile(fileId)
  }

  const runSearch = () => {
    if (!searchQuery.trim()) return
    void workbench.search({
      query: searchQuery,
      includeAdded: true,
      includeDeleted: true,
      includeContext: true,
      maximumResults: 5_000,
    })
  }

  const apply = () => {
    void workbench.apply({
      actorId: "console-operator",
      sessionId: `console:${task.taskId}`,
      sessionRevision: 0,
      workerRequestId: `console-diff-review:${task.taskId}`,
      sealed,
    })
  }

  const rollback = () => {
    void workbench.rollback({
      actorId: "console-operator",
      sessionId: `console:${task.taskId}`,
      sessionRevision: 0,
      workerRequestId: `console-diff-review:${task.taskId}`,
      sealed,
    })
  }

  const retryPermission = () => {
    if (!permitId.trim()) return
    void workbench.retryPermission(permitId.trim())
  }

  const submitComment = () => {
    if (!commentBody.trim() || !state.review?.selection) return
    void workbench
      .submitComment({
        body: commentBody,
        actorId: "console-operator",
        sealed,
      })
      .then(() => setCommentBody(""))
  }

  const handleKeyboard = (
    event: ReactKeyboardEvent<HTMLDivElement>,
    rowProjection: DiffRowProjection,
  ) => {
    const action =
      event.key === "ArrowDown"
        ? "next"
        : event.key === "ArrowUp"
          ? "previous"
          : event.key === "PageDown"
            ? "page-next"
            : event.key === "PageUp"
              ? "page-previous"
              : event.key === "Home"
                ? "first"
                : event.key === "End"
                  ? "last"
                  : event.key === "]"
                    ? "next-hunk"
                    : event.key === "["
                      ? "previous-hunk"
                      : undefined
    if (!action) return
    event.preventDefault()
    const result = navigateDiffRows(
      rowProjection,
      state.review?.focusedRowId,
      action,
      { viewportHeight, lineHeight },
    )
    if (!result.rowId) return
    workbench.reviewModel?.focusRow(result.rowId)
    if (viewportRef.current) {
      viewportRef.current.scrollTop = result.scrollTop
      setScrollTop(result.scrollTop)
    }
  }

  const lineClick = (
    event: ReactMouseEvent,
    hunk: DiffHunkContract,
    line: DiffLineContract,
    side: "old" | "new",
  ) => {
    event.preventDefault()
    const coordinate = side === "old" ? line.oldLine : line.newLine
    if (coordinate === undefined) return
    const start =
      selectionStart
      && selectionStart.hunkId === hunk.hunkId
      && selectionStart.side === side
        ? selectionStart
        : undefined
    if (!start || !event.shiftKey) {
      setSelectionStart({ hunkId: hunk.hunkId, lineId: line.lineId, side })
      workbench.reviewModel?.beginSelection({
        hunk,
        startLineId: line.lineId,
        endLineId: line.lineId,
        side,
      })
      return
    }
    const selection = workbench.reviewModel?.beginSelection({
      hunk,
      startLineId: start.lineId,
      endLineId: line.lineId,
      side,
    })
    if (selection && !sealed) {
      void workbench.submitSelection(selection, "console-operator", false)
    }
  }

  return (
    <div
      className="diff-review-workbench"
      aria-labelledby="diff-review-heading"
      data-task-id={task.taskId}
      data-diff-review="true"
      data-artifact-id={state.selectedArtifact?.artifactId}
    >
      <header className="diff-review-header">
        <div>
          <h3 id="diff-review-heading">代码变更审查</h3>
          <p>
            Revision-bound diffs with permission-gated apply, verification,
            rollback, and transaction receipts.
          </p>
        </div>
        <div className="diff-review-header-actions">
          {state.phase !== "idle" && state.phase !== "ready" ? (
            <button
              className="button button-secondary"
              type="button"
              onClick={() => workbench.cancel()}
            >
              Cancel
            </button>
          ) : null}
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void workbench.loadCatalog().catch(() => { /* Load failures are published by the runtime; cancellation on unmount is expected. */ })}
          >
            刷新变更
          </button>
        </div>
      </header>

      <ArtifactSelector
        artifacts={state.patchArtifacts}
        selected={state.selectedArtifact}
        onSelect={selectArtifact}
      />

      {state.manifest && state.review ? (
        <div className="diff-review-shell">
          <DiffSidebar
            state={state}
            activeFile={activeFile}
            onSelectFile={selectFile}
            onFilter={(value) => workbench.reviewModel?.setFilter(value)}
            onToggleFile={(fileId) =>
              workbench.reviewModel?.toggleFileSelection(fileId)}
          />

          <main className="diff-review-main">
            <DiffToolbar
              state={state}
              sealed={sealed}
              searchQuery={searchQuery}
              onSearchQuery={setSearchQuery}
              onSearch={runSearch}
              onLayout={(layout) => workbench.reviewModel?.setLayout(layout)}
              onViewMode={(mode) => workbench.reviewModel?.setViewMode(mode)}
              onApply={apply}
              onRollback={rollback}
            />

            {activeFile && projection && windowed ? (
              <div
                ref={viewportRef}
                className="diff-review-viewport"
                tabIndex={0}
                aria-label={`Diff for ${activeFile.path}`}
                onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
                onKeyDown={(event) => handleKeyboard(event, projection)}
              >
                <div
                  className="diff-review-virtual-canvas"
                  style={{ height: `${windowed.totalHeight}px` }}
                >
                  {windowed.rows.map((row) => (
                    <DiffRow
                      key={row.rowId}
                      row={row}
                      state={state}
                      onFocus={(rowId) =>
                        workbench.reviewModel?.focusRow(rowId)}
                      onToggleHunk={(hunkId) =>
                        workbench.reviewModel?.toggleHunk(hunkId)}
                      onExpandFold={(foldId) =>
                        workbench.reviewModel?.expandFold(foldId)}
                      onLineClick={lineClick}
                    />
                  ))}
                </div>
              </div>
            ) : (
              <div className="diff-review-empty">
                {state.phase === "loading"
                  ? "Loading the selected file…"
                  : "Select a patch file to inspect its hunks."}
              </div>
            )}

            <ReviewComposer
              selection={state.review.selection}
              comments={state.review.comments.filter((comment) =>
                comment.fileId === activeFile?.fileId)}
              body={commentBody}
              busy={state.review.commentBusy}
              sealed={sealed}
              onBody={setCommentBody}
              onSubmit={submitComment}
              onClear={() => workbench.reviewModel?.clearSelection()}
            />

            <TransactionPanel
              state={state}
              permitId={permitId}
              onPermitId={setPermitId}
              onRetryPermission={retryPermission}
            />
          </main>
        </div>
      ) : (
        <div className="diff-review-empty">
          {state.patchArtifacts.length
            ? phaseMessage(state) || "Select a patch artifact."
            : state.phase === "catalog-loading"
              ? "正在加载代码变更…"
              : "No verified text patch artifact is attached to this task."}
        </div>
      )}

      {phaseMessage(state) ? (
        <div
          className={`diff-review-status diff-review-status-${state.phase}`}
          role={state.error ? "alert" : "status"}
        >
          {phaseMessage(state)}
        </div>
      ) : null}
    </div>
  )
}

function ArtifactSelector({
  artifacts,
  selected,
  onSelect,
}: {
  artifacts: readonly ArtifactContractLike[]
  selected?: ArtifactContractLike
  onSelect(artifactId: string, revision: string): void
}) {
  if (!artifacts.length) return null
  return (
    <div className="diff-review-artifact-selector">
      <label htmlFor="diff-review-artifact">Patch artifact</label>
      <select
        id="diff-review-artifact"
        value={selected ? `${selected.artifactId}:${selected.revision}` : ""}
        onChange={(event) => {
          const artifact = artifacts.find(
            (candidate) =>
              `${candidate.artifactId}:${candidate.revision}`
              === event.currentTarget.value,
          )
          if (artifact) onSelect(artifact.artifactId, artifact.revision)
        }}
      >
        {artifacts.map((artifact) => (
          <option
            key={`${artifact.artifactId}:${artifact.revision}`}
            value={`${artifact.artifactId}:${artifact.revision}`}
          >
            {artifact.title} · {displayCount(artifact.sizeBytes)} bytes
          </option>
        ))}
      </select>
      {selected ? (
        <span title={selected.sha256}>
          {shortDigest(selected.revision)}
        </span>
      ) : null}
    </div>
  )
}

interface ArtifactContractLike {
  artifactId: string
  revision: string
  title: string
  sizeBytes: number
  sha256: string
}

function DiffSidebar({
  state,
  activeFile,
  onSelectFile,
  onFilter,
  onToggleFile,
}: {
  state: DiffReviewWorkbenchState
  activeFile?: DiffFileContract
  onSelectFile(fileId: string): void
  onFilter(value: string): void
  onToggleFile(fileId: string): void
}) {
  const review = state.review!
  return (
    <aside
      className="diff-review-sidebar"
      style={{ width: `${review.preferences.sidebarWidth}px` }}
    >
      <div className="diff-review-sidebar-heading">
        <strong>Changed files</strong>
        <span>{review.filteredFiles.length}/{review.files.length}</span>
      </div>
      <input
        type="search"
        value={review.filter}
        placeholder="Filter paths, language, or risk"
        onChange={(event) => onFilter(event.currentTarget.value)}
      />
      <div className="diff-review-file-list" role="listbox">
        {review.filteredFiles.map((file) => {
          const selected = review.selectedFileIds.includes(file.fileId)
          const load = state.fetch?.files[file.fileId]
          return (
            <div
              className={`diff-review-file ${file.fileId === activeFile?.fileId ? "is-active" : ""}`}
              key={file.fileId}
            >
              <input
                type="checkbox"
                aria-label={`Include ${file.path} in apply`}
                checked={selected}
                disabled={file.binary}
                onChange={() => onToggleFile(file.fileId)}
              />
              <button
                type="button"
                role="option"
                aria-selected={file.fileId === activeFile?.fileId}
                onClick={() => onSelectFile(file.fileId)}
              >
                <span className={`diff-kind diff-kind-${file.kind}`}>
                  {fileKindSymbol(file)}
                </span>
                <span className="diff-review-file-copy">
                  <strong title={file.path}>{file.path}</strong>
                  {file.previousPath ? (
                    <small title={file.previousPath}>
                      from {file.previousPath}
                    </small>
                  ) : null}
                  <small>
                    +{file.additions} −{file.deletions} · {reviewRiskLabel(file)}
                  </small>
                </span>
                <span className="diff-review-file-progress">
                  {load?.complete
                    ? "✓"
                    : load
                      ? `${load.loadedHunkCount}/${file.hunkCount}`
                      : ""}
                </span>
              </button>
            </div>
          )
        })}
      </div>
    </aside>
  )
}

function DiffToolbar({
  state,
  sealed,
  searchQuery,
  onSearchQuery,
  onSearch,
  onLayout,
  onViewMode,
  onApply,
  onRollback,
}: {
  state: DiffReviewWorkbenchState
  sealed: boolean
  searchQuery: string
  onSearchQuery(value: string): void
  onSearch(): void
  onLayout(layout: "unified" | "split"): void
  onViewMode(mode: "diff" | "old" | "new"): void
  onApply(): void
  onRollback(): void
}) {
  const review = state.review!
  const transaction = state.transaction?.latestReceipt
  const applyDisabled =
    sealed
    || !review.selectedFileIds.length
    || state.phase === "preflighting"
    || state.phase === "applying"
    || state.phase === "rolling-back"
  return (
    <div className="diff-review-toolbar">
      <div className="diff-review-toggle-group" aria-label="Diff layout">
        {(["unified", "split"] as const).map((layout) => (
          <button
            type="button"
            className={review.preferences.layout === layout ? "is-active" : ""}
            aria-pressed={review.preferences.layout === layout}
            onClick={() => onLayout(layout)}
            key={layout}
          >
            {layout}
          </button>
        ))}
      </div>
      <div className="diff-review-toggle-group" aria-label="Content view">
        {(["old", "diff", "new"] as const).map((mode) => (
          <button
            type="button"
            className={review.preferences.viewMode === mode ? "is-active" : ""}
            aria-pressed={review.preferences.viewMode === mode}
            onClick={() => onViewMode(mode)}
            key={mode}
          >
            {mode}
          </button>
        ))}
      </div>
      <div className="diff-review-search">
        <input
          type="search"
          value={searchQuery}
          placeholder="Search loaded hunks"
          onChange={(event) => onSearchQuery(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") onSearch()
          }}
        />
        <button
          type="button"
          disabled={!searchQuery.trim() || state.searchBusy}
          onClick={onSearch}
        >
          {state.searchBusy ? "Searching…" : "Search"}
        </button>
        {state.search ? (
          <span>
            {state.search.matches.length}
            {state.search.truncated ? "+" : ""} matches
          </span>
        ) : null}
      </div>
      <div className="diff-review-transaction-actions">
        <button
          className="button button-primary"
          type="button"
          disabled={applyDisabled}
          title={sealed ? "Sealed runs are read-only for human controls." : undefined}
          onClick={onApply}
        >
          {sealed ? "Sealed: read only" : `Apply ${review.selectedFileIds.length}`}
        </button>
        <button
          className="button button-secondary"
          type="button"
          disabled={!transaction?.committed || sealed}
          onClick={onRollback}
        >
          Roll back
        </button>
      </div>
    </div>
  )
}

function DiffRow({
  row,
  state,
  onFocus,
  onToggleHunk,
  onExpandFold,
  onLineClick,
}: {
  row: DiffVirtualRow
  state: DiffReviewWorkbenchState
  onFocus(rowId: string): void
  onToggleHunk(hunkId: string): void
  onExpandFold(foldId: string): void
  onLineClick(
    event: ReactMouseEvent,
    hunk: DiffHunkContract,
    line: DiffLineContract,
    side: "old" | "new",
  ): void
}) {
  const style = {
    position: "absolute" as const,
    transform: `translateY(${row.top}px)`,
    height: `${row.height}px`,
    width: "100%",
  }
  const focused = state.review?.focusedRowId === row.rowId
  if (row.kind === "file-header") {
    return (
      <div
        className={`diff-row diff-file-header ${focused ? "is-focused" : ""}`}
        style={style}
        tabIndex={row.focusable ? 0 : -1}
        onFocus={() => onFocus(row.rowId)}
      >
        <strong>{row.file.path}</strong>
        <span>{fileStatusLabel(row.file)}</span>
        <span>{row.file.encoding} · {row.file.lineEnding}</span>
        <span>base {shortDigest(row.file.oldSha256)}</span>
        <span>current {shortDigest(row.file.currentSha256)}</span>
      </div>
    )
  }
  if (row.kind === "hunk-header") {
    const collapsed = state.review?.preferences.collapsedHunks.includes(
      row.hunk.hunkId,
    )
    return (
      <button
        className={`diff-row diff-hunk-header ${focused ? "is-focused" : ""}`}
        style={style}
        type="button"
        onFocus={() => onFocus(row.rowId)}
        onClick={() => onToggleHunk(row.hunk.hunkId)}
      >
        <span>{collapsed ? "▸" : "▾"}</span>
        <code>{row.hunk.header}</code>
        <span>+{row.hunk.additions} −{row.hunk.deletions}</span>
      </button>
    )
  }
  if (row.kind === "fold") {
    return (
      <button
        className={`diff-row diff-fold-row ${focused ? "is-focused" : ""}`}
        style={style}
        type="button"
        onFocus={() => onFocus(row.rowId)}
        onClick={() => onExpandFold(row.rowId)}
      >
        Show {row.hiddenCount} unchanged lines
      </button>
    )
  }
  if (row.kind === "line") {
    return (
      <UnifiedLineRow
        row={row}
        style={style}
        focused={focused}
        hunk={findHunk(state, row.hunkId)}
        onFocus={onFocus}
        onLineClick={onLineClick}
      />
    )
  }
  if (row.kind === "split-line") {
    return (
      <SplitLineRow
        row={row}
        style={style}
        focused={focused}
        hunk={findHunk(state, row.hunkId)}
        onFocus={onFocus}
        onLineClick={onLineClick}
      />
    )
  }
  if (row.kind === "binary") {
    return (
      <div
        className="diff-row diff-status-row"
        style={style}
        tabIndex={0}
        onFocus={() => onFocus(row.rowId)}
      >
        <strong>Binary file</strong>
        <span>{row.message}</span>
      </div>
    )
  }
  return (
    <div
      className={`diff-row diff-status-row ${row.kind === "error" ? "is-error" : ""}`}
      style={style}
      tabIndex={row.focusable ? 0 : -1}
      onFocus={() => onFocus(row.rowId)}
    >
      {row.message}
    </div>
  )
}

function findHunk(
  state: DiffReviewWorkbenchState,
  hunkId: string | undefined,
): DiffHunkContract | undefined {
  if (!hunkId || !state.review?.activeFileId) return undefined
  return state.fetch?.files[state.review.activeFileId]?.hunks.find(
    (hunk) => hunk.hunkId === hunkId,
  )
}

function UnifiedLineRow({
  row,
  style,
  focused,
  hunk,
  onFocus,
  onLineClick,
}: {
  row: DiffVirtualLineRow
  style: React.CSSProperties
  focused: boolean
  hunk?: DiffHunkContract
  onFocus(rowId: string): void
  onLineClick(
    event: ReactMouseEvent,
    hunk: DiffHunkContract,
    line: DiffLineContract,
    side: "old" | "new",
  ): void
}) {
  const line = row.line
  return (
    <div
      className={[
        "diff-row",
        "diff-line-row",
        `diff-line-${line.kind}`,
        focused ? "is-focused" : "",
        row.selected ? "is-selected" : "",
        row.commented ? "has-comment" : "",
      ].filter(Boolean).join(" ")}
      style={style}
      tabIndex={row.focusable ? 0 : -1}
      onFocus={() => onFocus(row.rowId)}
    >
      <button
        type="button"
        className="diff-line-number"
        disabled={!hunk || line.oldLine === undefined}
        onClick={(event) => {
          if (hunk) onLineClick(event, hunk, line, "old")
        }}
      >
        {line.oldLine ?? ""}
      </button>
      <button
        type="button"
        className="diff-line-number"
        disabled={!hunk || line.newLine === undefined}
        onClick={(event) => {
          if (hunk) onLineClick(event, hunk, line, "new")
        }}
      >
        {line.newLine ?? ""}
      </button>
      <span className="diff-line-marker">
        {line.kind === "added" ? "+" : line.kind === "deleted" ? "−" : " "}
      </span>
      <code>{line.text || " "}</code>
      {line.noNewline ? <span className="diff-no-newline">no newline</span> : null}
    </div>
  )
}

function SplitLineRow({
  row,
  style,
  focused,
  hunk,
  onFocus,
  onLineClick,
}: {
  row: DiffVirtualSplitRow
  style: React.CSSProperties
  focused: boolean
  hunk?: DiffHunkContract
  onFocus(rowId: string): void
  onLineClick(
    event: ReactMouseEvent,
    hunk: DiffHunkContract,
    line: DiffLineContract,
    side: "old" | "new",
  ): void
}) {
  return (
    <div
      className={`diff-row diff-split-row ${focused ? "is-focused" : ""}`}
      style={style}
      tabIndex={0}
      onFocus={() => onFocus(row.rowId)}
    >
      <SplitCell
        line={row.oldLine}
        side="old"
        selected={row.selectedOld}
        commented={row.commentedOld}
        hunk={hunk}
        onLineClick={onLineClick}
      />
      <SplitCell
        line={row.newLine}
        side="new"
        selected={row.selectedNew}
        commented={row.commentedNew}
        hunk={hunk}
        onLineClick={onLineClick}
      />
    </div>
  )
}

function SplitCell({
  line,
  side,
  selected,
  commented,
  hunk,
  onLineClick,
}: {
  line?: DiffLineContract
  side: "old" | "new"
  selected: boolean
  commented: boolean
  hunk?: DiffHunkContract
  onLineClick(
    event: ReactMouseEvent,
    hunk: DiffHunkContract,
    line: DiffLineContract,
    side: "old" | "new",
  ): void
}) {
  return (
    <div
      className={[
        "diff-split-cell",
        line ? `diff-line-${line.kind}` : "diff-line-empty",
        selected ? "is-selected" : "",
        commented ? "has-comment" : "",
      ].filter(Boolean).join(" ")}
    >
      <button
        type="button"
        className="diff-line-number"
        disabled={!line || !hunk}
        onClick={(event) => {
          if (line && hunk) onLineClick(event, hunk, line, side)
        }}
      >
        {side === "old" ? line?.oldLine ?? "" : line?.newLine ?? ""}
      </button>
      <span className="diff-line-marker">
        {line?.kind === "added" ? "+" : line?.kind === "deleted" ? "−" : " "}
      </span>
      <code>{line?.text ?? ""}</code>
    </div>
  )
}

function ReviewComposer({
  selection,
  comments,
  body,
  busy,
  sealed,
  onBody,
  onSubmit,
  onClear,
}: {
  selection?: DiffReviewLineSelection
  comments: readonly {
    commentId: string
    body: string
    state: string
    selection: DiffReviewLineSelection
  }[]
  body: string
  busy: boolean
  sealed: boolean
  onBody(value: string): void
  onSubmit(): void
  onClear(): void
}) {
  return (
    <section className="diff-review-comments" aria-label="Review comments">
      <div className="diff-review-comments-list">
        {comments.length ? (
          comments.map((comment) => (
            <article
              key={comment.commentId}
              className={`diff-review-comment diff-review-comment-${comment.state}`}
            >
              <div>
                {comment.selection.side} lines {comment.selection.startLine}–
                {comment.selection.endLine}
              </div>
              <p>{comment.body}</p>
              <span>{comment.state}</span>
            </article>
          ))
        ) : (
          <p>No review comments for this file.</p>
        )}
      </div>
      <div className="diff-review-comment-composer">
        <div>
          {selection
            ? `${selection.side} lines ${selection.startLine}–${selection.endLine}`
            : "Select one or more diff lines to comment."}
          {selection ? (
            <button type="button" onClick={onClear}>Clear</button>
          ) : null}
        </div>
        <textarea
          value={body}
          disabled={!selection || busy || sealed}
          placeholder={
            sealed
              ? "Sealed runs do not accept manual review mutations"
              : "Leave a revision-bound review comment"
          }
          onChange={(event) => onBody(event.currentTarget.value)}
        />
        <button
          className="button button-secondary"
          type="button"
          disabled={!selection || !body.trim() || busy || sealed}
          onClick={onSubmit}
        >
          {busy ? "Submitting…" : "Comment"}
        </button>
      </div>
    </section>
  )
}

function TransactionPanel({
  state,
  permitId,
  onPermitId,
  onRetryPermission,
}: {
  state: DiffReviewWorkbenchState
  permitId: string
  onPermitId(value: string): void
  onRetryPermission(): void
}) {
  const receipt = state.transaction?.latestReceipt
  const preflight = state.preflightReceipts
  if (!receipt && !preflight.length) return null
  return (
    <section className="diff-review-transaction" aria-label="Patch transaction">
      {preflight.length ? (
        <div className="diff-review-preflight-grid">
          {preflight.map((item) => (
            <div key={item.receiptId}>
              <strong>{item.path}</strong>
              <span className={`tag ${item.accepted ? "" : "tag-danger"}`}>
                {item.state}
              </span>
              <span>{item.reasonCode}</span>
              <span>
                {shortDigest(item.currentSha256)} → {shortDigest(item.proposedSha256)}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {receipt ? (
        <Fragment>
          <dl className="diff-review-transaction-facts">
            <div><dt>Phase</dt><dd>{receipt.phase}</dd></div>
            <div><dt>Transaction</dt><dd>{receipt.transactionId}</dd></div>
            <div><dt>Receipt</dt><dd>{receipt.receiptId}</dd></div>
            <div><dt>Permission</dt><dd>{receipt.permission.effect}</dd></div>
            <div><dt>Snapshot</dt><dd>{receipt.snapshotId || "—"}</dd></div>
            <div><dt>Events</dt><dd>{receipt.eventIds.length}</dd></div>
            <div><dt>Artifacts</dt><dd>{receipt.artifactRefs.length}</dd></div>
            <div><dt>Human intervention</dt><dd>{receipt.humanInterventionCount}</dd></div>
          </dl>
          <p>{receipt.message}</p>
          {receipt.phase === "permission_pending" ? (
            <div className="diff-review-permit">
              <label htmlFor="diff-review-permit-id">
                Approved exact-call permit
              </label>
              <input
                id="diff-review-permit-id"
                value={permitId}
                onChange={(event) => onPermitId(event.currentTarget.value)}
                placeholder="permit id from Permissions"
              />
              <button
                className="button button-primary"
                type="button"
                disabled={!permitId.trim()}
                onClick={onRetryPermission}
              >
                Retry exact request
              </button>
            </div>
          ) : null}
        </Fragment>
      ) : null}
    </section>
  )
}
