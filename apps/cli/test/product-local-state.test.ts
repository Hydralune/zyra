import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PromptDraft } from "../src/input/draft.ts"
import { ProductDraftStore } from "../src/product/session/local-state.ts"

const temporaryDirectories: string[] = []

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true, force: true })))
})

async function directory(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), "zyra-product-state-"))
  temporaryDirectories.push(path)
  return path
}

describe("product draft crash recovery", () => {
  test("atomically restores the latest bounded draft without persisting the workspace path", async () => {
    const stateDirectory = await directory()
    const workspace = "G:\\sensitive-workspace\\project"
    const store = ProductDraftStore.open({ workspace, stateDirectory })
    store.schedule({ text: "未提交的中文 draft 👨‍👩‍👧‍👦", cursor: 8 })
    await store.flush()
    store.schedule({ text: "latest draft", cursor: 6 })
    await store.flush()

    const material = await readFile(store.path, "utf8")
    expect(material).not.toContain("sensitive-workspace")
    expect(JSON.parse(material)).toMatchObject({ schema: "zyra.product-draft/v1", revision: 2 })
    expect(ProductDraftStore.open({ workspace, stateDirectory }).restored).toEqual({ text: "latest draft", cursor: 6 })
  })

  test("migrates v0 on the next save and isolates unknown or damaged state", async () => {
    const stateDirectory = await directory()
    const seed = ProductDraftStore.open({ workspace: "G:\\migration", stateDirectory })
    await writeFile(seed.path, JSON.stringify({ schema: "zyra.product-draft/v0", text: "legacy", cursor: 3 }), "utf8")
    const legacy = ProductDraftStore.open({ workspace: "G:\\migration", stateDirectory })
    expect(legacy.restored).toEqual({ text: "legacy", cursor: 3 })
    expect(legacy.warning).toContain("旧版本地草稿")
    legacy.schedule(legacy.restored)
    await legacy.flush()
    expect(JSON.parse(await readFile(legacy.path, "utf8")).schema).toBe("zyra.product-draft/v1")

    await writeFile(seed.path, "{broken", "utf8")
    const damaged = ProductDraftStore.open({ workspace: "G:\\migration", stateDirectory })
    expect(damaged.restored).toEqual({ text: "", cursor: 0 })
    expect(damaged.warning).toContain("未覆盖原文件")
    expect(await readFile(seed.path, "utf8")).toBe("{broken")
  })

  test("expands large paste references before persistence", () => {
    const draft = new PromptDraft()
    const pasted = "paste line\n".repeat(1_000)
    draft.paste(pasted)
    expect(draft.snapshot().text).toMatch(/^@paste:/)
    expect(draft.persistenceSnapshot()).toEqual({ text: pasted, cursor: pasted.length })
  })
})
