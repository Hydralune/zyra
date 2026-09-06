import { expect, test } from "bun:test"
import type { CommandReceipt } from "@zyra/commands"
import { commandResultLines } from "../src/product/commands/results.ts"
import { verificationLines } from "../src/commands/product.ts"
import { ProductSessionState } from "../src/product/state/session-state.ts"
import { ZYRA_UI_EVENT_SCHEMA } from "../src/presentation/events.ts"

test("command panels summarize memory without exposing internal record bodies", () => {
  const receipt = { name: "/memory", phase: "applied", summary: "任务记忆：共 1 条记录。", data: {
    layer_counts: { working: 0, episodic: 1, semantic: 0, skill: 0 },
    episodic: [{ summary: "运行测试通过", content: { payload_preview: { credential: "must-not-render" } } }],
  } } as unknown as CommandReceipt
  const lines = commandResultLines(receipt)
  expect(lines.join("\n")).toContain("运行测试通过")
  expect(lines.join("\n")).not.toContain("payload_preview")
  expect(lines.join("\n")).not.toContain("must-not-render")
  expect(lines.length).toBeLessThan(15)
  expect(commandResultLines({ ...receipt, phase: "rejected", error: { code: "bad_argument", message: "参数不支持" } } as CommandReceipt).join("\n")).toContain("参数不支持")
})

test("verification puts actual failed and passed commands before optional internal checks", () => {
  const state = new ProductSessionState()
  state.apply({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "verify", type: "verification.updated", verification: {
    status: "passed", label: "最终验证通过", details: [], commandEvidence: "recorded",
    checks: [
      { source: "final_verifier", name: "internal_evidence_digest_valid", status: "passed" },
      { source: "command", name: "unit tests", command: "python -m unittest", status: "failed", exitCode: 1 },
      { source: "command", name: "unit tests", command: "python -m unittest", status: "passed", exitCode: 0 },
    ],
  } })
  const lines = verificationLines(state.snapshot()).join("\n")
  expect(lines).toContain("失败 · 退出码 1")
  expect(lines).toContain("通过 · 退出码 0")
  expect(lines).toContain("系统校验 · 1/1 项通过")
  expect(lines).not.toContain("internal_evidence_digest_valid")
  expect(verificationLines(state.snapshot(), true).join("\n")).toContain("internal_evidence_digest_valid")
})

test("a follow-up retains conversation but resets the previous task's progress and permissions", () => {
  const state = new ProductSessionState()
  state.apply({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "one", type: "session.started", sessionId: "session", taskId: "task_one" })
  state.apply({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "answer", type: "assistant.message.completed", messageId: "answer", text: "上一轮回答", source: "canonical_final_answer" })
  state.apply({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "agent", type: "subagent.updated", agentId: "old_agent", label: "旧代理", status: "running" })
  state.apply({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "two", type: "session.started", sessionId: "session", taskId: "task_two" })
  expect(state.snapshot().agents).toHaveLength(0)
  expect(state.snapshot().verification).toBeUndefined()
  expect(state.snapshot().messages[0]?.text).toBe("上一轮回答")
  expect(state.snapshot().taskId).toBe("task_two")
})
