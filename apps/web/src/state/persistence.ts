import {
  PROJECTION_STORAGE_PREFIX,
  ProjectionError,
  type CanonicalProjectionState,
  type ProjectionPersistence,
  type ProjectionRestoreResult,
} from "./contracts.ts"
import {
  decodeProjectionSnapshot,
  encodeProjectionSnapshot,
} from "./migrations.ts"

export class MemoryProjectionPersistence implements ProjectionPersistence {
  readonly #values = new Map<string, string>()
  #disabled = false

  async load(storeId: string): Promise<string | undefined> {
    this.#assertEnabled()
    return this.#values.get(storeId)
  }

  async save(storeId: string, value: string): Promise<void> {
    this.#assertEnabled()
    this.#values.set(storeId, value)
  }

  async remove(storeId: string): Promise<void> {
    this.#assertEnabled()
    this.#values.delete(storeId)
  }

  async keys(): Promise<readonly string[]> {
    this.#assertEnabled()
    return Object.freeze([...this.#values.keys()].sort())
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  clear(): void {
    this.#values.clear()
  }

  snapshot(): Readonly<Record<string, string>> {
    return Object.freeze(
      Object.fromEntries([...this.#values.entries()].sort(([a], [b]) => a.localeCompare(b))),
    )
  }

  #assertEnabled(): void {
    if (this.#disabled) {
      throw new ProjectionError(
        "PERSISTENCE_DISABLED",
        "Projection persistence is disabled.",
      )
    }
  }
}

export class LocalStorageProjectionPersistence implements ProjectionPersistence {
  readonly #prefix: string
  readonly #storage: Storage

  constructor(
    storage: Storage,
    prefix = PROJECTION_STORAGE_PREFIX,
  ) {
    this.#storage = storage
    this.#prefix = prefix
  }

  async load(storeId: string): Promise<string | undefined> {
    return this.#storage.getItem(this.#key(storeId)) ?? undefined
  }

  async save(storeId: string, value: string): Promise<void> {
    this.#storage.setItem(this.#key(storeId), value)
  }

  async remove(storeId: string): Promise<void> {
    this.#storage.removeItem(this.#key(storeId))
  }

  async keys(): Promise<readonly string[]> {
    const result: string[] = []
    const prefix = `${this.#prefix}:`
    for (let index = 0; index < this.#storage.length; index += 1) {
      const key = this.#storage.key(index)
      if (!key?.startsWith(prefix)) continue
      result.push(key.slice(prefix.length))
    }
    return Object.freeze(result.sort())
  }

  #key(storeId: string): string {
    return `${this.#prefix}:${storeId}`
  }
}

export class IndexedDbProjectionPersistence implements ProjectionPersistence {
  readonly #databaseName: string
  readonly #storeName: string
  readonly #version: number
  #database?: Promise<IDBDatabase>

  constructor(
    databaseName = "zyra-workbench",
    storeName = "canonical-projections",
    version = 1,
  ) {
    this.#databaseName = databaseName
    this.#storeName = storeName
    this.#version = version
  }

  async load(storeId: string): Promise<string | undefined> {
    const value = await this.#request<string | undefined>(
      "readonly",
      (store) => store.get(storeId),
    )
    return typeof value === "string" ? value : undefined
  }

  async save(storeId: string, value: string): Promise<void> {
    await this.#request("readwrite", (store) => store.put(value, storeId))
  }

  async remove(storeId: string): Promise<void> {
    await this.#request("readwrite", (store) => store.delete(storeId))
  }

  async keys(): Promise<readonly string[]> {
    const keys = await this.#request<IDBValidKey[]>(
      "readonly",
      (store) => store.getAllKeys(),
    )
    return Object.freeze(
      (keys ?? []).map((key) => String(key)).sort(),
    )
  }

  close(): void {
    void this.#database?.then((database) => database.close())
    this.#database = undefined
  }

  async #open(): Promise<IDBDatabase> {
    if (this.#database) return this.#database
    if (typeof indexedDB === "undefined") {
      throw new ProjectionError(
        "INDEXED_DB_UNAVAILABLE",
        "IndexedDB is unavailable in this environment.",
      )
    }
    this.#database = new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(this.#databaseName, this.#version)
      request.onupgradeneeded = () => {
        const database = request.result
        if (!database.objectStoreNames.contains(this.#storeName)) {
          database.createObjectStore(this.#storeName)
        }
      }
      request.onsuccess = () => resolve(request.result)
      request.onerror = () =>
        reject(
          request.error ??
            new Error(`Unable to open IndexedDB database ${this.#databaseName}.`),
        )
      request.onblocked = () =>
        reject(
          new Error(`IndexedDB database ${this.#databaseName} upgrade is blocked.`),
        )
    })
    try {
      return await this.#database
    } catch (error) {
      this.#database = undefined
      throw error
    }
  }

  async #request<T>(
    mode: IDBTransactionMode,
    create: (store: IDBObjectStore) => IDBRequest,
  ): Promise<T> {
    const database = await this.#open()
    return await new Promise<T>((resolve, reject) => {
      const transaction = database.transaction(this.#storeName, mode)
      const store = transaction.objectStore(this.#storeName)
      let settled = false
      let requestSucceeded = false
      let result: T
      const fail = (error: unknown) => {
        if (settled) return
        settled = true
        reject(error)
      }
      transaction.oncomplete = () => {
        if (settled) return
        if (!requestSucceeded) {
          fail(new Error("IndexedDB transaction completed before its request."))
          return
        }
        settled = true
        resolve(result)
      }
      transaction.onerror = () =>
        fail(
          transaction.error ??
            new Error("IndexedDB projection transaction failed."),
        )
      transaction.onabort = () =>
        fail(
          transaction.error ??
            new Error("IndexedDB projection transaction was aborted."),
        )
      let request: IDBRequest
      try {
        request = create(store)
      } catch (error) {
        fail(error)
        return
      }
      request.onsuccess = () => {
        result = request.result as T
        requestSucceeded = true
      }
      request.onerror = () =>
        fail(
          request.error ??
            new Error("IndexedDB projection request failed."),
        )
    })
  }
}

interface ProjectionReplica {
  serialized: string
  revision: number
  checksum: string
}

function inspectProjectionReplica(
  serialized: string,
  storeId: string,
): ProjectionReplica {
  const restored = decodeProjectionSnapshot(serialized, storeId)
  const envelope = JSON.parse(serialized) as { checksum?: unknown }
  return {
    serialized,
    revision: restored.state.revision,
    checksum: String(envelope.checksum ?? ""),
  }
}

export class FallbackProjectionPersistence implements ProjectionPersistence {
  readonly #primary: ProjectionPersistence
  readonly #fallback: ProjectionPersistence
  #primaryAvailable = true

  constructor(
    primary: ProjectionPersistence,
    fallback: ProjectionPersistence,
  ) {
    this.#primary = primary
    this.#fallback = fallback
  }

  async load(storeId: string): Promise<string | undefined> {
    let primary: ProjectionReplica | undefined
    let primaryError: unknown
    if (this.#primaryAvailable) {
      try {
        const value = await this.#primary.load(storeId)
        if (value !== undefined) {
          primary = inspectProjectionReplica(value, storeId)
        }
      } catch (error) {
        this.#primaryAvailable = false
        primaryError = error
      }
    }
    let fallback: ProjectionReplica | undefined
    let fallbackError: unknown
    try {
      const value = await this.#fallback.load(storeId)
      if (value !== undefined) {
        fallback = inspectProjectionReplica(value, storeId)
      }
    } catch (error) {
      fallbackError = error
    }
    if (primary && fallback) {
      if (
        primary.revision === fallback.revision &&
        primary.checksum !== fallback.checksum
      ) {
        throw new ProjectionError(
          "SNAPSHOT_REPLICA_CONFLICT",
          `Projection replicas for ${storeId} disagree at revision ${primary.revision}.`,
          { storeId, revision: primary.revision },
        )
      }
      return primary.revision >= fallback.revision
        ? primary.serialized
        : fallback.serialized
    }
    if (primary) return primary.serialized
    if (fallback) return fallback.serialized
    if (primaryError) throw primaryError
    if (fallbackError) throw fallbackError
    return undefined
  }

  async save(storeId: string, value: string): Promise<void> {
    if (this.#primaryAvailable) {
      try {
        await this.#primary.save(storeId, value)
        return
      } catch {
        this.#primaryAvailable = false
      }
    }
    await this.#fallback.save(storeId, value)
  }

  async remove(storeId: string): Promise<void> {
    const results = await Promise.allSettled([
      this.#primary.remove(storeId),
      this.#fallback.remove(storeId),
    ])
    if (results.every((result) => result.status === "rejected")) {
      throw (results[0] as PromiseRejectedResult).reason
    }
  }

  async keys(): Promise<readonly string[]> {
    const values = new Set<string>()
    for (const persistence of [this.#primary, this.#fallback]) {
      try {
        const keys = await persistence.keys?.()
        keys?.forEach((key) => values.add(key))
      } catch {
        if (persistence === this.#primary) this.#primaryAvailable = false
      }
    }
    return Object.freeze([...values].sort())
  }

  get primaryAvailable(): boolean {
    return this.#primaryAvailable
  }
}

export class ProjectionPersistenceCoordinator {
  readonly #storeId: string
  readonly #persistence: ProjectionPersistence
  readonly #now: () => number
  #disabled = false
  #closed = false
  #persistedRevision = -1
  #pending?: Promise<void>

  constructor(
    storeId: string,
    persistence: ProjectionPersistence,
    now: () => number = Date.now,
  ) {
    this.#storeId = storeId
    this.#persistence = persistence
    this.#now = now
  }

  async restore(): Promise<ProjectionRestoreResult | undefined> {
    this.#assertAvailable()
    const serialized = await this.#persistence.load(this.#storeId)
    if (!serialized) return undefined
    const result = decodeProjectionSnapshot(
      serialized,
      this.#storeId,
      this.#now(),
    )
    this.#persistedRevision = result.state.revision
    return result
  }

  async persist(state: CanonicalProjectionState): Promise<void> {
    this.#assertAvailable()
    if (state.revision <= this.#persistedRevision) return
    const envelope = encodeProjectionSnapshot(
      this.#storeId,
      state,
      this.#now(),
    )
    const serialized = JSON.stringify(envelope)
    const predecessor = this.#pending
    const operation = (predecessor
      ? predecessor.catch(() => undefined)
      : Promise.resolve()
    ).then(async () => {
      if (state.revision <= this.#persistedRevision) return
      await this.#persistence.save(this.#storeId, serialized)
      this.#persistedRevision = state.revision
    })
    this.#pending = operation
    try {
      await operation
    } finally {
      if (this.#pending === operation) this.#pending = undefined
    }
  }

  async clear(): Promise<void> {
    this.#assertAvailable()
    await this.flush()
    await this.#persistence.remove(this.#storeId)
    this.#persistedRevision = -1
  }

  async flush(): Promise<void> {
    await this.#pending
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    if (this.#closed) return
    this.#disabled = false
  }

  async close(): Promise<void> {
    if (this.#closed) return
    this.#closed = true
    await this.#pending
  }

  get persistedRevision(): number {
    return this.#persistedRevision
  }

  get pending(): boolean {
    return Boolean(this.#pending)
  }

  get disabled(): boolean {
    return this.#disabled
  }

  #assertAvailable(): void {
    if (this.#closed) {
      throw new ProjectionError(
        "PERSISTENCE_CLOSED",
        "Projection persistence coordinator is closed.",
      )
    }
    if (this.#disabled) {
      throw new ProjectionError(
        "PERSISTENCE_DISABLED",
        "Projection persistence coordinator is disabled.",
      )
    }
  }
}

export function createBrowserProjectionPersistence(): ProjectionPersistence {
  if (typeof window === "undefined") return new MemoryProjectionPersistence()
  const local = new LocalStorageProjectionPersistence(window.localStorage)
  if (typeof indexedDB === "undefined") return local
  return new FallbackProjectionPersistence(
    new IndexedDbProjectionPersistence(),
    local,
  )
}
