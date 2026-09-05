import { useEffect, useState, useSyncExternalStore } from "react"
import { OPERATION_NAMES, type TaskProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../app/runtime.ts"
import { useOnlineStatus, useWorkbenchSnapshot } from "../../app/hooks.ts"
import { ScenarioWorkbench } from "../../features/scenarios/index.ts"
import { ExperimentWorkbench } from "../../features/experiments/index.ts"
import { PermissionWorkbench } from "../../features/permissions/index.ts"

export interface ModelChoice {
  providerId: string
  modelId: string
  displayName: string
  efforts: string[]
  defaultEffort?: string
}

export function modelChoices(value: Record<string, unknown>): ModelChoice[] {
  if (value.schema !== "zyra.provider-backend-api/v1" || value.state_owner !== "typescript.ProviderControlPlaneStore" || !Array.isArray(value.result)) {
    throw new Error("模型目录暂时不可用，请刷新后重试。")
  }
  return value.result.map((model) => {
    if (!model || typeof model.providerId !== "string" || typeof model.modelId !== "string") throw new Error("模型目录格式不正确。")
    return { providerId: model.providerId, modelId: model.modelId,
      displayName: String(model.displayName || model.modelId),
      efforts: Array.isArray(model.supportedReasoningEfforts) ? model.supportedReasoningEfforts.filter((v: unknown) => typeof v === "string") : [],
      defaultEffort: typeof model.requestDefaults?.reasoning_effort === "string" ? model.requestDefaults.reasoning_effort : undefined,
    }
  })
}

const effortLabels: Record<string, string> = { low: "低", medium: "中", high: "高", max: "最高", minimal: "最少", none: "关闭" }

const categories = [
  { id: "model", label: "模型", description: "选择回答问题和执行任务时使用的模型。" },
  { id: "appearance", label: "外观", description: "调整对话的文字大小，立即预览阅读效果。" },
  { id: "system", label: "连接与数据", description: "检查服务连接，管理当前浏览器保存的输入历史。" },
  { id: "permissions", label: "任务权限", description: "查看某个任务允许执行的操作和待审批请求。" },
  { id: "advanced", label: "高级工具", description: "试运行任务，或查看已有实验的结果。" },
] as const
type SettingsCategory = typeof categories[number]["id"]

function SettingsIcon({ kind }: { kind: SettingsCategory }) {
  const paths = { model: "M12 3 3 8l9 5 9-5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5", appearance: "M4 20 11 4h2l7 16M7 14h10", system: "M8 3v5m8-5v5M5 8h14v3a7 7 0 0 1-14 0V8Zm7 10v4", permissions: "m12 3 8 3v6c0 4-5 8-8 9-3-1-8-5-8-9V6l8-3Zm-4 9 3 3 5-6", advanced: "M9 3h6m-5 0v7l-6 9a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-9V3M8 15h8" }
  return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]} /></svg>
}

export function ProductSettings({ runtime }: { runtime: WorkbenchRuntime }) {
  const preferences = useSyncExternalStore(runtime.preferences.subscribe, runtime.preferences.getSnapshot, runtime.preferences.getSnapshot)
  const workbench = useWorkbenchSnapshot(runtime)
  const online = useOnlineStatus()
  const runtimeReady = workbench.runtime.phase === "ready" && workbench.runtime.readiness?.ready
  const runtimeStatus = !online ? { phase: "offline", label: "离线" }
    : runtimeReady ? { phase: "ready", label: "就绪" }
    : workbench.runtime.phase === "loading" ? { phase: "loading", label: "正在检查" }
    : workbench.runtime.phase === "reconnecting" ? { phase: "reconnecting", label: "正在重连" }
    : workbench.runtime.phase === "error" ? { phase: "error", label: "连接失败" }
    : { phase: "degraded", label: "需要检查" }
  const [tab, setTab] = useState<SettingsCategory>("model")
  const [tool, setTool] = useState<"scenario" | "experiment">()
  const [models, setModels] = useState<ModelChoice[]>([])
  const [modelPhase, setModelPhase] = useState("loading")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [selectedModel, setSelectedModel] = useState(preferences.execution ? `${preferences.execution.providerId}:${preferences.execution.modelId}` : "auto")
  const [effort, setEffort] = useState(preferences.execution?.reasoningEffort ?? "")
  const [historyCount, setHistoryCount] = useState(runtime.history.list().length)
  const [permissionTaskId, setPermissionTaskId] = useState("")
  const [permissionTask, setPermissionTask] = useState<TaskProjection>()
  const [permissionBusy, setPermissionBusy] = useState(false)
  const model = models.find((item) => `${item.providerId}:${item.modelId}` === selectedModel)
  const modelChanged = selectedModel !== (preferences.execution ? `${preferences.execution.providerId}:${preferences.execution.modelId}` : "auto")
    || effort !== (preferences.execution?.reasoningEffort ?? "")
  const category = categories.find((item) => item.id === tab)!

  const loadModels = async (signal?: AbortSignal) => {
    setModelPhase("loading")
    setError("")
    try {
      const response = await runtime.api.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.providerModels, {
        query: { available_only: true }, timeoutMs: 30_000, signal,
        coordinationKey: "web.settings.models", latestWins: true,
      })
      if (signal?.aborted) return
      setModels(modelChoices(response.data)); setModelPhase("ready")
    } catch (reason) {
      if (signal?.aborted) return
      setModelPhase("error"); setError(reason instanceof Error ? reason.message : String(reason))
    }
  }
  useEffect(() => { const controller = new AbortController(); void loadModels(controller.signal); return () => controller.abort() }, [runtime])

  const saveModel = () => {
    setError(""); setNotice("")
    try {
      if (selectedModel !== "auto" && !model) throw new Error("当前选择的模型不可用，请重新选择。")
      if (effort && model && !model.efforts.includes(effort)) throw new Error("该模型不支持当前思考强度，请重新选择。")
      runtime.preferences.update({ execution: model ? { providerId: model.providerId, modelId: model.modelId, ...(effort ? { reasoningEffort: effort } : {}) } : undefined })
      setNotice("模型设置已保存，将用于之后提交的新任务和追问。")
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法保存设置。") }
  }
  const changeFont = (fontSize: 14 | 16 | 18) => {
    try { runtime.preferences.update({ fontSize }); setNotice("文字大小已保存并立即生效。"); setError("") }
    catch { setError("无法保存文字大小，请检查浏览器存储权限。") }
  }
  const inspectPermissions = async () => {
    if (!permissionTaskId) return
    setPermissionBusy(true); setError("")
    try { setPermissionTask(await runtime.api.tasks.get(permissionTaskId)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setPermissionBusy(false) }
  }

  return <section className="settings-view product-settings" aria-labelledby="settings-heading">
    <header className="settings-page-heading"><h1 id="settings-heading">设置</h1><p>让 Zyra 更适合你的使用习惯。</p></header>
    <div className="settings-layout">
    <div className="settings-navigation" role="tablist" aria-label="设置分类" aria-orientation="vertical" onKeyDown={(event) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return
      event.preventDefault()
      const index = categories.findIndex((item) => item.id === tab)
      const next = event.key === "Home" ? 0 : event.key === "End" ? categories.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + categories.length) % categories.length
      document.getElementById(`settings-tab-${categories[next]!.id}`)?.click()
      document.getElementById(`settings-tab-${categories[next]!.id}`)?.focus()
    }}>
      {categories.map(({ id, label }) => <button type="button" role="tab" id={`settings-tab-${id}`} aria-selected={tab === id} tabIndex={tab === id ? 0 : -1} aria-controls={`settings-panel-${id}`} key={id} onClick={() => { setTab(id); setError(""); setNotice("") }}><SettingsIcon kind={id} />{label}</button>)}
      <p>偏好设置保存在此浏览器</p>
    </div>
    <div className="settings-content" role="tabpanel" id={`settings-panel-${tab}`} aria-labelledby={`settings-tab-${tab}`} tabIndex={0}>
    <header className="settings-section-heading"><h2>{category.label}</h2><p>{category.description}</p></header>
    {error ? <p className="product-error-copy" role="alert">{error}</p> : null}
    {notice ? <p className="settings-notice" role="status">{notice}</p> : null}
    {tab === "model" ? <section className="settings-card">
        <div className="settings-row"><div><h3>默认模型</h3><p>自动选择会沿用服务端配置。</p></div>
          <select aria-label="默认模型" value={selectedModel} disabled={modelPhase !== "ready"} onChange={(event) => { setSelectedModel(event.target.value); setEffort(""); setNotice("") }}>
            <option value="auto">自动选择（推荐）</option>
            {models.map((item) => <option key={`${item.providerId}:${item.modelId}`} value={`${item.providerId}:${item.modelId}`}>{item.displayName} · {item.providerId}</option>)}
            {selectedModel !== "auto" && !model ? <option value={selectedModel}>{modelPhase === "loading" ? "正在加载已保存的模型…" : modelPhase === "error" ? "已保存的模型（暂时无法检查）" : "之前选择的模型（当前不可用）"}</option> : null}
          </select></div>
        <div className="settings-row"><div><h3>思考强度</h3><p>{!model ? "选择具体模型后，可调整其支持的思考强度。" : !model.efforts.length ? "此模型不支持单独调整思考强度。" : "更高的强度适合复杂问题，通常需要更多时间。"}</p></div>
          <select aria-label="思考强度" value={effort} disabled={!model || !model.efforts.length} onChange={(event) => { setEffort(event.target.value); setNotice("") }}>
            <option value="">{model?.defaultEffort ? `模型默认（${effortLabels[model.defaultEffort] || model.defaultEffort}）` : "模型默认"}</option>
            {model?.efforts.map((value) => <option key={value} value={value}>{effortLabels[value] || value}</option>)}
          </select></div>
        {modelPhase === "loading" ? <p role="status">正在读取可用模型…</p> : null}
        {modelPhase === "ready" && !models.length ? <p>当前没有可用模型，请先配置后端模型服务。</p> : null}
        <div className="settings-save-row"><span>{modelChanged ? "有尚未保存的更改" : "用于之后提交的新任务和追问"}</span><button className="product-button product-button-primary" type="button" disabled={modelPhase !== "ready" || !modelChanged} onClick={saveModel}>保存更改</button></div>
        <button className="settings-text-button" type="button" disabled={modelPhase === "loading"} onClick={() => void loadModels()}>重新获取模型列表</button>
      </section> : tab === "appearance" ? <section className="settings-card">
        <div className="settings-row"><div><h3>回答文字大小</h3><p>选择后自动保存。</p></div><div className="settings-font-options" role="group" aria-label="回答文字大小">{([14, 16, 18] as const).map((size, index) => <button type="button" key={size} aria-pressed={preferences.fontSize === size} onClick={() => changeFont(size)}>{["小", "标准", "大"][index]}</button>)}</div></div>
        <div className="settings-reading-preview"><span>阅读预览</span><div className="settings-preview-answer"><span className="product-avatar-zyra" aria-hidden="true">Z</span><div><strong>Zyra</strong><p className="product-answer settings-text-preview">你好！告诉我你想完成什么，我会整理思路、逐步执行，并把结果交给你。</p><small>执行步骤和辅助说明会以较小的灰色文字显示。</small></div></div></div>
      </section> : tab === "permissions" ? <section className="settings-card"><h3>查看任务权限</h3><p>权限按任务管理。先选择任务，再查看它的允许范围和审批请求。</p>
        <div className="settings-actions"><select aria-label="选择权限所属任务" value={permissionTaskId} disabled={permissionBusy} onChange={(event) => { setPermissionTaskId(event.target.value); setPermissionTask(undefined) }}><option value="">选择一个任务</option>{workbench.list.tasks.map((task) => <option key={task.taskId} value={task.taskId}>{task.userGoal}</option>)}</select><button type="button" className="product-button" disabled={!permissionTaskId || permissionBusy} onClick={() => void inspectPermissions()}>{permissionBusy ? "读取中…" : "查看权限与审批"}</button></div>
        {!workbench.list.tasks.length ? <p>还没有任务。开始一次对话后，即可在这里查看其权限。</p> : null}
        {permissionTask ? <PermissionWorkbench runtime={runtime} task={permissionTask} /> : null}
      </section> : tab === "system" ? <>
      <section className="settings-card"><div className="settings-row"><div><h3>服务连接</h3><p>{runtimeStatus.phase === "ready" ? "连接正常，可以提交任务。" : "提交任务前，请确保服务已连接。"}</p></div><span className="tag" data-phase={runtimeStatus.phase}>{runtimeStatus.label}</span></div><div className="settings-row"><div><h3>服务地址</h3><p className="settings-address">{runtime.api.client.baseUrl}</p></div><button type="button" className="product-button" disabled={workbench.runtime.phase === "loading"} onClick={() => void runtime.workbench.refreshRuntime()}>重新检查</button></div></section>
      <section className="settings-card"><div className="settings-row"><div><h3>输入历史</h3><p>保存过的输入可在输入框中用方向键找回。</p><p>此浏览器已保存 {historyCount} 条。清除后不会删除会话。</p></div><button className="product-button product-button-danger" type="button" disabled={!historyCount} onClick={() => { try { runtime.history.clear(); setHistoryCount(0); setNotice("输入历史已清除，任务和会话记录仍保留。") } catch { setError("输入历史未能清除，请重试。") } }}>清除输入历史</button></div><p className="settings-footnote">会话记录由服务端保存；草稿、置顶和外观偏好保存在此浏览器。</p></section>
    </> : <div className="settings-experiments">
      {tool ? <><button className="settings-text-button settings-back" type="button" onClick={() => setTool(undefined)}>← 返回工具列表</button>{tool === "scenario" ? <ScenarioWorkbench runtime={runtime.scenarioConsole} /> : <ExperimentWorkbench runtime={runtime.experimentConsole} />}</> : <div className="settings-tool-list">
        <button type="button" className="settings-tool-card" onClick={() => setTool("scenario")}><span className="settings-tool-icon"><SettingsIcon kind="model" /></span><span><strong>任务试运行</strong><span>给定一个软件开发或研究目标，检查 Zyra 的执行过程和结果。</span><small>填写目标 → 创建试运行 → 启动并查看结果</small></span><span aria-hidden="true">→</span></button>
        <button type="button" className="settings-tool-card" onClick={() => setTool("experiment")}><span className="settings-tool-icon"><SettingsIcon kind="advanced" /></span><span><strong>实验结果</strong><span>查看已创建实验的完成情况，对比不同方案的指标和证据。</span><small>已有实验的查看与管理入口</small></span><span aria-hidden="true">→</span></button>
      </div>}
    </div>}
    </div>
    </div>
  </section>
}
