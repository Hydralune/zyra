import { createZyraApi, type ZyraClientOptions } from "../api/index.ts"
import { defaultCommandCatalog } from "../command/catalog.ts"
import { CommandCoordinator } from "../command/coordinator.ts"
import { browserCommandHistory, CommandHistory } from "../command/history.ts"
import { browserCommandDraftStore, CommandDraftStore } from "../command/draft-store.ts"
import { CommandQueue } from "../command/queue.ts"
import { createBrowserFocusManager, FocusManager } from "../shell/focus-manager.ts"
import { createBrowserLayoutRuntime, LayoutRuntime } from "../shell/layout-runtime.ts"
import { NotificationCenter } from "../shell/notification-center.ts"
import { OverlayRuntime } from "../shell/overlay-runtime.ts"
import { createBrowserRouter, WorkbenchRouter } from "../shell/router.ts"
import { WorkbenchController } from "../shell/workbench-controller.ts"
import { WorkbenchRouteLoader } from "../shell/route-loader.ts"
import { AccessibilityAnnouncer } from "../shell/accessibility-announcer.ts"
import {
  createCanonicalProjectionStore,
  type CanonicalProjectionStore,
} from "../state/index.ts"
import type { ProjectionIngressBinding } from "../state/contracts.ts"

export interface WorkbenchRuntime {
  api: ReturnType<typeof createZyraApi>
  projections: CanonicalProjectionStore
  router: WorkbenchRouter
  focus: FocusManager
  layout: LayoutRuntime
  overlays: OverlayRuntime
  notifications: NotificationCenter
  announcer: AccessibilityAnnouncer
  workbench: WorkbenchController
  routeLoader: WorkbenchRouteLoader
  catalog: ReturnType<typeof defaultCommandCatalog>
  history: CommandHistory
  drafts: CommandDraftStore
  queue: CommandQueue
  commands: CommandCoordinator
  close(reason?: unknown): void
}

function configuredClientOptions(): ZyraClientOptions {
  if (typeof document === "undefined") return {}
  const root = document.documentElement
  const baseUrl = root.dataset.apiBaseUrl?.trim()
  const token = root.dataset.apiToken?.trim()
  return {
    ...(baseUrl ? { baseUrl } : {}),
    ...(token ? { token } : {}),
    clientName: "zyra-workbench",
    clientVersion: "0.2.0",
  }
}

export function createWorkbenchRuntime(
  options: {
    client?: ZyraClientOptions
    router?: WorkbenchRouter
    focus?: FocusManager
    history?: CommandHistory
  } = {},
): WorkbenchRuntime {
  const api = createZyraApi(options.client ?? configuredClientOptions())
  const projections = createCanonicalProjectionStore({
    id: "workbench",
    autoPersist: true,
    restore: true,
  })
  const router = options.router ?? createBrowserRouter()
  const focus = options.focus ?? createBrowserFocusManager()
  const layout = createBrowserLayoutRuntime()
  const overlays = new OverlayRuntime()
  const notifications = new NotificationCenter()
  const announcer = new AccessibilityAnnouncer()
  const workbench = new WorkbenchController(api.tasks)
  const routeLoader = new WorkbenchRouteLoader(workbench)
  const catalog = defaultCommandCatalog()
  const history = options.history ?? browserCommandHistory()
  const drafts = browserCommandDraftStore()
  const queue = new CommandQueue()
  const commands = new CommandCoordinator({
    catalog,
    queue,
    history,
    lifecycle: api.lifecycle,
    workbench,
    router,
    overlays,
  })
  const unsubscribeLifecycle = api.lifecycle.listen((record) => {
    if (record.phase === "committed") {
      notifications.push({
        id: `lifecycle-${record.operationId}`,
        title: `${record.action[0]?.toUpperCase()}${record.action.slice(1)} committed`,
        message: record.taskId
          ? `Backend receipt confirmed for ${record.taskId}.`
          : "Backend receipt confirmed.",
        tone: "success",
        taskId: record.taskId,
      })
      announcer.announce(`${record.action} committed for ${record.taskId ?? "task"}.`)
    } else if (record.phase === "failed" || record.phase === "cancelled") {
      notifications.push({
        id: `lifecycle-${record.operationId}`,
        title: `${record.action[0]?.toUpperCase()}${record.action.slice(1)} ${record.phase}`,
        message: record.error?.message ?? "The lifecycle operation did not commit.",
        tone: record.phase === "failed" ? "error" : "warning",
        durationMs: record.phase === "failed" ? 0 : 8_000,
        taskId: record.taskId,
      })
      announcer.announce(
        `${record.action} ${record.phase}: ${record.error?.message ?? "operation did not commit"}.`,
        record.phase === "failed" ? "assertive" : "polite",
      )
    }
  })
  let projectionBinding: ProjectionIngressBinding | undefined
  let projectionTaskId: string | undefined
  let projectionRouteGeneration = 0
  const bindProjectionRoute = (taskId?: string) => {
    const generation = ++projectionRouteGeneration
    projectionBinding?.close()
    projectionBinding = undefined
    if (projectionTaskId) projections.unpinTask(projectionTaskId)
    projectionTaskId = undefined
    if (!taskId) return
    void projections.ready.then(() => {
      if (generation !== projectionRouteGeneration || closed) return
      projectionBinding = projections.bind(api.events, taskId)
      projections.pinTask(taskId)
      projectionTaskId = taskId
    })
  }
  const unsubscribeProjectionRoute = router.listen((route) => {
    bindProjectionRoute(route.kind === "task" ? route.taskId : undefined)
  })
  let closed = false
  const initialRoute = router.current
  bindProjectionRoute(
    initialRoute.kind === "task" ? initialRoute.taskId : undefined,
  )
  return {
    api,
    projections,
    router,
    focus,
    layout,
    overlays,
    notifications,
    announcer,
    workbench,
    routeLoader,
    catalog,
    history,
    drafts,
    queue,
    commands,
    close(reason?: unknown) {
      if (closed) return
      closed = true
      projectionRouteGeneration += 1
      unsubscribeProjectionRoute()
      projectionBinding?.close()
      projectionBinding = undefined
      if (projectionTaskId) projections.unpinTask(projectionTaskId)
      projectionTaskId = undefined
      void projections.close(String(reason ?? "Workbench closed."))
      unsubscribeLifecycle()
      commands.close(String(reason ?? "Workbench closed."))
      queue.close(String(reason ?? "Workbench closed."))
      drafts.close()
      routeLoader.close(String(reason ?? "Workbench closed."))
      workbench.close(String(reason ?? "Workbench closed."))
      notifications.close()
      announcer.close()
      overlays.closeRuntime()
      layout.close()
      router.close()
      focus.clear()
      api.close(reason)
    },
  }
}
