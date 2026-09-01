import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { workspaceReferenceCandidates } from "../src/product/files/index.ts"

const temporaryRoots: string[] = []

async function temporary(prefix: string): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), prefix))
  temporaryRoots.push(root)
  return root
}

afterEach(async () => {
  for (const root of temporaryRoots.splice(0)) await rm(root, { recursive: true, force: true })
})

describe("workspace reference index", () => {
  test("indexes deep Unicode paths while excluding dependency and credential names", async () => {
    const root = await temporary("zyra-file-index-")
    let current = root
    for (let depth = 0; depth < 8; depth += 1) {
      current = join(current, `层-${depth}`)
      await mkdir(current)
    }
    await writeFile(join(current, "结果.ts"), "export {}\n")
    await mkdir(join(root, "node_modules"))
    await writeFile(join(root, "node_modules", "hidden.js"), "secret\n")
    await writeFile(join(root, ".env"), "TOKEN=must-not-index\n")
    await writeFile(join(root, ".env.example"), "TOKEN=example\n")

    const candidates = await workspaceReferenceCandidates(root)
    expect(candidates).toContain(`@${Array.from({ length: 8 }, (_, index) => `层-${index}`).join("/")}/结果.ts`)
    expect(candidates).toContain("@.env.example")
    expect(candidates.some((value) => value.includes("node_modules") || value === "@.env")).toBe(false)
  })

  test("does not traverse a workspace symlink or junction", async () => {
    const root = await temporary("zyra-file-index-root-")
    const outside = await temporary("zyra-file-index-outside-")
    await writeFile(join(outside, "outside-secret.txt"), "must-not-index\n")
    await symlink(outside, join(root, "escape"), process.platform === "win32" ? "junction" : "dir")
    const candidates = await workspaceReferenceCandidates(root)
    expect(candidates.some((value) => value.includes("escape") || value.includes("outside-secret"))).toBe(false)
  })

  test("clamps caller capacity and rejects a non-directory root", async () => {
    const root = await temporary("zyra-file-index-limit-")
    await Promise.all(Array.from({ length: 12 }, (_, index) => writeFile(join(root, `file-${index}.ts`), "")))
    expect(await workspaceReferenceCandidates(root, 3)).toHaveLength(3)
    const file = join(root, "file-0.ts")
    await expect(workspaceReferenceCandidates(file)).rejects.toThrow("must be a directory")
  })
})
