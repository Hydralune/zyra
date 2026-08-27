import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, test } from "bun:test"
import type { TaskProjection } from "@zyra/typed-api-client"
import { CliApi } from "../src/api.ts"
import { materializeWorkspaceDelivery, stageWorkspace } from "../src/workspace-transfer.ts"

function task(delivery: Readonly<Record<string, unknown>> = {}): TaskProjection {
  return {
    taskId: "task_workspace_transfer",
    runId: "run_workspace_transfer",
    rootNodeId: "node_workspace_transfer",
    userGoal: "Transfer a bounded workspace.",
    status: "completed",
    createdAt: "2026-08-27T00:00:00.000Z",
    updatedAt: "2026-08-27T00:00:01.000Z",
    planNodes: [],
    artifacts: [],
    metadata: {
      workspace_ref: { workspace_id: "ws_workspace_transfer" },
      delivery,
    },
    binding: { taskId: "task_workspace_transfer", runId: "run_workspace_transfer" },
    terminal: true,
    active: false,
  }
}

describe("CLI managed-workspace transfer", () => {
  test("stages regular workspace files while excluding credentials and dependency trees", async () => {
    const root = await mkdtemp(join(tmpdir(), "zyra-cli-stage-"))
    try {
      await mkdir(join(root, "inputs"), { recursive: true })
      await mkdir(join(root, "node_modules", "ignored"), { recursive: true })
      await writeFile(join(root, "TASK.md"), "task")
      await writeFile(join(root, "inputs", "data.json"), '{"ok":true}')
      await writeFile(join(root, ".env.deepseek.local"), "DEEPSEEK_API_KEY=never-stage")
      await writeFile(join(root, "node_modules", "ignored", "index.js"), "ignored")
      const writes: Array<{ path: string; content: string }> = []
      const api = {
        async writeWorkspaceFile(_workspaceId: string, path: string, content: Uint8Array) {
          writes.push({ path, content: Buffer.from(content).toString("utf8") })
          return {}
        },
      } as unknown as CliApi

      const report = await stageWorkspace(api, task(), root, new AbortController().signal)

      expect(report.paths).toEqual(["inputs/data.json", "TASK.md"])
      expect(writes).toEqual([
        { path: "inputs/data.json", content: '{"ok":true}' },
        { path: "TASK.md", content: "task" },
      ])
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })

  test("materializes only canonical changed paths under the startup root", async () => {
    const root = await mkdtemp(join(tmpdir(), "zyra-cli-materialize-"))
    try {
      const api = {
        async readWorkspaceFile(_workspaceId: string, path: string) {
          return Buffer.from(`content:${path}`, "utf8")
        },
      } as unknown as CliApi
      const selected = task({
        schema: "zyra.task-workspace-delivery/v1",
        workspace_id: "ws_workspace_transfer",
        changed_paths: ["deliverables/report.md", "deliverables/result.json"],
        deleted_paths: [],
      })

      const report = await materializeWorkspaceDelivery(
        api,
        selected,
        root,
        new AbortController().signal,
      )

      expect(report.paths).toEqual(["deliverables/report.md", "deliverables/result.json"])
      expect(await readFile(join(root, "deliverables", "report.md"), "utf8"))
        .toBe("content:deliverables/report.md")
      expect(await readFile(join(root, "deliverables", "result.json"), "utf8"))
        .toBe("content:deliverables/result.json")
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })

  test("rejects a delivery path that escapes the startup root", async () => {
    const root = await mkdtemp(join(tmpdir(), "zyra-cli-escape-"))
    try {
      const api = {
        async readWorkspaceFile() { return Buffer.from("no") },
      } as unknown as CliApi
      const selected = task({
        schema: "zyra.task-workspace-delivery/v1",
        workspace_id: "ws_workspace_transfer",
        changed_paths: ["../outside.txt"],
        deleted_paths: [],
      })

      await expect(materializeWorkspaceDelivery(
        api,
        selected,
        root,
        new AbortController().signal,
      )).rejects.toMatchObject({ code: "contract_workspace_path_invalid" })
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })
})
