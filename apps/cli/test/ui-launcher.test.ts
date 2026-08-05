import { describe, expect, test } from "bun:test"
import { parseCliArgs } from "../src/args.ts"
import {
  UI_STATE_SCHEMA,
  buildProductUrl,
  launchUi,
  type UiLauncherEnvironment,
  type UiProbe,
  type UiState,
} from "../src/ui.ts"

function environment(probes: UiProbe[]) {
  let state: UiState | undefined
  let builds = 0
  let starts = 0
  const opened: string[] = []
  const value: UiLauncherEnvironment = {
    async reservePort(requested) {
      return requested || 43127
    },
    async probe() {
      return probes.shift() ?? "unavailable"
    },
    async readState() {
      return state
    },
    async writeState(next) {
      state = next
    },
    async removeState() {
      state = undefined
    },
    processAlive(pid) {
      return pid === 4123
    },
    async buildWeb() {
      builds += 1
    },
    async startWeb() {
      starts += 1
      return { pid: 4123 }
    },
    async stopWeb() {},
    async openBrowser(url) {
      opened.push(url)
      return true
    },
    now: () => Date.parse("2026-08-05T10:00:00Z"),
  }
  return {
    value,
    get state() { return state },
    get builds() { return builds },
    get starts() { return starts },
    opened,
    seed(next: UiState) { state = next },
  }
}

describe("FE-S05 zyra ui launcher", () => {
  test("parses bounded UI options through the existing CLI route", () => {
    const command = parseCliArgs([
      "ui",
      "--web-port=0",
      "--open=false",
      "--task=task_demo_001",
      "--base-url=http://127.0.0.1:8010",
    ])
    expect(command).toMatchObject({
      kind: "ui",
      webPort: 0,
      open: false,
      taskId: "task_demo_001",
      baseUrl: "http://127.0.0.1:8010",
    })
  })

  test("builds a direct product URL without exposing credentials", () => {
    expect(buildProductUrl({
      webOrigin: "http://127.0.0.1:5173",
      apiOrigin: "http://127.0.0.1:8000",
      taskId: "task_cli_created_001",
    })).toBe(
      "http://127.0.0.1:5173/tasks/task_cli_created_001?api=http%3A%2F%2F127.0.0.1%3A8000",
    )
    expect(() => buildProductUrl({
      webOrigin: "https://example.com",
      apiOrigin: "http://127.0.0.1:8000",
    })).toThrow("loopback")
  })

  test("starts the existing Web server once and writes only launcher identity", async () => {
    const harness = environment(["unavailable", "zyra"])
    const receipt = await launchUi({
      baseUrl: "http://127.0.0.1:8000",
      webPort: 0,
      startupTimeoutMs: 10_000,
      open: true,
      taskId: "task_demo_001",
    }, harness.value)
    expect(receipt).toMatchObject({
      schema: "zyra.cli-ui-result.v1",
      pid: 4123,
      started: true,
      already_running: false,
      browser_opened: true,
    })
    expect(receipt.url).toContain("/tasks/task_demo_001?api=")
    expect(harness.builds).toBe(1)
    expect(harness.starts).toBe(1)
    expect(harness.state).toMatchObject({
      schema: UI_STATE_SCHEMA,
      pid: 4123,
      api_origin: "http://127.0.0.1:8000",
    })
    expect(JSON.stringify(harness.state)).not.toContain("task_demo_001")
    expect(harness.opened).toEqual([receipt.url])
  })

  test("reuses a healthy product endpoint and fails closed on a foreign port", async () => {
    const healthy = environment(["zyra"])
    healthy.seed({
      schema: UI_STATE_SCHEMA,
      pid: 4123,
      generation: "generation-existing",
      web_origin: "http://127.0.0.1:5173",
      api_origin: "http://127.0.0.1:8000",
      started_at: "2026-08-05T09:00:00Z",
      project_id: "zyra",
    })
    const replay = await launchUi({
      baseUrl: "http://127.0.0.1:8000",
      webPort: 5173,
      startupTimeoutMs: 10_000,
      open: false,
    }, healthy.value)
    expect(replay).toMatchObject({
      started: false,
      already_running: true,
      generation: "generation-existing",
    })
    expect(healthy.builds).toBe(0)
    expect(healthy.starts).toBe(0)

    const occupied = environment(["occupied"])
    await expect(launchUi({
      baseUrl: "http://127.0.0.1:8000",
      webPort: 5173,
      startupTimeoutMs: 10_000,
      open: false,
    }, occupied.value)).rejects.toMatchObject({ code: "ui_port_conflict" })
  })

  test("reuses the recorded generation before reserving another ephemeral port", async () => {
    const harness = environment(["zyra"])
    harness.seed({
      schema: UI_STATE_SCHEMA,
      pid: 4123,
      generation: "generation-recorded",
      web_origin: "http://127.0.0.1:43127",
      api_origin: "http://127.0.0.1:8000",
      started_at: "2026-08-05T10:00:00.000Z",
      project_id: "zyra",
    })
    const receipt = await launchUi({
      baseUrl: "http://127.0.0.1:8010",
      webPort: 0,
      startupTimeoutMs: 5_000,
      open: false,
    }, harness.value)
    expect(receipt).toMatchObject({
      web_origin: "http://127.0.0.1:43127",
      api_origin: "http://127.0.0.1:8010",
      pid: 4123,
      generation: "generation-recorded",
      started: false,
      already_running: true,
    })
    expect(harness.starts).toBe(0)
    expect(harness.builds).toBe(0)
  })
})
