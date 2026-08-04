import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "./runtime.ts"
import {
  useAnnouncementSnapshot,
  useCommandSnapshot,
  useLayoutSnapshot,
  useLiveSyncSnapshot,
  useMediaQuery,
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

/** Text equivalent for the colour-only status dot in the conversation list. */
function statusText(task: TaskProjection): string {
  const tone = statusTone(task)
  if (tone === "active") return "进行中"
  if (tone === "success") return "已完成"
  if (tone === "danger") return "未完成"
  return "等待中"
}

export interface ProductConversation {
  key: string
  title: string
  latest: TaskProjection
  turnCount: number
}

export function productConversationList(
  tasks: readonly TaskProjection[],
): ProductConversation[] {
  const groups = new Map<string, TaskProjection[]>()
  for (const task of tasks) {
    const key = task.sessionId ?? `task:${task.taskId}`
    const group = groups.get(key) ?? []
    group.push(task)
    groups.set(key, group)
  }
  return [...groups.entries()].map(([key, group]) => {
    const ordered = [...group].sort((left, right) => {
      const created = Date.parse(left.createdAt) - Date.parse(right.createdAt)
      return created || left.taskId.localeCompare(right.taskId)
    })
    const latest = [...ordered].sort((left, right) => {
      const updated = Date.parse(right.updatedAt) - Date.parse(left.updatedAt)
      return updated || right.taskId.localeCompare(left.taskId)
    })[0]!
    return {
      key,
      title: ordered[0]?.userGoal || latest.userGoal || "未命名会话",
      latest,
      turnCount: ordered.length,
    }
  }).sort((left, right) => {
    const updated = Date.parse(right.latest.updatedAt) - Date.parse(left.latest.updatedAt)
    return updated || left.key.localeCompare(right.key)
  })
}

export function seedProductPrompt(runtime: WorkbenchRuntime, prompt: string): void {
  runtime.router.openNewTask()
  runtime.drafts.set("new", prompt, prompt.length)
  if (typeof window === "undefined") return
  // The composer is already mounted on the home route, so its draft scope does
  // not change; the event is what tells it to pick the seeded draft up.
  window.dispatchEvent(new CustomEvent("zyra:restore-command-draft", {
    detail: { value: prompt, cursor: prompt.length },
  }))
  runtime.focus.focusId("workbench-command-input")
}

function AppNavigation({
  runtime,
  routeKind,
  tasks,
  listPhase,
  selectedTaskId,
  drawer,
  open,
  onNavigate,
  navigationRef,
}: {
  runtime: WorkbenchRuntime
  routeKind: string
  tasks: readonly TaskProjection[]
  listPhase: string
  selectedTaskId?: string
  drawer: boolean
  open: boolean
  onNavigate: () => void
  navigationRef: React.RefObject<HTMLElement | null>
}) {
  const recent = productConversationList(tasks).slice(0, 12)
  const selectedTask = tasks.find((task) => task.taskId === selectedTaskId)
  const selectedConversationKey = selectedTask?.sessionId ?? (selectedTask ? `task:${selectedTask.taskId}` : undefined)
  const navigate = (action: () => void) => {
    action()
    onNavigate()
  }
  // Off-canvas on a narrow viewport means visually gone but still in the tab
  // order unless it is explicitly removed from the accessibility tree.
  const hidden = drawer && !open
  return (
    <nav
      ref={navigationRef}
      className="app-nav product-sidebar"
      aria-label="Zyra 会话导航"
      id="product-sidebar"
      aria-hidden={hidden || undefined}
      inert={hidden ? true : undefined}
    >
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
          <span id="product-recents-heading">最近会话</span>
          <button
            type="button"
            aria-label="刷新最近会话"
            disabled={listPhase === "loading"}
            onClick={() => void runtime.workbench.refreshTasks({ preserveOnError: true })}
          >
            ↻
          </button>
        </div>
        <div className="product-recent-list">
          {recent.map((conversation) => (
            <button
              type="button"
              key={conversation.key}
              className="product-recent-task"
              aria-current={selectedConversationKey === conversation.key ? "page" : undefined}
              onClick={() => navigate(() => runtime.router.openTask(conversation.latest.taskId))}
            >
              <span className="product-recent-status" data-tone={statusTone(conversation.latest)} aria-hidden="true" />
              <span>
                <strong>{conversation.title}</strong>
                <small>
                  <span className="sr-only">{statusText(conversation.latest)} · </span>
                  {conversation.turnCount > 1 ? `${conversation.turnCount} 轮 · ` : ""}
                  {taskTime(conversation.latest.updatedAt)}
                </small>
              </span>
            </button>
          ))}
          {!recent.length && listPhase === "loading" ? (
            <p className="product-sidebar-empty" role="status">正在载入会话…</p>
          ) : null}
          {!recent.length && listPhase === "error" ? (
            <p className="product-sidebar-empty" role="status">
              无法读取会话列表。
              <button type="button" onClick={() => void runtime.workbench.refreshTasks({ preserveOnError: true })}>重试</button>
            </p>
          ) : null}
          {!recent.length && !["loading", "error"].includes(listPhase) ? (
            <p className="product-sidebar-empty">开始会话后会显示在这里。</p>
          ) : null}
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
  drawer,
  sidebarOpen,
  onMenu,
  menuRef,
}: {
  runtime: WorkbenchRuntime
  online: boolean
  routeKind: string
  task?: TaskProjection
  drawer: boolean
  sidebarOpen: boolean
  onMenu: () => void
  menuRef: React.RefObject<HTMLButtonElement | null>
}) {
  const state = useWorkbenchSnapshot(runtime)
  const live = useLiveSyncSnapshot(runtime)
  const phase = online ? state.runtime.phase : "reconnecting"
  const title = task?.userGoal
    || (routeKind === "settings" ? "高级 Workbench" : "Zyra")
  const subtitle = task
    ? live.live && !live.paused
      ? "进行中 · 实时更新"
      : statusText(task)
    : "动态异构多智能体工作空间"
  const runtimeLabel = !online
    ? "离线"
    : state.runtime.readiness?.ready
      ? "运行时就绪"
      : state.runtime.phase === "loading"
        ? "正在检查"
        : "运行时不可用"
  return (
    <header className="top-bar product-topbar">
      <div className="product-topbar-title">
        {drawer ? (
          <button
            ref={menuRef}
            className="product-menu-button"
            type="button"
            aria-label="打开会话导航"
            aria-controls="product-sidebar"
            aria-expanded={sidebarOpen}
            onClick={onMenu}
          >
            ☰
          </button>
        ) : null}
        <div>
          {/* The goal is the page identity; the opaque task id belongs in the
              tooltip and the advanced surfaces, not in the primary line. */}
          <strong title={task ? `${title}\n${task.taskId}` : title}>{title}</strong>
          <small>{subtitle}</small>
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
          title={`${runtimeLabel} · 查看运行时状态`}
          onClick={() => void runtime.commands.submit("/status", { origin: "button" })}
        >
          <span className="connection-dot" aria-hidden="true" />
          <span>{runtimeLabel}</span>
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

function ProductHome({
  runtime,
  taskCount,
  listPhase,
}: {
  runtime: WorkbenchRuntime
  taskCount: number
  listPhase: string
}) {
  const command = useCommandSnapshot(runtime)
  const creating =
    command.active
    && ["validating", "dispatching"].includes(command.active.phase)
    && !command.active.value.trimStart().startsWith("/")
      ? command.active.value
      : undefined
  // Submitting from the home screen clears the composer before the backend
  // returns a task to route to.  Without this the message just disappears.
  if (creating) {
    return (
      <section className="product-home product-home-creating">
        <div className="product-home-inner">
          <p className="product-creating-goal">{creating}</p>
          <p className="product-creating-status" role="status">
            <span className="product-working-spinner" aria-hidden="true" />
            <span>正在创建任务并准备执行资源…</span>
          </p>
          <button
            className="product-button product-button-quiet"
            type="button"
            onClick={() => runtime.commands.cancelActive("已取消创建任务。")}
          >
            取消
          </button>
        </div>
      </section>
    )
  }
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
        <div className="product-capability-strip">
          <span>长程目标保持</span>
          <span>动态多智能体</span>
          <span>端边云调度</span>
          <span>可追溯交付</span>
          {/* Never present an unread list as "0 tasks". */}
          {listPhase === "error" ? (
            <strong data-tone="danger">历史会话读取失败</strong>
          ) : listPhase === "loading" && !taskCount ? (
            <strong>正在读取历史会话…</strong>
          ) : taskCount ? (
            <strong>{taskCount} 个历史任务</strong>
          ) : null}
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
    return <ProductTaskDetail runtime={runtime} state={state.detail} tasks={state.list.tasks} />
  }
  return (
    <ProductHome
      runtime={runtime}
      taskCount={state.list.total}
      listPhase={state.list.phase}
    />
  )
}

/** Matches the stylesheet breakpoint where the sidebar becomes an overlay drawer. */
export const SIDEBAR_DRAWER_QUERY = "(max-width: 860px)"

export function WorkbenchApp({ runtime }: { runtime: WorkbenchRuntime }) {
  const route = useRoute(runtime)
  const state = useWorkbenchSnapshot(runtime)
  const layout = useLayoutSnapshot(runtime)
  const announcements = useAnnouncementSnapshot(runtime)
  const online = useOnlineStatus()
  const drawer = useMediaQuery(SIDEBAR_DRAWER_QUERY)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const navigationRef = useRef<HTMLElement | null>(null)
  const menuRef = useRef<HTMLButtonElement | null>(null)
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
    if (state.runtime.phase === "ready") runtime.announcer.announce("Zyra 运行时就绪。")
    else if (state.runtime.phase === "reconnecting") runtime.announcer.announce("连接中断，正在重新连接 Zyra。")
    else if (state.runtime.phase === "error") {
      runtime.announcer.announce(
        state.runtime.failure?.message ?? "Zyra 运行时不可用。",
        "assertive",
      )
    }
  }, [runtime, state.runtime.generation, state.runtime.phase])

  useEffect(() => {
    if (route.kind === "new-task") runtime.focus.focusId("workbench-command-input")
    else runtime.focus.routeFocus(route.query.focus)
  }, [route.kind, route.query.focus, runtime])

  const closeSidebar = useCallback(() => {
    setSidebarOpen((current) => {
      if (current) requestAnimationFrame(() => menuRef.current?.focus())
      return false
    })
  }, [])

  // A drawer that is only translated off-screen still traps keyboard users, so
  // opening it must move focus in and closing it must give focus back.
  useEffect(() => {
    if (!drawer) {
      setSidebarOpen(false)
      return
    }
    if (!sidebarOpen) return
    const frame = requestAnimationFrame(() => {
      navigationRef.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [drawer, sidebarOpen])

  useEffect(() => {
    if (!drawer || !sidebarOpen) return
    const onFocusIn = (event: FocusEvent) => {
      const navigation = navigationRef.current
      if (!navigation || navigation.contains(event.target as Node)) return
      if (menuRef.current === event.target) return
      navigation.querySelector<HTMLElement>("button:not([disabled])")?.focus()
    }
    document.addEventListener("focusin", onFocusIn)
    return () => document.removeEventListener("focusin", onFocusIn)
  }, [drawer, sidebarOpen])

  useEffect(() => {
    const handleGlobalKey = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey)
        && !event.altKey
        && event.key.toLowerCase() === "k"
        // A modal overlay owns the keyboard while it is open.
        && !runtime.overlays.active()
      ) {
        event.preventDefault()
        setSidebarOpen(false)
        runtime.router.openNewTask()
        runtime.focus.focusId("workbench-command-input")
        return
      }
      if (event.key === "Escape") {
        if (sidebarOpen) {
          event.preventDefault()
          closeSidebar()
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
  }, [closeSidebar, runtime, sidebarOpen])

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
        listPhase={state.list.phase}
        selectedTaskId={state.selectedTaskId}
        drawer={drawer}
        open={sidebarOpen}
        navigationRef={navigationRef}
        onNavigate={closeSidebar}
      />
      {drawer && sidebarOpen ? (
        <button className="product-sidebar-scrim" type="button" aria-label="关闭会话导航" onClick={closeSidebar} />
      ) : null}
      <div className="app-stage product-stage">
        <TopBar
          runtime={runtime}
          online={online}
          routeKind={route.kind}
          task={route.kind === "task" ? state.detail.task : undefined}
          drawer={drawer}
          sidebarOpen={sidebarOpen}
          menuRef={menuRef}
          onMenu={() => setSidebarOpen(true)}
        />
        <div className="product-connection-slot">
          {!online ? (
            <ReconnectingState
              tone="offline"
              onRetry={() => void runtime.workbench.refreshRuntime({ reconnect: true })}
            />
          ) : state.runtime.phase === "reconnecting" ? (
            <ReconnectingState
              failure={state.runtime.failure}
              onRetry={() => void runtime.workbench.refreshRuntime({ reconnect: true })}
            />
          ) : state.runtime.phase === "error" ? (
            <ReconnectingState
              tone="error"
              failure={state.runtime.failure}
              onRetry={() => void runtime.workbench.refreshRuntime({ reconnect: true })}
            />
          ) : null}
        </div>
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
