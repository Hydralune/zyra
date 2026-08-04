import { TerminalProtocolError } from "./contracts.ts"
import { TERMINAL_CAPABILITIES, TerminalNodeServer } from "./server.ts"

interface ApiEnvelope {
  ok?: boolean
  schema?: string
  result?: unknown
  error?: string
  message?: string
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TerminalProtocolError(`${label} is not an object.`, "terminal_registration_contract", 502)
  }
  return value as Record<string, unknown>
}

export interface TerminalRegistrationOptions {
  baseUrl: string
  token?: string
  timeoutMs?: number
  fetch?: typeof fetch
  priority?: number
}

export interface TerminalRegistrationReceipt {
  schema: "zyra.cli-terminal-registration-receipt/v1"
  backend_id: string
  generation: string
  registry_revision: number
  enabled: boolean
  state_owner: "python.BackendRegistryStore"
  endpoint_projected: false
  capability_token_projected: false
}

export class TerminalNodeRegistration {
  readonly server: TerminalNodeServer
  readonly baseUrl: string
  readonly token?: string
  readonly timeoutMs: number
  readonly priority: number
  readonly #fetch: typeof fetch

  constructor(server: TerminalNodeServer, options: TerminalRegistrationOptions) {
    this.server = server
    this.baseUrl = options.baseUrl
    this.token = options.token
    this.timeoutMs = options.timeoutMs ?? 15_000
    this.priority = options.priority ?? 1_000
    this.#fetch = options.fetch ?? fetch
  }

  async enable(): Promise<TerminalRegistrationReceipt> {
    const receipt = await this.#upsert(true)
    this.server.markRegistered(true)
    return receipt
  }

  async disable(): Promise<TerminalRegistrationReceipt> {
    const receipt = await this.#upsert(false)
    this.server.markRegistered(false)
    return receipt
  }

  async #revision(): Promise<number> {
    const response = await this.#request("GET", "/backends/health")
    const result = record(response.result, "backend registry health")
    const revision = Number(result.registry_revision)
    if (!Number.isSafeInteger(revision) || revision < 0) {
      throw new TerminalProtocolError("Backend registry revision is invalid.", "terminal_registration_contract", 502)
    }
    return revision
  }

  async #upsert(enabled: boolean): Promise<TerminalRegistrationReceipt> {
    let lastError: unknown
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const expectedRevision = await this.#revision()
      try {
        const response = await this.#request("POST", "/backends", {
          expected_revision: expectedRevision,
          terminal_registration: {
            generation: this.server.generation,
            owner_id: this.server.ownerId,
            capability_token: this.server.capabilityToken,
          },
          backend: {
            backend_id: this.server.backendId,
            display_name: `Zyra CLI Terminal ${this.server.backendId.slice(-8)}`,
            kind: "edge_http",
            location: "local",
            runtime_worker: "CodeWorkerRuntime",
            capabilities: [...TERMINAL_CAPABILITIES],
            endpoint: this.server.endpoint,
            health_endpoint: `${this.server.endpoint}/health`,
            command: [],
            docker_image: null,
            workspace_policy: {
              scope: "task",
              read_only: false,
              artifact_only: false,
              require_existing: true,
              require_writable: true,
              isolation: "workspace-manager",
            },
            limits: {
              maximum_concurrency: this.server.maximumConcurrency,
              turn_timeout_seconds: 600,
              connect_timeout_seconds: 15,
              health_timeout_seconds: 3,
              memory_megabytes: null,
              cpu_millicores: null,
            },
            enabled,
            priority: this.priority,
            cost_weight: 0,
            latency_weight: 0,
            metadata: {
              execution_mode: "terminal_http",
              terminal_protocol: "zyra.backend-transport-request/v1",
              terminal_generation: this.server.generation,
              real_terminal_dispatch_claimed: true,
            },
          },
        })
        const result = record(response.result, "terminal registration result")
        const revision = Number(result.registry_revision)
        if (result.backend_id !== this.server.backendId || !Number.isSafeInteger(revision) || revision <= expectedRevision) {
          throw new TerminalProtocolError("Terminal registration receipt is inconsistent.", "terminal_registration_contract", 502)
        }
        return Object.freeze({
          schema: "zyra.cli-terminal-registration-receipt/v1",
          backend_id: this.server.backendId,
          generation: this.server.generation,
          registry_revision: revision,
          enabled,
          state_owner: "python.BackendRegistryStore",
          endpoint_projected: false,
          capability_token_projected: false,
        })
      } catch (error) {
        lastError = error
        if (!(error instanceof TerminalProtocolError) || error.code !== "terminal_registration_conflict") throw error
      }
    }
    throw lastError
  }

  async #request(method: "GET" | "POST", path: string, payload?: Record<string, unknown>): Promise<ApiEnvelope> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error("terminal registration request timed out")), this.timeoutMs)
    timer.unref()
    try {
      const headers = new Headers({ accept: "application/json", "cache-control": "no-store" })
      if (payload) headers.set("content-type", "application/json")
      if (this.token) headers.set("authorization", `Bearer ${this.token}`)
      const response = await this.#fetch(new URL(path, `${this.baseUrl.replace(/\/$/u, "")}/`), {
        method,
        headers,
        body: payload ? JSON.stringify(payload) : undefined,
        signal: controller.signal,
      })
      const raw = await response.text()
      let decoded: ApiEnvelope
      try {
        decoded = record(JSON.parse(raw), "provider backend API response") as ApiEnvelope
      } catch (error) {
        if (error instanceof TerminalProtocolError) throw error
        throw new TerminalProtocolError("Provider backend API returned invalid JSON.", "terminal_registration_contract", 502, { cause: error })
      }
      if (!response.ok || decoded.ok !== true || decoded.schema !== "zyra.provider-backend-api/v1") {
        const code = typeof decoded.error === "string" ? decoded.error : "terminal_registration_rejected"
        const conflict = response.status === 409 && (code.includes("revision") || code.includes("conflict") || code === "provider_backend_operation_rejected")
        throw new TerminalProtocolError(
          typeof decoded.message === "string" ? decoded.message : "Terminal registration was rejected.",
          conflict ? "terminal_registration_conflict" : code,
          response.status,
        )
      }
      return decoded
    } finally {
      clearTimeout(timer)
    }
  }
}
