import { recordLabel, recordTitle, recordSummary } from "../../evidence/record-copy.ts"
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
  type KeyboardEvent,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import {
  TraceCompleteness,
  TraceNodeKind,
  TraceSemanticKind,
  TraceViewKind,
  type TraceNavigationTarget,
  type TraceNode,
  type TraceSemanticKindValue,
} from "../contracts.ts"
import { selectCausalTrace } from "../projector.ts"
import { TraceWorkbenchController } from "./controller.ts"

function formatDuration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 ms"
  if (value < 1_000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1_000)}s`
}

function targetLabel(target: TraceNavigationTarget): string {
  switch (target.view) {
    case TraceViewKind.TIMELINE:
      return "执行过程"
    case TraceViewKind.TOPOLOGY:
      return "关系图"
    case TraceViewKind.TERMINAL:
      return "终端"
    case TraceViewKind.BROWSER:
      return "浏览器"
    case TraceViewKind.ARTIFACT:
      return "产物"
    case TraceViewKind.DIFF:
      return "代码差异"
    default:
      return "事件追踪"
  }
}

function TraceMetrics({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const projection = snapshot.projection
  const diagnostics = projection.diagnostics
  return (
    <dl className="fact-grid trace-metrics">
      <div><dt>事件记录</dt><dd>{diagnostics.eventCount}</dd></div>
      <div><dt>追踪节点</dt><dd>{diagnostics.nodeCount}</dd></div>
      <div><dt>关联关系</dt><dd>{diagnostics.edgeCount}</dd></div>
      <div><dt>关键路径用时</dt><dd>{formatDuration(projection.criticalPath.durationMs)}</dd></div>
      <div><dt>待同步</dt><dd>{diagnostics.lag}</dd></div>
      <div><dt>不完整 / 延迟</dt><dd>{diagnostics.partialCount} / {diagnostics.lateCount}</dd></div>
      <div><dt>未关联 / 隔离</dt><dd>{diagnostics.orphanCount} / {diagnostics.quarantineCount}</dd></div>
      <div><dt>显示 / 隐藏</dt><dd>{snapshot.folded.nodes.length} / {snapshot.folded.hiddenNodeKeys.length}</dd></div>
    </dl>
  )
}

function SemanticToggle({
  controller,
  semantic,
  label,
}: {
  controller: TraceWorkbenchController
  semantic: TraceSemanticKindValue
  label: string
}) {
  const selected = controller.getSnapshot().filter.semantics?.includes(semantic) ?? false
  return (
    <button
      type="button"
      className={`button button-secondary${selected ? " is-active" : ""}`}
      aria-pressed={selected}
      onClick={() => {
        const current = controller.getSnapshot().filter.semantics ?? []
        controller.patchFilter({
          semantics: selected
            ? current.filter((value) => value !== semantic)
            : [...current, semantic],
        })
      }}
    >
      {label}
    </button>
  )
}

function TraceToolbar({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const onSearch = (event: ChangeEvent<HTMLInputElement>) => {
    controller.patchFilter({ search: event.currentTarget.value })
  }
  return (
    <div className="trace-toolbar" role="group" aria-label="Causal trace filters">
      <label className="trace-search">
        <span>搜索追踪记录</span>
        <input
          type="search"
          value={snapshot.filter.search ?? ""}
          placeholder="搜索事件、执行者或产物…"
          onChange={onSearch}
        />
      </label>
      <div className="trace-toolbar-actions">
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.PERMISSION} label="权限" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.PLACEMENT} label="执行位置" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.FAULT} label="故障" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.RECOVERY} label="恢复" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.MCP} label="MCP" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.SKILL} label="技能" />
        <SemanticToggle controller={controller} semantic={TraceSemanticKind.SUBAGENT} label="子代理" />
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.filter.criticalOnly === true}
          onClick={() => controller.patchFilter({ criticalOnly: !snapshot.filter.criticalOnly })}
        >
          关键路径
        </button>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.filter.failuresOnly === true}
          onClick={() => controller.patchFilter({ failuresOnly: !snapshot.filter.failuresOnly })}
        >
          失败
        </button>
        <button type="button" className="button button-secondary" onClick={() => controller.resetFilter()}>
          重置
        </button>
      </div>
      <div className="trace-toolbar-actions">
        <label>
          <span>折叠方式 </span>
          <select
            value={snapshot.fold.mode}
            onChange={(event) => controller.patchFold({
              mode: event.currentTarget.value as "manual" | "span" | "worker" | "semantic",
            })}
          >
            <option value="span">追踪片段</option>
            <option value="worker">执行者</option>
            <option value="semantic">语义</option>
            <option value="manual">层级</option>
          </select>
        </label>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.fold.preserveCritical}
          onClick={() => controller.patchFold({ preserveCritical: !snapshot.fold.preserveCritical })}
        >
          保留关键路径
        </button>
        <button
          type="button"
          className="button button-secondary"
          aria-pressed={snapshot.fold.preserveFailures}
          onClick={() => controller.patchFold({ preserveFailures: !snapshot.fold.preserveFailures })}
        >
          保留故障
        </button>
      </div>
    </div>
  )
}

function TraceIntegrity({ controller }: { controller: TraceWorkbenchController }) {
  const projection = controller.getSnapshot().projection
  const diagnostics = projection.diagnostics
  const reconciliation = controller.reconciliation()
  if (!diagnostics.warnings.length && !reconciliation?.changes.length) return null
  return (
    <aside className="trace-integrity" aria-label="Trace integrity and reconciliation">
      <div className="section-heading">
        <h4>完整性与延迟记录</h4>
        <span>{diagnostics.quarantineCount} quarantined</span>
      </div>
      {diagnostics.warnings.length ? (
        <ul>
          {diagnostics.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}
      {reconciliation?.changes.length ? (
        <details>
          <summary>{reconciliation.changes.length} change(s) in revision {reconciliation.currentRevision}</summary>
          <ol>
            {reconciliation.changes.slice(-20).map((change) => (
              <li key={change.id} data-trace-reconciliation-kind={change.kind}>
                <strong>{change.kind}</strong> · {change.message}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      {projection.quarantine.length ? (
        <details>
          <summary>隔离记录</summary>
          <ol>
            {projection.quarantine.slice(0, 40).map((record) => (
              <li key={record.id} data-trace-quarantine-code={record.code}>
                <strong>{record.code}</strong> · {record.message}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </aside>
  )
}

function CriticalPathSummary({ controller }: { controller: TraceWorkbenchController }) {
  const path = controller.getSnapshot().projection.criticalPath
  if (!path.nodeKeys.length) return null
  return (
    <aside className="trace-critical-path" aria-label="关键因果路径">
      <div className="section-heading">
        <h4>关键路径</h4>
        <span>{path.nodeKeys.length} 个节点 · {formatDuration(path.durationMs)}</span>
      </div>
      <div className="trace-path-strip">
        {path.nodeKeys.slice(0, 24).map((key, index) => {
          const node = controller.getSnapshot().projection.nodesByKey[key]
          if (!node) return null
          return (
            <button
              type="button"
              key={key}
              className="trace-path-step"
              title={node.summary}
              onClick={() => controller.reveal(key)}
            >
              <span>{index + 1}</span>
              {node.title}
            </button>
          )
        })}
      </div>
      {path.bottlenecks.length ? (
        <ul className="trace-bottlenecks">
          {path.bottlenecks.slice(0, 5).map((bottleneck) => (
            <li key={bottleneck.id}>
              <button type="button" onClick={() => controller.reveal(bottleneck.nodeKey)}>
                {bottleneck.kind}: {bottleneck.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </aside>
  )
}

function TraceEvidence({ node }: { node: TraceNode }) {
  return (
    <dl className="inline-facts trace-evidence">
      <dt>事件</dt><dd>{node.primaryEventId ?? "—"}</dd>
      {node.refs.spanId ? <><dt>追踪片段</dt><dd>{node.refs.spanId}</dd></> : null}
      {node.refs.workerId ? <><dt>执行者</dt><dd>{node.refs.workerId}</dd></> : null}
      {node.refs.toolCallId ? <><dt>工具调用</dt><dd>{node.refs.toolCallId}</dd></> : null}
      {node.refs.providerId ? <><dt>服务商</dt><dd>{node.refs.providerId}</dd></> : null}
      {node.refs.permissionId ? <><dt>权限</dt><dd>{node.refs.permissionId}</dd></> : null}
      {node.refs.failureId ? <><dt>失败</dt><dd>{node.refs.failureId}</dd></> : null}
      {node.refs.recoveryId ? <><dt>恢复</dt><dd>{node.refs.recoveryId}</dd></> : null}
      {node.refs.mutationId ? <><dt>变更</dt><dd>{node.refs.mutationId}</dd></> : null}
      {node.refs.artifactIds.length ? <><dt>产物</dt><dd>{node.refs.artifactIds.join(", ")}</dd></> : null}
    </dl>
  )
}

function TraceRow({
  controller,
  node,
  selected,
  expanded,
}: {
  controller: TraceWorkbenchController
  node: TraceNode
  selected: boolean
  expanded: boolean
}) {
  const ref = useRef<HTMLElement | null>(null)
  const targets = controller.navigationTargets(node.key).filter((target) => target.view !== TraceViewKind.TRACE)
  const pinned = controller.getSnapshot().pins.some((reference) => reference.nodeKey === node.key)
  useEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === "undefined") return
    // The virtual item includes the spacing around the article. Measuring only
    // the article makes every following row overlap by the wrapper's padding.
    const item = element.closest<HTMLElement>(".trace-virtual-item") ?? element
    const observer = new ResizeObserver(() => {
      controller.measure(node.key, Math.ceil(item.getBoundingClientRect().height))
    })
    observer.observe(item)
    return () => observer.disconnect()
  }, [controller, node.key])
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      controller.toggleExpanded(node.key)
    } else if (event.key === "p" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      controller.togglePin(node.key)
    }
  }
  return (
    <article
      ref={ref}
      className={`trace-row${selected ? " is-selected" : ""}${node.critical ? " is-critical" : ""}`}
      tabIndex={0}
      aria-expanded={expanded}
      data-trace-node-key={node.key}
      data-trace-node-kind={node.identity.kind}
      data-trace-completeness={node.completeness}
      data-event-id={node.primaryEventId}
      data-span-id={node.refs.spanId}
      data-worker-id={node.refs.workerId}
      data-tool-call-id={node.refs.toolCallId}
      data-artifact-id={node.refs.artifactIds[0]}
      data-mutation-id={node.refs.mutationId}
      onFocus={() => controller.select(node.key, { expand: false, anchor: false })}
      onClick={() => controller.select(node.key, { expand: false, anchor: false })}
      onKeyDown={onKeyDown}
    >
      <header className="trace-row-heading">
        <div>
          <span className={`status-marker status-${node.terminal ? "completed" : "running"}`} aria-hidden="true" />
          <strong title={node.title}>{recordTitle(node.title)}</strong>
          <span className="tag tag-muted">{recordLabel(node.identity.kind)}</span>
          <span className={`tag${node.completeness === TraceCompleteness.COMPLETE ? " tag-muted" : " tag-danger"}`}>
            {recordLabel(node.completeness)}
          </span>
          {node.critical ? <span className="tag">关键路径</span> : null}
          {node.retryCount ? <span className="tag tag-danger">retry {node.retryCount}</span> : null}
        </div>
        <span>#{node.sequence}{node.endSequence !== node.sequence ? `–${node.endSequence}` : ""}</span>
      </header>
      <p>{recordSummary(node.summary)}</p>
      <div className="trace-tags">
        {node.semantics.map((semantic) => <span className="tag tag-muted" key={semantic}>{recordLabel(semantic)}</span>)}
      </div>
      <div className="trace-row-actions">
        <button type="button" onClick={(event) => { event.stopPropagation(); controller.toggleExpanded(node.key) }}>
          {expanded ? "收起详情" : "查看详情"}
        </button>
        <button type="button" onClick={(event) => { event.stopPropagation(); controller.togglePin(node.key) }}>
          {pinned ? "移除报告引用" : "加入报告引用"}
        </button>
        {targets.length ? <details className="trace-linked-views"><summary>关联页面</summary>{targets.slice(0, 8).map((target) => (
          <button
            type="button"
            key={target.id}
            title={target.label}
            onClick={(event) => {
              event.stopPropagation()
              controller.navigate(target)
            }}
          >
            {targetLabel(target)}
          </button>
        ))}</details> : null}
      </div>
      {expanded ? (
        <div className="trace-row-details">
          <p className="trace-original-copy">{node.title}<br />{node.summary}</p>
          <TraceEvidence node={node} />
          <dl className="inline-facts">
            <dt>用时</dt><dd>{formatDuration(node.durationMs)}</dd>
            <dt>影响</dt><dd>{node.contributionScore.toFixed(1)}</dd>
            <dt>前置与后续</dt><dd>{node.incomingEdgeIds.length} / {node.outgoingEdgeIds.length}</dd>
            <dt>事件</dt><dd>{node.eventIds.length}</dd>
          </dl>
          {node.diagnostics.length ? (
            <ul className="trace-diagnostics">
              {node.diagnostics.map((diagnostic) => <li key={diagnostic}>{diagnostic}</li>)}
            </ul>
          ) : null}
          {Object.keys(node.safeAttributes).length ? (
            <details>
              <summary>原始属性</summary>
              <dl className="inline-facts">
                {Object.entries(node.safeAttributes).map(([key, value]) => (
                  <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>
                ))}
              </dl>
            </details>
          ) : null}
        </div>
      ) : null}
    </article>
  )
}

function VirtualTraceList({ controller }: { controller: TraceWorkbenchController }) {
  const snapshot = controller.getSnapshot()
  const container = useRef<HTMLDivElement | null>(null)
  const onScroll = () => {
    const element = container.current
    if (element) controller.setViewport(element.scrollTop, element.clientHeight)
  }
  if (!snapshot.folded.nodes.length) {
    return <p className="muted-copy">没有匹配的追踪记录。</p>
  }
  return (
    <div
      ref={container}
      className="trace-virtual-scroll"
      data-trace-virtual-total={snapshot.virtualWindow.total}
      data-trace-virtual-mounted={snapshot.virtualWindow.items.length}
      onScroll={onScroll}
    >
      <div className="trace-virtual-space" style={{ height: `${snapshot.virtualWindow.totalHeight}px`, position: "relative" }}>
        {snapshot.virtualWindow.items.map((item) => {
          const node = snapshot.folded.nodes[item.index]
          if (!node || node.key !== item.key) return null
          return (
            <div
              key={item.key}
              className="trace-virtual-item"
              style={{ position: "absolute", top: `${item.top}px`, left: 0, right: 0 }}
            >
              <TraceRow
                controller={controller}
                node={node}
                selected={snapshot.selectedNodeKey === node.key}
                expanded={snapshot.expandedNodeKeys.has(node.key)}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PinnedReferences({ controller }: { controller: TraceWorkbenchController }) {
  const pins = controller.getSnapshot().pins
  if (!pins.length) return null
  return (
    <aside className="trace-pins" aria-label="已固定的报告引用">
      <div className="section-heading">
        <h4>报告引用</h4>
        <span>{pins.length}</span>
      </div>
      <ol>
        {pins.map((reference) => (
          <li key={reference.id} data-trace-report-reference={reference.id}>
            <button type="button" onClick={() => controller.reveal(reference.nodeKey)}>
              {reference.label}{reference.stale ? " · stale" : ""}
            </button>
            <button type="button" onClick={() => controller.unpin(reference.nodeKey)}>移除</button>
          </li>
        ))}
      </ol>
      <details>
        <summary>Markdown 报告</summary>
        <pre>{controller.exportReport().markdown}</pre>
      </details>
    </aside>
  )
}

export function CausalTraceWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const selector = useMemo(() => selectCausalTrace(task.taskId), [task.taskId])
  const projection = useProjectionSelector(runtime, selector)
  const controller = useMemo(
    () => new TraceWorkbenchController(task.taskId, projection),
    [task.taskId],
  )
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  )
  const [collapsed, setCollapsed] = useState(false)
  useEffect(() => {
    controller.updateProjection(projection)
  }, [controller, projection])
  useEffect(() => () => controller.close("Trace workbench unmounted."), [controller])
  return (
    <section
      className="detail-section causal-trace-workbench"
      aria-labelledby="causal-trace-heading"
      data-task-id={task.taskId}
      data-trace-state-owner="CanonicalProjectionStore"
      data-trace-second-replay-store="false"
      data-trace-close-stops-task="false"
      data-trace-projection-revision={snapshot.projection.projectionRevision}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Canonical cross-view evidence</p>
          <h3 id="causal-trace-heading">事件追踪</h3>
        </div>
        <div className="trace-heading-actions">
          {snapshot.activeNavigation ? (
            <span role="status" data-trace-navigation-phase={snapshot.activeNavigation.phase}>
              {snapshot.activeNavigation.phase}: {snapshot.activeNavigation.target.label}
            </span>
          ) : null}
          <button type="button" className="button button-secondary" onClick={() => controller.navigateBack()}>
            返回上一视图
          </button>
          <button type="button" className="button button-secondary" onClick={() => setCollapsed((value) => !value)}>
            {collapsed ? "打开查看器" : "关闭查看器"}
          </button>
        </div>
      </div>
      {collapsed ? (
        <p className="muted-copy" data-trace-viewer-closed="true">
          The trace viewer is closed locally. Task {task.taskId} and all worker sessions continue unchanged.
        </p>
      ) : (
        <>
          <TraceMetrics controller={controller} />
          <TraceToolbar controller={controller} />
          <details className="record-advanced-options"><summary>完整性检查与关键路径</summary><TraceIntegrity controller={controller} /><CriticalPathSummary controller={controller} /></details>
          <p className="muted-copy">
            选择一条记录，可查看来源并跳转到对应的执行过程、关系图或产物。
          </p>
          <VirtualTraceList controller={controller} />
          <PinnedReferences controller={controller} />
        </>
      )}
    </section>
  )
}
