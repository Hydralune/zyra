import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react"
import type { TaskProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import type { TaskDetailState } from "../../shell/workbench-controller.ts"
import { TaskDetail, TaskEvidenceSection } from "../../components/tasks/task-detail.tsx"
import { SafeMarkdown } from "../../components/content/safe-markdown.tsx"
import { useProjectionSelector } from "../../app/hooks.ts"
import { selectProjectionReadiness } from "../../state/panel-selectors.ts"
import { selectEventsForTask } from "../../state/selectors.ts"
import type { CausalEventProjection } from "../../state/contracts.ts"
import { nodeLabel, nodeDescription, nodeStateLabel, phaseLabel } from "../../shell/product-copy.ts"
import { taskMetrics, formatDuration } from "../../shell/task-metrics.ts"
import { orderedProductPlan, statusCopy } from "../../components/tasks/product-task-detail.tsx"
import { productArtifacts } from "../artifacts/product-artifacts.ts"
import { advancedSections, evidenceDestination, evidenceTabs, eventCategories, eventCategory, eventLabel, eventSummary, filterEvidenceEvents, type EventCategory, type EvidenceNavigationTarget } from "./model.ts"

export type EvidenceSourceStatus = "live" | "degraded" | "missing" | "stale"
export function evidenceSourceStatus(input: { connected: boolean; ready: boolean; stale: boolean; count: number }): EvidenceSourceStatus {
  if (!input.connected) return "degraded"
  if (input.stale || !input.ready) return "stale"
  return input.count <= 0 ? "missing" : "live"
}

function dateTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false })
}

function EvidenceEvents({ events, initialQuery = "" }: { events: readonly CausalEventProjection[]; initialQuery?: string }) {
  const [query, setQuery] = useState(initialQuery)
  const [category, setCategory] = useState<EventCategory | "all">("all")
  const [page, setPage] = useState(0)
  const filtered = useMemo(() => filterEvidenceEvents(events, category, query), [events, category, query])
  const lastPage = Math.max(0, Math.ceil(filtered.length / 25) - 1)
  const current = Math.min(page, lastPage)
  const visible = filtered.slice(current * 25, (current + 1) * 25)
  return <section className="evidence-card evidence-events" id="evidence-canonical-events">
    <div className="evidence-section-heading"><div><h2>事件记录</h2><p>按记录序号排列，展开可查看原文和关联编号。</p></div><span>已加载 {events.length} 条</span></div>
    <div className="evidence-event-filters">
      <label><span className="sr-only">搜索事件</span><input type="search" placeholder="搜索内容、事件或节点编号…" value={query} onChange={(event) => { setQuery(event.target.value); setPage(0) }} /></label>
      <label><span className="sr-only">事件类别</span><select value={category} onChange={(event) => { setCategory(event.target.value as typeof category); setPage(0) }}>
        <option value="all">全部事件</option>{Object.entries(eventCategories).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
    </div>
    {visible.length ? <ol className="evidence-event-list">{visible.map((event) => <li key={event.eventId} data-event-id={event.eventId} data-correlation-id={event.correlationId}>
      <span className="evidence-event-dot" data-category={eventCategory(event)} aria-hidden="true" />
      <details><summary><span><strong>{eventLabel(event)}</strong><time dateTime={event.createdAt}>{dateTime(event.createdAt)}</time></span><span className="evidence-event-preview">{eventSummary(event)}</span></summary>
        <dl className="evidence-record-facts"><div><dt>事件类型</dt><dd>{event.eventType}</dd></div><div><dt>事件编号</dt><dd>{event.eventId}</dd></div><div><dt>顺序</dt><dd>#{event.sequence}</dd></div><div><dt>是否生效</dt><dd>{event.effective ? "已生效" : "未生效"}</dd></div>{event.nodeId ? <div><dt>节点</dt><dd>{event.nodeId}</dd></div> : null}{event.workerId ? <div><dt>执行者</dt><dd>{event.workerId}</dd></div> : null}<div><dt>关联编号</dt><dd>{event.correlationId || "—"}</dd></div></dl>
        {event.summary ? <p className="evidence-event-full">{event.summary}</p> : null}
      </details>
    </li>)}</ol> : <div className="evidence-empty"><strong>{events.length ? "没有匹配的事件" : "暂无事件记录"}</strong><p>{events.length ? "试试其他关键词，或切换事件类别。" : "事件将在同步后显示；任务结果和执行步骤仍可在上方查看。"}</p>{query || category !== "all" ? <button className="product-button product-button-quiet" onClick={() => { setQuery(""); setCategory("all"); setPage(0) }}>清除筛选</button> : null}</div>}
    {filtered.length > 25 ? <div className="evidence-pagination"><span>{current * 25 + 1}–{Math.min((current + 1) * 25, filtered.length)} / {filtered.length} 条</span><button className="product-button product-button-quiet" disabled={current === 0} onClick={() => setPage(current - 1)}>上一页</button><button className="product-button product-button-quiet" disabled={current >= lastPage} onClick={() => setPage(current + 1)}>下一页</button></div> : null}
  </section>
}

function EvidencePlan({ task }: { task: TaskProjection }) {
  const nodes = orderedProductPlan(task.planNodes).filter((node) => node.nodeId !== task.rootNodeId)
  return <section className="evidence-card" id="evidence-plan">
    <div className="evidence-section-heading"><div><h2>执行步骤</h2><p>每个步骤的状态由当前任务记录提供。</p></div><span>{nodes.length} 个步骤</span></div>
    {nodes.length ? <ol className="evidence-step-list">{nodes.map((node, index) => <li key={node.nodeId}>
      <span className={`evidence-step-number status-${node.status}`} aria-hidden="true">{index + 1}</span><div><strong>{nodeLabel(node)}</strong>{node.description ? <p>{nodeDescription(node)}</p> : null}</div><span className="evidence-state" data-state={node.status}>{nodeStateLabel(node.status)}</span>
    </li>)}</ol> : <div className="evidence-empty"><p>此任务尚无分步执行记录。</p></div>}
  </section>
}

function LoadedEvidenceWorkbench({ runtime, task, section }: { runtime: WorkbenchRuntime; task: TaskProjection; section?: string }) {
  const destination = evidenceDestination(section)
  const readiness = useProjectionSelector(runtime, useMemo(() => selectProjectionReadiness(task.taskId), [task.taskId]))
  const events = useProjectionSelector(runtime, useMemo(() => selectEventsForTask(task.taskId), [task.taskId]))
  const [now, setNow] = useState(Date.now)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState("")
  const [target, setTarget] = useState<EvidenceNavigationTarget>({})
  const root = useRef<HTMLElement>(null)
  const tabList = useRef<HTMLElement>(null)
  const metrics = taskMetrics(task, now)
  const status = statusCopy(task)
  const artifacts = productArtifacts(task.artifacts)
  const verificationNodes = task.planNodes.filter((node) => /verif|验证|校验/i.test(node.title))
  const result = [task.metadata.final_answer, task.metadata.finalAnswer, task.metadata.result, task.metadata.summary, task.metadata.output].find((value): value is string => typeof value === "string" && Boolean(value.trim()))
  const stale = readiness.lag > 0 || readiness.integrityWarnings.length > 0
  const dataStatus = evidenceSourceStatus({ ...readiness, stale, count: 1 })
  const selectedAdvanced = advancedSections.find((entry) => entry.id === destination.section) ?? advancedSections[0]
  const open = (nextSection: string) => runtime.router.openEvidence(task.taskId, { section: nextSection })
  const navigate = (next: EvidenceNavigationTarget) => { setTarget(next); open(next.artifactId ? "evidence-artifacts" : "evidence-canonical-events") }
  useEffect(() => {
    if (!task.active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [task.active])
  useEffect(() => {
    root.current?.scrollTo({ top: 0 })
    if (section === "evidence-canonical-events") document.getElementById(section)?.scrollIntoView({ block: "start" })
  }, [destination.tab, destination.section, section])
  const refresh = async () => {
    setRefreshing(true); setRefreshError("")
    try { await runtime.liveSync.refreshNow() }
    catch { setRefreshError("刷新暂未成功，请稍后重试。") }
    finally { setRefreshing(false) }
  }
  const tabKeys = (event: KeyboardEvent<HTMLElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
    event.preventDefault()
    const index = evidenceTabs.findIndex((tab) => tab.id === destination.tab)
    const next = event.key === "Home" ? 0 : event.key === "End" ? 3 : (index + (event.key === "ArrowRight" ? 1 : 3)) % 4
    open(evidenceTabs[next]!.section)
    tabList.current?.querySelectorAll<HTMLButtonElement>("[role=tab]")[next]?.focus()
  }
  return <section ref={root} className="evidence-route" data-task-id={task.taskId}>
    <div className="evidence-page">
      <header className="evidence-header"><div><h1>证据中心</h1><p>查看这次任务的结果、执行过程和来源记录。</p></div><div className="evidence-header-actions"><button className="product-button product-button-quiet" disabled={refreshing} onClick={() => void refresh()}>{refreshing ? "正在刷新…" : "刷新记录"}</button><button className="product-button product-button-quiet" onClick={() => runtime.router.openTask(task.taskId)}>返回对话 <span aria-hidden="true">↗</span></button></div></header>
      <div className="evidence-task-context"><span className="evidence-state" data-state={task.status}>{phaseLabel(task.status)}</span><strong title={task.userGoal}>{task.userGoal || "未命名任务"}</strong><span className="evidence-sync-state" data-status={dataStatus}><i aria-hidden="true" />{dataStatus === "live" ? "记录已同步" : dataStatus === "degraded" ? "连接待恢复" : "记录待同步"}</span></div>
      {refreshError ? <p className="evidence-sync-notice" role="alert">{refreshError}</p> : null}
      {dataStatus !== "live" ? <details className="evidence-sync-notice"><summary>{!readiness.connected ? "实时连接暂不可用，以下为已加载的记录。" : "部分执行记录尚未同步完整。"}</summary><p>已加载的记录仍可查看，记录数量不代表验证已通过。</p><code>{readiness.integrityWarnings.join(" · ") || `版本 ${readiness.revision} · 待同步 ${readiness.lag}`}</code></details> : null}
      <nav ref={tabList} className="evidence-tabs" role="tablist" aria-label="证据中心分类" onKeyDown={tabKeys}>{evidenceTabs.map((tab) => <button key={tab.id} id={`evidence-tab-${tab.id}`} role="tab" aria-selected={destination.tab === tab.id} aria-controls="evidence-tab-content" tabIndex={destination.tab === tab.id ? 0 : -1} onClick={() => open(tab.section)}>{tab.label}</button>)}</nav>
      <div id="evidence-tab-content" role="tabpanel" tabIndex={0} aria-labelledby={`evidence-tab-${destination.tab}`} className="evidence-tab-content">
        {destination.tab === "overview" ? <div id="evidence-overview">
          <div className="evidence-metrics"><div><span>执行用时</span><strong>{formatDuration(metrics.elapsedMs)}</strong></div><div><span>执行节点</span><strong>{metrics.completedNodeCount}<small> / {metrics.nodeCount} 完成</small></strong></div><div><span>任务交付物</span><strong>{artifacts.length}<small> 项</small></strong></div><div><span>执行者</span><strong>{metrics.workerCount}<small> 个</small></strong></div></div>
          <div className="evidence-overview-grid"><section className="evidence-card evidence-result"><div className="evidence-section-heading"><div><h2>任务结果</h2></div><span className="evidence-state" data-state={task.status}>{status.eyebrow}</span></div>{result ? <div className="evidence-result-content"><SafeMarkdown text={result} /></div> : <div className="evidence-empty"><strong>{status.title}</strong><p>{status.detail}</p></div>}</section>
          <section className="evidence-card evidence-verification"><div className="evidence-section-heading"><h2>验证与来源</h2></div>{verificationNodes.length ? <ul>{verificationNodes.map((node) => <li key={node.nodeId}><span className="evidence-state" data-state={node.status}>{nodeStateLabel(node.status)}</span><strong>{nodeLabel(node)}</strong><p>{nodeDescription(node) || "查看对应执行步骤与验证记录。"}</p></li>)}</ul> : <p className="evidence-muted">此任务没有单独的验证步骤。</p>}<div className="evidence-source-row"><span>已加载事件</span><strong>{events.length} 条</strong></div><div className="evidence-source-row"><span>产物与运行文件</span><strong>{task.artifacts.length} 项</strong></div><button className="evidence-text-button" onClick={() => open("evidence-artifacts")}>查看产物与校验 <span aria-hidden="true">→</span></button></section></div>
          <div className="evidence-shortcuts">{[{ section: "evidence-process", title: "查看执行过程", detail: "步骤状态与已加载事件" }, { section: "evidence-topology", title: "查看任务关系图", detail: "执行节点、依赖和协作" }, { section: "evidence-policy", title: "查看策略回执", detail: "提案、约束检查与实际执行" }].map((entry) => <button key={entry.section} onClick={() => open(entry.section)}><span><strong>{entry.title}</strong><small>{entry.detail}</small></span><span aria-hidden="true">→</span></button>)}</div>
          <details className="evidence-identifiers"><summary>运行信息</summary><dl className="evidence-record-facts"><div><dt>任务编号</dt><dd>{task.taskId}</dd></div><div><dt>运行编号</dt><dd>{task.runId}</dd></div><div><dt>会话编号</dt><dd>{task.sessionId || "—"}</dd></div><div><dt>创建时间</dt><dd>{dateTime(task.createdAt)}</dd></div><div><dt>数据版本</dt><dd>{readiness.revision}</dd></div><div><dt>事件序号</dt><dd>{readiness.committedSequence}</dd></div></dl></details>
        </div> : destination.tab === "process" ? <div className="evidence-process"><EvidencePlan task={task} /><EvidenceEvents key={target.eventId} events={events} initialQuery={target.eventId} /></div>
          : destination.tab === "artifacts" ? <TaskEvidenceSection runtime={runtime} task={task} section="evidence-artifacts" artifactId={target.artifactId} />
            : <div className="evidence-advanced"><div className="evidence-advanced-picker"><label htmlFor="evidence-record-category">记录类型</label><select id="evidence-record-category" value={selectedAdvanced.id} onChange={(event) => open(event.target.value)}>{advancedSections.map((entry) => <option key={entry.id} value={entry.id}>{entry.title}</option>)}</select><p>{selectedAdvanced.detail}</p></div><TaskEvidenceSection runtime={runtime} task={task} section={selectedAdvanced.id} onNavigate={navigate} /></div>}
      </div>
    </div>
  </section>
}

export function EvidenceWorkbench({ runtime, state, section }: { runtime: WorkbenchRuntime; state: TaskDetailState; section?: string }) {
  if (state.task && state.task.taskId === state.taskId) return <LoadedEvidenceWorkbench key={state.taskId} runtime={runtime} task={state.task} section={section} />
  return <section className="evidence-route evidence-route-loading"><TaskDetail runtime={runtime} state={state} /></section>
}
