import { createHash, randomUUID } from "node:crypto"
import { existsSync, readFileSync, statSync } from "node:fs"
import { mkdir, rename, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, isAbsolute, join, resolve } from "node:path"
import { MAX_PROMPT_BYTES } from "../../input/draft.ts"

const SCHEMA = "zyra.product-draft/v1" as const
const LEGACY_SCHEMA = "zyra.product-draft/v0" as const
const MAX_STATE_BYTES = MAX_PROMPT_BYTES + 16 * 1_024

export interface PersistedProductDraft {
  text: string
  cursor: number
}

interface ProductDraftDocument {
  schema: typeof SCHEMA
  revision: number
  workspace_digest: string
  draft: PersistedProductDraft
  updated_at: string
}

function boundedDraft(text: unknown, cursor: unknown): PersistedProductDraft | undefined {
  if (typeof text !== "string" || Buffer.byteLength(text, "utf8") > MAX_PROMPT_BYTES) return undefined
  if (!Number.isSafeInteger(cursor) || Number(cursor) < 0 || Number(cursor) > text.length) return undefined
  return Object.freeze({ text, cursor: Number(cursor) })
}

function workspaceDigest(workspace: string): string {
  return createHash("sha256").update(resolve(workspace).toLowerCase(), "utf8").digest("hex")
}

export function defaultProductStateDirectory(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.ZYRA_STATE_DIR?.trim()
  if (explicit && isAbsolute(explicit)) return resolve(explicit, "product-tui", "drafts")
  if (process.platform === "win32") {
    const local = env.LOCALAPPDATA?.trim() || env.APPDATA?.trim()
    return resolve(local || join(homedir(), "AppData", "Local"), "Zyra", "product-tui", "drafts")
  }
  const state = env.XDG_STATE_HOME?.trim()
  return resolve(state && isAbsolute(state) ? state : join(homedir(), ".local", "state"), "zyra", "product-tui", "drafts")
}

export class ProductDraftStore {
  readonly path: string
  readonly workspaceDigest: string
  readonly restored: PersistedProductDraft
  readonly warning?: string
  #revision: number
  #pending: PersistedProductDraft
  #timer: ReturnType<typeof setTimeout> | undefined
  #write: Promise<void> = Promise.resolve()

  private constructor(input: {
    path: string
    workspaceDigest: string
    restored: PersistedProductDraft
    revision: number
    warning?: string
  }) {
    this.path = input.path
    this.workspaceDigest = input.workspaceDigest
    this.restored = input.restored
    this.#pending = input.restored
    this.#revision = input.revision
    this.warning = input.warning
  }

  static open(input: { workspace: string; stateDirectory?: string }): ProductDraftStore {
    const digest = workspaceDigest(input.workspace)
    const directory = resolve(input.stateDirectory ?? defaultProductStateDirectory())
    const path = join(directory, `${digest}.json`)
    const empty = Object.freeze({ text: "", cursor: 0 })
    if (!existsSync(path)) return new ProductDraftStore({ path, workspaceDigest: digest, restored: empty, revision: 0 })
    try {
      const size = statSync(path).size
      if (size < 0 || size > MAX_STATE_BYTES) throw new TypeError("state file exceeds the bounded draft budget")
      const raw = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>
      if (raw.schema === SCHEMA) {
        if (raw.workspace_digest !== digest || !Number.isSafeInteger(raw.revision) || Number(raw.revision) < 1) {
          throw new TypeError("state identity or revision is invalid")
        }
        const draft = raw.draft && typeof raw.draft === "object" && !Array.isArray(raw.draft)
          ? boundedDraft((raw.draft as Record<string, unknown>).text, (raw.draft as Record<string, unknown>).cursor)
          : undefined
        if (!draft) throw new TypeError("state draft is invalid")
        return new ProductDraftStore({ path, workspaceDigest: digest, restored: draft, revision: Number(raw.revision) })
      }
      if (raw.schema === LEGACY_SCHEMA) {
        const draft = boundedDraft(raw.text, raw.cursor)
        if (!draft) throw new TypeError("legacy state draft is invalid")
        return new ProductDraftStore({
          path,
          workspaceDigest: digest,
          restored: draft,
          revision: 0,
          warning: "已安全读取旧版本地草稿；下次保存会迁移到 v1。",
        })
      }
      throw new TypeError("state schema is unknown")
    } catch {
      return new ProductDraftStore({
        path,
        workspaceDigest: digest,
        restored: empty,
        revision: 0,
        warning: "本地草稿状态已损坏或版本不兼容；未覆盖原文件，当前使用空草稿。",
      })
    }
  }

  schedule(draft: PersistedProductDraft): void {
    const selected = boundedDraft(draft.text, draft.cursor)
    if (!selected) return
    this.#pending = selected
    if (this.#timer) clearTimeout(this.#timer)
    this.#timer = setTimeout(() => {
      this.#timer = undefined
      this.#enqueueWrite()
    }, 50)
  }

  async flush(): Promise<void> {
    if (this.#timer) {
      clearTimeout(this.#timer)
      this.#timer = undefined
      this.#enqueueWrite()
    }
    await this.#write
  }

  #enqueueWrite(): void {
    const draft = this.#pending
    const revision = this.#revision + 1
    this.#revision = revision
    const document: ProductDraftDocument = {
      schema: SCHEMA,
      revision,
      workspace_digest: this.workspaceDigest,
      draft,
      updated_at: new Date().toISOString(),
    }
    this.#write = this.#write.then(async () => {
      await mkdir(dirname(this.path), { recursive: true, mode: 0o700 })
      const temporary = `${this.path}.${process.pid}.${randomUUID()}.tmp`
      await writeFile(temporary, `${JSON.stringify(document)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 })
      await rename(temporary, this.path)
    })
  }
}
