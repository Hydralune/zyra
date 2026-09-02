import type { TerminalRegistrationReceipt } from "./registration.ts"
import { TerminalNodeRegistration, type TerminalRegistrationOptions } from "./registration.ts"
import { TerminalNodeServer, type TerminalNodeServerOptions, type TerminalNodeStatus } from "./server.ts"

export interface TerminalLifecycleOptions extends TerminalRegistrationOptions, TerminalNodeServerOptions {
  shutdownTimeoutMs?: number
}

export interface LocalExecutorEnvironment {
  schema: "zyra.local-executor-environment/v1"
  kind: "local_terminal"
  backendId: string
  generation: string
  cwd: string
  workspaceRoots: readonly string[]
  capabilityProof: string
}

export class TerminalNodeLifecycle {
  readonly server: TerminalNodeServer
  readonly registration: TerminalNodeRegistration
  readonly shutdownTimeoutMs: number
  #started = false
  #stopped = false

  private constructor(server: TerminalNodeServer, options: TerminalLifecycleOptions) {
    this.server = server
    this.registration = new TerminalNodeRegistration(server, options)
    this.shutdownTimeoutMs = options.shutdownTimeoutMs ?? 5_000
  }

  static async create(options: TerminalLifecycleOptions): Promise<TerminalNodeLifecycle> {
    const server = await TerminalNodeServer.create(options)
    return new TerminalNodeLifecycle(server, options)
  }

  async start(): Promise<TerminalRegistrationReceipt> {
    if (this.#started) throw new Error("Terminal node lifecycle has already started.")
    await this.server.listen()
    try {
      const receipt = await this.registration.enable()
      this.#started = true
      return receipt
    } catch (error) {
      await this.server.close()
      throw error
    }
  }

  status(): TerminalNodeStatus {
    return this.server.status()
  }

  executorEnvironment(): LocalExecutorEnvironment {
    if (!this.#started || this.#stopped) {
      throw new Error("Terminal node is not available as a local executor environment.")
    }
    return Object.freeze({
      schema: "zyra.local-executor-environment/v1",
      kind: "local_terminal",
      backendId: this.server.backendId,
      generation: this.server.generation,
      cwd: this.server.startupRoot,
      workspaceRoots: Object.freeze([this.server.startupRoot]),
      capabilityProof: this.server.capabilityToken,
    })
  }

  async stop(reason = "Zyra CLI session complete"): Promise<TerminalRegistrationReceipt | undefined> {
    if (this.#stopped) return undefined
    this.#stopped = true
    const deadline = Date.now() + Math.max(100, this.shutdownTimeoutMs)
    const trace = (stage: string) => {
      if (process.env.ZYRA_CLI_TRACE_SHUTDOWN === "1") process.stderr.write(`[zyra shutdown] ${stage}\n`)
    }
    try {
      trace("terminal drain start")
      await this.server.drain(reason)
      trace("terminal settle start")
      await this.server.settle(Math.max(100, deadline - Date.now()))
      if (!this.#started) return undefined
      const remaining = deadline - Date.now()
      if (remaining <= 0) throw new Error("Terminal node cleanup deadline expired.")
      trace("terminal registration disable start")
      const receipt = await this.registration.disable({
        timeoutMs: remaining,
        maximumAttempts: 1,
      })
      trace("terminal registration disable complete")
      return receipt
    } finally {
      trace("terminal server close start")
      await this.server.close()
      trace("terminal server close complete")
    }
  }
}
