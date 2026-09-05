import { eventLabel } from "./model.ts"

/** Translate known display values while preserving unknown backend values verbatim. */
export function recordLabel(value: string | undefined): string {
  if (!value) return "—"
  return labels[value] ?? value
}

export function recordTitle(value: string): string {
  if (value.startsWith("runtime.")) return eventLabel({ eventType: value })
  if (value.startsWith("Mutation ")) return "数据变更"
  if (value.startsWith("Runtime event payload:")) return "原始事件内容"
  if (value.startsWith("Run run_")) return "运行记录"
  if (value.startsWith("task task_")) return "任务记录"
  return value
}

export function recordSummary(value: string): string {
  if (/^Legacy .+ event normalized into the runtime event spine\.$/.test(value)) return "已记录，展开可查看原文和关联标识。"
  if (value.startsWith("Canonical mutation identity ")) return "任务状态的一次变更，展开可查看完整来源。"
  return value
}

const labels: Record<string, string> = {
  live: "已同步", connected: "已连接", disconnected: "未连接", offline: "离线", online: "在线",
  loading: "加载中", ready: "就绪", idle: "待命", disabled: "不可用", degraded: "连接异常",
  running: "执行中", active: "运行中", completed: "已完成", failed: "失败", starting: "启动中",
  admitted: "已受理", unknown: "未确认", unavailable: "暂无记录", none: "无", nominal: "正常",
  allow: "允许", deny: "拒绝", approved: "已允许", rejected: "已拒绝", pending: "待处理",
  applied: "已执行", queued: "排队中", sealed: "全自主", interactive: "交互模式",
  complete: "完整", partial: "不完整", missing: "缺失", verified: "校验通过", unverified: "待校验",
  internal: "内部", public: "公开", private: "私有", compliant: "符合要求", violation: "不符合要求",
  unplaced: "未指定", device: "设备端", edge: "边缘端", cloud: "云端", eligible: "可选",
  working: "工作记忆", episodic: "经历记忆", semantic: "语义记忆", skill: "技能记忆",
  "System and policy": "系统与规则", Conversation: "对话", "Tools and results": "工具与结果",
  Memory: "记忆", "Artifact references": "产物引用", "Auto-compact reserve": "自动压缩预留",
  Skills: "技能数量", Available: "可用", Blocked: "受限", "Active invocations": "进行中的调用",
  "Projection revision": "记录版本", "select a canonical skill": "选择技能后可查看可用操作",
  "live-event-stream": "实时事件", "All nodes complete": "所有节点已完成",
  "Causal identities": "关联标识", "Causal events": "关联事件", Details: "结果详情",
  "Runtime status": "运行状态", "Current task status.": "当前任务状态。",
  "explicit_allow_rule": "命中允许规则", "explicit_deny_rule": "命中拒绝规则",
  event: "事件", mutation: "变更", artifact: "产物", task: "任务", node: "节点", run: "运行",
  topology: "关系", worker: "执行者", tool: "工具", command: "命令", permission: "权限",
  placement: "执行位置", fault: "故障", recovery: "恢复", subagent: "子代理", critical: "关键路径",
  "effective step": "有效步骤", terminal: "已结束", lease: "执行租约", default: "默认模式",
}
