import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import type { ProductViewState } from "../src/product/state/session-state.ts"
import {
  copyLatestAssistantMessage,
  exportProductTranscript,
  rawTranscriptLines,
} from "../src/product/transcript/export.ts"

const temporaryDirectories: string[] = []

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true, force: true })))
})

async function directory(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), "zyra-transcript-export-"))
  temporaryDirectories.push(path)
  return path
}

function view(messages: ProductViewState["messages"], evicted = 0): ProductViewState {
  return {
    sessionId: "session_1",
    taskId: "task_1",
    messages,
    activities: [],
    tools: [],
    agents: [],
    issues: [],
    permissions: [],
    changes: [],
    connection: "connected",
    taskStatus: "completed",
    evicted: { messages: evicted, activities: 0, tools: 0, agents: 0, issues: 0, changes: 0 },
  }
}

describe("product transcript export", () => {
  test("copies the exact latest assistant response through an injected clipboard writer", async () => {
    let clipboard = ""
    const result = await copyLatestAssistantMessage(view([
      { messageId: "user_1", role: "user", text: "问题", streaming: false },
      { messageId: "assistant_1", role: "assistant", text: "第一条", streaming: false },
      { messageId: "assistant_2", role: "assistant", text: "最终回答 👨‍👩‍👧‍👦", streaming: false },
    ]), async (text) => { clipboard = text })

    expect(clipboard).toBe("最终回答 👨‍👩‍👧‍👦")
    expect(result.messageId).toBe("assistant_2")
    expect(result.byteCount).toBeGreaterThan(clipboard.length)
  })

  test("refuses an oversized clipboard payload instead of silently truncating it", async () => {
    const oversized = "x".repeat(4 * 1024 * 1024 + 1)
    await expect(copyLatestAssistantMessage(view([
      { messageId: "assistant_large", role: "assistant", text: oversized, streaming: false },
    ]), async () => undefined)).rejects.toMatchObject({ code: "clipboard_message_too_large" })
  })

  test("exports retained messages inside the workspace without overwriting", async () => {
    const workspace = await directory()
    const source = view([
      { messageId: "user_1", role: "user", text: "请修复表格", streaming: false },
      { messageId: "assistant_1", role: "assistant", text: "| A | B |\n|---|---|\n| 1 | 2 |", streaming: false },
    ], 3)
    const result = await exportProductTranscript({ view: source, workspace, requestedPath: "handoff.md" })
    const material = await readFile(result.path, "utf8")

    expect(result.path).toBe(join(workspace, "handoff.md"))
    expect(result.messageCount).toBe(2)
    expect(result.truncated).toBe(false)
    expect(material).toContain("Evicted messages: 3")
    expect(material).toContain("## Assistant")
    expect(material).toContain("| A | B |")
    await expect(exportProductTranscript({ view: source, workspace, requestedPath: "handoff.md" }))
      .rejects.toMatchObject({ code: "transcript_export_exists" })
  })

  test("rejects traversal and existing symlink-like targets before writing", async () => {
    const workspace = await directory()
    const outside = resolve(workspace, "..", "outside.md")
    await expect(exportProductTranscript({ view: view([]), workspace, requestedPath: outside }))
      .rejects.toMatchObject({ code: "transcript_export_path_outside_workspace" })
    await writeFile(join(workspace, "existing.md"), "keep", "utf8")
    await expect(exportProductTranscript({ view: view([]), workspace, requestedPath: "existing.md" }))
      .rejects.toMatchObject({ code: "transcript_export_exists" })
    expect(await readFile(join(workspace, "existing.md"), "utf8")).toBe("keep")
  })

  test("renders a copy-friendly bounded raw view with explicit role and eviction markers", () => {
    const lines = rawTranscriptLines(view([
      { messageId: "user_1", role: "user", text: "第一行\n第二行", streaming: false },
      { messageId: "assistant_1", role: "assistant", text: "回答", streaming: false },
    ], 2))
    expect(lines.join("\n")).toContain("[2 earlier messages evicted from local view]")
    expect(lines.join("\n")).toContain("user> 第一行\n第二行")
    expect(lines.join("\n")).toContain("assistant> 回答")
  })
})
