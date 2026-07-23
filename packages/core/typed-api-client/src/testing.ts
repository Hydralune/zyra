import type { FetchTransportOptions } from "./transport.ts"
import { FetchApiTransport } from "./transport.ts"
import { TransportRegistry } from "./registry.ts"
import { registerCoreNormalizers } from "./normalizers.ts"

export interface ScriptedFetchCall {
  url: URL
  init: RequestInit
  index: number
}

export type ScriptedFetchStep =
  | Response
  | Error
  | ((call: ScriptedFetchCall) => Response | Promise<Response>)

export function scriptedFetch(steps: readonly ScriptedFetchStep[]): {
  fetch: typeof fetch
  calls: ScriptedFetchCall[]
  remaining(): number
} {
  const queue = [...steps]
  const calls: ScriptedFetchCall[] = []
  const fetchImpl = async (input: URL | RequestInfo, init: RequestInit = {}): Promise<Response> => {
    const url = input instanceof URL ? new URL(input) : new URL(input instanceof Request ? input.url : String(input))
    const call = { url, init: { ...init, headers: new Headers(init.headers) }, index: calls.length }
    calls.push(call)
    const step = queue.shift()
    if (!step) throw new Error(`No scripted fetch step for call ${call.index}`)
    if (step instanceof Error) throw step
    if (typeof step === "function") return step(call)
    return step
  }
  return {
    fetch: fetchImpl as typeof fetch,
    calls,
    remaining: () => queue.length,
  }
}

export function jsonResponse(
  body: unknown,
  options: {
    status?: number
    headers?: HeadersInit
    requestId?: string
    apiVersion?: string
    receiptId?: string
    replayed?: boolean
  } = {},
): Response {
  const headers = new Headers(options.headers)
  headers.set("Content-Type", "application/json; charset=utf-8")
  headers.set("X-Zyra-Api-Version", options.apiVersion ?? "1.0")
  if (options.requestId) headers.set("X-Request-Id", options.requestId)
  if (options.receiptId) headers.set("X-Zyra-Receipt-Id", options.receiptId)
  if (options.replayed !== undefined) headers.set("X-Zyra-Receipt-Replayed", String(options.replayed))
  return new Response(JSON.stringify(body), { status: options.status ?? 200, headers })
}

export function createTestRegistry(options: FetchTransportOptions): {
  transport: FetchApiTransport
  registry: TransportRegistry
} {
  const transport = new FetchApiTransport(options)
  const registry = new TransportRegistry(transport)
  registerCoreNormalizers(registry.normalizers)
  return { transport, registry }
}

export async function waitFor(
  predicate: () => boolean | Promise<boolean>,
  options: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<void> {
  const timeoutMs = options.timeoutMs ?? 5_000
  const intervalMs = options.intervalMs ?? 10
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
  throw new Error(`Condition was not met within ${timeoutMs} ms`)
}
