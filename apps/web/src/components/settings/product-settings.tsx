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
  const [tab, setTab] = useState<"general" | "system" | "experiments">("general")
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
      setNotice("已保存。此浏览器之后提交的新任务和追问使用该设置；已提交或排队的任务保持原设置。")
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

  return <section className="settings-view advanced-center product-settings" aria-labelledby="settings-heading">
    <header className="advanced-center-heading"><div><p className="eyebrow">工作区</p><h1 id="settings-heading">设置</h1><p>配置日常使用偏好，查看运行状态和高级实验。</p></div></header>
    <div className="settings-tabs" role="tablist" aria-label="设置分类">
      {([['general', '使用偏好'], ['system', '连接与历史'], ['experiments', '场景与实验']] as const).map(([id, label]) => <button type="button" role="tab" id={`settings-tab-${id}`} aria-selected={tab === id} aria-controls={`settings-panel-${id}`} key={id} onClick={() => setTab(id)}>{label}</button>)}
    </div>
    {error ? <p className="product-error-copy" role="alert">{error}</p> : null}
    {notice ? <p className="settings-notice" role="status">{notice}</p> : null}
    <div role="tabpanel" id={`settings-panel-${tab}`} aria-labelledby={`settings-tab-${tab}`}>
    {tab === "general" ? <>
      <section className="settings-card"><h2>模型与思考强度</h2>
        <p>用于此浏览器之后提交的新任务和追问，保存后不会改变正在运行的任务。</p>
        <p className="settings-current">当前默认：<strong>{preferences.execution ? `${preferences.execution.modelId} · ${effortLabels[preferences.execution.reasoningEffort ?? ''] || preferences.execution.reasoningEffort || "模型默认强度"}` : "由运行时自动选择模型"}</strong></p>
        <div className="settings-form-grid">
          <label>默认模型<select aria-label="默认模型" value={selectedModel} disabled={modelPhase !== "ready"} onChange={(event) => { setSelectedModel(event.target.value); setEffort("") }}>
            <option value="auto">自动选择</option>
            {models.map((item) => <option key={`${item.providerId}:${item.modelId}`} value={`${item.providerId}:${item.modelId}`}>{item.displayName} · {item.providerId}</option>)}
            {selectedModel !== "auto" && !model ? <option value={selectedModel}>{modelPhase === "loading" ? "正在加载已保存的模型…" : modelPhase === "error" ? "已保存的模型（暂时无法检查）" : "之前选择的模型（当前不可用）"}</option> : null}
          </select></label>
          <label>思考强度<select aria-label="思考强度" value={effort} disabled={!model || !model.efforts.length} onChange={(event) => setEffort(event.target.value)}>
            <option value="">{model?.defaultEffort ? `模型默认（${effortLabels[model.defaultEffort] || model.defaultEffort}）` : "模型默认"}</option>
            {model?.efforts.map((value) => <option key={value} value={value}>{effortLabels[value] || value}</option>)}
          </select></label>
        </div>
        {modelPhase === "loading" ? <p role="status">正在读取可用模型…</p> : null}
        {modelPhase === "ready" && !models.length ? <p>当前没有可用模型，请先配置后端模型服务。</p> : null}
        <div className="settings-actions"><button className="product-button product-button-primary" type="button" disabled={modelPhase !== "ready"} onClick={saveModel}>保存模型设置</button><button className="product-button" type="button" onClick={() => void loadModels()}>刷新模型列表</button></div>
      </section>
      <section className="settings-card"><h2>外观与阅读</h2><p>以黑白灰为主，操作和状态使用辅助色，主页面与运行详情保持一致。</p>
        <label>回答文字大小<select aria-label="回答文字大小" value={preferences.fontSize} onChange={(event) => changeFont(Number(event.target.value) as 14 | 16 | 18)}><option value={14}>紧凑 · 14 px</option><option value={16}>标准 · 16 px</option><option value={18}>大字 · 18 px</option></select></label>
        <p className="product-answer settings-text-preview">这是回复正文的阅读效果。执行步骤和辅助说明使用较小的灰色文字。</p>
      </section>
      <section className="settings-card"><h2>权限与审批</h2><p>每个任务沿用后端权限策略。选择任务查看当前权限、待审批操作和允许范围。</p>
        <div className="settings-actions"><select aria-label="选择权限所属任务" value={permissionTaskId} onChange={(event) => { setPermissionTaskId(event.target.value); setPermissionTask(undefined) }}><option value="">选择一个任务</option>{workbench.list.tasks.map((task) => <option key={task.taskId} value={task.taskId}>{task.userGoal}</option>)}</select><button type="button" className="product-button" disabled={!permissionTaskId || permissionBusy} onClick={() => void inspectPermissions()}>{permissionBusy ? "读取中…" : "查看权限与审批"}</button></div>
        {permissionTask ? <PermissionWorkbench runtime={runtime} task={permissionTask} /> : null}
      </section>
    </> : tab === "system" ? <div className="settings-grid">
      <article className="settings-card"><h2>API 与运行时</h2><p>当前浏览器连接的服务。</p><dl className="fact-grid"><div><dt>服务地址</dt><dd>{runtime.api.client.baseUrl}</dd></div><div><dt>运行状态</dt><dd><span className="tag" data-phase={runtimeStatus.phase}>{runtimeStatus.label}</span></dd></div></dl><button type="button" className="product-button" onClick={() => void runtime.commands.submit("/status", { origin: "button" })}>查看运行状态</button></article>
      <article className="settings-card"><h2>浏览器历史</h2><p>输入历史、草稿、归档标记和外观偏好保存在当前浏览器。</p><dl className="fact-grid"><div><dt>输入历史</dt><dd>{historyCount}</dd></div><div><dt>已归档会话</dt><dd>{preferences.archived.length}</dd></div></dl><button className="product-button product-button-danger" type="button" disabled={!historyCount} onClick={() => { runtime.history.clear(); setHistoryCount(0); setNotice("输入历史已清除，任务和会话记录仍保留。") }}>清除输入历史</button></article>
    </div> : <div className="settings-experiments"><p>以下用于场景试运行与正式实验，后台任务在浏览器关闭后继续执行。</p><ScenarioWorkbench runtime={runtime.scenarioConsole} /><ExperimentWorkbench runtime={runtime.experimentConsole} /></div>}
    </div>
  </section>
}
