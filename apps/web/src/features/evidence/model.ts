import type { CausalEventProjection } from "../../state/contracts.ts"

export const evidenceTabs = [
  { id: "overview", section: "evidence-overview", label: "概览" },
  { id: "process", section: "evidence-process", label: "执行过程" },
  { id: "artifacts", section: "evidence-artifacts", label: "产物与验证" },
  { id: "advanced", section: "evidence-topology", label: "高级记录" },
] as const
export type EvidenceTab = typeof evidenceTabs[number]["id"]
export interface EvidenceNavigationTarget { artifactId?: string; eventId?: string }

export const advancedSections = [
  { id: "evidence-topology", title: "任务关系图", detail: "节点、依赖与协作关系" },
  { id: "evidence-policy", title: "策略与执行回执", detail: "方案、约束检查与实际执行" },
  { id: "evidence-continuity-placement", title: "会话与记忆", detail: "上下文、检查点与执行位置" },
  { id: "evidence-recovery", title: "故障与恢复", detail: "执行时间线与恢复记录" },
  { id: "evidence-causal-trace", title: "事件追踪", detail: "事件之间的因果关系" },
  { id: "evidence-controls", title: "权限与审批", detail: "权限请求和操作回执" },
  { id: "evidence-command-queue", title: "命令队列", detail: "待执行命令与处理结果" },
  { id: "evidence-diff", title: "代码变更", detail: "差异与审查记录" },
  { id: "evidence-browser", title: "浏览器记录", detail: "浏览器操作与观察结果" },
  { id: "evidence-terminal", title: "工作区终端", detail: "终端输出与工作区操作" },
  { id: "evidence-long-horizon", title: "长程任务", detail: "循环、阶段与持续执行" },
  { id: "subagent-runtime-panel", title: "子代理", detail: "委派任务与执行状态" },
  { id: "mcp-runtime-panel", title: "MCP 服务", detail: "工具服务与连接状态" },
  { id: "skill-runtime-panel", title: "技能", detail: "技能加载与使用记录" },
  { id: "evidence-diagnostics", title: "运行诊断", detail: "运行编号、版本与原始指标" },
] as const

export function evidenceDestination(section?: string): { tab: EvidenceTab; section: string } {
  if (section === "evidence-plan" || section === "evidence-canonical-events" || section === "evidence-process") {
    return { tab: "process", section }
  }
  if (section === "evidence-artifacts") return { tab: "artifacts", section }
  if (advancedSections.some((entry) => entry.id === section)) return { tab: "advanced", section: section! }
  return { tab: "overview", section: "evidence-overview" }
}

export type EventCategory = "progress" | "verification" | "issues" | "other"
export const eventCategories = { progress: "任务进展", verification: "产物与校验", issues: "异常与恢复", other: "运行记录" } as const

export function eventCategory(event: Pick<CausalEventProjection, "eventType">): EventCategory {
  const type = event.eventType.toLowerCase().replace(/^runtime\./, "")
  if (/fail|error|reject|cancel|recovery|failover|interrupt/.test(type)) return "issues"
  if (/verif|artifact|validation|constraint/.test(type)) return "verification"
  if (/^(task|plan|node|worker|tool)\./.test(type)) return "progress"
  return "other"
}

export function eventLabel(event: Pick<CausalEventProjection, "eventType">): string {
  const labels: Record<string, string> = {
    "task.created": "任务已创建", "task.started": "任务开始执行", "task.completed": "任务已完成",
    "task.failed": "任务执行失败", "task.cancelled": "任务已停止", "task.updated": "任务状态更新",
    "plan.updated": "执行计划已更新", "node.started": "步骤开始执行", "node.completed": "步骤已完成",
    "node.failed": "步骤执行失败", "artifact.created": "产物已生成", "worker.started": "执行者已启动",
    "worker.completed": "执行者已完成", "verification.completed": "校验已完成",
    "node.created": "执行步骤已创建", "node.updated": "步骤状态更新", "agent.message": "协作消息",
    "backend.dispatch.requested": "已请求执行资源", "artifact.committed": "产物已保存", "audit.finding": "审计记录",
    "query.admitted": "请求已受理", "text.started": "开始生成回答", "text.ended": "回答生成结束", "topology.route": "执行路由更新",
    "tool.called": "工具开始执行", "tool.succeeded": "工具执行完成", "tool.failed": "工具执行失败", "tool.cancelled": "工具已停止",
  }
  return labels[event.eventType.replace(/^runtime\./, "")] ?? eventCategories[eventCategory(event)]
}

export function eventSummary(event: Pick<CausalEventProjection, "eventType" | "summary">): string {
  if (/^Legacy .+ event normalized into the runtime event spine\.$/.test(event.summary)) return "已记录，展开查看原始事件。"
  return ({ running: "开始执行。", completed: "执行完成。", failed: "执行失败。" } as Record<string, string>)[event.summary]
    ?? (event.summary || "暂无摘要")
}

export function filterEvidenceEvents(events: readonly CausalEventProjection[], category: EventCategory | "all", query: string) {
  const text = query.trim().toLocaleLowerCase()
  return events.filter((event) => (category === "all" || eventCategory(event) === category)
    && (!text || [eventLabel(event), event.summary, event.eventType, event.eventId, event.nodeId, event.workerId]
      .some((value) => value?.toLocaleLowerCase().includes(text))))
}
