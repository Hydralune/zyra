import { useEffect, useMemo } from "react"
import type { WorkbenchRuntime } from "./runtime.ts"
import {
  useAnnouncementSnapshot,
  useLayoutSnapshot,
  useOnlineStatus,
  useRoute,
  useWorkbenchSnapshot,
} from "./hooks.ts"
import { CommandInput } from "../components/command-input/command-input.tsx"
import { OverlayHost } from "../components/overlays/overlay-host.tsx"
import { TaskDetail } from "../components/tasks/task-detail.tsx"
import { TaskList } from "../components/tasks/task-list.tsx"
import { EmptyState, ReconnectingState } from "../components/status/request-state.tsx"
import { NotificationTray } from "../components/status/notification-tray.tsx"
import { PaneDivider } from "../components/layout/pane-divider.tsx"
import { ScenarioWorkbench } from "../features/scenarios/index.ts"
import { ExperimentWorkbench } from "../features/experiments/index.ts"

function AppNavigation({
  runtime,
  routeKind,
}: {
  runtime: WorkbenchRuntime
  routeKind: string
}) {
  return (
    <nav className="app-nav" aria-label="Workbench">
      <button className="brand" type="button" onClick={() => runtime.router.openTasks()}>
        <span className="brand-mark" aria-hidden="true">Z</span>
        <span>
          <strong>Zyra</strong>
          <small>Workbench</small>
        </span>
      </button>
      <div className="nav-items">
        <button
          type="button"
          aria-current={routeKind === "tasks" || routeKind === "task" ? "page" : undefined}
          onClick={() => runtime.router.openTasks()}
        >
          <span aria-hidden="true">◫</span>
          <span>Tasks</span>
        </button>
        <button
          type="button"
          aria-current={routeKind === "new-task" ? "page" : undefined}
          onClick={() => runtime.router.openNewTask()}
        >
          <span aria-hidden="true">＋</span>
          <span>New</span>
        </button>
        <button
          type="button"
          aria-current={routeKind === "settings" ? "page" : undefined}
          onClick={() => runtime.router.openSettings()}
        >
          <span aria-hidden="true">◎</span>
          <span>Scenarios</span>
        </button>
      </div>
      <div className="nav-help">
        <button type="button" onClick={() => void runtime.commands.submit("/help", { origin: "button" })}>
          <span aria-hidden="true">?</span>
          <span>Commands</span>
        </button>
        <button type="button" onClick={() => void runtime.commands.submit("/keys", { origin: "button" })}>
          <span aria-hidden="true">⌘</span>
          <span>Keys</span>
        </button>
      </div>
    </nav>
  )
}

function TopBar({ runtime, online }: { runtime: WorkbenchRuntime; online: boolean }) {
  const state = useWorkbenchSnapshot(runtime)
  const phase = online ? state.runtime.phase : "reconnecting"
  return (
    <header className="top-bar">
      <div className="breadcrumb">
        <span>Console</span>
        {state.selectedTaskId ? <><span>/</span><strong>{state.selectedTaskId}</strong></> : null}
      </div>
      <button
        className="runtime-pill"
        type="button"
        data-phase={phase}
        onClick={() => void runtime.commands.submit("/status", { origin: "button" })}
      >
        <span className="connection-dot" aria-hidden="true" />
        <span>
          {!online
            ? "Offline"
            : state.runtime.readiness?.ready
              ? "Runtime ready"
              : state.runtime.phase === "loading"
                ? "Checking runtime"
                : "Runtime unavailable"}
        </span>
      </button>
    </header>
  )
}

function SettingsView({ runtime }: { runtime: WorkbenchRuntime }) {
  const snapshot = runtime.api.client.snapshot()
  return (
    <section className="settings-view" aria-labelledby="settings-heading">
      <header className="panel-heading">
        <div>
          <p className="eyebrow">Local workbench</p>
          <h1 id="settings-heading">Settings</h1>
        </div>
      </header>
      <div className="settings-grid">
        <article>
          <h2>Typed API transport</h2>
          <p>All workbench requests use the single registered Zyra typed client.</p>
          <dl className="fact-grid">
            <div><dt>Base URL</dt><dd>{runtime.api.client.baseUrl}</dd></div>
            <div><dt>Transport</dt><dd>{snapshot.registry.enabled ? "enabled" : "disabled"}</dd></div>
            <div><dt>Normalizers</dt><dd>{snapshot.registry.normalizersEnabled ? "enabled" : "disabled"}</dd></div>
            <div><dt>In flight</dt><dd>{snapshot.inFlight.length}</dd></div>
          </dl>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void runtime.commands.submit("/status", { origin: "button" })}
          >
            Inspect live status
          </button>
        </article>
        <article>
          <h2>Command input</h2>
          <p>Drafts, history, suggestions, and queued previews remain local to this browser.</p>
          <dl className="fact-grid">
            <div><dt>History</dt><dd>{runtime.history.list().length}</dd></div>
            <div><dt>Queued</dt><dd>{runtime.queue.getSnapshot().pendingCount}</dd></div>
            <div><dt>Commands</dt><dd>{runtime.catalog.list().length}</dd></div>
            <div><dt>Policy</dt><dd>fail closed</dd></div>
          </dl>
          <button className="button button-secondary" type="button" onClick={() => runtime.history.clear()}>
            Clear local history
          </button>
        </article>
      </div>
      <ScenarioWorkbench runtime={runtime.scenarioConsole} />
      <ExperimentWorkbench runtime={runtime.experimentConsole} />
    </section>
  )
}

function MainRoute({
  runtime,
  route,
}: {
  runtime: WorkbenchRuntime
  route: ReturnType<typeof useRoute>
}) {
  const state = useWorkbenchSnapshot(runtime)
  const layout = useLayoutSnapshot(runtime)
  if (route.kind === "settings") return <SettingsView runtime={runtime} />
  if (route.kind === "not-found") {
    return (
      <section className="route-state">
        <EmptyState
          title="Page not found"
          detail={`No Zyra workbench route matches ${route.attemptedPath}.`}
          action={
            <button className="button button-primary" type="button" onClick={() => runtime.router.recover()}>
              Return to tasks
            </button>
          }
        />
      </section>
    )
  }
  if (route.kind === "new-task") {
    return (
      <section className="route-state new-task-state">
        <EmptyState
          title="Start with a goal"
          detail="Use the command input below. A plain prompt creates a task through the typed API and starts it immediately."
          action={
            <button className="button button-primary" type="button" onClick={() => runtime.focus.focusId("workbench-command-input")}>
              Focus command input
            </button>
          }
        />
      </section>
    )
  }
  return (
    <div
      className="workbench-columns"
      style={{
        gridTemplateColumns: `${layout.taskListWidth}px 7px minmax(0, 1fr)`,
      }}
    >
      <TaskList
        runtime={runtime}
        state={state.list}
        selectedTaskId={state.selectedTaskId}
      />
      <PaneDivider runtime={runtime} />
      <TaskDetail runtime={runtime} state={state.detail} />
    </div>
  )
}

export function WorkbenchApp({ runtime }: { runtime: WorkbenchRuntime }) {
  const route = useRoute(runtime)
  const state = useWorkbenchSnapshot(runtime)
  const layout = useLayoutSnapshot(runtime)
  const announcements = useAnnouncementSnapshot(runtime)
  const online = useOnlineStatus()
  const routeTaskId = route.kind === "task" ? route.taskId : undefined
  const bootstrapKey = useMemo(
    () => `${route.kind}:${routeTaskId ?? ""}:${route.query.status ?? ""}:${route.query.cursor ?? ""}`,
    [route.kind, route.query.cursor, route.query.status, routeTaskId],
  )

  useEffect(() => {
    runtime.layout.start()
    runtime.layout.routeChanged(route.kind)
  }, [route.kind, runtime])

  useEffect(() => {
    void runtime.routeLoader.load(route)
  }, [bootstrapKey])

  useEffect(() => {
    if (state.runtime.phase === "ready") {
      runtime.announcer.announce("Zyra runtime ready.")
    } else if (state.runtime.phase === "reconnecting") {
      runtime.announcer.announce("Connection lost. Zyra is reconnecting.")
    } else if (state.runtime.phase === "error") {
      runtime.announcer.announce(
        state.runtime.failure?.message ?? "Zyra runtime is unavailable.",
        "assertive",
      )
    }
  }, [runtime, state.runtime.generation, state.runtime.phase])

  useEffect(() => {
    if (route.kind === "new-task") runtime.focus.focusId("workbench-command-input")
    else runtime.focus.routeFocus(route.query.focus)
  }, [route.kind, route.query.focus, runtime])

  useEffect(() => {
    const handleGlobalKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        runtime.focus.focusId("workbench-command-input")
        return
      }
      if (event.key === "Escape") {
        if (runtime.overlays.handleEscape()) {
          event.preventDefault()
          return
        }
        if (runtime.commands.getSnapshot().busy && runtime.commands.cancelActive()) {
          event.preventDefault()
        }
      }
    }
    window.addEventListener("keydown", handleGlobalKey)
    return () => window.removeEventListener("keydown", handleGlobalKey)
  }, [runtime])

  useEffect(() => () => runtime.close("React workbench unmounted."), [runtime])

  return (
    <div
      className="app-shell"
      data-layout-mode={layout.mode}
      data-active-pane={layout.activePane}
    >
      <a className="skip-link" href="#workbench-main">Skip to workbench</a>
      <AppNavigation runtime={runtime} routeKind={route.kind} />
      <div className="app-stage">
        <TopBar runtime={runtime} online={online} />
        {!online || state.runtime.phase === "reconnecting" ? (
          <ReconnectingState
            failure={state.runtime.failure}
            onRetry={() => void runtime.workbench.refreshRuntime({ reconnect: true })}
          />
        ) : null}
        <main id="workbench-main" tabIndex={-1}>
          <MainRoute runtime={runtime} route={route} />
        </main>
        <CommandInput runtime={runtime} />
      </div>
      <OverlayHost runtime={runtime} />
      <NotificationTray runtime={runtime} />
      <div className="sr-only" role="status" aria-live="polite">{announcements.polite}</div>
      <div className="sr-only" role="alert" aria-live="assertive">{announcements.assertive}</div>
    </div>
  )
}
