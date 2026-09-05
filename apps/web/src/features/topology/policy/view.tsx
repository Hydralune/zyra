import {
  useEffect,
  useState,
  useSyncExternalStore,
} from "react"
import type {
  PolicyCausalReference,
  PolicyEvidenceTransition,
} from "../../../api/policy-api.ts"
import type { PolicyEvidenceRuntime } from "./runtime.ts"

const VIRTUAL_WINDOW = 20

type PhysicalLocation = "LOCAL" | "EDGE" | "CLOUD" | "UNKNOWN"
type EvidenceTruth = "real" | "simulated" | "degraded" | "missing" | "stale"

export interface PhysicalDispatchViewModel {
  location: PhysicalLocation
  lane: "terminal" | "local" | "edge" | "cloud" | "unknown"
  label: string
  truth: EvidenceTruth
  realGateClosed: boolean
  backendId?: string
  terminalId?: string
  attemptId?: string
  leaseId?: string
  permissionRef?: string
  failoverCount: number
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function optionalText(value: unknown): string | undefined {
  const selected = typeof value === "string" ? value.trim() : ""
  return selected || undefined
}

export function physicalDispatchViewModel(
  transition: PolicyEvidenceTransition,
): PhysicalDispatchViewModel | undefined {
  if (transition.contract_kind !== "physical_dispatch_receipt") return undefined
  const details = record(transition.details)
  const identity = record(details.physical_identity)
  const validation = record(details.physical_validation)
  const rawLocation = optionalText(identity.location)?.toLowerCase()
  const location: PhysicalLocation =
    rawLocation === "local"
      ? "LOCAL"
      : rawLocation === "edge"
        ? "EDGE"
        : rawLocation === "cloud"
          ? "CLOUD"
          : "UNKNOWN"
  const terminalId = optionalText(identity.terminal_id)
  const lane =
    location === "LOCAL" && terminalId
      ? "terminal"
      : location === "LOCAL"
        ? "local"
        : location === "EDGE"
          ? "edge"
          : location === "CLOUD"
            ? "cloud"
            : "unknown"
  const realGateClosed = validation.real_gate_closed === true
  const truth: EvidenceTruth =
    transition.integrity === "missing"
      ? "missing"
      : transition.integrity === "stale"
        ? "stale"
        : transition.execution === "real"
          && transition.integrity === "verified"
          && realGateClosed
          ? "real"
          : transition.execution === "simulated"
            ? "simulated"
            : "degraded"
  const recoveryEvidence = Array.isArray(details.recovery_evidence)
    ? details.recovery_evidence
    : []
  return Object.freeze({
    location,
    lane,
    label:
      lane === "terminal"
        ? "TERMINAL · LOCAL"
        : lane === "local"
          ? "LOCAL PROCESS"
          : lane === "edge"
            ? "EDGE RUNTIME"
            : lane === "cloud"
              ? "CLOUD PROVIDER"
              : "UNVERIFIED LOCATION",
    truth,
    realGateClosed,
    backendId: optionalText(identity.backend_id),
    terminalId,
    attemptId: optionalText(details.physical_attempt_id),
    leaseId: optionalText(details.lease_id),
    permissionRef: optionalText(details.permission_ref),
    failoverCount: recoveryEvidence.length,
  })
}

function compact(value: unknown, maximum = 32): string {
  const selected = String(value || "")
  if (!selected) return "—"
  return selected.length > maximum
    ? `${selected.slice(0, maximum - 9)}…${selected.slice(-8)}`
    : selected
}

function downloadEvidence(runtime: PolicyEvidenceRuntime): void {
  if (typeof document === "undefined" || typeof URL === "undefined") return
  const content = runtime.exportLoaded()
  const url = URL.createObjectURL(
    new Blob([content], { type: "application/json;charset=utf-8" }),
  )
  const link = document.createElement("a")
  link.href = url
  link.download = `zyra-policy-evidence-${runtime.getSnapshot().snapshotDigest || "degraded"}.json`
  link.click()
  URL.revokeObjectURL(url)
}

function EvidenceReference({
  reference,
  transition,
  onNavigate,
}: {
  reference: PolicyCausalReference
  transition: PolicyEvidenceTransition
  onNavigate?: (
    reference: PolicyCausalReference,
    transition: PolicyEvidenceTransition,
  ) => void
}) {
  return (
    <button
      className="button button-secondary policy-evidence-ref"
      type="button"
      data-evidence-ref-kind={reference.kind}
      data-evidence-ref-id={reference.id}
      onClick={() => onNavigate?.(reference, transition)}
    >
      <span>{policyLabel(reference.kind)}</span>
      <strong>{compact(reference.id, 24)}</strong>
    </button>
  )
}

function PhysicalDispatchSummary({
  transition,
}: {
  transition: PolicyEvidenceTransition
}) {
  const dispatch = physicalDispatchViewModel(transition)
  if (!dispatch) return null
  return (
    <div
      className="physical-dispatch-summary"
      data-physical-location={dispatch.location}
      data-physical-lane={dispatch.lane}
      data-evidence-truth={dispatch.truth}
      data-real-gate-closed={dispatch.realGateClosed || undefined}
    >
      <div>
        <strong>{({ terminal: "本地终端", local: "本地进程", edge: "边缘运行时", cloud: "云端服务", unknown: "执行位置未验证" })[dispatch.lane]}</strong>
        <span className="tag">{policyLabel(dispatch.truth)}</span>
        <span className="tag">
          {dispatch.realGateClosed ? "实际执行已确认" : "实际执行未确认"}
        </span>
      </div>
      <dl>
        <div><dt>执行后端</dt><dd>{dispatch.backendId ?? "未提供"}</dd></div>
        <div><dt>执行尝试</dt><dd>{dispatch.attemptId ?? "missing"}</dd></div>
        <div><dt>资源租约</dt><dd>{dispatch.leaseId ?? "missing"}</dd></div>
        <div><dt>权限依据</dt><dd>{dispatch.permissionRef ?? "missing"}</dd></div>
        <div><dt>故障转移次数</dt><dd>{dispatch.failoverCount}</dd></div>
        {dispatch.terminalId ? (
          <div><dt>终端</dt><dd>{dispatch.terminalId}</dd></div>
        ) : null}
      </dl>
      {dispatch.truth !== "real" ? (
        <p>
          此回执可用于诊断，尚不能证明实际执行成功。
        </p>
      ) : null}
    </div>
  )
}


const policyLabels: Record<string, string> = {
  ready: "已加载", loading: "加载中", idle: "等待加载", degraded: "连接不稳定", failed: "加载失败", closed: "已关闭",
  real: "真实执行", simulated: "模拟执行", not_executed: "未执行", unknown: "未确认", not_applicable: "无需执行", pending: "待校验",
  verified: "校验通过", missing: "证据缺失", stale: "待更新", rejected: "已拒绝", unverified: "未校验",
  proposal: "执行提案", symbolic_verdict: "约束裁决", graph_commit: "关系图变更",
  mechanism_proposal: "机制提案", mechanism_verdict: "机制裁决", mechanism_commit: "机制提交",
  physical_dispatch_receipt: "实际执行回执", operator_receipt: "操作回执", lease_receipt: "资源租约",
  artifact_receipt: "产物记录", verifier_receipt: "验证回执", continuity_receipt: "记忆连续性记录",
  artifact_manifest: "产物清单", verification_receipt: "验证回执", execution_receipt: "执行回执",
  topology_proposal_artifact: "协作方案", policy_decision_receipt: "策略裁决", policy_outcome: "策略结果",
  memory_continuity_receipt: "记忆连续性回执", neuro_symbolic_evidence_bundle: "推理与约束验证", runtime_evidence_event: "运行证据",
  event: "事件", artifact: "产物", node: "节点", checkpoint: "检查点", receipt: "回执", mutation: "变更",
}
function policyLabel(value: string): string { return policyLabels[value] ?? value }

export function PolicyEvidenceView({ runtime, title = "策略与执行回执", onNavigate }: {
  runtime: PolicyEvidenceRuntime
  title?: string
  onNavigate?: (reference: PolicyCausalReference, transition: PolicyEvidenceTransition) => void
}) {
  const snapshot = useSyncExternalStore(runtime.subscribe, runtime.getSnapshot, runtime.getSnapshot)
  const [mechanismVersion, setMechanismVersion] = useState(snapshot.query.mechanismVersion ?? "")
  const [receiptId, setReceiptId] = useState(snapshot.query.receiptId ?? "")
  const [windowStart, setWindowStart] = useState(0)
  const [expanded, setExpanded] = useState<string | undefined>(snapshot.selectedTransitionId || undefined)
  const maximumStart = Math.max(0, (Math.ceil(snapshot.transitions.length / VIRTUAL_WINDOW) - 1) * VIRTUAL_WINDOW)
  const boundedStart = Math.min(windowStart, maximumStart)
  const visible = snapshot.transitions.slice(boundedStart, boundedStart + VIRTUAL_WINDOW)
  useEffect(() => {
    void runtime.open()
    return () => runtime.close("Policy evidence view unmounted.")
  }, [runtime])
  return <section className="policy-evidence-view" aria-labelledby="policy-evidence-heading"
    data-policy-connection={snapshot.connection} data-policy-snapshot={snapshot.snapshotDigest}
    data-policy-transition-count={snapshot.transitions.length}>
    <div className="section-heading"><h3 id="policy-evidence-heading">{title}</h3><span className="tag">{policyLabel(snapshot.connection)}</span></div>
    <p className="muted-copy">核对提案、约束检查与执行结果。点击一条记录展开来源和验证详情。</p>
    {snapshot.error ? <div className="plan-warning" role="alert">回执暂时无法更新：{snapshot.error}</div> : null}
    {snapshot.issues.length ? <details className="evidence-sync-notice"><summary>{snapshot.issues.length} 项记录需要核对</summary>{snapshot.issues.map((issue, index) => <p key={index} data-evidence-integrity={issue.integrity}>{issue.code}: {issue.message}</p>)}</details> : null}
    <div className="policy-evidence-tools"><details><summary>筛选记录</summary><form className="policy-evidence-filters" onSubmit={(event) => {
      event.preventDefault(); setWindowStart(0); setExpanded(undefined)
      void runtime.setFilters({ mechanismVersion: mechanismVersion.trim() || undefined, receiptId: receiptId.trim() || undefined })
    }}><label>机制版本<input value={mechanismVersion} onChange={(event) => setMechanismVersion(event.target.value)} placeholder="全部版本" /></label><label>回执编号<input value={receiptId} onChange={(event) => setReceiptId(event.target.value)} placeholder="输入回执编号" /></label><button className="button button-primary" type="submit">应用筛选</button><button className="button button-secondary" type="button" onClick={() => { setMechanismVersion(""); setReceiptId(""); setWindowStart(0); setExpanded(undefined); void runtime.setFilters({ mechanismVersion: undefined, receiptId: undefined }) }}>重置</button></form></details><button className="button button-secondary" onClick={() => downloadEvidence(runtime)} disabled={!snapshot.transitions.length}>导出已加载记录</button></div>
    <div className="policy-evidence-window-controls"><span>已加载 {snapshot.transitions.length} 条{snapshot.hasMore ? " · 还有更多记录" : ""}</span><button className="button button-secondary" disabled={boundedStart === 0} onClick={() => setWindowStart(Math.max(0, boundedStart - VIRTUAL_WINDOW))}>上一页</button><span>{snapshot.transitions.length ? boundedStart + 1 : 0}–{boundedStart + visible.length}</span><button className="button button-secondary" disabled={boundedStart >= maximumStart} onClick={() => setWindowStart(Math.min(maximumStart, boundedStart + VIRTUAL_WINDOW))}>下一页</button>{snapshot.hasMore ? <button className="button button-secondary" disabled={snapshot.connection === "loading"} onClick={() => void runtime.loadNext()}>加载更多</button> : null}</div>
    <ol className="policy-evidence-timeline" aria-label="策略回执列表">{visible.map((item) => <li key={item.transition_id} data-policy-transition-id={item.transition_id} data-policy-contract-kind={item.contract_kind} data-policy-execution={item.execution} data-policy-integrity={item.integrity}>
      <button className="policy-evidence-transition" type="button" aria-expanded={expanded === item.transition_id} onClick={() => { runtime.select(item.transition_id); setExpanded(expanded === item.transition_id ? undefined : item.transition_id) }}>
        <span className="policy-evidence-sequence">#{item.sequence}</span><span><strong>{policyLabel(item.contract_kind)}</strong></span><span className="tag">{policyLabel(item.execution)}</span><span className="tag" data-integrity={item.integrity}>{policyLabel(item.integrity)}</span>
      </button>
      {expanded === item.transition_id ? <div className="policy-record-detail">
        <dl className="evidence-record-facts"><div><dt>原始类型</dt><dd>{item.contract_kind}</dd></div><div><dt>机制与版本</dt><dd>{item.mechanism.id}@{item.mechanism.version}</dd></div><div><dt>回执编号</dt><dd>{item.receipt_id}</dd></div><div><dt>处理结果</dt><dd>{item.disposition || "—"}</dd></div><div><dt>机制状态</dt><dd>{item.mechanism.lifecycle} · {item.mechanism.readiness}</dd></div><div><dt>校验摘要</dt><dd>{item.contract_digest}</dd></div></dl>
        {item.causal_refs.length ? <div className="policy-evidence-links">{item.causal_refs.slice(0, 12).map((reference) => <EvidenceReference key={reference.kind + ":" + reference.id} reference={reference} transition={item} onNavigate={onNavigate} />)}</div> : null}
        <PhysicalDispatchSummary transition={item} />
        {item.constraints.length ? <div><h4>约束检查</h4><ul>{item.constraints.slice(0, 40).map((constraint, index) => <li key={index}>{String(constraint.constraint_id || "约束")} · {constraint.passed === true ? "通过" : "未通过"} · {String(constraint.reason_code || "")}</li>)}</ul></div> : null}
        {item.graph_diff.length ? <details><summary>关系图变更（{item.graph_diff.length} 项）</summary><pre>{JSON.stringify(item.graph_diff, null, 2)}</pre></details> : null}
      </div> : null}
    </li>)}</ol>
    {!snapshot.transitions.length ? <div className="evidence-empty"><strong>{snapshot.connection === "loading" ? "正在加载回执…" : "暂无匹配的回执"}</strong><p>尚未提供记录，或当前筛选条件下没有结果。</p></div> : null}
    <details className="policy-evidence-audit"><summary>数据版本与导出范围</summary><dl className="evidence-record-facts"><div><dt>快照摘要</dt><dd>{snapshot.snapshotDigest || "—"}</dd></div><div><dt>最新事件序号</dt><dd>{snapshot.highWatermark}</dd></div><div><dt>已加载回执</dt><dd>{snapshot.transitions.length}</dd></div><div><dt>是否还有更多</dt><dd>{snapshot.hasMore ? "是" : "否"}</dd></div><div><dt>超出本地保留上限</dt><dd>{snapshot.droppedTransitions}</dd></div><div><dt>证据摘要数量</dt><dd>{snapshot.evidenceDigests.length}</dd></div></dl><p>导出包含当前已加载的记录及校验摘要；模拟执行会保留明确标记。</p></details>
  </section>
}
