import { buildCommandResultModel, type CommandReceipt } from "@zyra/commands"
import { sanitizeForOutput } from "../../output.ts"

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
const array = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(object) : []
const text = (value: unknown): string => typeof value === "string" ? String(sanitizeForOutput(value)).slice(0, 600) : ""
const count = (value: unknown): string => typeof value === "number" && Number.isFinite(value) ? String(value) : "未提供"
const layers = (value: unknown): string => {
  const data = object(value)
  return [["working", "工作记忆"], ["episodic", "执行经历"], ["semantic", "知识记忆"], ["skill", "技能记忆"]]
    .map(([key, label]) => `${label} ${count(data[key!])}`).join(" · ")
}

export function commandResultLines(receipt: CommandReceipt): string[] {
  if (receipt.error) return [`操作未完成 · ${receipt.error.message}`, `错误代码 · ${receipt.error.code}`]
  if (receipt.phase !== "applied") return [
    receipt.phase === "running" || receipt.phase === "queued" || receipt.phase === "received" ? "操作仍在处理中。" : "操作未完成。",
    receipt.summary || receipt.displayText || "后端尚未提供可显示的结果。请稍后重试或查看 /status。",
  ]
  const data = receipt.data
  if (receipt.name === "/context") return [
    "当前任务的上下文记录", "",
    `可见记录 · ${count(data.visible_events)} / ${count(data.events)}`,
    `历史快照 · ${count(data.snapshot_count)}`,
    `计划步骤 · ${count(data.plan_nodes)} · 交付物 ${count(data.artifacts)}`,
    `记忆记录 · ${count(object(data.memory).record_count)}`,
    layers(object(data.memory).layer_counts), "",
    "以上是记录数量；当前接口未提供 token 用量。",
  ]
  if (receipt.name === "/memory") {
    const lines = [text(receipt.summary) || "任务记忆", layers(data.layer_counts)]
    const results = array(data.search_results)
    const sections: Array<[string, Record<string, unknown>[]]> = results.length
      ? [["查询结果", results]]
      : [["当前目标", array(data.working).slice(0, 1)], ["知识记忆", array(data.semantic)], ["最近经历", array(data.episodic)], ["技能记忆", array(data.skill)]]
    for (const [label, records] of sections) {
      if (!records.length) continue
      lines.push("", label)
      for (const record of records.slice(0, 8)) {
        const body = object(record.content)
        lines.push(`• ${text(label === "当前目标" ? body.user_goal : record.summary) || "未提供摘要"}`)
      }
      if (records.length > 8) lines.push(`… 还有 ${records.length - 8} 条；使用 /memory <关键词> 缩小范围。`)
    }
    if (!sections.some(([, records]) => records.length)) lines.push("", "当前没有匹配的记忆记录。")
    lines.push("", `压缩记录 · ${Array.isArray(data.compactions) ? data.compactions.length : 0}`)
    return lines
  }
  if (receipt.name === "/skills") {
    const revisions = array(object(data.registry).revisions)
    const active = object(data.registry).activeSkills
    const skills = Array.isArray(active) ? array(active) : revisions.length ? array(revisions.at(-1)?.skills) : array(data.skills)
    if (!skills.length) return ["当前没有可用技能。"]
    return [`可用技能 · ${skills.length}`, "", ...skills.slice(0, 40).flatMap((skill) => [
      `• ${text(skill.displayName ?? skill.name ?? skill.id) || "未命名技能"}${skill.availability && skill.availability !== "available" ? ` · ${text(skill.availability)}` : ""}`,
      `  ${text(skill.description ?? skill.purpose) || "暂无说明"}`,
    ])]
  }
  if (receipt.name === "/mcp" && (Object.keys(object(data.health)).length || typeof data.connected_servers === "number")) {
    const health = Object.keys(object(data.health)).length ? object(data.health) : data
    return [
      `MCP 服务 · ${health.opened === true ? "已就绪" : "未就绪"}`, "",
      `已连接 · ${count(health.connected_servers)}`,
      `等待请求 · ${count(health.pending_requests)}`,
      `正在恢复 · ${count(health.session_reconciling_servers)}`,
      ...(health.connected_servers === 0 ? ["", "当前没有连接 MCP 服务器；内置工具仍可使用。"] : []),
      "", "输入 /mcp servers 查看服务器，/mcp tools 查看工具。",
    ]
  }
  const result = buildCommandResultModel(receipt, { maximumRows: 60, maximumValueLength: 600 })
  const lines = [result.summary || result.displayText || "操作已完成。"]
  if (result.usage) lines.push(`用量 · 输入 ${result.usage.inputTokens} · 输出 ${result.usage.outputTokens}${result.usage.cachedTokens ? ` · 缓存 ${result.usage.cachedTokens}` : ""}`)
  for (const section of result.sections) {
    lines.push("", section.title === "Details" ? "详细信息" : section.title)
    for (const row of section.rows) {
      const marker = row.tone === "error" ? "!" : row.tone === "success" ? "✓" : "•"
      lines.push(`${marker} ${row.title}${row.summary && row.summary !== row.title ? ` · ${row.summary}` : ""}`)
      if (row.kind !== "property") for (const field of row.fields) {
        if (field.value !== row.summary && field.value !== row.title) lines.push(`  ${field.label} · ${field.value}`)
      }
    }
  }
  return lines
}
