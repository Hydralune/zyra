import { describe, expect, test } from "bun:test"
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../src/api.ts"
import { ZYRA_UI_EVENT_SCHEMA } from "../src/presentation/events.ts"
import { projectProductEvents } from "../src/presentation/projector.ts"
import { reduceProductEvents, renderProductSnapshot } from "../src/presentation/renderer.ts"
import { buildBoundedWorkspaceDiff } from "../src/presentation/workspace-diff.ts"

function task(status: string, metadata: Readonly<Record<string, unknown>>): TaskProjection {
  return {
    taskId: "task_result",
    runId: "run_result",
    sessionId: "session_result",
    rootNodeId: "node_root",
    userGoal: "修改文件并验证",
    status,
    createdAt: "2026-09-01T00:00:00.000Z",
    updatedAt: "2026-09-01T00:00:02.000Z",
    planNodes: [],
    artifacts: [],
    metadata,
    binding: { taskId: "task_result", runId: "run_result", sessionId: "session_result" },
    terminal: true,
    active: false,
  } as TaskProjection
}

function frame(sequence: number, eventType: string, inline: Readonly<Record<string, unknown>>): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation: 1,
    taskId: "task_result",
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_result_${sequence}`,
    eventType,
    event: {
      schema: "zyra.runtime-event/v1",
      eventType,
      identity: { taskId: "task_result", runId: "run_result", toolCallId: "tool_result" },
      inline,
      artifactRefs: [],
    },
    raw: {},
  }
}

describe("product file, verification, and failure results", () => {
  test("builds a readable, bounded diff only from canonical delivery paths", async () => {
    const root = await mkdtemp(join(tmpdir(), "zyra-product-diff-"))
    try {
      await mkdir(join(root, "src"), { recursive: true })
      await writeFile(join(root, "src", "created.ts"), "export const answer = 42\n", "utf8")
      await writeFile(join(root, "outside.txt"), "must not be selected\n", "utf8")
      const selected = task("completed", {
        delivery: {
          schema: "zyra.task-workspace-delivery/v1",
          changed_paths: ["src/created.ts", "../outside.txt"],
          created_paths: ["src/created.ts", "../outside.txt"],
          modified_paths: [],
          deleted_paths: [],
        },
      })
      const event = await buildBoundedWorkspaceDiff(selected, root)
      expect(event).toMatchObject({ type: "workspace.diff", source: "local_workspace" })
      if (!event || event.type !== "workspace.diff") throw new Error("workspace diff expected")
      expect(event.lines.join("\n")).toContain("+++ b/src/created.ts")
      expect(event.lines.join("\n")).toContain("+export const answer = 42")
      expect(event.lines.join("\n")).not.toContain("must not be selected")
      expect(event.lines.length).toBeLessThanOrEqual(120)
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })

  test("shows tool failure, changed files, failed verification, and actionable task failure", () => {
    const failed = task("failed", {
      failure_reason: "测试失败，修改未通过验收。",
      delivery: {
        schema: "zyra.task-workspace-delivery/v1",
        changed_paths: ["src/example.ts"],
        modified_paths: ["src/example.ts"],
        created_paths: [],
        deleted_paths: [],
      },
      canonical_task_outcome: {
        schema: "zyra.task-outcome/v1",
        verification: {
          final_verifier: {
            passed: false,
            checks: [
              { name: "artifact_integrity", status: "passed", passed: true },
              { name: "required_tests", status: "failed", passed: false },
            ],
          },
          delivery_verifier: {
            passed: false,
            checks: [{ name: "required_paths_present", status: "passed", passed: true }],
          },
          completion_gate: { hard_conditions_passed: false, failed_conditions: ["unit tests"] },
          command_evidence: { status: "not_recorded", receipts: [] },
        },
      },
    })
    const events = projectProductEvents({
      task: failed,
      frames: [
        frame(1, "runtime.tool.called", { tool_name: "测试" }),
        frame(2, "runtime.tool.failed", { tool_name: "测试", error_code: "exit_1" }),
      ],
      permissions: [{ requestId: "permission_expired", status: "expired", prompt: "运行命令", selectable: false }],
    })
    const state = reduceProductEvents(events)
    const rendered = renderProductSnapshot(events, { width: 100, workspace: "workspace" })

    expect(state.permissions).toHaveLength(0)
    expect(events).toContainEqual(expect.objectContaining({ type: "permission.resolved", decision: "expired" }))
    expect(state.tools).toContainEqual(expect.objectContaining({ status: "failed", summary: "测试 执行失败（exit_1）" }))
    expect(state.verification?.status).toBe("failed")
    expect(state.verification?.details).toEqual(expect.arrayContaining(["unit tests", "final_verifier: required_tests"]))
    expect(state.verification?.checks).toEqual([
      { source: "final_verifier", name: "artifact_integrity", status: "passed", summary: undefined },
      { source: "final_verifier", name: "required_tests", status: "failed", summary: undefined },
      { source: "delivery_verifier", name: "required_paths_present", status: "passed", summary: undefined },
      { source: "completion_gate", name: "unit tests", status: "failed" },
    ])
    expect(state.verification?.commandEvidence).toBe("not_recorded")
    expect(state.changes).toContainEqual(expect.objectContaining({ path: "src/example.ts", kind: "modified" }))
    expect(rendered).toContain("最终验证未通过")
    expect(rendered).toContain("测试失败，修改未通过验收")
    expect(rendered).toContain(`zyra resume ${failed.taskId}`)
    expect(rendered).not.toContain("runtime.")
  })

  test("preserves canonical command, status, and exit code receipts", () => {
    const completed = task("completed", {
      canonical_task_outcome: {
        schema: "zyra.task-outcome/v1",
        verification: {
          final_verifier: { passed: true, checks: [] },
          delivery_verifier: { passed: true, checks: [] },
          completion_gate: { hard_conditions_passed: true, failed_conditions: [] },
          command_evidence: {
            schema: "zyra.verification-command-evidence/v1",
            status: "recorded",
            receipts: [
              {
                schema: "zyra.verification-command-receipt/v1",
                label: "unit tests",
                command: "python -m pytest tests/unit",
                status: "passed",
                exit_code: 0,
              },
              { command: "", status: "passed" },
            ],
          },
        },
      },
    })

    const state = reduceProductEvents(projectProductEvents({ task: completed }))

    expect(state.verification?.status).toBe("passed")
    expect(state.verification?.commandEvidence).toBe("recorded")
    expect(state.verification?.checks).toContainEqual({
      source: "command",
      name: "unit tests",
      command: "python -m pytest tests/unit",
      status: "passed",
      summary: undefined,
      exitCode: 0,
    })
  })

  test("renders permission risk, expiry, and an unambiguous request identity", () => {
    const running = { ...task("completed", {}), status: "running", terminal: false, active: true }
    const events = projectProductEvents({
      task: running,
      permissions: [{
        requestId: "permission_visible",
        status: "delivered",
        prompt: "写入受保护文件",
        reason: "保存用户要求的修改",
        risk: "medium",
        scope: "once",
        expiresAt: "2099-01-01T00:00:00.000Z",
        selectable: true,
      }],
    })
    const rendered = renderProductSnapshot(events, { width: 100, workspace: "workspace" })
    expect(rendered).toContain("风险：medium")
    expect(rendered).toContain("作用域：once")
    expect(rendered).toContain("有效期至：2099-01-01")
    expect(rendered).toContain("permission_visible")
    expect(rendered).toContain("[A]+Enter 允许本次")
    const pressured = renderProductSnapshot([
      ...events,
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "diff-pressure", type: "workspace.diff", lines: Array.from({ length: 200 }, (_, index) => `+line ${index}`), truncated: true, source: "local_workspace" },
    ], { width: 100, height: 24, workspace: "workspace" })
    expect(pressured).toContain("权限请求 1/1")
    expect(pressured).toContain("写入受保护文件")
    expect(pressured).toContain("[D]+Enter 拒绝")
  })
})
