import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type RefObject,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import { parseTerminalSession, type TerminalSessionProjection } from "../contracts.ts"
import { renderTerminalViewport, type TerminalRenderRun } from "../render.ts"
import { TerminalRuntime, type TerminalRuntimeSnapshot } from "../runtime.ts"
import {
  extractTerminalSelection,
  searchTerminalSelections,
  type TerminalSelectionRange,
} from "../selection.ts"
import {
  TerminalTabStore,
  browserTerminalTabStorage,
  type TerminalTabState,
} from "../tabs.ts"

interface TerminalDraft {
  command: string
  title: string
  cwd: string
  shell: string
  permitId: string
  sessionId: string
  workerId: string
  commandId: string
  toolCallId: string
  spanId: string
}

function token(prefix: string): string {
  const suffix = (
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replaceAll("-", "").slice(0, 20)
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`
  )
  return `${prefix}_${suffix}`
}

function taskSession(task: TaskProjection): string {
  const candidates = [
    task.sessionId,
    task.metadata.query_session_id,
    task.metadata.session_id,
  ]
  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return `session_${task.taskId.replace(/[^A-Za-z0-9]/gu, "").slice(-32)}`
}

function defaultDraft(task: TaskProjection): TerminalDraft {
  const root = task.planNodes.find((node) => node.nodeId === task.rootNodeId)
  return {
    command: "python -i",
    title: "Task terminal",
    cwd: ".",
    shell: "",
    permitId: "",
    sessionId: taskSession(task),
    workerId: root?.assignedWorkerId || "terminal-viewer",
    commandId: token("terminal_command"),
    toolCallId: token("terminal_tool"),
    spanId: token("span"),
  }
}

function terminalValues(value: unknown): readonly unknown[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return []
  const source = value as Record<string, unknown>
  return Array.isArray(source.terminals) ? source.terminals : []
}

function initialTabState(tabs: TerminalTabStore): TerminalTabState {
  return tabs.snapshot()
}

function useTerminalSnapshot(
  runtime: TerminalRuntime,
  terminalId: string | undefined,
): TerminalRuntimeSnapshot | undefined {
  const [snapshot, setSnapshot] = useState<TerminalRuntimeSnapshot>()
  useEffect(() => {
    if (!terminalId) {
      setSnapshot(undefined)
      return
    }
    try {
      setSnapshot(runtime.snapshot(terminalId))
      return runtime.listen(terminalId, setSnapshot)
    } catch {
      setSnapshot(undefined)
      return
    }
  }, [runtime, terminalId])
  return snapshot
}

function statusTone(phase: string | undefined): string {
  if (phase === "running") return "is-running"
  if (phase === "opening" || phase === "permission_pending") return "is-pending"
  if (phase === "exited") return "is-exited"
  return "is-failed"
}

function Run({ run }: { run: TerminalRenderRun }) {
  if (run.hyperlink) {
    return (
      <a
        className={run.className}
        href={run.hyperlink}
        rel="noreferrer"
        target="_blank"
        style={run.style as CSSProperties}
      >
        {run.text}
      </a>
    )
  }
  return (
    <span className={run.className} style={run.style as CSSProperties}>
      {run.text}
    </span>
  )
}

function Screen({
  snapshot,
  container,
}: {
  snapshot: TerminalRuntimeSnapshot
  container: RefObject<HTMLDivElement | null>
}) {
  const viewport = useMemo(
    () => renderTerminalViewport(snapshot.screen, {
      includeScrollback: true,
      followOutput: true,
      visibleRows: snapshot.screen.rows,
      overscan: 12,
    }),
    [snapshot.screen],
  )
  useEffect(() => {
    const element = container.current
    if (element) element.scrollTop = element.scrollHeight
  }, [container, viewport.revision, viewport.totalRows])
  return (
    <div
      className="terminal-screen-lines"
      style={{
        paddingTop: `${viewport.beforeRows * 18}px`,
        paddingBottom: `${viewport.afterRows * 18}px`,
      }}
    >
      {viewport.rows.map((line) => (
        <div
          className={`terminal-screen-line${line.wrapped ? " is-wrapped" : ""}`}
          data-terminal-row={line.row}
          key={line.key}
        >
          {line.runs.map((run) => <Run key={run.key} run={run} />)}
          {line.cursorColumn === undefined ? null : (
            <span
              aria-hidden="true"
              className={[
                "terminal-cursor",
                `is-${line.cursorShape ?? "block"}`,
                line.cursorBlinking ? "is-blinking" : "",
              ].join(" ")}
              style={{ left: `${line.cursorColumn}ch` }}
            />
          )}
        </div>
      ))}
    </div>
  )
}

export function TerminalWorkbench({
  runtime,
  task,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
}) {
  const tabs = useMemo(() => {
    const storage = (
      typeof window !== "undefined" && window.localStorage
        ? browserTerminalTabStorage(window.localStorage)
        : undefined
    )
    return new TerminalTabStore(`${task.runId}:${task.taskId}`, { storage })
  }, [task.runId, task.taskId])
  const terminal = useMemo(
    () => new TerminalRuntime({ taskApi: runtime.api.tasks, tabs }),
    [runtime.api.tasks, tabs],
  )
  const [tabState, setTabState] = useState(() => initialTabState(tabs))
  const [draft, setDraft] = useState(() => defaultDraft(task))
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const screenRef = useRef<HTMLDivElement>(null)
  const activeId = tabState.active
  const active = useTerminalSnapshot(terminal, activeId)
  const searchResult = useMemo(
    () => active
      ? searchTerminalSelections(active.screen, search, {
          includeScrollback: true,
          maximumMatches: 10_000,
        })
      : { query: "", matches: [], truncated: false },
    [active, search],
  )

  useEffect(() => tabs.listen(setTabState), [tabs])
  useEffect(() => () => terminal.dispose(), [terminal])
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const preferred = tabs.snapshot().active
    void runtime.api.tasks.terminalList(task.taskId, { includeClosed: true })
      .then(async (response) => {
        const adopted = new Set<string>()
        for (const value of terminalValues(response)) {
          if (cancelled) return
          const projection = parseTerminalSession(value)
          adopted.add(projection.binding.terminalId)
          await terminal.adopt(projection, { connect: false })
        }
        for (const tab of tabs.snapshot().tabs) {
          if (!adopted.has(tab.terminalId)) tabs.close(tab.terminalId)
        }
        if (preferred && adopted.has(preferred)) {
          tabs.select(preferred)
        }
        const selected = tabs.snapshot().active
        if (selected) {
          const snapshot = terminal.snapshot(selected)
          if (snapshot.status?.phase === "running") {
            await terminal.connect(selected)
          }
        }
      })
      .catch((failure) => {
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : String(failure))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [runtime.api.tasks, tabs, task.taskId, terminal])

  useEffect(() => {
    const element = screenRef.current
    if (!element || !activeId || !active?.connected) return
    let previous = ""
    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect
      if (!box) return
      const rows = Math.max(2, Math.min(500, Math.floor(box.height / 18)))
      const cols = Math.max(2, Math.min(1_000, Math.floor(box.width / 8.4)))
      const key = `${rows}:${cols}`
      if (key === previous) return
      previous = key
      terminal.resize(activeId, rows, cols)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [active?.connected, activeId, terminal])

  const create = async () => {
    if (busy) return
    setBusy(true)
    setError(undefined)
    try {
      const snapshot = await terminal.create({
        taskId: task.taskId,
        runId: task.runId,
        sessionId: draft.sessionId,
        workerId: draft.workerId,
        commandId: draft.commandId,
        toolCallId: draft.toolCallId,
        spanId: draft.spanId,
        command: draft.command,
        title: draft.title,
        cwd: draft.cwd,
        shell: draft.shell || undefined,
        actorId: "zyra-web-terminal",
        permissionPermitId: draft.permitId || undefined,
      })
      if (snapshot.terminal?.permission.effect === "ask") {
        setError(
          `Approval required: ${snapshot.terminal.permission.requestId ?? "pending request"}. `
          + "Approve it in Permissions, paste the resulting permit below, then retry.",
        )
      } else if (snapshot.terminal?.permission.effect === "deny") {
        setError(snapshot.terminal.permission.reason)
      } else {
        setDraft(defaultDraft(task))
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setBusy(false)
    }
  }

  const select = async (terminalId: string) => {
    tabs.select(terminalId)
    setError(undefined)
    try {
      const snapshot = terminal.snapshot(terminalId)
      if (snapshot.status?.phase === "running" && !snapshot.connected) {
        await terminal.connect(terminalId)
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  const refresh = async () => {
    if (!activeId || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const snapshot = await terminal.refresh(task.taskId, activeId)
      if (snapshot.status?.phase === "running" && !snapshot.connected) {
        await terminal.connect(activeId)
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setBusy(false)
    }
  }

  const send = () => {
    if (!activeId || !input || !active?.connected) return
    try {
      terminal.input(activeId, `${input}\r`, {
        permissionPermitId: draft.permitId || undefined,
      })
      setInput("")
      setError(undefined)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  const rawKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!activeId || !active?.connected) return
    if (!(event.ctrlKey || event.altKey || event.metaKey) && ![
      "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
      "Home", "End", "PageUp", "PageDown", "Escape", "Tab",
    ].includes(event.key)) return
    try {
      const sequence = terminal.key(
        activeId,
        {
          key: event.key,
          code: event.code,
          ctrlKey: event.ctrlKey,
          altKey: event.altKey,
          metaKey: event.metaKey,
          shiftKey: event.shiftKey,
        },
        { permissionPermitId: draft.permitId || undefined },
      )
      if (sequence !== undefined) event.preventDefault()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  const kill = async () => {
    const projection = active?.terminal
    if (!projection || busy) return
    setBusy(true)
    setError(undefined)
    try {
      await terminal.kill({
        taskId: projection.binding.taskId,
        runId: projection.binding.runId,
        terminalId: projection.binding.terminalId,
        sessionId: projection.binding.sessionId,
        workerId: projection.binding.workerId,
        toolCallId: projection.binding.toolCallId,
        spanId: projection.binding.spanId,
        actorId: "zyra-web-terminal",
        reason: "Killed from the task terminal workbench.",
        permissionPermitId: draft.permitId || undefined,
      })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setBusy(false)
    }
  }

  const close = (terminalId: string) => {
    terminal.closeTab(terminalId)
    setError(undefined)
  }

  const copyTranscript = async () => {
    if (!active) return
    const lines = [...active.screen.scrollback, ...active.screen.lines]
    const last = lines.at(-1)
    const range: TerminalSelectionRange = {
      anchor: { row: 0, column: 0 },
      focus: {
        row: Math.max(0, lines.length - 1),
        column: last?.cells.filter((cell) => cell.width !== 0).length ?? 0,
      },
      rectangular: false,
    }
    const value = extractTerminalSelection(active.screen, range, {
      includeScrollback: true,
      maximumBytes: 8 * 1_024 * 1_024,
    })
    try {
      await navigator.clipboard.writeText(value)
      runtime.notifications.push({
        id: token("terminal_copy"),
        title: "Terminal transcript copied",
        message: `${new TextEncoder().encode(value).byteLength} bytes copied from the viewer.`,
        tone: "success",
        taskId: task.taskId,
      })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  return (
    <section
      className="terminal-workbench"
      aria-labelledby="terminal-workbench-heading"
      data-terminal-state-owner="zyra_workers.terminal.TerminalSessionRegistry"
    >
      <header className="terminal-workbench-header">
        <div>
          <h3 id="terminal-workbench-heading">Terminal PTY</h3>
          <p>
            Real workspace PTY with cursor replay. Closing a tab detaches the viewer;
            Kill is the only process-ending control.
          </p>
        </div>
        <div className="terminal-workbench-actions">
          <button className="button button-secondary" disabled={!activeId || busy} onClick={() => void refresh()} type="button">
            Refresh
          </button>
          <button className="button button-danger" disabled={!active || active.status?.phase !== "running" || busy} onClick={() => void kill()} type="button">
            Kill
          </button>
        </div>
      </header>

      <div className="terminal-create-grid">
        <label>
          <span>Command</span>
          <input value={draft.command} onChange={(event) => setDraft({ ...draft, command: event.target.value })} />
        </label>
        <label>
          <span>Cwd</span>
          <input value={draft.cwd} onChange={(event) => setDraft({ ...draft, cwd: event.target.value })} />
        </label>
        <label>
          <span>Shell override</span>
          <input placeholder="platform default" value={draft.shell} onChange={(event) => setDraft({ ...draft, shell: event.target.value })} />
        </label>
        <label>
          <span>Permission permit</span>
          <input placeholder="optional approval permit" value={draft.permitId} onChange={(event) => setDraft({ ...draft, permitId: event.target.value })} />
        </label>
        <button className="button button-primary" disabled={busy || !draft.command.trim()} onClick={() => void create()} type="button">
          {busy ? "Working…" : "Create PTY"}
        </button>
      </div>

      <div className="terminal-tabs" role="tablist" aria-label="Task terminals">
        {tabState.tabs.map((tab) => (
          <div className={`terminal-tab ${tab.terminalId === activeId ? "is-active" : ""}`} key={tab.terminalId}>
            <button
              aria-selected={tab.terminalId === activeId}
              className="terminal-tab-select"
              onClick={() => void select(tab.terminalId)}
              role="tab"
              type="button"
            >
              <span className={`terminal-status-dot ${statusTone(tab.phase)}`} />
              <span>{tab.title}</span>
              <small>{tab.phase}</small>
            </button>
            <button
              aria-label={`Close ${tab.title} viewer`}
              className="terminal-tab-close"
              onClick={() => close(tab.terminalId)}
              title="Close viewer only"
              type="button"
            >
              ×
            </button>
          </div>
        ))}
        {!tabState.tabs.length && !loading ? (
          <span className="terminal-tabs-empty">No PTY sessions for this task.</span>
        ) : null}
      </div>

      {error ? <div className="terminal-error" role="alert">{error}</div> : null}

      {active ? (
        <div className="terminal-stage">
          <div className="terminal-statusbar">
            <span className={`terminal-status-dot ${statusTone(active.status?.phase)}`} />
            <strong>{active.status?.phase ?? "opening"}</strong>
            <span>{active.status?.cwd ?? "."}</span>
            <span>{active.status?.rows ?? active.screen.rows}×{active.status?.cols ?? active.screen.cols}</span>
            <span>cursor {active.replay.acknowledgedCursor}/{active.replay.cursor}</span>
            <span>{active.connected ? "socket connected" : active.reconnect.phase}</span>
            {active.status?.pid ? <span>pid {active.status.pid}</span> : null}
            {active.status?.exitCode !== undefined ? <span>exit {active.status.exitCode}</span> : null}
          </div>
          <div className="terminal-view-toolbar">
            <label>
              <span className="sr-only">Search terminal output</span>
              <input
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search terminal output"
                value={search}
              />
            </label>
            <span>
              {search
                ? `${searchResult.matches.length}${searchResult.truncated ? "+" : ""} matches`
                : `${active.transcript.entries.length} transcript entries`}
            </span>
            <button className="button button-secondary" onClick={() => void copyTranscript()} type="button">
              Copy transcript
            </button>
          </div>
          <div
            aria-label="Interactive terminal screen"
            className="terminal-screen"
            onKeyDown={rawKey}
            ref={screenRef}
            role="application"
            tabIndex={0}
          >
            <Screen container={screenRef} snapshot={active} />
          </div>
          <div className="terminal-input-row">
            <input
              aria-label="Terminal input"
              disabled={!active.connected || active.status?.phase !== "running"}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  send()
                }
              }}
              placeholder={active.connected ? "Send input and press Enter" : "Reconnect to send input"}
              value={input}
            />
            <button className="button button-primary" disabled={!input || !active.connected} onClick={send} type="button">
              Send
            </button>
          </div>
          <details className="terminal-structured">
            <summary>Structured output ({active.structured.length})</summary>
            <ol>
              {active.structured.slice(-100).map((line, index) => (
                <li data-terminal-line-kind={line.kind} key={`${index}:${line.kind}:${line.text}`}>
                  <span>{line.kind}</span>
                  <code>{line.text}</code>
                </li>
              ))}
            </ol>
          </details>
        </div>
      ) : (
        <div className="terminal-empty">
          {loading ? "Loading terminal projections…" : "Create or select a terminal to open its viewer."}
        </div>
      )}
    </section>
  )
}
