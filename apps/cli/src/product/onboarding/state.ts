import { mkdir, open, readFile, rename, rm } from "node:fs/promises"
import { join, resolve } from "node:path"
import { cliStateDirectory } from "../../daemon.ts"

export const PRODUCT_ONBOARDING_SCHEMA = "zyra.product-onboarding/v1" as const

export interface ProductOnboardingState {
  schema: typeof PRODUCT_ONBOARDING_SCHEMA
  completed_at: string
  choice: "automatic" | "explicit-model"
}

export type ProductOnboardingLoad =
  | { status: "pending" }
  | { status: "complete"; state: ProductOnboardingState }
  | { status: "invalid"; reason: string }

function statePath(directory: string): string {
  return join(resolve(directory), "product-onboarding.json")
}

function validState(value: unknown): value is ProductOnboardingState {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const state = value as Record<string, unknown>
  return state.schema === PRODUCT_ONBOARDING_SCHEMA
    && typeof state.completed_at === "string"
    && Number.isFinite(Date.parse(state.completed_at))
    && (state.choice === "automatic" || state.choice === "explicit-model")
}

export class ProductOnboardingStore {
  readonly #directory: string

  constructor(directory = cliStateDirectory()) {
    this.#directory = resolve(directory)
  }

  async load(): Promise<ProductOnboardingLoad> {
    try {
      const raw = await readFile(statePath(this.#directory), "utf8")
      if (Buffer.byteLength(raw, "utf8") > 16 * 1024) {
        return { status: "invalid", reason: "首次使用状态超过安全大小上限" }
      }
      const decoded: unknown = JSON.parse(raw)
      return validState(decoded)
        ? { status: "complete", state: Object.freeze({ ...decoded }) }
        : { status: "invalid", reason: "首次使用状态使用未知 schema 或字段" }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return { status: "pending" }
      if (error instanceof SyntaxError) return { status: "invalid", reason: "首次使用状态 JSON 已损坏" }
      return { status: "invalid", reason: "首次使用状态不可读取" }
    }
  }

  async complete(choice: ProductOnboardingState["choice"], now = new Date()): Promise<ProductOnboardingState> {
    const state: ProductOnboardingState = Object.freeze({
      schema: PRODUCT_ONBOARDING_SCHEMA,
      completed_at: now.toISOString(),
      choice,
    })
    await mkdir(this.#directory, { recursive: true })
    const temporary = join(this.#directory, `product-onboarding-${process.pid}-${crypto.randomUUID()}.tmp`)
    const handle = await open(temporary, "wx", 0o600)
    try {
      await handle.writeFile(`${JSON.stringify(state)}\n`, "utf8")
      await handle.sync()
    } finally {
      await handle.close()
    }
    try {
      await rename(temporary, statePath(this.#directory))
    } finally {
      await rm(temporary, { force: true }).catch(() => undefined)
    }
    return state
  }
}
