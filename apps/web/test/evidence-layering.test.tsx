import { describe, expect, test } from "bun:test"
import { evidenceSourceStatus } from "../src/features/evidence/index.ts"

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

  test("keeps product, evidence, scenario, and causal exits on real routes", async () => {
    const app = await Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text()
    const evidence = await Bun.file(new URL("../src/features/evidence/evidence-workbench.tsx", import.meta.url)).text()
    const detail = await Bun.file(new URL("../src/components/tasks/task-detail.tsx", import.meta.url)).text()
    expect(app).toContain('route.kind === "evidence"')
    expect(app).toContain("<EvidenceWorkbench")
    expect(app).toContain("证据中心")
    expect(evidence).toContain("CLI 与 Web 不互相同步")
    expect(evidence).toContain("<TaskDetail runtime={runtime} state={state} />")
    expect(evidence).toContain("<ScenarioWorkbench runtime={runtime.scenarioConsole} />")
    expect(evidence).toContain("physical_identity.location")
    expect(detail).toContain('id="evidence-topology"')
    expect(detail).toContain('id="evidence-continuity-placement"')
    expect(detail).toContain('id="evidence-recovery"')
    expect(detail).toContain('id="evidence-artifacts"')
  })
})
