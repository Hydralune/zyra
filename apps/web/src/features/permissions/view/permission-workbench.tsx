import { phaseLabel } from "../../../shell/product-copy.ts"
import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { useProjectionSelector } from "../../../app/hooks.ts"
import { selectEventsForTask } from "../../../state/selectors.ts"
import type {
  PermissionProductMode,
  PermissionResponseEffect,
} from "../contracts.ts"
import { permissionDisplayActor } from "../sealed-control.ts"
import { PermissionDetail } from "./permission-detail.tsx"
import { PermissionQueue } from "./permission-queue.tsx"
import { PermissionTimeline } from "./permission-timeline.tsx"
import { PermissionPolicyPanel } from "./permission-policy-panel.tsx"

export function PermissionWorkbench(input: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}): React.JSX.Element {
  const consoleRuntime = input.runtime.permissionConsole
  const snapshot = useSyncExternalStore(
    consoleRuntime.subscribe,
    consoleRuntime.getSnapshot,
    consoleRuntime.getSnapshot,
  )
  const [feedback, setFeedback] = useState("")
  const canonicalEvents = useProjectionSelector(
    input.runtime,
    selectEventsForTask(input.task.taskId, { limit: 5_000 }),
  )
  const productMode = useMemo(
    () => taskPermissionMode(input.task),
    [input.task],
  )
  useEffect(() => {
    const sessionId = consoleRuntime.consoleSessionId(input.task.taskId)
    void consoleRuntime.bindTask({
      taskId: input.task.taskId,
      runId: input.task.runId,
      sessionId,
      productMode,
      policyHash: metadataDigest(
        input.task.metadata,
        "permission_policy_hash",
      ),
      policyRevision: metadataRevision(
        input.task.metadata,
        "permission_policy_revision",
      ),
      humanInterventionCount: metadataRevision(
        input.task.metadata,
        "human_intervention_count",
      ),
    })
  }, [
    consoleRuntime,
    input.task.taskId,
    input.task.runId,
    input.task.metadata.permission_policy_hash,
    input.task.metadata.permission_policy_revision,
    input.task.metadata.human_intervention_count,
    productMode,
  ])
  useEffect(() => {
    consoleRuntime.ingestCanonicalEvents(canonicalEvents)
  }, [consoleRuntime, canonicalEvents])

  const selected = snapshot.selected
  const responding = selected
    ? snapshot.respondingRequestIds.includes(selected.requestId)
    : false
  const respond = async (effect: PermissionResponseEffect) => {
    if (!selected) return
    try {
      const result = await consoleRuntime.respond({
        requestId: selected.requestId,
        effect,
        source: sourceForRequest(selected.sourceSurface),
        displayResponder: permissionDisplayActor(input.task.metadata),
        feedback,
      })
      if ("accepted" in result && result.accepted) {
        setFeedback("")
        input.runtime.notifications.push({
          id: `permission-response-${result.responseId}`,
          title:
            effect === "allow"
              ? "Exact call allowed once"
              : "Permission denied",
          message:
            result.permitId
              ? `Permit ${result.permitId} is bound to the approved call.`
              : "Canonical permission decision resumed the worker.",
          tone: "success",
          taskId: input.task.taskId,
        })
        input.runtime.announcer.announce(
          `${effect} permission response accepted.`,
        )
      }
    } catch (error) {
      const message =
        error instanceof Error ? error.message : String(error)
      input.runtime.notifications.push({
        id: `permission-response-error-${selected.requestId}`,
        title: "Permission response rejected",
        message,
        tone: "error",
        durationMs: 10_000,
        taskId: input.task.taskId,
      })
      input.runtime.announcer.announce(
        `Permission response rejected: ${message}`,
        "assertive",
      )
    }
  }
  const scrollTo = (selector: string) => {
    if (typeof document === "undefined") return
    const target = document.querySelector<HTMLElement>(selector)
    if (!target) {
      input.runtime.notifications.push({
        id: `permission-cross-view-${selected?.requestId ?? "none"}`,
        title: "Related view is not projected",
        message:
          "The canonical request is available, but the related panel has no matching projection yet.",
        tone: "warning",
        durationMs: 6_000,
        taskId: input.task.taskId,
      })
      return
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" })
    target.focus({ preventScroll: true })
  }
  return (
    <section
      className="detail-section permission-workbench"
      aria-labelledby="permission-workbench-heading"
      data-permission-console
      data-permission-phase={snapshot.phase}
      data-permission-product-mode={snapshot.productMode}
      data-permission-owner={snapshot.diagnostics.sourceOwner}
      data-permission-local-pending-store="false"
    >
      <header className="permission-workbench-header">
        <div>
          <h3 id="permission-workbench-heading">权限与审批</h3>
          <p>
            查看待审批操作，核对执行范围后决定是否允许本次调用。
          </p>
        </div>
        <div className="permission-status-badges">
          <span className="tag">{snapshot.pendingCount} 项待处理</span>
          <span className="tag tag-muted">{phaseLabel(snapshot.phase)}</span>
          <span
            className={
              snapshot.productMode === "sealed"
                ? "tag tag-danger"
                : "tag"
            }
          >
            {snapshot.productMode === "sealed" ? "全自主模式" : "交互模式"}
          </span>
          <button
            type="button"
            className="button button-secondary"
            disabled={
              snapshot.phase === "binding"
              || snapshot.phase === "loading"
              || snapshot.phase === "responding"
            }
            onClick={() => void consoleRuntime.refresh("manual")}
          >
            刷新
          </button>
        </div>
      </header>

      <details className="product-disclosure"><summary>权限来源与诊断</summary>
      <PermissionOwnershipStatus
        mode={snapshot.productMode}
        owner={snapshot.diagnostics.sourceOwner}
        schema={snapshot.diagnostics.sourceSchema}
        responseError={snapshot.responseError}
        reconnectCount={snapshot.diagnostics.reconnectCount}
        interventionCount={snapshot.interventions.length}
      />

      </details>
      <div className="permission-workbench-grid">
        <PermissionQueue
          rows={snapshot.rows}
          selectedRequestId={snapshot.selectedRequestId}
          onSelect={(requestId) => {
            setFeedback("")
            consoleRuntime.select(requestId)
          }}
        />
        <PermissionDetail
          request={selected}
          mode={snapshot.productMode}
          responding={responding}
          feedback={feedback}
          onFeedback={setFeedback}
          onRespond={(effect) => void respond(effect)}
          onCrossView={scrollTo}
        />
      </div>

      <PermissionTimeline
        entries={snapshot.timeline}
        selectedRequestId={snapshot.selectedRequestId}
      />
      <PermissionPolicyPanel
        mode={snapshot.mode}
        rules={snapshot.rules}
        request={snapshot.selected}
      />
    </section>
  )
}

function PermissionOwnershipStatus(input: {
  mode: PermissionProductMode
  owner: string
  schema: string
  responseError?: {
    code: string
    message: string
    requestId?: string
    retryable: boolean
  }
  reconnectCount: number
  interventionCount: number
}): React.JSX.Element {
  return (
    <div className="permission-ownership-status" role="status">
      <div>
        <strong>状态来源</strong>
        <span>{input.owner}</span>
        <span className="mono">{input.schema}</span>
      </div>
      <div>
        <strong>浏览器存储</strong>
        <span>审批状态由后端保存</span>
      </div>
      {input.mode === "sealed" ? (
        <div className="permission-sealed-banner">
          <strong>全自主模式</strong>
          <span>
            该模式不接受人工审批、引导、重试或模式修改，相关请求由后端记录并处理。
          </span>
          <span>
            {input.interventionCount} local receipt(s) ·{" "}
            {input.reconnectCount} reconnect(s)
          </span>
        </div>
      ) : null}
      {input.responseError ? (
        <div className="permission-error-banner" role="alert">
          <strong>{input.responseError.code}</strong>
          <span>{input.responseError.message}</span>
        </div>
      ) : null}
    </div>
  )
}

function taskPermissionMode(task: TaskProjection): PermissionProductMode {
  const metadata = task.metadata
  const mode = String(
    metadata.competition_mode
      ?? metadata.permission_mode
      ?? metadata.mode
      ?? "",
  ).toLowerCase()
  return (
    metadata.sealed_autonomous === true
    || metadata.sealed === true
    || mode === "sealed"
    || mode === "sealed_autonomous"
  )
    ? "sealed"
    : "interactive"
}

function metadataDigest(
  metadata: Readonly<Record<string, unknown>>,
  key: string,
): string | undefined {
  const value = metadata[key]
  return typeof value === "string" && /^[0-9a-f]{64}$/i.test(value)
    ? value.toLowerCase()
    : undefined
}

function metadataRevision(
  metadata: Readonly<Record<string, unknown>>,
  key: string,
): number | undefined {
  const value = Number(metadata[key])
  return Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

function sourceForRequest(
  source: string,
): "permission-panel" | "browser-panel" | "terminal-panel" | "command" {
  if (source === "browser") return "browser-panel"
  if (source === "terminal") return "terminal-panel"
  if (source === "command") return "command"
  return "permission-panel"
}
