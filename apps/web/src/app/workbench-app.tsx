import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react"
import { OPERATION_NAMES, type SessionListProjection, type TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"
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
import { ConversationIcon, ConversationMenu } from "../components/tasks/conversation-menu.tsx"
import { EmptyState, ReconnectingState } from "../components/status/request-state.tsx"
import { NotificationTray } from "../components/status/notification-tray.tsx"
import { ProductSettings } from "../components/settings/product-settings.tsx"
import { EvidenceWorkbench } from "../features/evidence/index.ts"

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
  if (["failed", "error", "blocked", "needs_revision"].includes(task.status.toLowerCase())) return "danger"
  return "idle"
}

/** Text equivalent for the colour-only status dot in the conversation list. */
function statusText(task: TaskProjection): string {
  if (["cancelled", "canceled"].includes(task.status.toLowerCase())) return "已停止"
  if (task.status === "needs_revision") return "需要修改"
  if (task.status === "blocked") return "等待处理"
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
  pinned: readonly string[] = [],
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
    const latest = ordered[ordered.length - 1]!
    return {
      key,
      title: ordered[0]?.userGoal || latest.userGoal || "未命名会话",
      latest,
      turnCount: ordered.length,
    }
  }).sort((left, right) => {
    const pinOrder = Number(pinned.includes(right.key)) - Number(pinned.includes(left.key))
    if (pinOrder) return pinOrder
    const updated = Date.parse(right.latest.createdAt) - Date.parse(left.latest.createdAt)
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
  listUpdatedAt,
  selectedTaskId,
  drawer,
  open,
  onNavigate,
  navigationRef,
  listCursor,
}: {
  runtime: WorkbenchRuntime
  routeKind: string
  tasks: readonly TaskProjection[]
  listPhase: string
  listUpdatedAt?: number
  selectedTaskId?: string
  drawer: boolean
  open: boolean
  onNavigate: () => void
  navigationRef: React.RefObject<HTMLElement | null>
  listCursor?: string
}) {
  const [query, setQuery] = useState("")
  const [visibleCount, setVisibleCount] = useState(12)
  const preferences = useSyncExternalStore(runtime.preferences.subscribe, runtime.preferences.getSnapshot, runtime.preferences.getSnapshot)
  const [menu, setMenu] = useState<{ key: string; anchor: HTMLElement }>()
  const [feedback, setFeedback] = useState("")
  useEffect(() => {
    if (listPhase !== "ready") return
    const controller = new AbortController()
    void runtime.api.client.endpoint<SessionListProjection>(OPERATION_NAMES.sessionList, {
      query: { limit: 200 }, signal: controller.signal,
      coordinationKey: "web.conversation.titles", latestWins: true,
    }).then((response) => {
      if (controller.signal.aborted) return
      const titles = { ...runtime.preferences.getSnapshot().titles }
      for (const session of response.data.sessions) if (session.title) titles[session.sessionId] = session.title
      if (JSON.stringify(titles) !== JSON.stringify(runtime.preferences.getSnapshot().titles)) runtime.preferences.update({ titles })
    }).catch(() => { /* Existing titles stay available while offline. */ })
    return () => controller.abort()
  }, [runtime, listPhase, listUpdatedAt])
  const conversations = productConversationList(tasks, preferences.pinned).map((conversation) => ({ ...conversation,
    title: preferences.titles[conversation.key] || conversation.title,
  })).filter((conversation) =>
    conversation.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  )
  const recent = conversations.slice(0, visibleCount)
  const selectedTask = tasks.find((task) => task.taskId === selectedTaskId)
  const selectedConversationKey = selectedTask?.sessionId ?? (selectedTask ? `task:${selectedTask.taskId}` : undefined)
  const navigate = (action: () => void) => {
    action()
    onNavigate()
  }
  // Off-canvas on a narrow viewport means visually gone but still in the tab
  // order unless it is explicitly removed from the accessibility tree.
  const hidden = drawer && !open
  const rename = async (conversation: ProductConversation, value: string) => {
    const title = value.trim()
    if (!title) throw new Error("请输入会话名称。")
    if (new TextEncoder().encode(title).length > 256) throw new Error("名称太长，请缩短后再保存。")
    const receipt = await runtime.controlCommands.renameConversation(conversation.latest, title)
    if (receipt.phase !== "applied") throw new Error(receipt.error?.message || "名称尚未保存，请稍后刷新会话列表。")
    runtime.preferences.rememberTitle(conversation.key, title)
  }
  const deleteConversation = async (conversation: ProductConversation) => {
    const result = await runtime.api.tasks.deleteConversation(conversation.latest.sessionId ?? `session_${conversation.latest.taskId}`)
    if (selectedConversationKey === conversation.key) navigate(() => runtime.router.openNewTask())
    runtime.workbench.forgetTasks(result.taskIds)
    await runtime.workbench.refreshTasks({ preserveOnError: true })
    try { runtime.preferences.forgetConversation(conversation.key) }
    catch { setFeedback("会话已删除；本地置顶信息未能清除。") }
  }
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
            <small>你的智能工作空间</small>
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
          disabled={!selectedTaskId}
          aria-current={routeKind === "evidence" ? "page" : undefined}
          title={selectedTaskId ? "打开当前任务的完整证据链" : "选择任务后查看证据"}
          onClick={() => selectedTaskId && navigate(() => runtime.router.openEvidence(selectedTaskId))}
        >
          <span aria-hidden="true">⌘</span><span>证据中心</span>
        </button>
        <button
          type="button"
          disabled={!selectedTaskId}
          title={selectedTaskId ? "打开当前任务的交付物" : "完成任务后可查看交付物"}
          onClick={() => selectedTaskId && navigate(() => runtime.router.openTask(selectedTaskId, { view: "artifacts" }))}
        >
          <span aria-hidden="true">◇</span><span>交付物</span>
        </button>
        <button
          type="button"
          aria-current={routeKind === "settings" ? "page" : undefined}
          onClick={() => navigate(() => runtime.router.openSettings())}
        >
          <span aria-hidden="true">⚙</span><span>设置</span>
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
        <input
          className="product-conversation-search"
          type="search"
          aria-label="搜索会话"
          placeholder="搜索会话…"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setVisibleCount(12) }}
        />
        <div className="product-recent-list">
          {recent.map((conversation) => (
            <div className="product-recent-row" key={conversation.key} data-pinned={preferences.pinned.includes(conversation.key) || undefined}>
            <button
              type="button"
              className="product-recent-task"
              aria-current={selectedConversationKey === conversation.key ? "page" : undefined}
              onClick={() => navigate(() => runtime.router.openTask(conversation.latest.taskId))}
            >
              <span className="product-recent-status" data-tone={statusTone(conversation.latest)} aria-hidden="true" />
              <span>
                <strong>{preferences.pinned.includes(conversation.key) ? <span className="conversation-pin" title="已置顶"><ConversationIcon kind="pin" /></span> : null}{conversation.title}</strong>
                <small>
                  <span className="sr-only">{statusText(conversation.latest)} · </span>
                  {conversation.turnCount > 1 ? `${conversation.turnCount} 轮 · ` : ""}
                  {taskTime(conversation.latest.createdAt)}
                </small>
              </span>
            </button>
            <button className="product-conversation-more" type="button" aria-label={`会话操作：${conversation.title}`} aria-haspopup="menu" aria-expanded={menu?.key === conversation.key} onClick={(event) => { setMenu(menu?.key === conversation.key ? undefined : { key: conversation.key, anchor: event.currentTarget }); setFeedback("") }}>⋯</button>
            {menu?.key === conversation.key ? <ConversationMenu key={conversation.key} anchor={menu.anchor} title={conversation.title}
              pinned={preferences.pinned.includes(conversation.key)}
              canDelete={tasks.filter((task) => (task.sessionId ?? `task:${task.taskId}`) === conversation.key).every((task) => task.terminal)}
              onClose={() => setMenu(undefined)} onRename={(title) => rename(conversation, title)}
              onPin={() => runtime.preferences.pin(conversation.key, !preferences.pinned.includes(conversation.key))}
              onDelete={() => deleteConversation(conversation)} /> : null}
            </div>
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
            <p className="product-sidebar-empty">{query ? "没有匹配的会话。" : "开始会话后会显示在这里。"}</p>
          ) : null}
          {conversations.length > visibleCount ? (
            <button className="product-history-more" type="button" onClick={() => setVisibleCount((count) => count + 12)}>显示更多会话</button>
          ) : listCursor ? (
            <button className="product-history-more" type="button" disabled={listPhase === "loading"} onClick={() => void runtime.workbench.refreshTasks({ cursor: listCursor, append: true, preserveOnError: true })}>
              {listPhase === "loading" ? "正在读取…" : "加载更早的会话"}
            </button>
          ) : null}
        </div>
        {feedback ? <p className="product-sidebar-feedback" role="status">{feedback}</p> : null}
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
  const phase = online ? state.runtime.phase : "offline"
  const preferences = useSyncExternalStore(runtime.preferences.subscribe, runtime.preferences.getSnapshot, runtime.preferences.getSnapshot)
  const title = (task ? preferences.titles[task.sessionId ?? `task:${task.taskId}`] || task.userGoal : undefined)
    || (routeKind === "settings" ? "设置" : "Zyra")
  const subtitle = task
    ? task.active && live.live && !live.paused
      ? "进行中 · 实时更新"
      : statusText(task)
    : "动态异构多智能体工作空间"
  const runtimeLabel = !online
    ? "离线"
    : phase === "ready" && state.runtime.readiness?.ready
      ? "服务已就绪"
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
          id="runtime-status-trigger"
          className="runtime-pill"
          type="button"
          data-phase={phase}
          title={`${runtimeLabel} · 查看运行时状态`}
          onClick={() => runtime.overlays.open({ kind: "transport-status", title: "运行时连接", replaceKind: true })}
        >
          <span className="connection-dot" aria-hidden="true" />
          <span>{runtimeLabel}</span>
        </button>
      </div>
    </header>
  )
}


function ProductHome({
  runtime,
}: {
  runtime: WorkbenchRuntime
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
          <span>Z</span>
        </div>
        <p className="product-kicker">与你一起完成复杂任务</p>
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
  if (route.kind === "settings") return <ProductSettings runtime={runtime} />
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
    return <ProductTaskDetail runtime={runtime} state={state.detail} tasks={state.list.tasks} view={route.query.view} />
  }
  if (route.kind === "evidence") {
    return <EvidenceWorkbench runtime={runtime} state={state.detail} section={route.query.section} />
  }
  return (
    <ProductHome
      runtime={runtime}
    />
  )
}

/** Matches the stylesheet breakpoint where the sidebar becomes an overlay drawer. */
export const SIDEBAR_DRAWER_QUERY = "(max-width: 860px)"

export function WorkbenchApp({ runtime }: { runtime: WorkbenchRuntime }) {
  const route = useRoute(runtime)
  const preferences = useSyncExternalStore(runtime.preferences.subscribe, runtime.preferences.getSnapshot, runtime.preferences.getSnapshot)
  useEffect(() => { document.documentElement.style.setProperty("--answer-font-size", `${preferences.fontSize}px`) }, [preferences.fontSize])
  const state = useWorkbenchSnapshot(runtime)
  const layout = useLayoutSnapshot(runtime)
  const announcements = useAnnouncementSnapshot(runtime)
  const online = useOnlineStatus()
  const drawer = useMediaQuery(SIDEBAR_DRAWER_QUERY)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const navigationRef = useRef<HTMLElement | null>(null)
  const menuRef = useRef<HTMLButtonElement | null>(null)
  const routeTaskId =
    route.kind === "task" || route.kind === "evidence"
      ? route.taskId
      : undefined
  const bootstrapKey = useMemo(
    () => `${routeTaskId ? "task-detail" : route.kind}:${routeTaskId ?? ""}:${route.query.status ?? ""}:${route.query.cursor ?? ""}`,
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
    if (state.runtime.phase === "ready") runtime.announcer.announce("Zyra 服务已就绪。")
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
        const task = runtime.workbench.selectedTask()
        if (task?.active) {
          event.preventDefault()
          runtime.overlays.open({ kind: "task-cancel", title: "停止任务", replaceKind: true,
            payload: { taskId: task.taskId, runId: task.runId, goal: task.userGoal } })
        } else if (runtime.commands.getSnapshot().busy && runtime.commands.cancelActive()) event.preventDefault()
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
        listUpdatedAt={state.list.loadedAt}
        listCursor={state.list.cursor}
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
          task={
            route.kind === "task" || route.kind === "evidence"
              ? state.detail.task
              : undefined
          }
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
        {route.kind !== "settings" && route.kind !== "evidence" ? (
          <CommandInput
            runtime={runtime}
            taskContext={
              route.kind === "task"
                ? state.detail.task
                : undefined
            }
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
