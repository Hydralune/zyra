import { describe, expect, test } from "bun:test"
import { createServer } from "node:net"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import {
  DAEMON_STATE_SCHEMA,
  daemonRuntimeStateRoot,
  daemonHealthOwnsState,
  managedDaemonLaunchConflict,
  selectDeploymentProfileBasePort,
  type DaemonHealthIdentity,
  type DaemonState,
} from "../src/daemon.ts"

const state: DaemonState = {
  schema: DAEMON_STATE_SCHEMA,
  pid: 51512,
  generation: "generation-owned",
  base_url: "http://127.0.0.1:8000",
  started_at: "2026-08-10T12:00:00.000Z",
  project_id: "zyra",
}

describe("daemon process identity", () => {
  test("requires both the health PID and generation before a process is managed", () => {
    const owned: DaemonHealthIdentity = {
      healthy: true,
      pid: state.pid,
      generation: state.generation,
    }
    expect(daemonHealthOwnsState(state, owned)).toBe(true)
    expect(daemonHealthOwnsState(state, { ...owned, pid: 5668 })).toBe(false)
    expect(daemonHealthOwnsState(state, { ...owned, generation: "generation-stale" })).toBe(false)
    expect(daemonHealthOwnsState(state, { ...owned, healthy: false })).toBe(false)
    expect(daemonHealthOwnsState(undefined, owned)).toBe(false)
  })

  test("skips an occupied deployment port block before daemon launch", async () => {
    const occupied = createServer()
    await new Promise<void>((resolve, reject) => {
      occupied.once("error", reject)
      occupied.listen({ host: "127.0.0.1", port: 0, exclusive: true }, resolve)
    })
    try {
      const address = occupied.address()
      if (!address || typeof address === "string") throw new Error("test port unavailable")
      const selected = await selectDeploymentProfileBasePort({
        apiPort: 8000,
        candidates: [address.port, 40_000, 41_000, 42_000, 43_000, 44_000],
      })
      expect(selected).not.toBe(address.port)
      expect(selected).toBeGreaterThanOrEqual(40_000)
    } finally {
      await new Promise<void>((resolve) => occupied.close(() => resolve()))
    }
  })

  test("isolates managed API state by daemon generation", () => {
    const first = daemonRuntimeStateRoot("generation-a", "")
    const second = daemonRuntimeStateRoot("generation-b", "")
    const explicit = join(tmpdir(), "explicit-zyra-state")
    expect(first).not.toBe(second)
    expect(first).toContain("generation-a")
    expect(daemonRuntimeStateRoot("ignored", explicit)).toBe(resolve(explicit))
  })

  test("reports a live managed daemon on another origin as a launch conflict", () => {
    expect(managedDaemonLaunchConflict(state, state.base_url, true)).toBe(false)
    expect(managedDaemonLaunchConflict(state, "http://127.0.0.1:8170", true)).toBe(true)
    expect(managedDaemonLaunchConflict(state, "http://127.0.0.1:8170", false)).toBe(false)
    expect(managedDaemonLaunchConflict(undefined, "http://127.0.0.1:8170", true)).toBe(false)
  })
})
