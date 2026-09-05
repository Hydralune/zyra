import { describe, expect, test } from "bun:test"
import { evidenceSourceStatus } from "../src/features/evidence/index.ts"
import { advancedSections, evidenceDestination, eventCategory, eventLabel, eventSummary, filterEvidenceEvents } from "../src/features/evidence/model.ts"
import type { CausalEventProjection } from "../src/state/contracts.ts"

describe("FE-S05 product and evidence layering", () => {
  test("fails closed for disconnected, stale, and missing evidence sources", () => {
    expect(evidenceSourceStatus({
      connected: false,
      ready: false,
      stale: false,
      count: 8,
    })).toBe("degraded")
    expect(evidenceSourceStatus({
      connected: true,
      ready: false,
      stale: true,
      count: 8,
    })).toBe("stale")
    expect(evidenceSourceStatus({
      connected: true,
      ready: true,
      stale: false,
      count: 0,
    })).toBe("missing")
    expect(evidenceSourceStatus({
      connected: true,
      ready: true,
      stale: false,
      count: 8,
    })).toBe("live")
  })

  test("preserves focused evidence routes and keeps scenario creation in settings", async () => {
    const app = await Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text()
    const evidence = await Bun.file(new URL("../src/features/evidence/evidence-workbench.tsx", import.meta.url)).text()
    const detail = await Bun.file(new URL("../src/components/tasks/task-detail.tsx", import.meta.url)).text()
    expect(app).toContain('route.kind === "evidence"')
    expect(app).toContain("<EvidenceWorkbench")
    expect(app).toContain("证据中心")
    expect(evidence).toContain("<TaskDetail runtime={runtime} state={state} />")
    expect(evidence).not.toContain("<ScenarioWorkbench")
    expect(evidence).toContain("<TaskEvidenceSection")
    expect(detail).toContain('id="evidence-topology"')
    expect(detail).toContain('id="evidence-continuity-placement"')
    expect(detail).toContain('id="evidence-recovery"')
    expect(detail).toContain('id="evidence-artifacts"')
  })

  test("resolves existing deep links to the appropriate tab", () => {
    expect(evidenceDestination()).toEqual({ tab: "overview", section: "evidence-overview" })
    expect(evidenceDestination("evidence-plan").tab).toBe("process")
    expect(evidenceDestination("evidence-canonical-events").tab).toBe("process")
    expect(evidenceDestination("evidence-artifacts").tab).toBe("artifacts")
    for (const entry of advancedSections) expect(evidenceDestination(entry.id)).toEqual({ tab: "advanced", section: entry.id })
    expect(evidenceDestination("unrecognized").tab).toBe("overview")
  })

  test("classifies real runtime event names without turning failures into verification successes", () => {
    const events = [
      { eventType: "runtime.task.created", eventId: "first", summary: "Legacy task_created event normalized into the runtime event spine." },
      { eventType: "runtime.artifact.committed", eventId: "second", summary: "report.md", nodeId: "verify-node" },
      { eventType: "runtime.verification.failed", eventId: "third", summary: "checksum mismatch" },
    ] as CausalEventProjection[]
    expect(eventCategory(events[0]!)).toBe("progress")
    expect(eventLabel(events[0]!)).toBe("任务已创建")
    expect(eventSummary(events[0]!)).toBe("已记录，展开查看原始事件。")
    expect(eventCategory(events[2]!)).toBe("issues")
    expect(filterEvidenceEvents(events, "verification", "").map((event) => event.eventId)).toEqual(["second"])
    expect(filterEvidenceEvents(events, "all", "VERIFY-NODE").map((event) => event.eventId)).toEqual(["second"])
    expect(filterEvidenceEvents(events, "all", "checksum").map((event) => event.eventId)).toEqual(["third"])
    expect(filterEvidenceEvents(events, "all", "不存在")).toEqual([])
  })
})
