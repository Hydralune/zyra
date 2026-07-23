import { createZyraApi } from "./api/index.ts"

function apiBaseUrl(): string {
  const configured = document.documentElement.dataset.apiBaseUrl?.trim()
  return configured || window.location.origin
}

function requiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id)
  if (!element) throw new Error(`Missing application element #${id}`)
  return element as T
}

function setStatus(state: "checking" | "ready" | "blocked", message: string): void {
  const indicator = requiredElement<HTMLElement>("transport-status")
  indicator.dataset.state = state
  indicator.textContent = message
}

function renderPayload(value: unknown): void {
  requiredElement<HTMLElement>("transport-payload").textContent = JSON.stringify(value, null, 2)
}

const api = createZyraApi({ baseUrl: apiBaseUrl() })
const refresh = requiredElement<HTMLButtonElement>("transport-refresh")

async function inspectTransport(): Promise<void> {
  refresh.disabled = true
  setStatus("checking", "Checking typed transport…")
  try {
    const [health, readiness] = await Promise.all([
      api.tasks.health({ timeoutMs: 5_000 }),
      api.tasks.readiness({ waitMs: 0, timeoutMs: 5_000 }),
    ])
    setStatus(readiness.ready ? "ready" : "blocked", readiness.ready ? "Transport ready" : "Runtime blocked")
    renderPayload({
      health,
      readiness,
      client: api.client.snapshot(),
    })
  } catch (error) {
    setStatus("blocked", error instanceof Error ? error.message : String(error))
    renderPayload({
      error: error instanceof Error ? { name: error.name, message: error.message } : String(error),
      client: api.client.snapshot(),
    })
  } finally {
    refresh.disabled = false
  }
}

refresh.addEventListener("click", () => void inspectTransport())
window.addEventListener("pagehide", () => api.close("Page hidden."), { once: true })
void inspectTransport()
