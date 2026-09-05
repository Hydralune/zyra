import { recordLabel } from "../../features/evidence/record-copy.ts"
import { useEffect, useRef, useState, type ReactNode } from "react"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import { useOverlaySnapshot } from "../../app/hooks.ts"
import type { CommandDefinition } from "../../command/catalog.ts"
import type { OverlayDescriptor } from "../../shell/overlay-runtime.ts"
import { FocusTrap } from "../../shell/focus-trap.ts"
import type {
  CommandResultModel,
} from "../../../../../packages/commands/src/index.ts"

function payloadString(overlay: OverlayDescriptor, key: string): string | undefined {
  const value = overlay.payload[key]
  return typeof value === "string" ? value : undefined
}

function CommandHelp({ commands }: { commands: readonly CommandDefinition[] }) {
  const [query, setQuery] = useState("")
  const search = query.trim().toLowerCase()
  const groups = new Map<string, CommandDefinition[]>()
  for (const command of commands) {
    if (search && !`/${command.trigger} ${command.title} ${command.description}`.toLowerCase().includes(search)) continue
    const current = groups.get(command.category) ?? []
    current.push(command)
    groups.set(command.category, current)
  }
  return (
    <div className="command-reference">
      <div className="command-reference-toolbar">
        <p>在输入框中输入 /，即可搜索和选择命令。</p>
        <input type="search" aria-label="搜索命令" placeholder="搜索命令或用途…" value={query} onChange={(event) => setQuery(event.target.value)} />
      </div>
      <div className="command-reference-list">
      {[...groups.entries()].map(([category, values]) => (
        <section key={category}>
          <h3>{{ help: "帮助", navigation: "导航", runtime: "运行与工具", task: "任务" }[category] ?? category}</h3>
          <dl>
            {values.map((command) => (
              <div key={command.id}>
                <dt>
                  <code>/{command.trigger}</code>
                </dt>
                <dd>{command.description}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      {!groups.size ? <p className="muted-copy" role="status">没有找到匹配的命令。</p> : null}
      </div>
    </div>
  )
}

function KeyboardHelp({ shortcuts }: { shortcuts: readonly (readonly string[])[] }) {
  return (
    <dl className="shortcut-list">
      {shortcuts.map(([keys, description]) => (
        <div key={keys}>
          <dt>{keys?.split("+").map((key) => <kbd key={key}>{key}</kbd>)}</dt>
          <dd>{description}</dd>
        </div>
      ))}
    </dl>
  )
}

function RuntimeStatus({ runtime, overlay }: { runtime: WorkbenchRuntime; overlay: OverlayDescriptor }) {
  const state = runtime.workbench.getSnapshot().runtime
  const readiness = state.readiness
  return (
    <div className="runtime-status-detail">
      <div className={`runtime-summary runtime-${state.phase}`}>
        <span className="connection-dot" aria-hidden="true" />
        <div>
          <strong>{readiness?.ready ? "运行时就绪" : "运行时不可用"}</strong>
          <p>{state.failure?.message ?? state.health?.service ?? "尚未收到服务响应"}</p>
        </div>
      </div>
      <dl className="fact-grid">
        <div><dt>连接状态</dt><dd>{{ ready: "已连接", loading: "正在连接", reconnecting: "正在重连", error: "连接失败", idle: "尚未连接", empty: "尚未连接" }[state.phase] ?? state.phase}</dd></div>
        <div><dt>API 版本</dt><dd>{state.health?.apiVersion ?? "—"}</dd></div>
        <div><dt>服务</dt><dd>{state.health?.service ?? "—"}</dd></div>
        <div><dt>可用能力</dt><dd>{state.health?.capabilities.length ?? 0}</dd></div>
      </dl>
      {readiness ? (
        <details>
          <summary>运行组件 · {Object.keys(readiness.owners).length} 项</summary>
          <ul className="owner-list">
            {Object.entries(readiness.owners).map(([owner, ready]) => (
              <li key={owner} data-ready={ready}>
                <span>{owner}</span>
                <strong>{ready ? "就绪" : "不可用"}</strong>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <button
        className="button button-secondary"
        type="button"
        onClick={() => void runtime.workbench.refreshRuntime().then(() => {
          runtime.overlays.update(overlay.id, {
            payload: { refreshedAt: Date.now() },
          })
        })}
      >
        刷新状态
      </button>
    </div>
  )
}

function MutationConfirm({
  runtime,
  overlay,
  action,
}: {
  runtime: WorkbenchRuntime
  overlay: OverlayDescriptor
  action: "cancel" | "resume"
}) {
  const taskId = payloadString(overlay, "taskId")
  const runId = payloadString(overlay, "runId")
  const goal = payloadString(overlay, "goal")
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  const confirm = async () => {
    if (!taskId || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const value =
        action === "cancel"
          ? `/cancel ${reason.trim() || "Cancelled from the Zyra web console."}`
          : "/resume"
      const pending = runtime.commands.submit(value, {
        origin: "overlay",
        taskId,
        runId,
        taskActive: action === "cancel",
        taskTerminal: action === "resume",
        allowQueue: false,
      })
      if (action === "resume") runtime.overlays.close(overlay.id, { force: true })
      await pending
      runtime.overlays.close(overlay.id, { force: true })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="confirmation-content">
      <p>
        {action === "cancel"
          ? "停止当前任务及其子任务。已有结果会保留，你可以稍后重新运行。"
          : "从已保存的状态继续执行这个任务。"}
      </p>
      {goal ? <blockquote>{goal}</blockquote> : null}
      <code>{taskId}</code>
      {action === "cancel" ? (
        <label>
          停止原因（可选）
          <textarea
            value={reason}
            rows={3}
            maxLength={8192}
            disabled={busy}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      ) : null}
      {error ? <div className="command-error" role="alert">{error}</div> : null}
      <div className="dialog-actions">
        <button
          className="button button-secondary"
          type="button"
          disabled={busy}
          onClick={() => runtime.overlays.close(overlay.id)}
        >
          返回
        </button>
        <button
          className={action === "cancel" ? "button button-danger" : "button button-primary"}
          type="button"
          disabled={busy || !taskId}
          onClick={() => void confirm()}
        >
          {busy ? "正在提交…" : action === "cancel" ? "停止任务" : "继续执行"}
        </button>
      </div>
    </div>
  )
}

function CommandResultContent({
  runtime,
  overlay,
}: {
  runtime: WorkbenchRuntime
  overlay: OverlayDescriptor
}) {
  const candidate = overlay.payload.result
  const result =
    candidate &&
    typeof candidate === "object" &&
    (candidate as { schema?: unknown }).schema === "zyra.command-result-model/v1"
      ? candidate as CommandResultModel
      : undefined
  const [query, setQuery] = useState("")
  const [limit, setLimit] = useState(100)
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle")
  if (!result) {
    return (
      <div className="command-error" role="alert">
        命令结果暂时无法读取。
      </div>
    )
  }
  const filtered =
    runtime.controlCommands.results.filtered(
      result.commandId,
      query,
      { offset: 0, limit },
    ) ?? result
  const totalRows = filtered.sections.reduce(
    (total, section) => total + section.rows.length,
    0,
  )
  const operation = runtime.controlCommands.coordinator
    .records(500)
    .find((record) => record.identity.commandId === result.commandId)
  const panelTarget = new Map([
    ["/mcp", { id: "mcp-runtime-panel", label: "查看 MCP 服务" }],
    ["/skills", { id: "skill-runtime-panel", label: "查看技能" }],
    ["/agents", { id: "subagent-runtime-panel", label: "查看协作代理" }],
    ["/tasks", { id: "subagent-runtime-panel", label: "查看子任务" }],
  ]).get(result.name)
  return (
    <div
      className="overlay-scroll command-result"
      data-command={result.name}
      data-command-phase={result.phase}
      data-command-durable={result.durable ? "true" : "false"}
    >
      <div className={`runtime-summary runtime-${result.tone}`}>
        <span className="connection-dot" aria-hidden="true" />
        <div>
          <strong>{recordLabel(result.summary || result.title)}</strong>
          {result.displayText && result.displayText !== result.summary ? (
            <p>{result.displayText}</p>
          ) : null}
        </div>
      </div>
      <dl className="fact-grid">
        <div><dt>执行状态</dt><dd>{recordLabel(result.phase)}</dd></div>
        <div><dt>命令</dt><dd><code>{result.name}</code></dd></div>
        <div><dt>记录已保存</dt><dd>{result.durable ? "是" : "否"}</dd></div>
        <div><dt>历史回放</dt><dd>{result.replayed ? "是" : "否"}</dd></div>
        <div><dt>关联事件</dt><dd>{result.eventIds.length}</dd></div>
        <div><dt>处理用时</dt><dd>{result.elapsedMs === undefined ? "—" : `${result.elapsedMs} ms`}</dd></div>
      </dl>
      <div className="dialog-actions">
        <label>
          筛选结果
          <input
            type="search"
            value={query}
            placeholder="搜索内容、状态或编号…"
            onChange={(event) => {
              setQuery(event.target.value)
              setLimit(100)
            }}
          />
        </label>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => {
            void navigator.clipboard
              .writeText(result.displayText || result.summary)
              .then(
                () => setCopyState("copied"),
                () => setCopyState("failed"),
              )
          }}
        >
          {copyState === "copied"
            ? "已复制"
            : copyState === "failed"
              ? "复制失败"
              : "复制摘要"}
        </button>
        {result.retryable && operation ? (
          <button
            className="button button-primary"
            type="button"
            onClick={() => {
              void runtime.controlCommands.retry(operation.operationId)
            }}
          >
            重试命令
          </button>
        ) : null}
        {panelTarget ? (
          <button
            className="button button-primary"
            type="button"
            onClick={() => {
              const taskId = operation?.context.taskId ?? runtime.workbench.getSnapshot().selectedTaskId
              if (!taskId) return
              runtime.overlays.close(overlay.id)
              runtime.router.openEvidence(taskId, { section: panelTarget.id })
            }}
          >
            {panelTarget.label}
          </button>
        ) : null}
      </div>
      {result.error ? (
        <div className="command-error" role="alert">
          <strong>{result.error.code}</strong>
          <p>{result.error.message}</p>
        </div>
      ) : null}
      {filtered.sections.map((section) => (
        <details
          key={section.id}
          data-result-section={section.id}
          data-result-tone={section.tone}
          open={Boolean(query) || section.id === "details"}
        >
          <summary>{recordLabel(section.title)} <span className="tag tag-muted">{section.count}</span></summary>
          {section.description ? <p>{section.description}</p> : null}
          <ol className="owner-list">
            {section.rows.map((row) => (
              <li key={row.id} data-result-kind={row.kind} data-result-tone={row.tone}>
                <div>
                  <strong>{row.title}</strong>
                  {row.summary ? <p>{row.summary}</p> : null}
                </div>
                {row.status ? <span className="tag">{row.status}</span> : null}
                {row.fields.length ? (
                  <dl className="fact-grid">
                    {row.fields.map((field) => (
                      <div key={field.id}>
                        <dt>{field.label}</dt>
                        <dd>{field.copyable ? <code>{field.value}</code> : field.value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </li>
            ))}
          </ol>
        </details>
      ))}
      {!totalRows ? (
        <p role="status">没有匹配的结果。</p>
      ) : null}
      {totalRows >= limit ? (
        <button
          className="button button-secondary"
          type="button"
          onClick={() => setLimit((value) => Math.min(5000, value + 100))}
        >
          显示更多
        </button>
      ) : null}
      {result.diagnostics.length ? (
        <details>
          <summary>诊断信息</summary>
          <ul>
            {result.diagnostics.map((diagnostic) => (
              <li key={diagnostic}>{diagnostic}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}

function OverlayContent({
  runtime,
  overlay,
}: {
  runtime: WorkbenchRuntime
  overlay: OverlayDescriptor
}): ReactNode {
  if (overlay.kind === "command-help") {
    const commands = Array.isArray(overlay.payload.commands)
      ? overlay.payload.commands as CommandDefinition[]
      : runtime.catalog.list()
    return <CommandHelp commands={commands} />
  }
  if (overlay.kind === "keyboard-help") {
    const shortcuts = Array.isArray(overlay.payload.shortcuts)
      ? overlay.payload.shortcuts as string[][]
      : []
    return <KeyboardHelp shortcuts={shortcuts} />
  }
  if (overlay.kind === "transport-status") {
    return <RuntimeStatus runtime={runtime} overlay={overlay} />
  }
  if (overlay.kind === "task-cancel") {
    return <MutationConfirm runtime={runtime} overlay={overlay} action="cancel" />
  }
  if (overlay.kind === "task-resume") {
    return <MutationConfirm runtime={runtime} overlay={overlay} action="resume" />
  }
  if (overlay.kind === "command-result") {
    return <CommandResultContent runtime={runtime} overlay={overlay} />
  }
  return <pre>{JSON.stringify(overlay.payload, null, 2)}</pre>
}

export function OverlayHost({ runtime }: { runtime: WorkbenchRuntime }) {
  const snapshot = useOverlaySnapshot(runtime)
  const overlay = snapshot.active
  const dialogRef = useRef<HTMLDivElement>(null)
  const previousRef = useRef<string | undefined>(undefined)
  const trapRef = useRef<FocusTrap | undefined>(undefined)

  useEffect(() => {
    if (overlay) {
      runtime.focus.remember(overlay.id, {
        targetId: document.activeElement instanceof HTMLElement ? document.activeElement.id : undefined,
        fallbackSelector: "#workbench-command-input",
        reason: `overlay:${overlay.kind}`,
      })
      previousRef.current = overlay.id
      requestAnimationFrame(() => {
        if (!dialogRef.current) return
        const trap = new FocusTrap(dialogRef.current)
        trapRef.current = trap
        trap.activate({ fallbackToContainer: true })
      })
      return
    }
    const previous = previousRef.current
    if (previous) {
      runtime.focus.restore(previous)
      previousRef.current = undefined
    }
    trapRef.current?.deactivate()
    trapRef.current = undefined
  }, [overlay?.id, runtime])

  if (!overlay) return null
  return (
    <div
      className="overlay-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && overlay.dismissible) {
          runtime.overlays.close(overlay.id)
        }
      }}
    >
      <div
        ref={dialogRef}
        className="overlay-dialog"
        role="dialog"
        aria-modal={overlay.modal}
        aria-labelledby="overlay-title"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Tab" && trapRef.current?.handleTab(event.nativeEvent)) return
          if (event.key === "Escape" && overlay.dismissible) {
            event.preventDefault()
            event.stopPropagation()
            runtime.overlays.close(overlay.id)
          }
        }}
      >
        <header>
          <h2 id="overlay-title">{overlay.title}</h2>
          {overlay.dismissible ? (
            <button
              className="icon-button"
              type="button"
              aria-label="关闭对话框"
              onClick={() => runtime.overlays.close(overlay.id)}
            >
              ×
            </button>
          ) : null}
        </header>
        <OverlayContent runtime={runtime} overlay={overlay} />
      </div>
    </div>
  )
}
