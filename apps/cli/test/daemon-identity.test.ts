import { describe, expect, test } from "bun:test"
import {
  DAEMON_STATE_SCHEMA,
  daemonHealthOwnsState,
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
})
