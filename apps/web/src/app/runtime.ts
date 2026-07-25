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
import { CommandSurfaceRuntime } from "../features/commands/index.ts"
import { PermissionConsoleRuntime } from "../features/permissions/index.ts"
import { SessionConsoleRuntime } from "../features/session/index.ts"
import { McpConsoleController } from "../features/mcp/index.ts"
import { SkillWorkbenchController } from "../features/skills/index.ts"
import { SubagentPanelController } from "../features/subagents/index.ts"
import { ScenarioWorkbenchRuntime } from "../features/scenarios/index.ts"

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
  controlCommands: CommandSurfaceRuntime
  permissionConsole: PermissionConsoleRuntime
  sessionConsole: SessionConsoleRuntime
  mcpConsole: McpConsoleController
  skillConsole: SkillWorkbenchController
  subagentConsole: SubagentPanelController
  scenarioConsole: ScenarioWorkbenchRuntime
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
    controls: undefined,
  })
  const permissionConsole = new PermissionConsoleRuntime({
    api: api.permissions,
    taskApi: api.tasks,
  })
  const selectedTaskIsSealed = () => {
    const task = workbench.selectedTask()
    const metadata = task?.metadata ?? {}
    const mode = String(
      metadata.competition_mode
        ?? metadata.permission_mode
        ?? metadata.mode
        ?? "",
    ).trim().toLowerCase()
    return (
      permissionConsole.getSnapshot().productMode === "sealed"
      || metadata.sealed === true
      || metadata.sealed_autonomous === true
      || mode === "sealed"
      || mode === "sealed_autonomous"
    )
  }
  const recordSealedMutation = async (input: {
    value: string
    reason: string
  }) => {
    await permissionConsole.recordSealedAction({
      action: "steer",
      actorId: "zyra-web-command-surface",
      reason: `${input.reason} Attempted command: ${input.value.split(/\s+/, 3).join(" ")}.`,
    })
  }
  const controlCommands = new CommandSurfaceRuntime({
    api: api.tasks,
    workbench,
    overlays,
    projections,
    sealed: selectedTaskIsSealed,
    recordSealedMutation,
  })
  const sessionConsole = new SessionConsoleRuntime({
    projections,
    commands: controlCommands,
  })
  const mcpConsole = new McpConsoleController({
    projections,
    commands: controlCommands,
    sealed: selectedTaskIsSealed,
    sealedAttempts: {
      record: async (input) => {
        await permissionConsole.recordSealedAction({
          action: "steer",
          actorId: "zyra-web-mcp-panel",
          requestId: input.requestId,
          reason: `${input.reason} MCP ${input.action} on ${input.serverId}.`,
        })
      },
    },
  })
  const skillConsole = new SkillWorkbenchController({
    projections,
    commands: controlCommands,
    permissions: permissionConsole,
  })
  const subagentConsole = new SubagentPanelController({
    projections,
    commands: controlCommands,
    sealed: selectedTaskIsSealed(),
    interventions: {
      record: async (input) => {
        const receipt = await permissionConsole.recordSealedAction({
          action: "steer",
          actorId: input.actorId,
          requestId: input.nonce,
          reason:
            `${input.reason} Subagent ${input.action} on ${input.binding.childId}.`,
        })
        return {
          id: receipt.interventionId,
          interventionCounted: receipt.counted,
          humanInterventionCount: 0,
          operatorInterventionAttemptCount:
            permissionConsole.getSnapshot().interventions.length,
          eventIds: [],
        }
      },
    },
  })
  const scenarioConsole = new ScenarioWorkbenchRuntime({
    api: api.scenarios,
  })
  const synchronizePanelSealedMode = () => {
    const sealed = selectedTaskIsSealed()
    mcpConsole.setSealed(sealed)
    skillConsole.setSealed(sealed)
    subagentConsole.setSealed(sealed)
  }
  const unsubscribePanelPermissions = permissionConsole.subscribe(
    synchronizePanelSealedMode,
  )
  const unsubscribePanelTaskMode = workbench.subscribe(
    synchronizePanelSealedMode,
  )
  commands.attachControls(controlCommands)
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
    void controlCommands.bindTask(taskId).catch((error) => {
      notifications.push({
        id: `command-queue-restore-${generation}`,
        title: "Command queue restore failed",
        message:
          error instanceof Error
            ? error.message
            : String(error || "The backend command queue could not be restored."),
        tone: "error",
        durationMs: 10_000,
        taskId,
      })
    })
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
    controlCommands,
    permissionConsole,
    sessionConsole,
    mcpConsole,
    skillConsole,
    subagentConsole,
    scenarioConsole,
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
      unsubscribePanelPermissions()
      unsubscribePanelTaskMode()
      commands.close(String(reason ?? "Workbench closed."))
      controlCommands.close(String(reason ?? "Workbench closed."))
      permissionConsole.close(String(reason ?? "Workbench closed."))
      sessionConsole.close(String(reason ?? "Workbench closed."))
      mcpConsole.close(String(reason ?? "Workbench closed."))
      skillConsole.close(String(reason ?? "Workbench closed."))
      subagentConsole.close(String(reason ?? "Workbench closed."))
      scenarioConsole.close(String(reason ?? "Workbench closed."))
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
