import { useMemo, useState, type FormEvent } from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type {
  NavigationIntent,
  TopologyControllerSnapshot,
  TopologyControlAction,
} from "./contracts.ts"
import type { TopologyWorkbenchController } from "./controller.ts"
import { topologyLabel } from "./copy.ts"

function metadataBoolean(metadata: Record<string, unknown>, ...keys: string[]): boolean {
  for (const key of keys) {
    const value = metadata[key]
    if (typeof value === "boolean") return value
    if (typeof value === "string" && ["1", "true", "yes", "sealed"].includes(value.toLowerCase())) {
      return true
    }
  }
  return false
}
function taskSealed(task: TaskProjection): boolean {
  const mode = String(
    task.metadata.competition_mode ??
      task.metadata.permission_mode ??
      task.metadata.benchmark_mode ??
      "",
  ).toLocaleLowerCase()
  return (
    metadataBoolean(task.metadata, "sealed", "formal_benchmark", "sealed_autonomous") ||
    mode.includes("sealed")
  )
}

function DetailFacts({
  snapshot,
  controller,
  onNavigate,
}: {
  snapshot: TopologyControllerSnapshot
  controller: TopologyWorkbenchController
  onNavigate(intent: NavigationIntent): void
}) {
  const details = snapshot.selectedDetails
  const intents = useMemo(() => controller.navigationIntents(), [controller, snapshot.viewport.selected])
  if (!details) {
    return (
      <div className="topology-inspector-empty">
        <h4>选择一个节点</h4>
        <p>点击图中的节点或连线，查看状态、依赖和关联记录。</p>
      </div>
    )
  }
  return (
    <article className="topology-entity-details" id={`topology-inspector-${details.id}`}>
      <header>
        <span className="topology-detail-kind">{topologyLabel(details.kind)}</span>
        <h4>{topologyLabel(details.title)}</h4>
        <p>{topologyLabel(details.subtitle)}</p>
        <span className={`tag status-${details.state}`}>{topologyLabel(details.state)}</span>
      </header>
      <dl className="topology-detail-facts">
        {details.facts.map((fact) => (
          <div key={`${fact.label}:${fact.value}`} className={fact.tone ? `tone-${fact.tone}` : undefined}>
            <dt>{topologyLabel(fact.label)}</dt>
            <dd>{topologyLabel(fact.value)}</dd>
          </div>
        ))}
      </dl>
      {details.relatedEntityIds.length > 0 ? (
        <details>
          <summary>关联节点（{details.relatedEntityIds.length}）</summary>
          <div className="topology-related-list">
            {details.relatedEntityIds.slice(0, 80).map((id) => (
              <button
                key={id}
                type="button"
                className="topology-related-entity"
                onClick={() => {
                  for (const kind of ["node", "edge", "route", "placement", "checkpoint", "branch", "change"] as const) {
                    if (controller.select(kind, id, "causation")) return
                  }
                }}
              >
                {id}
              </button>
            ))}
          </div>
        </details>
      ) : null}
      {intents.length > 0 ? (
        <div className="topology-jump-actions" aria-label="Topology cross-view navigation">
          {intents.slice(0, 30).map((intent) => (
            <button
              key={intent.id}
              type="button"
              className="button button-secondary"
              onClick={() => {
                if (
                  intent.kind === "select-node" ||
                  intent.kind === "select-route" ||
                  intent.kind === "select-placement" ||
                  intent.kind === "select-checkpoint" ||
                  intent.kind === "focus-subgraph"
                ) {
                  controller.navigate(intent)
                } else {
                  onNavigate(intent)
                }
              }}
            >
              {intent.kind === "open-artifact"
                ? `查看产物 ${intent.artifactId}`
                : intent.kind === "open-timeline"
                  ? `查看事件 ${intent.eventId}`
                  : intent.kind === "open-causation"
                    ? `追踪来源 ${intent.causationId ?? intent.correlationId}`
                    : topologyLabel(intent.kind)}
            </button>
          ))}
        </div>
      ) : null}
      <details>
        <summary>原始证据</summary>
        <dl className="topology-evidence-list">
          <dt>事件</dt>
          <dd>{details.eventIds.join(", ") || "—"}</dd>
          <dt>变更</dt>
          <dd>{details.mutationIds.join(", ") || "—"}</dd>
          <dt>检查点</dt>
          <dd>{details.checkpointIds.join(", ") || "—"}</dd>
          <dt>产物</dt>
          <dd>{details.artifactIds.join(", ") || "—"}</dd>
        </dl>
      </details>
    </article>
  )
}

function CheckpointLineage({
  snapshot,
  controller,
}: {
  snapshot: TopologyControllerSnapshot
  controller: TopologyWorkbenchController
}) {
  if (snapshot.model.checkpoints.length === 0) {
    return <p className="muted-copy">暂无恢复检查点。</p>
  }
  return (
    <ol className="topology-checkpoint-list">
      {snapshot.model.checkpoints.slice(0, 80).map((checkpoint) => (
        <li key={checkpoint.id}>
          <button
            type="button"
            className={
              snapshot.viewport.selected?.kind === "checkpoint" &&
              snapshot.viewport.selected.id === checkpoint.id
                ? "is-selected"
                : undefined
            }
            onClick={() => controller.select("checkpoint", checkpoint.id, "pointer")}
          >
            <span className="checkpoint-title">{checkpoint.id}</span>
            <span>{checkpoint.phase}</span>
            <span>r{checkpoint.commitRevision}</span>
            <span className={checkpoint.pendingWrites > 0 ? "warning" : undefined}>
              {checkpoint.pendingWrites} pending / {checkpoint.committedWrites} committed
            </span>
            <span>{checkpoint.interruptCount} interrupt · {checkpoint.resumeCount} resume · {checkpoint.nextTaskCount} next</span>
          </button>
          {checkpoint.parentId ? <span className="checkpoint-parent">↳ {checkpoint.parentId}</span> : null}
        </li>
      ))}
    </ol>
  )
}

function BranchConflicts({
  snapshot,
  controller,
}: {
  snapshot: TopologyControllerSnapshot
  controller: TopologyWorkbenchController
}) {
  if (snapshot.model.branches.length === 0 && snapshot.model.conflicts.length === 0) {
    return <p className="muted-copy">暂无分支变更或冲突。</p>
  }
  return (
    <div className="topology-branch-list">
      {snapshot.model.branches.map((branch) => (
        <button
          key={branch.id}
          type="button"
          className={`topology-branch-card outcome-${branch.outcome}`}
          onClick={() => controller.select("branch", branch.id, "pointer")}
        >
          <strong>{branch.id}</strong>
          <span>{branch.outcome} · {branch.strategy}</span>
          <span>base {branch.baseRevision} → commit {branch.commitRevision}</span>
          <span>{branch.conflictCount} conflicts · {branch.visibleInCanonicalState ? "canonical" : "isolated"}</span>
        </button>
      ))}
      {snapshot.model.conflicts.map((conflict) => (
        <article className="topology-conflict-card" key={conflict.id}>
          <strong>{conflict.kind}: {conflict.key}</strong>
          <p>{conflict.reason}</p>
          <span>
            revision {conflict.baseRevision} → {conflict.currentRevision} · {conflict.recoverable ? "recoverable" : "requires replan"}
          </span>
        </article>
      ))}
    </div>
  )
}

function ControlForm({
  controller,
  snapshot,
  task,
}: {
  controller: TopologyWorkbenchController
  snapshot: TopologyControllerSnapshot
  task: TaskProjection
}) {
  const [action, setAction] = useState<TopologyControlAction>("local-update")
  const [nodeId, setNodeId] = useState("")
  const [checkpointId, setCheckpointId] = useState("")
  const [fieldName, setFieldName] = useState("role")
  const [fieldValue, setFieldValue] = useState("")
  const [text, setText] = useState("")
  const [error, setError] = useState("")
  const sealed = taskSealed(task)
  const busy = snapshot.receipts.some((receipt) =>
    ["validating", "submitting", "pending"].includes(receipt.phase),
  )
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError("")
    try {
      await controller.submitControl({
        action,
        actorId: "zyra-web-topology-operator",
        sealed,
        sessionId: task.sessionId,
        nodeId: nodeId || snapshot.viewport.selected?.kind === "node"
          ? nodeId || snapshot.viewport.selected?.id
          : undefined,
        checkpointId:
          checkpointId ||
          (snapshot.viewport.selected?.kind === "checkpoint"
            ? snapshot.viewport.selected.id
            : undefined),
        expectedRevision: snapshot.model.commitRevision,
        text,
        fields:
          action === "local-update"
            ? { [fieldName.trim() || "value"]: fieldValue }
            : undefined,
      })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }
  return (
    <form className="topology-control-form" onSubmit={submit}>
      <div className="topology-control-policy">
        <span className={sealed ? "tag tag-danger" : "tag"}>{sealed ? "封闭运行" : "交互运行"}</span>
        <span>
          操作将提交给运行时，处理结果以返回的回执为准。
        </span>
      </div>
      <label>
        操作类型
        <select value={action} onChange={(event) => setAction(event.currentTarget.value as TopologyControlAction)}>
          <option value="local-update">更新节点状态</option>
          <option value="requirement-change">修改需求并重新规划</option>
          <option value="time-travel">回到检查点</option>
          <option value="resume-checkpoint">从检查点恢复</option>
        </select>
      </label>
      {action === "local-update" || action === "requirement-change" ? (
        <label>
          节点
          <select value={nodeId} onChange={(event) => setNodeId(event.currentTarget.value)}>
            <option value="">当前选中节点 / 根节点</option>
            {snapshot.model.nodes.slice(0, 2_000).map((node) => (
              <option key={node.id} value={node.id}>{node.title} ({node.id})</option>
            ))}
          </select>
        </label>
      ) : null}
      {action === "local-update" ? (
        <div className="topology-control-fields">
          <label>
            字段
            <input value={fieldName} onChange={(event) => setFieldName(event.currentTarget.value)} />
          </label>
          <label>
            值
            <input value={fieldValue} onChange={(event) => setFieldValue(event.currentTarget.value)} required />
          </label>
        </div>
      ) : null}
      {action === "requirement-change" || action === "local-update" ? (
        <label>
          {action === "requirement-change" ? "新需求" : "备注"}
          <textarea
            value={text}
            onChange={(event) => setText(event.currentTarget.value)}
            required={action === "requirement-change"}
            rows={3}
          />
        </label>
      ) : null}
      {action === "time-travel" || action === "resume-checkpoint" ? (
        <label>
          检查点
          <select value={checkpointId} onChange={(event) => setCheckpointId(event.currentTarget.value)} required>
            <option value="">选择检查点</option>
            {snapshot.model.checkpoints.map((checkpoint) => (
              <option key={checkpoint.id} value={checkpoint.id}>
                {checkpoint.id} · r{checkpoint.commitRevision} · {checkpoint.phase}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <button type="submit" className="button button-primary" disabled={busy}>
        {busy ? "正在提交…" : sealed ? "提交操作（可能被拒绝）" : "提交操作"}
      </button>
    </form>
  )
}

function ReceiptList({
  snapshot,
  controller,
}: {
  snapshot: TopologyControllerSnapshot
  controller: TopologyWorkbenchController
}) {
  if (snapshot.receipts.length === 0) {
    return <p className="muted-copy">暂无控制操作回执。</p>
  }
  return (
    <ol className="topology-receipt-list">
      {snapshot.receipts.map((receipt) => (
        <li key={receipt.id} className={`receipt-${receipt.phase}`}>
          <div>
            <strong>{receipt.commandName}</strong>
            <span className="tag">{receipt.phase}</span>
            {receipt.interventionCounted ? <span className="tag tag-danger">intervention counted</span> : null}
            {receipt.replayed ? <span className="tag tag-muted">replayed</span> : null}
          </div>
          <p>{receipt.summary}</p>
          <dl>
            <dt>Request</dt>
            <dd>{receipt.requestId || "—"}</dd>
            <dt>Command</dt>
            <dd>{receipt.commandId || "—"}</dd>
            <dt>Checkpoint</dt>
            <dd>{receipt.checkpointId ?? "—"}</dd>
            <dt>Revision</dt>
            <dd>{receipt.expectedRevision ?? "—"} → {receipt.observedRevision ?? "pending"}</dd>
            <dt>事件</dt>
            <dd>{receipt.observedEventIds.join(", ") || "—"}</dd>
          </dl>
          {receipt.errorMessage ? <p className="form-error">{receipt.errorCode}: {receipt.errorMessage}</p> : null}
          {["validating", "submitting", "pending"].includes(receipt.phase) ? (
            <button type="button" className="link-button" onClick={() => controller.cancelControl(receipt.id)}>
              取消提交
            </button>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

export function TopologyInspector({
  controller,
  snapshot,
  task,
  onNavigate,
}: {
  controller: TopologyWorkbenchController
  snapshot: TopologyControllerSnapshot
  task: TaskProjection
  onNavigate(intent: NavigationIntent): void
}) {
  const [tab, setTab] = useState<"details" | "checkpoints" | "branches" | "control">("details")
  return (
    <aside className="topology-inspector" aria-label="Topology inspector">
      <nav className="topology-inspector-tabs" aria-label="Topology inspector views">
        {(["details", "checkpoints", "branches", "control"] as const).map((id) => (
          <button
            key={id}
            type="button"
            aria-current={tab === id ? "page" : undefined}
            onClick={() => setTab(id)}
          >
            {id === "checkpoints"
              ? `检查点 ${snapshot.model.checkpoints.length}`
              : id === "branches"
                ? `分支 ${snapshot.model.branches.length}`
                : id === "control"
                  ? `操作 ${snapshot.receipts.length}`
                  : "详情"}
          </button>
        ))}
      </nav>
      <div className="topology-inspector-content">
        {tab === "details" ? (
          <DetailFacts snapshot={snapshot} controller={controller} onNavigate={onNavigate} />
        ) : tab === "checkpoints" ? (
          <CheckpointLineage snapshot={snapshot} controller={controller} />
        ) : tab === "branches" ? (
          <BranchConflicts snapshot={snapshot} controller={controller} />
        ) : (
          <>
            <ControlForm controller={controller} snapshot={snapshot} task={task} />
            <ReceiptList snapshot={snapshot} controller={controller} />
          </>
        )}
      </div>
    </aside>
  )
}
