import { useEffect, useRef, useState, type ReactNode } from "react"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import { useOverlaySnapshot } from "../../app/hooks.ts"
import { commandUsage, type CommandDefinition } from "../../command/catalog.ts"
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
  const groups = new Map<string, CommandDefinition[]>()
  for (const command of commands) {
    const current = groups.get(command.category) ?? []
    current.push(command)
    groups.set(command.category, current)
  }
  return (
    <div className="overlay-scroll command-reference">
      {[...groups.entries()].map(([category, values]) => (
        <section key={category}>
          <h3>{category}</h3>
          <dl>
            {values.map((command) => (
              <div key={command.id}>
                <dt>
                  <code>{commandUsage(command)}</code>
                  <span className={command.remoteSafe ? "tag" : "tag tag-muted"}>
                    {command.remoteSafe ? "remote safe" : "local"}
                  </span>
                </dt>
                <dd>{command.description}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
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
          <strong>{readiness?.ready ? "Runtime ready" : "Runtime unavailable"}</strong>
          <p>{state.failure?.message ?? state.health?.service ?? "No health response"}</p>
        </div>
      </div>
      <dl className="fact-grid">
        <div><dt>Phase</dt><dd>{state.phase}</dd></div>
        <div><dt>API version</dt><dd>{state.health?.apiVersion ?? "—"}</dd></div>
        <div><dt>Service</dt><dd>{state.health?.service ?? "—"}</dd></div>
        <div><dt>Capabilities</dt><dd>{state.health?.capabilities.length ?? 0}</dd></div>
      </dl>
      {readiness ? (
        <section>
          <h3>State owners</h3>
          <ul className="owner-list">
            {Object.entries(readiness.owners).map(([owner, ready]) => (
              <li key={owner} data-ready={ready}>
                <span>{owner}</span>
                <strong>{ready ? "ready" : "blocked"}</strong>
              </li>
            ))}
          </ul>
        </section>
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
        Refresh status
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
      await runtime.commands.submit(value, {
        origin: "overlay",
        taskId,
        runId,
        taskActive: action === "cancel",
        taskTerminal: action === "resume",
        allowQueue: false,
      })
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
          ? "This asks the backend to cancel the active run and its owned children."
          : "This asks the backend to resume from durable task state."}
      </p>
      {goal ? <blockquote>{goal}</blockquote> : null}
      <code>{taskId}</code>
      {action === "cancel" ? (
        <label>
          Cancellation reason
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
          Keep task
        </button>
        <button
          className={action === "cancel" ? "button button-danger" : "button button-primary"}
          type="button"
          disabled={busy || !taskId}
          onClick={() => void confirm()}
        >
          {busy ? "Sending…" : action === "cancel" ? "Cancel task" : "Resume task"}
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
        Command result payload is unavailable or invalid.
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
          <strong>{result.summary || result.title}</strong>
          {result.displayText && result.displayText !== result.summary ? (
            <p>{result.displayText}</p>
          ) : null}
        </div>
      </div>
      <dl className="fact-grid">
        <div><dt>Phase</dt><dd>{result.phase}</dd></div>
        <div><dt>Command</dt><dd><code>{result.name}</code></dd></div>
        <div><dt>Durable</dt><dd>{result.durable ? "yes" : "no"}</dd></div>
        <div><dt>Replayed</dt><dd>{result.replayed ? "yes" : "no"}</dd></div>
        <div><dt>Events</dt><dd>{result.eventIds.length}</dd></div>
        <div><dt>Elapsed</dt><dd>{result.elapsedMs === undefined ? "—" : `${result.elapsedMs} ms`}</dd></div>
      </dl>
      <div className="dialog-actions">
        <label>
          Filter result
          <input
            type="search"
            value={query}
            placeholder="event, owner, status, identity…"
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
            ? "Copied"
            : copyState === "failed"
              ? "Copy failed"
              : "Copy summary"}
        </button>
        {result.retryable && operation ? (
          <button
            className="button button-primary"
            type="button"
            onClick={() => {
              void runtime.controlCommands.retry(operation.operationId)
            }}
          >
            Retry command
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
        <section
          key={section.id}
          data-result-section={section.id}
          data-result-tone={section.tone}
        >
          <h3>{section.title} <span className="tag tag-muted">{section.count}</span></h3>
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
        </section>
      ))}
      {!totalRows ? (
        <p role="status">No result rows match this filter.</p>
      ) : null}
      {totalRows >= limit ? (
        <button
          className="button button-secondary"
          type="button"
          onClick={() => setLimit((value) => Math.min(5000, value + 100))}
        >
          Show more
        </button>
      ) : null}
      {result.diagnostics.length ? (
        <section>
          <h3>Diagnostics</h3>
          <ul>
            {result.diagnostics.map((diagnostic) => (
              <li key={diagnostic}>{diagnostic}</li>
            ))}
          </ul>
        </section>
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
              aria-label="Close dialog"
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
