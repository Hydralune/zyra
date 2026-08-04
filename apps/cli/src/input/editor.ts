import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { spawn } from "node:child_process"

function editorCommand(): { file: string; args: string[] } {
  const raw = (process.env.VISUAL || process.env.EDITOR || "").trim()
  if (!raw) throw new TypeError("Set VISUAL or EDITOR to use /edit.")
  const parts = raw.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((part) => part.replace(/^"|"$/g, "")) ?? []
  if (!parts[0]) throw new TypeError("VISUAL or EDITOR is invalid.")
  return { file: parts[0], args: parts.slice(1) }
}

export async function editDraftExternally(initial: string): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), "zyra-draft-"))
  const path = join(directory, "draft.md")
  try {
    await writeFile(path, initial, { encoding: "utf8", mode: 0o600 })
    const command = editorCommand()
    const code = await new Promise<number>((resolve, reject) => {
      const child = spawn(command.file, [...command.args, path], { stdio: "inherit", shell: false })
      child.once("error", reject)
      child.once("exit", (value) => resolve(value ?? 1))
    })
    if (code !== 0) throw new TypeError(`External editor exited with status ${code}.`)
    return await readFile(path, "utf8")
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
}
