import type { ProductExecutionConfig } from "../api/task-api.ts"

export interface ProductPreferences {
  fontSize: 14 | 16 | 18
  execution?: ProductExecutionConfig
  pinned: readonly string[]
  titles: Readonly<Record<string, string>>
}

export class ProductPreferenceStore {
  readonly #key: string
  readonly #storage?: Pick<Storage, "getItem" | "setItem">
  readonly #listeners = new Set<() => void>()
  #state: ProductPreferences = Object.freeze({ fontSize: 16, pinned: [], titles: {} })
  constructor(scope: string, storage?: Pick<Storage, "getItem" | "setItem">) {
    this.#key = `zyra.product-preferences.v1:${scope}`
    this.#storage = storage
    try {
      const value = JSON.parse(storage?.getItem(this.#key) ?? "null")
      if (value && typeof value === "object") {
        const execution = value.execution
        this.#state = Object.freeze({
          fontSize: [14, 16, 18].includes(value.fontSize) ? value.fontSize : 16,
          execution: execution && typeof execution.providerId === "string" && typeof execution.modelId === "string"
            ? { providerId: execution.providerId, modelId: execution.modelId,
                ...(typeof execution.reasoningEffort === "string" ? { reasoningEffort: execution.reasoningEffort } : {}) } : undefined,
          pinned: Array.isArray(value.pinned) ? [...new Set(value.pinned.filter((v: unknown) => typeof v === "string"))].slice(-2000) as string[] : [],
          titles: value.titles && typeof value.titles === "object" && !Array.isArray(value.titles)
            ? Object.fromEntries(Object.entries(value.titles).filter(([, v]) => typeof v === "string").slice(-2000)) as Record<string, string> : {},
        })
      }
    } catch { /* Missing or invalid browser preferences keep safe defaults. */ }
  }
  getSnapshot = (): ProductPreferences => this.#state
  subscribe = (listener: () => void): (() => void) => { this.#listeners.add(listener); return () => this.#listeners.delete(listener) }
  update(patch: Partial<ProductPreferences>): void {
    const next = Object.freeze({ ...this.#state, ...patch })
    // A storage failure is surfaced to the caller; never claim persistence.
    this.#storage?.setItem(this.#key, JSON.stringify(next))
    this.#state = next
    for (const listener of this.#listeners) listener()
  }
  pin(key: string, pinned: boolean): void {
    this.update({ pinned: [...this.#state.pinned.filter((item) => item !== key), ...(pinned ? [key] : [])] })
  }
  forgetConversation(key: string): void {
    const titles = { ...this.#state.titles }
    delete titles[key]
    this.update({ pinned: this.#state.pinned.filter((item) => item !== key), titles })
  }
  rememberTitle(key: string, title: string): void { this.update({ titles: { ...this.#state.titles, [key]: title } }) }
}

export function browserProductPreferences(scope: string): ProductPreferenceStore {
  let storage: Storage | undefined
  try { storage = typeof localStorage === "undefined" ? undefined : localStorage } catch { /* Browser may block storage. */ }
  return new ProductPreferenceStore(scope, storage ?? {
    getItem: () => null,
    setItem: () => { throw new Error("浏览器存储不可用，无法保存设置。") },
  })
}
