import { recordLabel } from "../../evidence/record-copy.ts"
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type ReactNode,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useOnlineStatus, useProjectionSelector } from "../../../app/hooks.ts"
import {
  selectArtifactsForTask,
  selectEventsForTask,
  selectRecoveriesForTask,
  selectRevision,
} from "../../../state/selectors.ts"
import {
  dispatchArtifactNavigation,
} from "../../artifacts/catalog.ts"
import {
  ArtifactWorkbenchRuntime,
  type ArtifactWorkbenchState,
} from "../../artifacts/runtime.ts"
import {
  BrowserControlAction,
  BrowserStepPhase,
  BrowserViewerPhase,
  safeExternalObservedUrl,
  type BrowserAction,
  type BrowserActionResult,
  type BrowserControlReceipt,
  type BrowserDomNodeSummary,
  type BrowserDownload,
  type BrowserFrame,
  type BrowserHealth,
  type BrowserScreenshot,
  type BrowserSessionProjection,
  type BrowserStep,
  type BrowserTarget,
  type BrowserViewerState,
} from "../contracts.ts"
import { BrowserViewerRuntime } from "../runtime.ts"
import { PermissionSurfaceStatus } from "../../permissions/index.ts"

interface BrowserWorkbenchProps {
  runtime: WorkbenchRuntime
  task: TaskProjection
}

interface BrowserControlDraft {
  url: string
  reason: string
}

const STATUS_ORDER = Object.freeze([
  BrowserStepPhase.FAILED,
  BrowserStepPhase.RUNNING,
  BrowserStepPhase.WAITING_PERMISSION,
  BrowserStepPhase.PARTIAL,
  BrowserStepPhase.CANCELLED,
  BrowserStepPhase.COMPLETED,
  BrowserStepPhase.QUEUED,
])

function metadataBoolean(
  metadata: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): boolean {
  for (const key of keys) {
    const value = metadata[key]
    if (typeof value === "boolean") return value
    if (
      typeof value === "string"
      && ["1", "true", "yes", "sealed", "sealed_autonomous"].includes(
        value.toLocaleLowerCase(),
      )
    ) return true
  }
  return false
}

function taskSealed(task: TaskProjection): boolean {
  const mode = String(
    task.metadata.competition_mode
    ?? task.metadata.permission_mode
    ?? task.metadata.benchmark_mode
    ?? "",
  ).toLocaleLowerCase()
  return (
    metadataBoolean(
      task.metadata,
      "sealed",
      "formal_benchmark",
      "sealed_autonomous",
    )
    || mode.includes("sealed")
  )
}

function formatTime(value: string | undefined): string {
  if (!value) return "—"
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(timestamp)
}

function ObservedPageLocation({ url }: { url?: string }): ReactNode {
  const href = safeExternalObservedUrl(url)
  if (!href) {
    return <span title="Observed page URL is display-only">{url || "No current URL"}</span>
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title="Open current URL in a separate browser tab"
    >
      {url}
    </a>
  )
}

function compactIdentity(value: string | undefined, length = 18): string {
  if (!value) return "—"
  if (value.length <= length) return value
  const head = Math.max(6, Math.floor((length - 1) / 2))
  return `${value.slice(0, head)}…${value.slice(-(length - head - 1))}`
}

function byteSize(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—"
  if (value < 1_024) return `${value} B`
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KiB`
  return `${(value / 1_048_576).toFixed(1)} MiB`
}

function phaseTone(phase: string): string {
  if (["ready", "healthy", "completed", "applied", "observed"].includes(phase)) {
    return "is-success"
  }
  if (["loading", "running", "reconnecting", "submitting", "accepted"].includes(phase)) {
    return "is-running"
  }
  if (["partial", "degraded", "waiting_permission", "queued"].includes(phase)) {
    return "is-warning"
  }
  if (["failed", "unhealthy", "crashed", "denied", "timed_out", "stale"].includes(phase)) {
    return "is-danger"
  }
  return "is-muted"
}

function selectedSession(state: BrowserViewerState): BrowserSessionProjection | undefined {
  const sessionId =
    state.selection.sessionId
    ?? state.projection.selectedSessionId
    ?? state.projection.activeSessionId
  return state.projection.sessions.find(
    (session) => session.scope.browserSessionId === sessionId,
  ) ?? state.projection.sessions.at(-1)
}

function selectedStep(
  state: BrowserViewerState,
  session: BrowserSessionProjection | undefined,
): BrowserStep | undefined {
  if (!session) return undefined
  return session.steps.find(
    (step) => step.stepId === state.selection.stepId,
  ) ?? session.steps.at(-1)
}

function selectedAction(
  state: BrowserViewerState,
  session: BrowserSessionProjection | undefined,
  step: BrowserStep | undefined,
): BrowserAction | undefined {
  if (!session) return undefined
  return session.actions.find(
    (action) => action.actionId === state.selection.actionId,
  ) ?? (
    step
      ? session.actions
        .filter((action) => step.actionIds.includes(action.actionId))
        .at(-1)
      : undefined
  )
}

function resultForAction(
  session: BrowserSessionProjection | undefined,
  step: BrowserStep | undefined,
  action: BrowserAction | undefined,
): BrowserActionResult | undefined {
  if (!session || !step) return undefined
  return session.results.find(
    (result) =>
      result.actionId === action?.actionId
      || step.resultIds.includes(result.resultId),
  )
}

function screenshotForStep(
  session: BrowserSessionProjection | undefined,
  step: BrowserStep | undefined,
  selectedArtifactId: string | undefined,
): BrowserScreenshot | undefined {
  if (!session) return undefined
  return session.screenshots.find(
    (screenshot) => screenshot.artifactId === selectedArtifactId,
  ) ?? (
    step
      ? session.screenshots.find(
        (screenshot) => step.screenshotArtifactIds.includes(screenshot.artifactId),
      )
      : undefined
  ) ?? session.screenshots.find((screenshot) => screenshot.current)
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function HealthStrip({ health }: { health: BrowserHealth }) {
  return (
    <dl className="browser-health-strip" data-browser-health={health.phase}>
      <div>
        <dt>会话</dt>
        <dd>{health.phase}</dd>
      </div>
      <div>
        <dt>重连次数</dt>
        <dd>{health.reconnectCount}</dd>
      </div>
      <div>
        <dt>进程批次</dt>
        <dd>{health.processEpoch ?? "—"}</dd>
      </div>
      <div>
        <dt>心跳</dt>
        <dd>{formatTime(health.lastHeartbeatAt)}</dd>
      </div>
      <div>
        <dt>恢复</dt>
        <dd>{health.recoveryIds.length}</dd>
      </div>
      <div>
        <dt>信号</dt>
        <dd>{health.signalIds.length}</dd>
      </div>
    </dl>
  )
}

function SessionRail({
  state,
  viewer,
}: {
  state: BrowserViewerState
  viewer: BrowserViewerRuntime
}) {
  return (
    <aside className="browser-session-rail" aria-label="Browser sessions">
      <div className="browser-subheading">
        <h4>浏览器会话</h4>
        <span>{state.projection.sessions.length}</span>
      </div>
      {state.projection.sessions.length ? (
        <ol>
          {state.projection.sessions.map((session) => {
            const active =
              session.scope.browserSessionId
              === (
                state.selection.sessionId
                ?? state.projection.selectedSessionId
              )
            return (
              <li key={`${session.scope.browserSessionId}:${session.scope.workerRequestId}`}>
                <button
                  className={`browser-session-button${active ? " is-selected" : ""}`}
                  type="button"
                  onClick={() => viewer.selectSession(session.scope.browserSessionId)}
                >
                  <span className={`browser-phase-dot ${phaseTone(session.status)}`} />
                  <span>
                    <strong>{compactIdentity(session.scope.browserSessionId)}</strong>
                    <small>
                      {session.steps.length} steps · #{session.lastSequence}
                    </small>
                  </span>
                </button>
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="browser-empty-copy">
          此任务暂无浏览器会话。
        </p>
      )}
    </aside>
  )
}

function targetLabel(target: BrowserTarget): string {
  return target.title || target.url || target.targetId
}

function TargetTree({
  session,
  selectedTargetId,
}: {
  session: BrowserSessionProjection
  selectedTargetId?: string
}) {
  const targets = useMemo(
    () => [...session.targets].sort(
      (left, right) =>
        left.depth - right.depth
        || left.createdSequence - right.createdSequence
        || left.targetId.localeCompare(right.targetId),
    ),
    [session],
  )
  const frames = useMemo(
    () => [...session.frames].sort(
      (left, right) =>
        left.depth - right.depth
        || left.sequence - right.sequence
        || left.frameId.localeCompare(right.frameId),
    ),
    [session],
  )
  return (
    <section className="browser-target-tree" aria-labelledby="browser-target-tree-heading">
      <div className="browser-subheading">
        <h4 id="browser-target-tree-heading">目标与页面框架</h4>
        <span>{targets.length + frames.length}</span>
      </div>
      <ol>
        {targets.map((target) => (
          <li
            className={`browser-target-row${
              target.targetId === selectedTargetId ? " is-selected" : ""
            }`}
            key={target.targetId}
            style={{ paddingInlineStart: `${12 + Math.min(6, target.depth) * 14}px` }}
          >
            <span className={`browser-target-kind is-${target.kind}`}>{target.kind}</span>
            <span>
              <strong>{targetLabel(target)}</strong>
              <small>{target.url || target.targetId}</small>
            </span>
            {target.active ? <span className="tag">active</span> : null}
            {target.crashed ? <span className="tag tag-danger">crashed</span> : null}
          </li>
        ))}
        {frames.map((frame) => (
          <FrameRow frame={frame} key={frame.frameId} />
        ))}
      </ol>
    </section>
  )
}

function FrameRow({ frame }: { frame: BrowserFrame }) {
  return (
    <li
      className="browser-target-row browser-frame-row"
      style={{ paddingInlineStart: `${28 + Math.min(6, frame.depth) * 14}px` }}
    >
      <span className="browser-target-kind is-frame">frame</span>
      <span>
        <strong>{frame.name || compactIdentity(frame.frameId)}</strong>
        <small>{frame.url || frame.securityOrigin || "No frame URL"}</small>
      </span>
      {frame.main ? <span className="tag tag-muted">main</span> : null}
    </li>
  )
}

function StepHistory({
  state,
  viewer,
  viewport,
}: {
  state: BrowserViewerState
  viewer: BrowserViewerRuntime
  viewport: React.RefObject<HTMLDivElement | null>
}) {
  const result = viewer.history
  const stepById = useMemo(
    () => new Map(result.steps.map((step) => [step.stepId, step])),
    [result],
  )
  const visible = state.window.stepIds
    .map((stepId) => stepById.get(stepId))
    .filter((step): step is BrowserStep => Boolean(step))
  return (
    <div
      className="browser-step-viewport"
      ref={viewport}
      role="listbox"
      aria-label="Browser action history"
      aria-activedescendant={
        state.selection.stepId
          ? `browser-step-${state.selection.stepId}`
          : undefined
      }
      onScroll={(event) => {
        viewer.updateWindow(
          event.currentTarget.scrollTop,
          event.currentTarget.clientHeight,
        )
      }}
    >
      <div style={{ height: `${state.window.beforeHeight}px` }} aria-hidden="true" />
      {visible.map((step) => {
        const selected = step.stepId === state.selection.stepId
        return (
          <button
            className={`browser-step-row${selected ? " is-selected" : ""}`}
            id={`browser-step-${step.stepId}`}
            key={step.stepId}
            type="button"
            role="option"
            aria-selected={selected}
            onClick={() => viewer.selectStep(step.stepId, step.actionIds.at(-1))}
          >
            <span className={`browser-phase-dot ${phaseTone(step.status)}`} />
            <span className="browser-step-index">#{step.index}</span>
            <span className="browser-step-copy">
              <strong>{step.title || step.url || compactIdentity(step.stepId)}</strong>
              <small>
                seq {step.startSequence}–{step.endSequence}
                {" · "}{step.actionIds.length} action
                {step.actionIds.length === 1 ? "" : "s"}
                {step.screenshotArtifactIds.length
                  ? ` · ${step.screenshotArtifactIds.length} screenshot`
                  : ""}
              </small>
              {step.errorMessage ? <em>{step.errorMessage}</em> : null}
            </span>
            <span className={`browser-step-status ${phaseTone(step.status)}`}>
              {step.status}
            </span>
          </button>
        )
      })}
      <div style={{ height: `${state.window.afterHeight}px` }} aria-hidden="true" />
      {!result.steps.length ? (
        <p className="browser-empty-copy">
          没有匹配的操作步骤。
        </p>
      ) : null}
    </div>
  )
}

function HistoryToolbar({
  state,
  viewer,
}: {
  state: BrowserViewerState
  viewer: BrowserViewerRuntime
}) {
  const result = viewer.history
  return (
    <div className="browser-history-toolbar">
      <label className="browser-search-field">
        <span>搜索操作记录</span>
        <input
          type="search"
          value={state.filters.query}
          placeholder="搜索网址、操作、结果或错误…"
          onChange={(event) => viewer.setFilters({ query: event.currentTarget.value })}
        />
      </label>
      <div className="browser-filter-actions" aria-label="Browser history filters">
        <label>
          <input
            type="checkbox"
            checked={state.filters.failuresOnly}
            onChange={(event) =>
              viewer.setFilters({ failuresOnly: event.currentTarget.checked })
            }
          />
          失败
        </label>
        <label>
          <input
            type="checkbox"
            checked={state.filters.downloadsOnly}
            onChange={(event) =>
              viewer.setFilters({ downloadsOnly: event.currentTarget.checked })
            }
          />
          下载
        </label>
        <label>
          <input
            type="checkbox"
            checked={state.filters.popupsOnly}
            onChange={(event) =>
              viewer.setFilters({ popupsOnly: event.currentTarget.checked })
            }
          />
          弹出窗口
        </label>
        <label>
          <input
            type="checkbox"
            checked={state.selection.followLive}
            onChange={(event) => viewer.followLive(event.currentTarget.checked)}
          />
          跟随实时更新
        </label>
        <button
          className="button button-secondary button-compact"
          type="button"
          onClick={() => viewer.clearFilters()}
        >
          清除
        </button>
      </div>
      <div className="browser-history-navigation">
        <button
          className="button button-secondary button-compact"
          type="button"
          disabled={!result.steps.length}
          onClick={() => viewer.previousStep()}
          aria-label="Previous browser step"
        >
          上一步
        </button>
        <span>
          {result.steps.length} visible
          {result.hiddenCount ? ` · ${result.hiddenCount} hidden` : ""}
        </span>
        <button
          className="button button-secondary button-compact"
          type="button"
          disabled={!result.steps.length}
          onClick={() => viewer.nextStep()}
          aria-label="Next browser step"
        >
          下一步
        </button>
      </div>
    </div>
  )
}

function BrowserArtifactPreview({
  runtime,
  taskId,
  screenshot,
}: {
  runtime: WorkbenchRuntime
  taskId: string
  screenshot?: BrowserScreenshot
}) {
  const workbench = useMemo(
    () => new ArtifactWorkbenchRuntime({
      api: runtime.api.tasks,
      taskId,
      options: {
        catalogLimit: 500,
        maximumPreviewBytes: 16 * 1_024 * 1_024,
      },
    }),
    [runtime, taskId],
  )
  const [state, setState] = useState<ArtifactWorkbenchState>(workbench.state)
  useEffect(() => {
    const unsubscribe = workbench.listen(setState)
    void workbench.loadCatalog()
    return () => {
      unsubscribe()
      workbench.close(
        "Browser screenshot preview unmounted; BrowserWorker remains running.",
      )
    }
  }, [workbench])
  useEffect(() => {
    if (!screenshot) return
    const artifact = state.catalog.artifacts.find(
      (candidate) => candidate.artifactId === screenshot.artifactId,
    )
    if (!artifact) return
    void workbench.select({
      artifactId: artifact.artifactId,
      revision: artifact.revision,
      source: "artifact",
      focus: false,
    })
  }, [workbench, screenshot?.artifactId, state.catalog.artifacts])
  const model = state.model
  return (
    <section
      className="browser-screenshot-preview"
      aria-labelledby="browser-screenshot-heading"
      data-browser-screenshot-integrity={screenshot?.integrity}
    >
      <div className="browser-subheading">
        <h4 id="browser-screenshot-heading">已校验截图</h4>
        {screenshot ? (
          <button
            className="button button-secondary button-compact"
            type="button"
            onClick={() => dispatchArtifactNavigation({
              artifactId: screenshot.artifactId,
              source: "artifact",
              focus: true,
            })}
          >
            查看产物详情
          </button>
        ) : null}
      </div>
      {!screenshot ? (
        <p className="browser-empty-copy">
          此步骤没有关联截图。
        </p>
      ) : model?.kind === "image" ? (
        <>
          <div
            className="browser-screenshot-canvas"
            data-quarantined={model.quarantined || undefined}
          >
            <img
              src={model.sourceUrl}
              alt={model.alt}
              draggable={false}
            />
          </div>
          <dl className="browser-artifact-facts">
            <div>
              <dt>产物</dt>
              <dd title={screenshot.artifactId}>
                {compactIdentity(screenshot.artifactId, 28)}
              </dd>
            </div>
            <div>
              <dt>完整性</dt>
              <dd>{screenshot.integrity}</dd>
            </div>
            <div>
              <dt>大小</dt>
              <dd>{byteSize(screenshot.sizeBytes)}</dd>
            </div>
            <div>
              <dt>尺寸</dt>
              <dd>
                {screenshot.width && screenshot.height
                  ? `${screenshot.width}×${screenshot.height}`
                  : "—"}
              </dd>
            </div>
          </dl>
        </>
      ) : state.phase === "failed" || state.error ? (
        <div className="browser-preview-refusal" role="alert">
          <strong>截图暂时无法预览</strong>
          <p>
            {state.error?.message
              ?? "Artifact integrity, trust, or MIME admission failed."}
          </p>
        </div>
      ) : model?.kind === "metadata" || model?.kind === "refused" ? (
        <div className="browser-preview-refusal" role="status">
          <strong>{model.reason}</strong>
          <p>
            The artifact remains available as canonical metadata without unsafe
            inline rendering.
          </p>
        </div>
      ) : (
        <div className="browser-preview-loading" role="status">
          正在校验截图内容…
        </div>
      )}
    </section>
  )
}

function DomNodeRow({ node }: { node: BrowserDomNodeSummary }) {
  return (
    <tr
      data-browser-dom-node={node.nodeId}
      data-browser-dom-clickable={node.clickable || undefined}
    >
      <td>{node.role || node.tag || "node"}</td>
      <td>
        <strong>{node.name || node.text || node.value || "—"}</strong>
        {node.description ? <small>{node.description}</small> : null}
      </td>
      <td>{node.selector || compactIdentity(node.nodeId)}</td>
      <td>
        {node.clickable ? <span className="tag">clickable</span> : null}
        {node.editable ? <span className="tag tag-muted">editable</span> : null}
        {node.focused ? <span className="tag tag-muted">focused</span> : null}
        {node.hidden ? <span className="tag tag-danger">hidden</span> : null}
      </td>
    </tr>
  )
}

function DomInspector({
  session,
  step,
}: {
  session: BrowserSessionProjection
  step?: BrowserStep
}) {
  const summary = (
    step
      ? session.domSummaries.find((candidate) => candidate.stepId === step.stepId)
      : undefined
  ) ?? session.domSummaries.at(-1)
  return (
    <section className="browser-dom-inspector" aria-labelledby="browser-dom-heading">
      <div className="browser-subheading">
        <h4 id="browser-dom-heading">页面结构与无障碍摘要</h4>
        <span>
          {summary
            ? `${summary.interactiveCount}/${summary.nodeCount} interactive`
            : "no snapshot"}
        </span>
      </div>
      {!summary ? (
        <p className="browser-empty-copy">
          此步骤没有页面结构摘要。
        </p>
      ) : (
        <>
          {summary.promptInjectionFindings.length ? (
            <div
              className="browser-prompt-warning"
              role="note"
              data-browser-prompt-display-only="true"
            >
              <strong>Untrusted page instructions detected</strong>
              <p>
                Display-only findings never become control input or permission
                decisions.
              </p>
              <ul>
                {summary.promptInjectionFindings.map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <div className="browser-dom-summary-line">
            <span>{summary.url || "No URL"}</span>
            <span>
              seq {summary.sequence}
              {summary.truncated ? " · truncated" : ""}
            </span>
          </div>
          <div className="browser-dom-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>角色</th>
                  <th>名称或文字</th>
                  <th>选择器或节点</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {summary.nodes.slice(0, 200).map((node) => (
                  <DomNodeRow key={node.nodeId} node={node} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

function ActionInspector({
  action,
  result,
  step,
}: {
  action?: BrowserAction
  result?: BrowserActionResult
  step?: BrowserStep
}) {
  return (
    <section className="browser-action-inspector" aria-labelledby="browser-action-heading">
      <div className="browser-subheading">
        <h4 id="browser-action-heading">操作与结果</h4>
        <span>{action?.name ?? "no action"}</span>
      </div>
      {!action ? (
        <p className="browser-empty-copy">
          选择一个操作步骤，查看执行详情。
        </p>
      ) : (
        <div className="browser-action-grid">
          <div>
            <dl className="browser-detail-facts">
              <div>
                <dt>操作编号</dt>
                <dd>{action.actionId}</dd>
              </div>
              <div>
                <dt>工具调用</dt>
                <dd>{action.toolCallId ?? "—"}</dd>
              </div>
              <div>
                <dt>追踪片段</dt>
                <dd>{action.spanId ?? "—"}</dd>
              </div>
              <div>
                <dt>权限</dt>
                <dd>{action.permissionDecision ?? "—"}</dd>
              </div>
              <div>
                <dt>记录序号</dt>
                <dd>{action.sequence}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>{action.status}</dd>
              </div>
            </dl>
            <h5>参数</h5>
            <pre>{safeJson(action.arguments)}</pre>
          </div>
          <div>
            <dl className="browser-detail-facts">
              <div>
                <dt>结果</dt>
                <dd>{result?.resultId ?? "—"}</dd>
              </div>
              <div>
                <dt>结果</dt>
                <dd>{result ? (result.ok ? "ok" : "failed") : "—"}</dd>
              </div>
              <div>
                <dt>变更</dt>
                <dd>{result?.mutationIds.length ?? step?.mutationIds.length ?? 0}</dd>
              </div>
              <div>
                <dt>产物</dt>
                <dd>{result?.artifactIds.length ?? 0}</dd>
              </div>
            </dl>
            <h5>观察结果</h5>
            <p className="browser-result-summary">
              {result?.summary || result?.errorMessage || "No correlated result."}
            </p>
            {result?.extractedContent ? (
              <pre>{result.extractedContent}</pre>
            ) : null}
          </div>
        </div>
      )}
    </section>
  )
}

function DownloadRow({
  download,
  open,
}: {
  download: BrowserDownload
  open: () => void
}) {
  return (
    <li data-browser-download={download.artifactId}>
      <div>
        <strong>{download.filename}</strong>
        <small>
          {download.mediaType} · {byteSize(download.sizeBytes)} · seq {download.sequence}
        </small>
      </div>
      <span className={`browser-step-status ${phaseTone(download.state)}`}>
        {download.quarantined ? "quarantined" : download.state}
      </span>
      <button
        className="button button-secondary button-compact"
        type="button"
        onClick={open}
      >
        查看产物
      </button>
    </li>
  )
}

function ArtifactHistory({
  session,
  step,
  viewer,
}: {
  session: BrowserSessionProjection
  step?: BrowserStep
  viewer: BrowserViewerRuntime
}) {
  const screenshots = step
    ? session.screenshots.filter((item) =>
      step.screenshotArtifactIds.includes(item.artifactId),
    )
    : session.screenshots
  const downloads = step
    ? session.downloads.filter((item) =>
      step.downloadArtifactIds.includes(item.artifactId),
    )
    : session.downloads
  return (
    <section className="browser-artifact-history" aria-labelledby="browser-artifacts-heading">
      <div className="browser-subheading">
        <h4 id="browser-artifacts-heading">步骤产物</h4>
        <span>{screenshots.length} screenshot · {downloads.length} download</span>
      </div>
      {screenshots.length ? (
        <div className="browser-screenshot-strip">
          {screenshots.map((screenshot) => (
            <button
              className={`browser-screenshot-chip is-${screenshot.integrity}`}
              key={screenshot.artifactId}
              type="button"
              title={`${screenshot.artifactId} · ${screenshot.sha256}`}
              onClick={() => viewer.selectArtifact(screenshot.artifactId)}
            >
              <span>Screenshot #{screenshot.sequence}</span>
              <small>
                {screenshot.integrity} · {byteSize(screenshot.sizeBytes)}
              </small>
            </button>
          ))}
        </div>
      ) : null}
      {downloads.length ? (
        <ol className="browser-download-list">
          {downloads.map((download) => (
            <DownloadRow
              download={download}
              key={download.artifactId}
              open={() => {
                viewer.selectArtifact(download.artifactId)
                dispatchArtifactNavigation({
                  artifactId: download.artifactId,
                  source: "artifact",
                  focus: true,
                })
              }}
            />
          ))}
        </ol>
      ) : null}
      {!screenshots.length && !downloads.length ? (
        <p className="browser-empty-copy">
          此步骤没有关联的截图或下载文件。
        </p>
      ) : null}
    </section>
  )
}

function ControlReceiptRow({
  receipt,
}: {
  receipt: BrowserControlReceipt
}) {
  return (
    <li
      data-browser-control-command={receipt.commandId}
      data-browser-control-phase={receipt.phase}
    >
      <span className={`browser-phase-dot ${phaseTone(receipt.phase)}`} />
      <div>
        <strong>/{receipt.action}</strong>
        <small>
          {receipt.phase} · {compactIdentity(receipt.commandId)}
          {receipt.replayed ? " · replayed" : ""}
        </small>
        {receipt.errorMessage ? <em>{receipt.errorMessage}</em> : null}
      </div>
      <span>
        {receipt.eventIds.length} events · {receipt.artifactIds.length} artifacts
      </span>
    </li>
  )
}

function BrowserControls({
  state,
  viewer,
  sealed,
}: {
  state: BrowserViewerState
  viewer: BrowserViewerRuntime
  sealed: boolean
}) {
  const [draft, setDraft] = useState<BrowserControlDraft>({
    url: "",
    reason: "",
  })
  const [busy, setBusy] = useState<string>()
  const [failure, setFailure] = useState<string>()
  const execute = async (
    action: "navigate" | "stop" | "retry" | "inspect",
  ) => {
    setBusy(action)
    setFailure(undefined)
    try {
      if (action === BrowserControlAction.NAVIGATE) {
        await viewer.navigate(draft.url)
      } else if (action === BrowserControlAction.STOP) {
        await viewer.stop(draft.reason)
      } else if (action === BrowserControlAction.RETRY) {
        await viewer.retry(draft.reason)
      } else {
        await viewer.inspect(draft.reason)
      }
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(undefined)
    }
  }
  const submitNavigate = (event: FormEvent) => {
    event.preventDefault()
    void execute("navigate")
  }
  return (
    <section
      className="browser-control-panel"
      aria-labelledby="browser-controls-heading"
      data-browser-control-mode={sealed ? "sealed_autonomous" : "interactive"}
    >
      <div className="browser-subheading">
        <h4 id="browser-controls-heading">Browser controls</h4>
        <span className={sealed ? "tag tag-danger" : "tag"}>
          {sealed ? "sealed autonomous" : "interactive"}
        </span>
      </div>
      {sealed ? (
        <div className="browser-sealed-notice" role="note">
          <strong>Manual mutation is sealed.</strong>
          <p>
            Submitting a command records one operator intervention attempt.
            The backend deterministically rejects it and never enters an
            approval wait; autonomous recovery or fail-closed handling remains
            the only valid path.
          </p>
        </div>
      ) : (
        <p className="browser-control-note">
          Commands use the existing BrowserWorker permission, idempotency,
          generation, lifecycle, and action-control paths.
        </p>
      )}
      <form className="browser-navigate-form" onSubmit={submitNavigate}>
        <label>
          <span>目标网址</span>
          <input
            type="url"
            inputMode="url"
            placeholder="https://example.com/path"
            value={draft.url}
            onChange={(event) =>
              setDraft((value) => ({ ...value, url: event.currentTarget.value }))
            }
          />
        </label>
        <button
          className="button button-primary"
          type="submit"
          disabled={Boolean(busy) || !draft.url.trim()}
        >
          {busy === "navigate"
            ? "Submitting…"
            : sealed
              ? "Record sealed navigate attempt"
              : "Navigate"}
        </button>
      </form>
      <label className="browser-control-reason">
        <span>原因</span>
        <input
          type="text"
          maxLength={2_048}
          placeholder="说明此次操作的目的"
          value={draft.reason}
          onChange={(event) =>
            setDraft((value) => ({ ...value, reason: event.currentTarget.value }))
          }
        />
      </label>
      <div className="browser-control-actions">
        <button
          className="button button-secondary"
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void execute("inspect")}
        >
          {busy === "inspect" ? "Inspecting…" : "Inspect session"}
        </button>
        <button
          className="button button-secondary"
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void execute("retry")}
        >
          {busy === "retry"
            ? "Retrying…"
            : sealed
              ? "Record sealed retry attempt"
              : "Retry selected"}
        </button>
        <button
          className="button button-danger"
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void execute("stop")}
        >
          {busy === "stop"
            ? "Stopping…"
            : sealed
              ? "Record sealed stop attempt"
              : "Stop BrowserWorker"}
        </button>
      </div>
      {failure ? (
        <p className="browser-control-failure" role="alert">{failure}</p>
      ) : null}
      {state.controls.length ? (
        <ol className="browser-control-receipts">
          {[...state.controls].reverse().slice(0, 12).map((receipt) => (
            <ControlReceiptRow receipt={receipt} key={receipt.commandId} />
          ))}
        </ol>
      ) : (
        <p className="browser-empty-copy">此会话暂无控制回执。</p>
      )}
    </section>
  )
}

function CausalityFindings({
  session,
}: {
  session: BrowserSessionProjection
}) {
  if (!session.findings.length) {
    return (
      <p className="browser-causality-ok">
        Action, tool, span, mutation, and artifact associations are complete.
      </p>
    )
  }
  return (
    <details className="browser-causality-findings">
      <summary>{session.findings.length} causality finding(s)</summary>
      <ol>
        {session.findings.slice(0, 100).map((finding, index) => (
          <li
            className={`is-${finding.severity}`}
            key={`${finding.code}:${finding.stepId ?? ""}:${index}`}
          >
            <strong>{finding.code}</strong>
            <span>{finding.message}</span>
          </li>
        ))}
      </ol>
    </details>
  )
}

export function BrowserWorkbench({
  runtime,
  task,
}: BrowserWorkbenchProps) {
  const sealed = useMemo(() => taskSealed(task), [task])
  const browser = useMemo(
    () => new BrowserViewerRuntime({
      api: runtime.api.tasks,
      taskId: task.taskId,
      runId: task.runId,
      sealed,
      refreshIntervalMs: 5_000,
    }),
    [runtime, task.taskId, task.runId, sealed],
  )
  const state = useSyncExternalStore(
    browser.subscribe,
    browser.getSnapshot,
    browser.getSnapshot,
  )
  const events = useProjectionSelector(
    runtime,
    selectEventsForTask(task.taskId, { limit: 5_000 }),
  )
  const artifacts = useProjectionSelector(
    runtime,
    selectArtifactsForTask(task.taskId),
  )
  const recoveries = useProjectionSelector(
    runtime,
    selectRecoveriesForTask(task.taskId),
  )
  const projectionRevision = useProjectionSelector(runtime, selectRevision())
  const online = useOnlineStatus()
  const historyViewport = useRef<HTMLDivElement>(null)
  const session = selectedSession(state)
  const step = selectedStep(state, session)
  const action = selectedAction(state, session, step)
  const result = resultForAction(session, step, action)
  const screenshot = screenshotForStep(
    session,
    step,
    state.selection.artifactId,
  )

  useEffect(() => {
    browser.updateCanonical({
      events,
      artifacts,
      recoveries,
      projectionRevision,
    })
  }, [browser, events, artifacts, recoveries, projectionRevision])

  useEffect(() => {
    const viewport = historyViewport.current
    if (viewport) {
      browser.updateWindow(viewport.scrollTop, viewport.clientHeight || 540)
    }
    void browser.start()
    return () => {
      browser.close(
        "Browser viewer component unmounted; BrowserWorker ownership is unchanged.",
      )
    }
  }, [browser])

  useEffect(() => {
    browser.setOnline(online)
  }, [browser, online])

  return (
    <section
      className="detail-section browser-workbench"
      aria-labelledby="browser-workbench-heading"
      data-browser-viewer-phase={state.projection.phase}
      data-browser-state-owner="BrowserWorker"
      data-browser-viewer-state-owner="CanonicalProjectionStore+observability"
      data-browser-close-stops-worker="false"
      data-browser-session-id={session?.scope.browserSessionId}
      data-browser-step-id={step?.stepId}
      data-browser-action-id={action?.actionId}
      data-event-id={action?.sourceEventIds[0] ?? step?.eventIds[0]}
      data-tool-call-id={action?.toolCallId}
      data-span-id={action?.spanId}
    >
      <header className="browser-workbench-header">
        <div>
          <div className="browser-heading-line">
            <h3 id="browser-workbench-heading">浏览器执行记录</h3>
            <span className={`browser-phase-badge ${phaseTone(state.projection.phase)}`}>
              {recordLabel(state.projection.phase)}
            </span>
            {!online ? <span className="tag tag-danger">offline</span> : null}
          </div>
          <p>
            查看浏览器操作、页面截图和下载文件。
          </p>
        </div>
        <button
          className="button button-secondary"
          type="button"
          disabled={state.projection.phase === BrowserViewerPhase.LOADING}
          onClick={() => void browser.refresh({ force: true, reason: "operator_refresh" })}
        >
          {state.projection.phase === BrowserViewerPhase.LOADING
            ? "正在刷新…"
            : "刷新记录"}
        </button>
      </header>

      <PermissionSurfaceStatus
        runtime={runtime.permissionConsole}
        surface="browser"
        label="browser"
      />

      <dl className="browser-summary-grid">
        <div>
          <dt>浏览器会话</dt>
          <dd>{state.projection.sessions.length}</dd>
        </div>
        <div>
          <dt>步骤</dt>
          <dd>{state.projection.totalSteps}</dd>
        </div>
        <div>
          <dt>操作</dt>
          <dd>{state.projection.totalActions}</dd>
        </div>
        <div>
          <dt>截图</dt>
          <dd>{state.projection.totalScreenshots}</dd>
        </div>
        <div>
          <dt>下载</dt>
          <dd>{state.projection.totalDownloads}</dd>
        </div>
        <div>
          <dt>记录版本</dt>
          <dd>{state.projection.projectionRevision}</dd>
        </div>
      </dl>

      {state.error ? (
        <div className="browser-viewer-error" role="alert">
          <strong>{state.error.code}</strong>
          <span>{state.error.message}</span>
          {state.error.retryable ? (
            <button
              className="button button-secondary button-compact"
              type="button"
              onClick={() => void browser.refresh({
                force: true,
                reason: "viewer_error_retry",
              })}
            >
              重试
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="browser-workbench-grid">
        <SessionRail state={state} viewer={browser} />
        <main className="browser-workbench-main">
          {session ? (
            <>
              <div className="browser-current-page">
                <div>
                  <span className={`browser-phase-dot ${phaseTone(session.status)}`} />
                  <div>
                    <strong>{session.title || "Untitled browser target"}</strong>
                    <ObservedPageLocation url={session.url} />
                  </div>
                </div>
                <span>
                  {compactIdentity(session.scope.browserSessionId, 30)}
                </span>
              </div>
              <HealthStrip health={session.health} />
              <CausalityFindings session={session} />
              <TargetTree
                session={session}
                selectedTargetId={step?.targetId ?? session.activeTargetId}
              />
              <HistoryToolbar state={state} viewer={browser} />
              <StepHistory state={state} viewer={browser} viewport={historyViewport} />
              <div className="browser-inspector-grid">
                <BrowserArtifactPreview
                  runtime={runtime}
                  taskId={task.taskId}
                  screenshot={screenshot}
                />
                <ArtifactHistory session={session} step={step} viewer={browser} />
              </div>
              <ActionInspector action={action} result={result} step={step} />
              <DomInspector session={session} step={step} />
              <BrowserControls state={state} viewer={browser} sealed={sealed} />
            </>
          ) : state.projection.phase === BrowserViewerPhase.LOADING ? (
            <div className="browser-loading-state" role="status">
              正在加载浏览器操作记录…
            </div>
          ) : (
            <div className="browser-empty-state">
              <strong>暂无浏览器记录</strong>
              <p>
                任务使用浏览器后，操作步骤与截图会显示在这里。
              </p>
            </div>
          )}
        </main>
      </div>
    </section>
  )
}
