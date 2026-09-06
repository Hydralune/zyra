import { createHash, randomUUID } from "node:crypto"
import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { cliStateDirectory } from "../../daemon.ts"

/** Local bearer custody, separate from drafts, transcripts, and diagnostics. */
export class ProductPermissionCustodyStore {
  constructor(readonly origin: string, readonly directory = join(cliStateDirectory(), "permission-custody")) {}

  #path(sessionId: string): string {
    const key = createHash("sha256").update(`${new URL(this.origin).origin}\n${sessionId}`).digest("hex")
    return join(this.directory, `${key}.json`)
  }

  async load(sessionId: string): Promise<string | undefined> {
    try {
      const raw = await readFile(this.#path(sessionId), "utf8")
      if (raw.length > 16_384) return undefined
      const value: unknown = JSON.parse(raw)
      if (!value || typeof value !== "object") return undefined
      const state = value as Record<string, unknown>
      return state.schema === "zyra.cli-permission-custody/v1" && typeof state.token === "string" && state.token.trim()
        ? state.token : undefined
    } catch { return undefined }
  }

  async save(sessionId: string, token: string): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 })
    const path = this.#path(sessionId)
    const temporary = `${path}.${randomUUID()}.tmp`
    await writeFile(temporary, JSON.stringify({ schema: "zyra.cli-permission-custody/v1", token }), { mode: 0o600, flag: "wx" })
    await rename(temporary, path)
  }
}
