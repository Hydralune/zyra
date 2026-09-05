import { describe, expect, test } from "bun:test"

import { CONTRACT_NAMES, OPERATION_NAMES } from "./constants.ts"
import { normalizeSessionDetail, normalizeSessionList } from "./normalizers.ts"
import { coreProtocolCatalog } from "./protocol.ts"

const TASK_A = "task_000000000000001_0123456789abcdefabcd"
const TASK_B = "task_000000000000002_0123456789abcdefabcd"
const SESSION = "session_000000000000001_0123456789abcdefabcd"

function session(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: SESSION,
    title: "Fix CLI session state",
    task_ids: [TASK_A],
    active_task_ids: [TASK_A],
    latest_task_id: TASK_A,
    resume_task_id: TASK_A,
    resolution: "resolved",
    task_count: 1,
    statuses: ["running"],
    active: true,
    terminal: false,
    created_at: "2026-08-04T00:00:00.000Z",
    updated_at: "2026-08-04T00:00:01.000Z",
    ...overrides,
  }
}

describe("task-backed session contract", () => {
  test("registers exact list and detail endpoints", () => {
    const catalog = coreProtocolCatalog()
    const list = catalog.resolve(OPERATION_NAMES.sessionList, { query: { limit: 25 } })
    expect(list.method).toBe("GET")
    expect(list.path).toBe("/sessions")
    expect(list.contract).toBe(CONTRACT_NAMES.sessionList)

    const detail = catalog.resolve(OPERATION_NAMES.sessionGet, { path: { session_id: SESSION } })
    expect(detail.path).toBe(`/sessions/${SESSION}`)
    expect(detail.contract).toBe(CONTRACT_NAMES.sessionDetail)
  })

  test("normalizes resolved and ambiguous projections without inventing an owner", () => {
    const resolved = normalizeSessionDetail({
      schema: CONTRACT_NAMES.sessionDetail,
      state_owner: "task_store_projection",
      session: session(),
    })
    expect(resolved.resumeTaskId).toBe(TASK_A)
    expect(resolved.title).toBe("Fix CLI session state")

    const list = normalizeSessionList({
      schema: CONTRACT_NAMES.sessionList,
      state_owner: "task_store_projection",
      sessions: [
        session({
          task_ids: [TASK_A, TASK_B],
          active_task_ids: [TASK_A, TASK_B],
          latest_task_id: TASK_B,
          resume_task_id: null,
          resolution: "ambiguous",
          task_count: 2,
        }),
      ],
      total: 1,
      cursor: null,
    })
    expect(list.stateOwner).toBe("task_store_projection")
    expect(list.sessions[0]?.resolution).toBe("ambiguous")
  })

  test("fails closed when ambiguity exposes a resume task", () => {
    expect(() => normalizeSessionDetail({
      state_owner: "task_store_projection",
      session: session({
        task_ids: [TASK_A, TASK_B],
        active_task_ids: [TASK_A, TASK_B],
        resolution: "ambiguous",
      }),
    })).toThrow("Ambiguous session exposed a resume task identity")
  })

  test("isolates an invalid historical entry without hiding healthy sessions", () => {
    const list = normalizeSessionList({
      schema: CONTRACT_NAMES.sessionList,
      state_owner: "task_store_projection",
      sessions: [
        session(),
        session({ session_id: "legacy-corrupt", task_ids: [], resume_task_id: null }),
      ],
      total: 2,
    })
    expect(list.sessions).toHaveLength(1)
    expect(list.sessions[0]?.sessionId).toBe(SESSION)
    expect(list.degraded).toEqual([{
      index: 1,
      code: "session_projection_invalid",
      message: expect.any(String),
    }])
  })

  test("preserves the exact scenario owner session namespace", () => {
    const id = `scenario:scenario_${"a".repeat(32)}`
    const list = normalizeSessionList({
      schema: CONTRACT_NAMES.sessionList, state_owner: "task_store_projection",
      sessions: [session({ session_id: id })], total: 1,
    })
    expect(list.sessions[0]?.sessionId).toBe(id)
    expect(list.degraded).toBeUndefined()
  })

  test("still rejects a non-canonical list owner instead of degrading it", () => {
    expect(() => normalizeSessionList({
      state_owner: "local-cache",
      sessions: [session()],
      total: 1,
    })).toThrow("not task-backed")
  })
})
