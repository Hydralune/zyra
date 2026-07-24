import {
  Fragment,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import {
  artifactCatalogWindow,
  type ArtifactCatalogVirtualRow,
} from "../catalog.ts"
import type { ArtifactContract } from "../contracts.ts"
import {
  ArtifactWorkbenchRuntime,
  type ArtifactRenderableModel,
  type ArtifactWorkbenchState,
} from "../runtime.ts"
import type {
  ArtifactBinaryViewerModel,
  ArtifactJsonViewerModel,
  ArtifactMarkdownViewerModel,
  ArtifactMediaViewerModel,
  ArtifactMetadataViewerModel,
  ArtifactTextViewerModel,
  JsonFlatRow,
  MarkdownBlock,
  MarkdownInline,
} from "../viewers.ts"

export interface ArtifactWorkbenchProps {
  runtime: WorkbenchRuntime
  taskId: string
}

const catalogRowHeight = 88
const textLineHeight = 22

export function ArtifactWorkbench({
  runtime,
  taskId,
}: ArtifactWorkbenchProps) {
  const workbench = useMemo(
    () =>
      new ArtifactWorkbenchRuntime({
        api: runtime.api.tasks,
        taskId,
      }),
    [runtime, taskId],
  )
  const [state, setState] = useState<ArtifactWorkbenchState>(workbench.state)
  const [catalogScrollTop, setCatalogScrollTop] = useState(0)
  const [catalogHeight, setCatalogHeight] = useState(460)
  const [textScrollTop, setTextScrollTop] = useState(0)
  const [textHeight, setTextHeight] = useState(520)
  const [catalogQuery, setCatalogQuery] = useState("")
  const [contentQuery, setContentQuery] = useState("")
  const [searchBusy, setSearchBusy] = useState(false)
  const [downloadConfirmed, setDownloadConfirmed] = useState(false)
  const catalogViewport = useRef<HTMLDivElement>(null)
  const textViewport = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const unsubscribe = workbench.listen(setState)
    void workbench.loadCatalog()
    return () => {
      unsubscribe()
      workbench.close("Artifact workbench component unmounted.")
    }
  }, [workbench])

  useEffect(() => {
    const element = catalogViewport.current
    if (!element || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height
      if (height && Number.isFinite(height)) setCatalogHeight(height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const element = textViewport.current
    if (!element || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height
      if (height && Number.isFinite(height)) setTextHeight(height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [state.selected?.revision])

  useEffect(() => {
    const preference = workbench.restoreView()
    if (!preference) {
      setTextScrollTop(0)
      return
    }
    setTextScrollTop(preference.scrollTop)
    requestAnimationFrame(() => {
      if (textViewport.current) {
        textViewport.current.scrollTop = preference.scrollTop
        textViewport.current.scrollLeft = preference.scrollLeft
      }
    })
  }, [workbench, state.selected?.artifactId, state.selected?.revision])

  const catalogWindow = useMemo(
    () =>
      artifactCatalogWindow(
        state.catalog.filtered,
        {
          scrollTop: catalogScrollTop,
          viewportHeight: catalogHeight,
          estimatedRowHeight: catalogRowHeight,
          overscan: 8,
        },
        {
          selectedKey: state.catalog.selectedKey,
          focusedKey: state.catalog.focusedKey,
        },
      ),
    [
      catalogHeight,
      catalogScrollTop,
      state.catalog.filtered,
      state.catalog.focusedKey,
      state.catalog.selectedKey,
    ],
  )

  const updateCatalogQuery = (value: string) => {
    setCatalogQuery(value)
    workbench.updateCatalogQuery({ text: value })
    setCatalogScrollTop(0)
    if (catalogViewport.current) catalogViewport.current.scrollTop = 0
  }

  const runContentSearch = async () => {
    if (!contentQuery.trim()) {
      setSearchBusy(false)
      return
    }
    setSearchBusy(true)
    try {
      await workbench.search(contentQuery, { maximumResults: 10_000 })
    } finally {
      setSearchBusy(false)
    }
  }

  const selectRow = (row: ArtifactCatalogVirtualRow, focus = true) => {
    setDownloadConfirmed(false)
    void workbench.select({
      artifactId: row.artifact.artifactId,
      revision: row.artifact.revision,
      source: "artifact",
      focus,
    })
  }

  const handleCatalogKey = (
    event: ReactKeyboardEvent<HTMLDivElement>,
  ) => {
    if (
      event.key !== "ArrowDown"
      && event.key !== "ArrowUp"
      && event.key !== "Home"
      && event.key !== "End"
      && event.key !== "Enter"
    ) {
      return
    }
    event.preventDefault()
    if (event.key === "Enter") {
      const artifact = state.catalog.filtered.find(
        (candidate) =>
          `${candidate.artifactId}@${candidate.revision}`
          === state.catalog.focusedKey,
      )
      if (artifact) {
        void workbench.select({
          artifactId: artifact.artifactId,
          revision: artifact.revision,
          source: "artifact",
          focus: true,
        })
      }
      return
    }
    const direction =
      event.key === "ArrowDown"
        ? "next"
        : event.key === "ArrowUp"
          ? "previous"
          : event.key === "Home"
            ? "first"
            : "last"
    const catalog = state.catalog.filtered
    const current = state.catalog.focusedKey
      ? catalog.findIndex(
          (artifact) =>
            `${artifact.artifactId}@${artifact.revision}`
            === state.catalog.focusedKey,
        )
      : -1
    const nextIndex =
      direction === "first"
        ? 0
        : direction === "last"
          ? catalog.length - 1
          : direction === "next"
            ? Math.min(catalog.length - 1, current + 1)
            : current < 0
              ? catalog.length - 1
              : Math.max(0, current - 1)
    const artifact = catalog[nextIndex]
    if (!artifact) return
    void workbench.select({
      artifactId: artifact.artifactId,
      revision: artifact.revision,
      source: "artifact",
      focus: true,
    })
    const targetTop = nextIndex * catalogRowHeight
    const viewport = catalogViewport.current
    if (viewport) {
      if (targetTop < viewport.scrollTop) viewport.scrollTop = targetTop
      if (targetTop + catalogRowHeight > viewport.scrollTop + viewport.clientHeight) {
        viewport.scrollTop =
          targetTop + catalogRowHeight - viewport.clientHeight
      }
    }
  }

  const saveScroll = (element: HTMLDivElement) => {
    setTextScrollTop(element.scrollTop)
    workbench.saveView({
      scrollTop: element.scrollTop,
      scrollLeft: element.scrollLeft,
      searchQuery: contentQuery || undefined,
      searchCursor: state.search?.matches[0]?.startOffset,
    })
  }

  const selected = state.selected
  const download = workbench.downloadInfo()
  const canDownload =
    download?.allowed
    && (!download.confirmationRequired || downloadConfirmed)

  return (
    <section
      className="artifact-workbench"
      aria-labelledby="artifact-workbench-heading"
      data-phase={state.phase}
    >
      <header className="artifact-workbench-header">
        <div>
          <h3 id="artifact-workbench-heading">Artifacts</h3>
          <p className="muted-copy">
            Canonical, integrity-checked task artifacts. Content is range-loaded
            and rendered as inert data.
          </p>
        </div>
        <ArtifactStatus state={state} />
      </header>

      <div className="artifact-workbench-layout">
        <aside
          className="artifact-catalog-panel"
          aria-label="Artifact catalog"
        >
          <div className="artifact-toolbar">
            <label className="artifact-search-label">
              <span>Filter catalog</span>
              <input
                value={catalogQuery}
                type="search"
                onChange={(event) => updateCatalogQuery(event.target.value)}
                placeholder="title, digest, node, worker…"
                aria-controls="artifact-catalog-list"
              />
            </label>
            <button
              type="button"
              onClick={() => void workbench.loadCatalog({ reset: true })}
              disabled={state.phase === "catalog-loading"}
            >
              Refresh
            </button>
          </div>
          <CatalogFacets state={state} />
          <div
            id="artifact-catalog-list"
            ref={catalogViewport}
            className="artifact-catalog-viewport"
            role="listbox"
            aria-label="Artifact revisions"
            aria-activedescendant={
              state.catalog.focusedKey
                ? catalogDomId(state.catalog.focusedKey)
                : undefined
            }
            tabIndex={0}
            onKeyDown={handleCatalogKey}
            onScroll={(event) =>
              setCatalogScrollTop(event.currentTarget.scrollTop)
            }
          >
            <div
              className="artifact-catalog-virtual-space"
              style={{ height: `${catalogWindow.totalHeight}px` }}
            >
              {catalogWindow.rows.map((row) => (
                <CatalogRow
                  key={row.key}
                  row={row}
                  onSelect={() => selectRow(row)}
                />
              ))}
            </div>
            {!state.catalog.filtered.length ? (
              <p className="muted-copy artifact-empty">
                {state.catalog.phase === "loading"
                  ? "Loading canonical artifact references…"
                  : "No artifacts match this catalog filter."}
              </p>
            ) : null}
          </div>
          {state.catalog.cursor ? (
            <button
              type="button"
              className="artifact-load-more"
              onClick={() => void workbench.loadNextCatalogPage()}
            >
              Load next catalog page
            </button>
          ) : null}
        </aside>

        <main className="artifact-viewer-panel" aria-live="polite">
          {selected ? (
            <>
              <ArtifactContractHeader artifact={selected} />
              <div className="artifact-toolbar artifact-content-toolbar">
                {state.viewer?.capabilities.search ? (
                  <form
                    className="artifact-content-search"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void runContentSearch()
                    }}
                  >
                    <label>
                      <span className="sr-only">Search artifact content</span>
                      <input
                        type="search"
                        value={contentQuery}
                        onChange={(event) =>
                          setContentQuery(event.target.value)
                        }
                        placeholder="Search loaded content"
                      />
                    </label>
                    <button type="submit" disabled={searchBusy}>
                      {searchBusy ? "Searching…" : "Search"}
                    </button>
                  </form>
                ) : null}
                <button
                  type="button"
                  onClick={() => workbench.bookmark()}
                >
                  Bookmark revision
                </button>
                <button
                  type="button"
                  onClick={() => workbench.bookmark({ pinned: true })}
                >
                  Pin
                </button>
                {download?.confirmationRequired && !downloadConfirmed ? (
                  <button
                    type="button"
                    onClick={() => setDownloadConfirmed(true)}
                    disabled={!download.allowed}
                    title={download.reasons.join("; ")}
                  >
                    Confirm download
                  </button>
                ) : null}
                {download ? (
                  <a
                    className={`artifact-download ${canDownload ? "" : "is-disabled"}`}
                    href={canDownload ? download.href : undefined}
                    aria-disabled={!canDownload}
                    download
                    onClick={(event) => {
                      if (!canDownload) event.preventDefault()
                    }}
                    title={
                      canDownload
                        ? "Download the verified artifact range"
                        : download.reasons.join("; ")
                    }
                  >
                    Download
                  </a>
                ) : null}
              </div>
              <SecurityBanner state={state} />
              {state.search ? (
                <p className="artifact-search-summary" role="status">
                  {state.search.matches.length.toLocaleString()} match(es) in{" "}
                  {state.search.scannedLines.toLocaleString()} loaded line(s)
                  {state.search.truncated ? " · result limit reached" : ""}
                </p>
              ) : null}
              <ArtifactModelView
                model={state.model}
                phase={state.phase}
                scrollTop={textScrollTop}
                viewportHeight={textHeight}
                viewportRef={textViewport}
                onScroll={saveScroll}
              />
              {state.content && !state.content.complete ? (
                <div className="artifact-partial-controls">
                  <span>
                    {state.content.loadedBytes.toLocaleString()} /{" "}
                    {state.content.totalBytes.toLocaleString()} bytes admitted
                  </span>
                  <button
                    type="button"
                    onClick={() => void workbench.loadMoreContent()}
                    disabled={state.phase === "artifact-loading"}
                  >
                    Load next verified range
                  </button>
                  <button
                    type="button"
                    onClick={() => workbench.cancel("User cancelled artifact reads.")}
                  >
                    Cancel reads
                  </button>
                </div>
              ) : null}
            </>
          ) : (
            <div className="artifact-empty-viewer">
              <h4>Select an artifact revision</h4>
              <p className="muted-copy">
                The viewer will request metadata and bounded content only after
                a canonical artifact reference is selected.
              </p>
            </div>
          )}
          {state.error ? (
            <div className="artifact-error" role="alert">
              <strong>{state.error.code}</strong>
              <span>{state.error.message}</span>
              {state.error.retryable ? (
                <button
                  type="button"
                  onClick={() => {
                    if (state.selected) {
                      void workbench.select({
                        artifactId: state.selected.artifactId,
                        revision: state.selected.revision,
                        source: "artifact",
                        focus: true,
                      })
                    } else {
                      void workbench.loadCatalog({ reset: true })
                    }
                  }}
                >
                  Retry guarded read
                </button>
              ) : null}
            </div>
          ) : null}
          <BookmarkRail
            state={state}
            onOpen={(bookmark) =>
              void workbench.select({
                artifactId: bookmark.artifactId,
                revision: bookmark.revision,
                source: bookmark.source,
                sourceEventId: bookmark.sourceEventId,
                focus: true,
              })
            }
            onTogglePin={(id) => workbench.toggleBookmarkPin(id)}
            onRemove={(id) => workbench.removeBookmark(id)}
          />
        </main>
      </div>
    </section>
  )
}

function ArtifactStatus({ state }: { state: ArtifactWorkbenchState }) {
  const cache = state.content
    ? `${state.content.loadedBytes.toLocaleString()} verified bytes`
    : "metadata only"
  return (
    <div className="artifact-status" role="status">
      <span className={`status-marker status-${statusTone(state.phase)}`} />
      <span>{humanPhase(state.phase)}</span>
      <span className="tag tag-muted">{cache}</span>
    </div>
  )
}

function CatalogFacets({ state }: { state: ArtifactWorkbenchState }) {
  const facets = state.catalog.facets
  return (
    <dl className="artifact-catalog-facets">
      <div>
        <dt>Revisions</dt>
        <dd>{state.catalog.filtered.length.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Total bytes</dt>
        <dd>{facets.totalBytes.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Verified</dt>
        <dd>{facets.verifiedBytes.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Legacy</dt>
        <dd>{facets.legacyCount.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Missing</dt>
        <dd>{facets.missingCount.toLocaleString()}</dd>
      </div>
    </dl>
  )
}

function CatalogRow({
  row,
  onSelect,
}: {
  row: ArtifactCatalogVirtualRow
  onSelect: () => void
}) {
  const artifact = row.artifact
  const style: CSSProperties = {
    position: "absolute",
    top: row.top,
    height: row.height,
    left: 0,
    right: 0,
  }
  return (
    <button
      id={catalogDomId(row.key)}
      className="artifact-catalog-row"
      style={style}
      type="button"
      role="option"
      aria-selected={row.selected}
      data-focused={row.focused || undefined}
      data-artifact-id={artifact.artifactId}
      data-artifact-revision={artifact.revision}
      onClick={onSelect}
    >
      <span className="artifact-catalog-row-title">
        <strong>{artifact.title || artifact.artifactId}</strong>
        <span className={`tag artifact-family-${artifact.contentFamily}`}>
          {artifact.contentFamily}
        </span>
      </span>
      <span className="artifact-catalog-row-meta">
        <span>{artifact.sizeBytes.toLocaleString()} bytes</span>
        <span>{artifact.mediaType}</span>
        <span>{artifact.status.integrity}</span>
      </span>
      <code>{artifact.revision.slice(0, 23)}…</code>
    </button>
  )
}

function ArtifactContractHeader({ artifact }: { artifact: ArtifactContract }) {
  return (
    <header className="artifact-contract-header">
      <div>
        <span className="artifact-kind">{artifact.kind}</span>
        <h4>{artifact.title || artifact.artifactId}</h4>
        <code>{artifact.artifactId}</code>
      </div>
      <dl className="artifact-contract-facts">
        <div>
          <dt>Revision</dt>
          <dd title={artifact.revision}>{artifact.revision.slice(0, 23)}…</dd>
        </div>
        <div>
          <dt>Integrity</dt>
          <dd>{artifact.status.integrity}</dd>
        </div>
        <div>
          <dt>Media</dt>
          <dd>{artifact.mediaType}</dd>
        </div>
        <div>
          <dt>Encoding</dt>
          <dd>{artifact.encoding ?? "binary"}</dd>
        </div>
        <div>
          <dt>Security</dt>
          <dd>{artifact.security.label}</dd>
        </div>
        <div>
          <dt>Producer</dt>
          <dd>
            {artifact.producer.nodeId
              ?? artifact.producer.workerId
              ?? "unknown"}
          </dd>
        </div>
      </dl>
    </header>
  )
}

function SecurityBanner({ state }: { state: ArtifactWorkbenchState }) {
  const artifact = state.selected
  if (!artifact) return null
  const quarantine =
    state.admission?.quarantined
    || artifact.security.trust !== "trusted"
  const transformations =
    state.model?.kind === "text" || state.model?.kind === "markdown"
      ? state.model.notices
      : []
  return (
    <div
      className={`artifact-security-banner ${quarantine ? "is-quarantined" : ""}`}
      role={quarantine ? "alert" : "status"}
    >
      <strong>
        {quarantine ? "Untrusted content quarantine" : "Verified inert content"}
      </strong>
      <span>
        This viewer cannot execute HTML, SVG, scripts, commands, tools, or
        permission actions.
      </span>
      {transformations.map((notice) => (
        <span key={notice}>{notice}</span>
      ))}
    </div>
  )
}

function ArtifactModelView({
  model,
  phase,
  scrollTop,
  viewportHeight,
  viewportRef,
  onScroll,
}: {
  model?: ArtifactRenderableModel
  phase: ArtifactWorkbenchState["phase"]
  scrollTop: number
  viewportHeight: number
  viewportRef: React.RefObject<HTMLDivElement | null>
  onScroll: (element: HTMLDivElement) => void
}) {
  if (!model) {
    return (
      <div className="artifact-viewer-loading" role="status">
        {phase === "artifact-loading"
          ? "Verifying immutable revision and loading a bounded range…"
          : "No admitted artifact content."}
      </div>
    )
  }
  if (model.kind === "text") {
    return (
      <TextViewer
        model={model}
        scrollTop={scrollTop}
        viewportHeight={viewportHeight}
        viewportRef={viewportRef}
        onScroll={onScroll}
      />
    )
  }
  if (model.kind === "markdown") return <MarkdownViewer model={model} />
  if (model.kind === "json") return <JsonViewer model={model} />
  if (model.kind === "binary") return <BinaryViewer model={model} />
  if (
    model.kind === "image"
    || model.kind === "audio"
    || model.kind === "video"
  ) {
    return <MediaViewer model={model} />
  }
  if (model.kind === "metadata" || model.kind === "refused") {
    return <MetadataViewer model={model} />
  }
  return null
}

function TextViewer({
  model,
  scrollTop,
  viewportHeight,
  viewportRef,
  onScroll,
}: {
  model: ArtifactTextViewerModel
  scrollTop: number
  viewportHeight: number
  viewportRef: React.RefObject<HTMLDivElement | null>
  onScroll: (element: HTMLDivElement) => void
}) {
  const start = Math.max(0, Math.floor(scrollTop / textLineHeight) - 30)
  const end = Math.min(
    model.lines.length,
    Math.ceil((scrollTop + viewportHeight) / textLineHeight) + 30,
  )
  const rows = model.lines.slice(start, end)
  return (
    <div
      ref={viewportRef}
      className="artifact-text-viewport"
      tabIndex={0}
      aria-label="Artifact text content"
      onScroll={(event) => onScroll(event.currentTarget)}
    >
      <div
        className="artifact-text-virtual-space"
        style={{ height: model.lines.length * textLineHeight }}
      >
        {rows.map((line, relative) => (
          <div
            key={`${line.lineNumber}:${line.startOffset}`}
            className="artifact-text-line"
            data-line-number={line.lineNumber}
            style={{
              position: "absolute",
              top: (start + relative) * textLineHeight,
              height: textLineHeight,
              left: 0,
              right: 0,
            }}
          >
            <span className="artifact-line-number" aria-hidden="true">
              {line.lineNumber}
            </span>
            <code>{renderHighlightedLine(line.text, model.highlighted.get(line.lineNumber))}</code>
            {!line.complete ? (
              <span className="artifact-partial-line" title="Partial loaded line">
                …
              </span>
            ) : null}
          </div>
        ))}
      </div>
      {model.partial ? (
        <div className="artifact-partial-watermark">partial verified ranges</div>
      ) : null}
    </div>
  )
}

function MarkdownViewer({ model }: { model: ArtifactMarkdownViewerModel }) {
  return (
    <article
      className="artifact-markdown-viewer"
      data-quarantined={model.quarantined || undefined}
    >
      {model.blocks.map((block, index) => (
        <MarkdownBlockView
          key={`${block.kind}:${block.sourceLine}:${index}`}
          block={block}
        />
      ))}
      {model.truncated ? (
        <p className="artifact-partial-watermark">
          Markdown view is bounded; remaining bytes are not structurally
          rendered.
        </p>
      ) : null}
    </article>
  )
}

function MarkdownBlockView({ block }: { block: MarkdownBlock }) {
  if (block.kind === "heading") {
    const content = renderMarkdownInline(block.children)
    if (block.level === 1) return <h1 id={block.id}>{content}</h1>
    if (block.level === 2) return <h2 id={block.id}>{content}</h2>
    if (block.level === 3) return <h3 id={block.id}>{content}</h3>
    if (block.level === 4) return <h4 id={block.id}>{content}</h4>
    if (block.level === 5) return <h5 id={block.id}>{content}</h5>
    return <h6 id={block.id}>{content}</h6>
  }
  if (block.kind === "paragraph") {
    return <p>{renderMarkdownInline(block.children)}</p>
  }
  if (block.kind === "code") {
    return (
      <pre data-language={block.language}>
        <code>{block.text}</code>
        {!block.closed ? <span>Unclosed code fence</span> : null}
      </pre>
    )
  }
  if (block.kind === "quote") {
    return <blockquote>{renderMarkdownInline(block.children)}</blockquote>
  }
  if (block.kind === "rule") return <hr />
  if (block.kind === "notice") {
    return (
      <div
        className={`artifact-markdown-notice tone-${block.tone}`}
        role="alert"
      >
        {block.text}
      </div>
    )
  }
  if (block.kind === "list") {
    const items = block.items.map((item) => (
      <li key={`${item.sourceLine}:${inlineText(item.children)}`}>
        {item.checked !== undefined ? (
          <input type="checkbox" checked={item.checked} readOnly tabIndex={-1} />
        ) : null}
        {renderMarkdownInline(item.children)}
      </li>
    ))
    return block.ordered ? <ol start={block.start}>{items}</ol> : <ul>{items}</ul>
  }
  return (
    <div className="artifact-markdown-table-wrap">
      <table>
        <thead>
          <tr>
            {block.header.map((cell, index) => (
              <th
                key={index}
                style={{ textAlign: block.alignments[index] }}
              >
                {renderMarkdownInline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  style={{ textAlign: block.alignments[cellIndex] }}
                >
                  {renderMarkdownInline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function JsonViewer({ model }: { model: ArtifactJsonViewerModel }) {
  return (
    <div
      className="artifact-json-viewer"
      role="tree"
      aria-label="Artifact JSON tree"
      data-valid={model.valid}
    >
      {!model.valid && model.error ? (
        <div className="artifact-json-error" role="alert">
          {model.error}
        </div>
      ) : null}
      {model.rows.slice(0, 10_000).map((row) => (
        <JsonRow key={row.path} row={row} />
      ))}
      {model.rows.length > 10_000 ? (
        <p className="artifact-partial-watermark">
          JSON row window capped at 10,000 visible rows.
        </p>
      ) : null}
    </div>
  )
}

function JsonRow({ row }: { row: JsonFlatRow }) {
  const node = row.node
  return (
    <div
      className="artifact-json-row"
      role="treeitem"
      aria-level={node.depth + 1}
      aria-expanded={node.expandable ? node.expanded : undefined}
      aria-selected={row.selected}
      data-matched={row.matched || undefined}
      style={{ paddingLeft: `${node.depth * 18 + 8}px` }}
    >
      <span className="artifact-json-disclosure">
        {node.expandable ? (node.expanded ? "▾" : "▸") : "·"}
      </span>
      <code className="artifact-json-key">{node.key}</code>
      <span>:</span>
      {node.kind === "object" || node.kind === "array" ? (
        <span>
          {node.kind === "object" ? "{" : "["}
          {!node.expanded ? `${node.childCount} item(s)` : ""}
          {node.kind === "object" ? "}" : "]"}
        </span>
      ) : (
        <code className={`artifact-json-value kind-${node.kind}`}>
          {jsonValue(node.value)}
        </code>
      )}
    </div>
  )
}

function BinaryViewer({ model }: { model: ArtifactBinaryViewerModel }) {
  return (
    <div className="artifact-binary-viewer">
      {model.executableWarning ? (
        <div className="artifact-markdown-notice tone-danger" role="alert">
          Active or executable content is displayed only as inert hexadecimal
          bytes.
        </div>
      ) : null}
      <div className="artifact-hex-table" role="table">
        {model.rows.map((row) => (
          <div
            className="artifact-hex-row"
            role="row"
            key={row.offset}
          >
            <code role="cell" className="artifact-hex-address">
              {row.address}
            </code>
            <code role="cell" className="artifact-hex-bytes">
              {row.hex}
            </code>
            <code role="cell" className="artifact-hex-ascii">
              {row.ascii}
            </code>
          </div>
        ))}
      </div>
      {model.partial ? (
        <p className="artifact-partial-watermark">
          Showing bytes {model.rangeOffset.toLocaleString()}–
          {model.rangeEndExclusive.toLocaleString()} of{" "}
          {model.artifact.sizeBytes.toLocaleString()}.
        </p>
      ) : null}
    </div>
  )
}

function MediaViewer({ model }: { model: ArtifactMediaViewerModel }) {
  if (model.kind === "image") {
    return (
      <div
        className="artifact-media-viewer artifact-image-viewer"
        data-quarantined={model.quarantined || undefined}
      >
        <img
          src={model.sourceUrl}
          alt={model.alt}
          draggable={false}
          style={{
            transform:
              `translate(${model.panX}px, ${model.panY}px) `
              + `scale(${model.zoom})`,
          }}
        />
      </div>
    )
  }
  if (model.kind === "audio") {
    return (
      <div
        className="artifact-media-viewer artifact-audio-viewer"
        data-quarantined={model.quarantined || undefined}
      >
        <audio
          src={model.sourceUrl}
          controls={model.controls}
          autoPlay={model.autoplay}
          preload="metadata"
          aria-label={model.alt}
        />
      </div>
    )
  }
  return (
    <div
      className="artifact-media-viewer artifact-video-viewer"
      data-quarantined={model.quarantined || undefined}
    >
      <video
        src={model.sourceUrl}
        controls={model.controls}
        autoPlay={model.autoplay}
        preload="metadata"
        aria-label={model.alt}
      />
    </div>
  )
}

function MetadataViewer({ model }: { model: ArtifactMetadataViewerModel }) {
  return (
    <div
      className={`artifact-metadata-viewer ${model.kind === "refused" ? "is-refused" : ""}`}
    >
      <p role={model.kind === "refused" ? "alert" : "status"}>
        {model.reason}
      </p>
      <dl>
        {model.facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>
              <code>{fact.value}</code>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function BookmarkRail({
  state,
  onOpen,
  onTogglePin,
  onRemove,
}: {
  state: ArtifactWorkbenchState
  onOpen: (bookmark: ArtifactWorkbenchState["bookmarks"][number]) => void
  onTogglePin: (id: string) => void
  onRemove: (id: string) => void
}) {
  if (!state.bookmarks.length) return null
  return (
    <aside className="artifact-bookmark-rail" aria-label="Artifact bookmarks">
      <h4>Bookmarks and report references</h4>
      <ul>
        {state.bookmarks.map((bookmark) => (
          <li key={bookmark.id}>
            <button type="button" onClick={() => onOpen(bookmark)}>
              <strong>{bookmark.title}</strong>
              <span>{bookmark.source}</span>
              <code>{bookmark.revision.slice(0, 16)}…</code>
            </button>
            <button
              type="button"
              onClick={() => onTogglePin(bookmark.id)}
              aria-pressed={bookmark.pinned}
            >
              {bookmark.pinned ? "Unpin" : "Pin"}
            </button>
            <button type="button" onClick={() => onRemove(bookmark.id)}>
              Remove
            </button>
          </li>
        ))}
      </ul>
    </aside>
  )
}

function renderHighlightedLine(
  text: string,
  highlights: readonly {
    startColumn: number
    endColumn: number
    matchId: string
  }[] | undefined,
): ReactNode {
  if (!highlights?.length) return text || " "
  const nodes: ReactNode[] = []
  let cursor = 0
  for (const highlight of highlights) {
    const start = Math.max(cursor, highlight.startColumn - 1)
    const end = Math.max(start, highlight.endColumn - 1)
    if (start > cursor) nodes.push(text.slice(cursor, start))
    nodes.push(
      <mark key={highlight.matchId}>
        {text.slice(start, end)}
      </mark>,
    )
    cursor = end
  }
  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

function renderMarkdownInline(nodes: readonly MarkdownInline[]): ReactNode {
  return nodes.map((node, index) => {
    const key = `${node.kind}:${index}`
    if (node.kind === "text") return <Fragment key={key}>{node.text}</Fragment>
    if (node.kind === "code") return <code key={key}>{node.text}</code>
    if (node.kind === "break") return <br key={key} />
    if (node.kind === "emphasis") {
      return <em key={key}>{renderMarkdownInline(node.children)}</em>
    }
    if (node.kind === "strong") {
      return <strong key={key}>{renderMarkdownInline(node.children)}</strong>
    }
    if (node.kind === "strike") {
      return <del key={key}>{renderMarkdownInline(node.children)}</del>
    }
    if (!node.link.allowed || !node.link.href) {
      return (
        <span
          key={key}
          className="artifact-link-refused"
          title={node.link.reason}
        >
          {renderMarkdownInline(node.children)}
        </span>
      )
    }
    return (
      <a
        key={key}
        href={node.link.href}
        target={node.link.external ? "_blank" : undefined}
        rel={node.link.external ? "noopener noreferrer" : undefined}
        referrerPolicy="no-referrer"
      >
        {renderMarkdownInline(node.children)}
      </a>
    )
  })
}

function inlineText(nodes: readonly MarkdownInline[]): string {
  return nodes
    .map((node) => {
      if (node.kind === "text" || node.kind === "code") return node.text
      if (node.kind === "break") return "\n"
      return inlineText(node.children)
    })
    .join("")
}

function jsonValue(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value)
  if (value === null) return "null"
  return String(value)
}

function catalogDomId(key: string): string {
  return `artifact-catalog-${key.replace(/[^A-Za-z0-9_-]+/g, "-")}`
}

function statusTone(
  phase: ArtifactWorkbenchState["phase"],
): "running" | "completed" | "failed" | "pending" {
  if (phase === "failed" || phase === "disconnected") return "failed"
  if (phase === "artifact-ready" || phase === "catalog-ready") return "completed"
  if (phase === "catalog-loading" || phase === "artifact-loading") return "running"
  return "pending"
}

function humanPhase(phase: ArtifactWorkbenchState["phase"]): string {
  return phase.replace(/-/g, " ")
}
