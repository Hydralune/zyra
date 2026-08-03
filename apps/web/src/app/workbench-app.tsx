import { useEffect, useMemo, useState } from "react"
import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"
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
import { ProductTaskDetail } from "../components/tasks/product-task-detail.tsx"
import { EmptyState, ReconnectingState } from "../components/status/request-state.tsx"
import { NotificationTray } from "../components/status/notification-tray.tsx"
import { ScenarioWorkbench } from "../features/scenarios/index.ts"
import { ExperimentWorkbench } from "../features/experiments/index.ts"

const STARTER_PROMPTS = [
  {
    icon: "⌕",
    tone: "blue",
    title: "理解一个项目",
    detail: "梳理架构、关键模块和主要风险",
    prompt: "分析当前项目的架构、核心模块和三个最高优先级风险，并给出可执行建议。",
  },
  {
    icon: "↗",
    tone: "violet",
    title: "构建或修改功能",
    detail: "从目标到实现、验证和交付",
    prompt: "检查当前项目，选择一个最值得改进的用户体验问题，完成实现并验证结果。",
  },
  {
    icon: "✓",
    tone: "green",
    title: "审查代码",
    detail: "定位缺陷、安全风险和回归点",
    prompt: "审查当前代码改动，找出正确性、安全性和可维护性问题，并按严重程度排序。",
  },
  {
    icon: "↻",
    tone: "orange",
    title: "修复复杂问题",
    detail: "诊断、恢复并留下可追溯证据",
    prompt: "诊断当前项目中最影响运行稳定性的问题，修复根因并运行相关验证。",
  },
] as const

function taskTime(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return ""
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" })
  const minutes = Math.round((timestamp - Date.now()) / 60_000)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute")
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour")
  return formatter.format(Math.round(hours / 24), "day")
}

function statusTone(task: TaskProjection): string {
  if (task.active) return "active"
  if (["completed", "succeeded", "verified"].includes(task.status.toLowerCase())) return "success"
  if (["failed", "error", "cancelled", "canceled"].includes(task.status.toLowerCase())) return "danger"
  return "idle"
}

export function seedProductPrompt(runtime: WorkbenchRuntime, prompt: string): void {
  runtime.router.openNewTask()
  runtime.drafts.set("new", prompt, prompt.length)
  globalThis.setTimeout(() => {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("zyra:restore-command-draft", {
        detail: { value: prompt, cursor: prompt.length },
      }))
    }
    runtime.focus.focusId("workbench-command-input")
  }, 0)
}

function AppNavigation({
  runtime,
  routeKind,
  tasks,
  selectedTaskId,
  onNavigate,
}: {
  runtime: WorkbenchRuntime
  routeKind: string
  tasks: readonly TaskProjection[]
  selectedTaskId?: string
  onNavigate: () => void
}) {
  const recent = tasks.slice(0, 12)
  const navigate = (action: () => void) => {
    action()
    onNavigate()
  }
  return (
    <nav className="app-nav product-sidebar" aria-label="Zyra">
      <div className="product-brand-row">
        <button className="brand" type="button" onClick={() => navigate(() => runtime.router.openTasks())}>
          <span className="brand-mark" aria-hidden="true">Z</span>
          <span>
            <strong>Zyra</strong>
            <small>Long-horizon agent</small>
          </span>
        </button>
        <button className="product-sidebar-close" type="button" aria-label="关闭侧边栏" onClick={onNavigate}>×</button>
      </div>

      <button className="product-new-task" type="button" onClick={() => navigate(() => runtime.router.openNewTask())}>
        <span aria-hidden="true">＋</span>
        <span>新任务</span>
        <kbd>Ctrl K</kbd>
      </button>

      <div className="product-primary-nav">
        <button
          type="button"
          aria-current={routeKind === "tasks" || routeKind === "new-task" ? "page" : undefined}
          onClick={() => navigate(() => runtime.router.openTasks())}
        >
          <span aria-hidden="true">⌂</span><span>首页</span>
        </button>
        <button
          type="button"
          aria-current={routeKind === "settings" ? "page" : undefined}
          onClick={() => navigate(() => runtime.router.openSettings())}
        >
          <span aria-hidden="true">⌘</span><span>高级 Workbench</span>
        </button>
        <button
          type="button"
          disabled={!selectedTaskId}
          title={selectedTaskId ? "打开当前任务的交付物" : "完成任务后可查看交付物"}
          onClick={() => selectedTaskId && navigate(() => runtime.router.openTask(selectedTaskId))}
        >
          <span aria-hidden="true">◇</span><span>交付物</span>
        </button>
      </div>

      <section className="product-recents" aria-labelledby="product-recents-heading">
        <div className="product-nav-section-heading">
          <span id="product-recents-heading">最近任务</span>
          <button type="button" aria-label="刷新最近任务" onClick={() => void runtime.workbench.refreshTasks({ preserveOnError: true })}>↻</button>
        </div>
        <div className="product-recent-list">
          {recent.length ? recent.map((task) => (
            <button
              type="button"
              key={task.taskId}
              className="product-recent-task"
              aria-current={selectedTaskId === task.taskId ? "page" : undefined}
              onClick={() => navigate(() => runtime.router.openTask(task.taskId))}
            >
              <span className="product-recent-status" data-tone={statusTone(task)} aria-hidden="true" />
              <span>
                <strong>{task.userGoal || "未命名任务"}</strong>
                <small>{taskTime(task.updatedAt)}</small>
              </span>
            </button>
          )) : (
            <p className="product-sidebar-empty">创建任务后会显示在这里。</p>
          )}
        </div>
      </section>

      <div className="product-sidebar-footer">
        <button type="button" onClick={() => void runtime.commands.submit("/help", { origin: "button" })}>
          <span className="product-user-avatar" aria-hidden="true">Z</span>
          <span><strong>Zyra 本地工作区</strong><small>配置与帮助</small></span>
          <span aria-hidden="true">⋯</span>
        </button>
      </div>
    </nav>
  )
}

function TopBar({
  runtime,
  online,
  routeKind,
  task,
  onMenu,
}: {
  runtime: WorkbenchRuntime
  online: boolean
  routeKind: string
  task?: TaskProjection
  onMenu: () => void
}) {
  const state = useWorkbenchSnapshot(runtime)
  const phase = online ? state.runtime.phase : "reconnecting"
  const title = task?.userGoal
    || (routeKind === "settings" ? "高级 Workbench" : "Zyra")
  return (
    <header className="top-bar product-topbar">
      <div className="product-topbar-title">
        <button className="product-menu-button" type="button" aria-label="打开侧边栏" onClick={onMenu}>☰</button>
        <div>
          <strong>{title}</strong>
          {task ? <small>{task.status} · {task.taskId}</small> : <small>动态异构多智能体工作空间</small>}
        </div>
      </div>
      <div className="product-topbar-actions">
        {routeKind === "task" ? (
          <button className="product-button product-button-quiet" type="button" onClick={() => runtime.router.openNewTask()}>
            ＋ 新任务
          </button>
        ) : null}
        <button
          className="runtime-pill"
          type="button"
          data-phase={phase}
          onClick={() => void runtime.commands.submit("/status", { origin: "button" })}
        >
          <span className="connection-dot" aria-hidden="true" />
          <span>
            {!online
              ? "离线"
              : state.runtime.readiness?.ready
                ? "运行时就绪"
                : state.runtime.phase === "loading"
                  ? "正在检查"
                  : "运行时不可用"}
          </span>
        </button>
      </div>
    </header>
  )
}

function SettingsView({ runtime }: { runtime: WorkbenchRuntime }) {
  const snapshot = runtime.api.client.snapshot()
  return (
    <section className="settings-view advanced-center" aria-labelledby="settings-heading">
      <header className="advanced-center-heading">
        <div>
          <p className="eyebrow">Advanced workbench</p>
          <h1 id="settings-heading">系统与证据中心</h1>
          <p>面向开发、调试和比赛审计的高级入口。日常任务请返回首页。</p>
        </div>
        <button className="product-button product-button-primary" type="button" onClick={() => runtime.router.openTasks()}>
          返回产品首页
        </button>
      </header>
      <div className="settings-grid">
        <article>
          <h2>API 与运行时</h2>
          <p>检查当前 Web 与 Zyra typed API 的真实连接状态。</p>
          <dl className="fact-grid">
            <div><dt>Base URL</dt><dd>{runtime.api.client.baseUrl}</dd></div>
            <div><dt>Transport</dt><dd>{snapshot.registry.enabled ? "enabled" : "disabled"}</dd></div>
            <div><dt>Normalizers</dt><dd>{snapshot.registry.normalizersEnabled ? "enabled" : "disabled"}</dd></div>
            <div><dt>In flight</dt><dd>{snapshot.inFlight.length}</dd></div>
          </dl>
          <button className="button button-secondary" type="button" onClick={() => void runtime.commands.submit("/status", { origin: "button" })}>
            Inspect live status
          </button>
        </article>
        <article>
          <h2>本地交互状态</h2>
          <p>命令历史、草稿和排队预览只保存在当前浏览器。</p>
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

function ProductHome({ runtime, taskCount }: { runtime: WorkbenchRuntime; taskCount: number }) {
  return (
    <section className="product-home">
      <div className="product-home-inner">
        <div className="product-orbit" aria-hidden="true">
          <span>Z</span><i /><i /><i />
        </div>
        <p className="product-kicker">Long-horizon collaboration</p>
        <h1>今天想让 Zyra 完成什么？</h1>
        <p className="product-home-copy">
          描述目标，Zyra 会规划步骤、选择合适的执行资源，并把结果与证据整理成可检查的交付物。
        </p>
        <div className="product-starter-grid">
          {STARTER_PROMPTS.map((item) => (
            <button type="button" key={item.title} data-tone={item.tone} onClick={() => seedProductPrompt(runtime, item.prompt)}>
              <span className="product-starter-icon" aria-hidden="true">{item.icon}</span>
              <span><strong>{item.title}</strong><small>{item.detail}</small></span>
              <span aria-hidden="true">↗</span>
            </button>
          ))}
        </div>
        <div className="product-capability-strip" aria-label="Zyra capabilities">
          <span>长程目标保持</span>
          <span>动态多智能体</span>
          <span>端边云调度</span>
          <span>可追溯交付</span>
          {taskCount ? <strong>{taskCount} 个历史任务</strong> : null}
        </div>
      </div>
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
  if (route.kind === "settings") return <SettingsView runtime={runtime} />
  if (route.kind === "not-found") {
    return (
      <section className="product-route-state">
        <EmptyState
          title="页面不存在"
          detail={`Zyra 没有找到 ${route.attemptedPath}。`}
          action={<button className="product-button product-button-primary" type="button" onClick={() => runtime.router.recover()}>返回首页</button>}
        />
      </section>
    )
  }
  if (route.kind === "task") {
    return <ProductTaskDetail runtime={runtime} state={state.detail} />
  }
  return <ProductHome runtime={runtime} taskCount={state.list.total} />
}

export function WorkbenchApp({ runtime }: { runtime: WorkbenchRuntime }) {
  const route = useRoute(runtime)
  const state = useWorkbenchSnapshot(runtime)
  const layout = useLayoutSnapshot(runtime)
  const announcements = useAnnouncementSnapshot(runtime)
  const online = useOnlineStatus()
  const [sidebarOpen, setSidebarOpen] = useState(false)
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
    if (state.runtime.phase === "ready") runtime.announcer.announce("Zyra runtime ready.")
    else if (state.runtime.phase === "reconnecting") runtime.announcer.announce("Connection lost. Zyra is reconnecting.")
    else if (state.runtime.phase === "error") {
      runtime.announcer.announce(state.runtime.failure?.message ?? "Zyra runtime is unavailable.", "assertive")
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
        runtime.router.openNewTask()
        runtime.focus.focusId("workbench-command-input")
        return
      }
      if (event.key === "Escape") {
        if (sidebarOpen) {
          setSidebarOpen(false)
          return
        }
        if (runtime.overlays.handleEscape()) {
          event.preventDefault()
          return
        }
        if (runtime.commands.getSnapshot().busy && runtime.commands.cancelActive()) event.preventDefault()
      }
    }
    window.addEventListener("keydown", handleGlobalKey)
    return () => window.removeEventListener("keydown", handleGlobalKey)
  }, [runtime, sidebarOpen])

  useEffect(() => () => runtime.close("React workbench unmounted."), [runtime])

  return (
    <div
      className="app-shell product-shell"
      data-layout-mode={layout.mode}
      data-active-pane={layout.activePane}
      data-sidebar-open={sidebarOpen || undefined}
    >
      <a className="skip-link" href="#workbench-main">跳到主要内容</a>
      <AppNavigation
        runtime={runtime}
        routeKind={route.kind}
        tasks={state.list.tasks}
        selectedTaskId={state.selectedTaskId}
        onNavigate={() => setSidebarOpen(false)}
      />
      {sidebarOpen ? <button className="product-sidebar-scrim" type="button" aria-label="关闭侧边栏" onClick={() => setSidebarOpen(false)} /> : null}
      <div className="app-stage product-stage">
        <TopBar
          runtime={runtime}
          online={online}
          routeKind={route.kind}
          task={route.kind === "task" ? state.detail.task : undefined}
          onMenu={() => setSidebarOpen(true)}
        />
        {!online || state.runtime.phase === "reconnecting" ? (
          <ReconnectingState
            failure={state.runtime.failure}
            onRetry={() => void runtime.workbench.refreshRuntime({ reconnect: true })}
          />
        ) : null}
        <main id="workbench-main" tabIndex={-1}>
          <MainRoute runtime={runtime} route={route} />
        </main>
        {route.kind !== "settings" ? (
          <CommandInput
            runtime={runtime}
            taskContext={route.kind === "task" ? state.detail.task : undefined}
          />
        ) : null}
      </div>
      <OverlayHost runtime={runtime} />
      <NotificationTray runtime={runtime} />
      <div className="sr-only" role="status" aria-live="polite">{announcements.polite}</div>
      <div className="sr-only" role="alert" aria-live="assertive">{announcements.assertive}</div>
    </div>
  )
}
