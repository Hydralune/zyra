import type { PlanNodeProjection } from "../../../../packages/core/typed-api-client/src/index.ts"

const COMPLETED_NODE_STATES = new Set(["completed", "succeeded", "verified"])
const RUNNING_NODE_STATES = new Set(["running", "active", "dispatched"])
const FAILED_NODE_STATES = new Set(["failed", "error"])

export function nodeLabel(node: PlanNodeProjection): string {
  const labels: Record<string, string> = { "Root task": "任务目标", Plan: "规划", Route: "选择执行资源", Execute: "执行", Verify: "验证", Finalize: "整理结果" }
  return labels[node.title] ?? (node.title || node.nodeId)
}

export function nodeDescription(node: PlanNodeProjection): string {
  const descriptions: Record<string, string> = {
    "Decompose the user goal into an executable task graph.": "将目标拆分为可执行的步骤。",
    "Select the worker and control route for the executable node.": "选择适合当前步骤的执行资源。",
    "Run the current node through the selected worker runtime.": "执行当前步骤并记录结果。",
    "Check node outputs, event coverage, and checkpoint readiness.": "检查步骤结果与恢复状态。",
    "Finalize the trace and mark the task ready for inspection.": "整理执行记录和交付结果。",
  }
  return descriptions[node.description] ?? node.description
}

export function nodeStateLabel(status: string): string {
  const normalized = status.toLowerCase()
  if (COMPLETED_NODE_STATES.has(normalized)) return "已完成"
  if (RUNNING_NODE_STATES.has(normalized)) return "进行中"
  if (["cancelled", "canceled"].includes(normalized)) return "已停止"
  if (FAILED_NODE_STATES.has(normalized)) return "未完成"
  return "待执行"
}

export function phaseLabel(value: string): string {
  return ({ online: "已连接", loading: "加载中", binding: "连接中", responding: "提交中", stale: "待更新", missing: "暂无记录", default: "默认", ready: "已连接", connecting: "连接中", restoring: "恢复中", live: "已连接", idle: "待命", disconnected: "未连接", degraded: "连接不稳定", created: "已创建", admitted: "已就绪", running: "运行中", completed: "已完成", succeeded: "已完成", failed: "失败", cancelled: "已停止", archived: "已归档", verified: "已验证", paused: "已暂停", blocked: "等待处理", needs_revision: "需要修改", awaiting_approval: "等待许可" } as Record<string, string>)[value] ?? value
}
