import { mkdir, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../../apps/cli/src/presentation/events.ts"
import { reduceProductEvents, renderProductFrame, type ProductRenderOptions } from "../../apps/cli/src/presentation/renderer.ts"
import type { ProductOverlay } from "../../apps/cli/src/tui/overlay/model.ts"

const root = resolve(import.meta.dir, "..", "..")
const output = resolve(root, "docs", "product-tui", "reference-frames", "zyra")
const workspace = "G:\\agent-zoo\\zyra"

const session: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "session",
  type: "session.started",
  sessionId: "session_reference",
  taskId: "task_reference",
}

const user: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "user",
  type: "user.message",
  messageId: "message_user",
  text: "检查当前项目，修复测试失败并说明改动。",
}

const context: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "context",
  type: "context.updated",
  context: {
    activeChars: 38_000,
    activeLimitChars: 100_000,
    usedPercent: 38,
    remainingPercent: 62,
    compactNeeded: false,
    inputTokens: 9_500,
    outputTokens: 2_100,
  },
}

const plan: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "plan",
  type: "plan.updated",
  plan: {
    schema: "zyra.ui-plan/v1",
    revision: 2,
    revisionSource: "canonical_graph",
    changes: [],
    steps: [
      { stepId: "one", label: "定位失败测试", status: "completed", dependsOn: [] },
      { stepId: "two", label: "修复会话状态更新", status: "running", dependsOn: ["one"] },
      { stepId: "three", label: "运行验证并总结", status: "pending", dependsOn: ["two"] },
    ],
  },
}

const running: readonly ZyraUiEvent[] = [session, user, context, plan, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "tool",
  type: "tool.started",
  toolCallId: "tool_reference",
  name: "shell",
  summary: "bun test apps/cli/test/product-presentation.test.ts",
  durationMs: 4_200,
  outputRefs: [{ artifactId: "artifact_output", stream: "stdout", title: "stdout", mediaType: "text/plain", sizeBytes: 12_480 }],
}]

const completedPlan: ZyraUiEvent = {
  ...plan,
  eventId: "plan-completed",
  plan: {
    ...plan.plan,
    revision: 3,
    steps: plan.plan.steps.map((step) => ({ ...step, status: "completed" as const })),
  },
}

const permission: readonly ZyraUiEvent[] = [...running, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "permission",
  type: "permission.requested",
  request: {
    requestId: "request_reference",
    action: "运行项目测试",
    target: "bun test apps/cli/test",
    reason: "确认修复没有引入回归",
    risk: "中",
    scope: "当前工作区",
    decisions: ["allow", "deny"],
  },
}]

const completed: readonly ZyraUiEvent[] = [session, user, context, completedPlan, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "tool-completed",
  type: "tool.completed",
  toolCallId: "tool_reference",
  name: "shell",
  summary: "测试通过：187 passed",
  durationMs: 8_420,
}, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "workspace",
  type: "workspace.changed",
  changes: [
    { path: "apps/cli/src/tui/shell.ts", kind: "modified" },
    { path: "apps/cli/src/presentation/renderer.ts", kind: "modified" },
  ],
}, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "verification",
  type: "verification.updated",
  verification: {
    status: "passed",
    label: "验证通过",
    details: [],
    commandEvidence: "recorded",
    checks: [{ source: "command", name: "CLI tests", status: "passed", command: "bun test apps/cli/test", exitCode: 0 }],
  },
}, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "assistant",
  type: "assistant.message.completed",
  messageId: "message_assistant",
  source: "canonical_final_answer",
  text: "已修复会话状态更新。\n\n- 保留了草稿与光标\n- 全量 CLI 测试通过",
}, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "completed",
  type: "task.completed",
  taskId: "task_reference",
  finalAnswer: "已修复会话状态更新。",
}]

const failed: readonly ZyraUiEvent[] = [session, user, context, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "tool-failed",
  type: "tool.failed",
  toolCallId: "tool_reference",
  name: "shell",
  message: "测试仍有 2 项失败",
  impact: "task",
  retryable: true,
  recovery: "查看失败详情，修复后重新运行验证。",
}, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "failed",
  type: "task.failed",
  taskId: "task_reference",
  message: "任务未能完成。",
  recovery: "使用 /resume 恢复会话，或输入新的修复要求。",
}]

const permissionOverlay: ProductOverlay = {
  kind: "approval",
  title: "允许 Zyra 运行这个命令吗？",
  description: ["bun test apps/cli/test", "用于确认刚才的代码修改没有引入回归。"],
  rows: [
    { id: "allow:once", label: "允许本次", detail: "只运行这一个命令" },
    { id: "allow:session", label: "本会话允许", detail: "后续相同操作不再询问" },
    { id: "deny:once", label: "拒绝", detail: "不运行命令并让任务继续" },
  ],
  selected: 0,
  footer: "Enter 确认 · Esc 拒绝",
}

const completionOverlay: ProductOverlay = {
  kind: "completion",
  title: "命令",
  rows: [
    { id: "/status", label: "/status", detail: "查看当前会话、模型和上下文" },
    { id: "/stop", label: "/stop", detail: "中断当前任务" },
  ],
  selected: 0,
  footer: "↑↓ 选择 · Tab/Enter 接受",
}

const questionEvents: readonly ZyraUiEvent[] = [...running, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "user-input",
  type: "user_input.requested",
  request: {
    requestId: "request_user_input_reference",
    status: "pending",
    revision: 0,
    questions: [{
      id: "database",
      header: "数据库",
      question: "这个服务应该采用哪种数据库？",
      options: [
        { label: "SQLite", description: "保持单机部署和零配置。" },
        { label: "PostgreSQL", description: "支持共享服务和并发写入。" },
      ],
    }],
  },
}]

const questionOverlay: ProductOverlay = {
  kind: "question",
  title: "数据库",
  description: ["这个服务应该采用哪种数据库？"],
  rows: [
    { id: "SQLite", label: "SQLite", detail: "保持单机部署和零配置。" },
    { id: "PostgreSQL", label: "PostgreSQL", detail: "支持共享服务和并发写入。" },
    { id: "__freeform", label: "其他", detail: "输入不同的回答" },
  ],
  selected: 0,
  footer: "↑↓ 选择 · Enter 回答 · Esc 稍后处理",
}

const answeredQuestionEvents: readonly ZyraUiEvent[] = [...running, {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "user-input-resolved",
  type: "user_input.resolved",
  request: {
    requestId: "request_user_input_reference",
    status: "answered",
    revision: 1,
    questions: questionEvents.at(-1)!.type === "user_input.requested"
      ? questionEvents.at(-1)!.request.questions
      : [],
    answers: { database: { answers: ["PostgreSQL"] } },
  },
}]

const diffOverlay: ProductOverlay = {
  kind: "pager",
  title: "apps/cli/src/tui/shell.ts · +4 -2",
  rows: [
    { id: "0", label: "--- a/apps/cli/src/tui/shell.ts", tone: "secondary" },
    { id: "1", label: "+++ b/apps/cli/src/tui/shell.ts", tone: "secondary" },
    { id: "2", label: "@@ -112,6 +112,8 @@", tone: "accent" },
    { id: "3", label: "   this.#acceptingInput = true" },
    { id: "4", label: "+  this.#showCurrentActivity = true", tone: "success" },
    { id: "5", label: "+  this.#renderer.renderNow()", tone: "success" },
    { id: "6", label: "-  this.#notice = message", tone: "error" },
    { id: "7", label: "   return await this.#composer.read()" },
  ],
  selected: -1,
  footer: "1–8 / 8 · ↑↓/PgUp/PgDn · Esc 返回",
}

const resumeOverlay: ProductOverlay = {
  kind: "picker",
  title: "恢复会话",
  query: "",
  rows: [
    { id: "one", label: "修复 CLI 会话状态", detail: "运行中 · 刚刚 · G:\\agent-zoo\\zyra" },
    { id: "two", label: "审查权限控制实现", detail: "已完成 · 2 小时前 · G:\\agent-zoo\\zyra" },
    { id: "three", label: "补齐 Web 看板文档", detail: "失败 · 昨天 · G:\\agent-zoo\\zyra" },
  ],
  selected: 0,
  footer: "输入筛选 · ↑↓ 选择 · Enter 恢复 · Esc 返回",
}

const scenarios: ReadonlyArray<{
  name: string
  events: readonly ZyraUiEvent[]
  options?: Partial<ProductRenderOptions>
}> = [
  { name: "cold-start", events: [] },
  { name: "composer-input", events: [], options: { composerText: "请检查 @apps/cli/src/tui/shell.ts", composerCursor: 34 } },
  { name: "command-completion", events: [], options: { composerText: "/st", composerCursor: 3, overlay: completionOverlay } },
  { name: "running-tool", events: running, options: { running: true } },
  { name: "plan-update", events: [session, user, context, plan], options: { running: true } },
  { name: "permission-summary", events: permission, options: { running: true } },
  { name: "permission-approval", events: permission, options: { running: true, overlay: permissionOverlay } },
  { name: "structured-question", events: questionEvents, options: { running: true, overlay: questionOverlay } },
  { name: "structured-question-answered", events: answeredQuestionEvents, options: { running: true } },
  { name: "diff", events: completed, options: { overlay: diffOverlay } },
  { name: "completed", events: completed },
  { name: "failed-recovery", events: failed },
  { name: "resume-picker", events: [], options: { overlay: resumeOverlay } },
]

await mkdir(output, { recursive: true })
for (const [width, height] of [[80, 24], [120, 40]] as const) {
  for (const scenario of scenarios) {
    const frame = renderProductFrame(reduceProductEvents(scenario.events), {
      width,
      height,
      workspace,
      acceptingInput: true,
      chrome: { model: "deepseek-v4", mode: "标准" },
      color: false,
      ...scenario.options,
    })
    await writeFile(resolve(output, `${scenario.name}-${width}x${height}.txt`), frame.text, "utf8")
  }
}

process.stdout.write(`Generated ${scenarios.length * 2} product frames in ${output}\n`)
