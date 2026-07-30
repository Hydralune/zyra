import { describe, expect, test } from "bun:test"
import { renderToStaticMarkup } from "react-dom/server"
import type {
  PolicyEvidencePage,
  PolicyEvidenceTransition,
} from "../src/api/policy-api.ts"
import {
  admitPolicyEvidencePage,
} from "../src/api/policy-api.ts"
import {
  PolicyEvidenceRuntime,
  PolicyEvidenceStore,
  PolicyEvidenceView,
} from "../src/features/topology/policy/index.ts"

function transition(
  sequence: number,
  overrides: Partial<PolicyEvidenceTransition> = {},
): PolicyEvidenceTransition {
  return {
    transition_id: `${sequence}:receipt-${sequence}`,
    sequence,
    event_id: `event-${sequence}`,
    event_type: "runtime.artifact.committed",
    occurred_at: "2026-07-30T12:00:00Z",
    run_id: "run-policy",
    task_id: "task-policy",
    receipt_id: `receipt-${sequence}`,
    contract_kind: "physical_dispatch_receipt",
    schema_version: "zyra.physical-dispatch-receipt/v2",
    contract_digest: `${sequence}`.padStart(64, "a").slice(-64),
    mechanism: {
      id: "MaAS",
      version: "maas-deterministic-v1",
      lifecycle: "validation",
      readiness: "deterministic_ready",
    },
    execution: "real",
    integrity: "verified",
    disposition: "succeeded",
    constraints: [],
    graph_diff: [],
    causal_refs: [
      { kind: "attempt", id: `attempt-${sequence}`, route: "" },
      { kind: "artifact", id: `artifact-${sequence}`, route: "/artifacts" },
      { kind: "verifier", id: `verifier-${sequence}`, route: "" },
    ],
    details: {
      physical_identity: {
        location: "edge",
        process_id: 4123,
      },
    },
    ...overrides,
  }
}

function page(
  start: number,
  count: number,
  options: {
    hasMore?: boolean
    execution?: "real" | "simulated"
    status?: "ready" | "degraded"
  } = {},
): PolicyEvidencePage {
  const transitions = Array.from(
    { length: count },
    (_, index) => transition(start + index, {
      execution: options.execution ?? "real",
    }),
  )
  return {
    schema_version: "zyra.policy-evidence-projection/v1",
    projection_owner: "canonical_event_artifact_metric_read_model",
    canonical_write_allowed: false,
    status: options.status ?? "ready",
    filters: {
      run_id: "run-policy",
      task_id: "task-policy",
      mechanism_version: "",
      receipt_id: "",
      report_id: "report-real",
    },
    filter_digest: "f".repeat(64),
    cursor: start === 1 ? "" : `cursor-${start - 1}`,
    next_cursor: options.hasMore === false ? "" : `cursor-${start + count - 1}`,
    high_watermark: 2_105,
    has_more: options.hasMore !== false,
    scanned: count,
    transition_count: count,
    transitions,
    metric_report: {
      report_id: "report-real",
      status: "verified",
      digest: "m".repeat(64),
      aggregate_report: {
        metrics: {
          "dispatch.causal_chain_completeness": {
            status: "observed",
            value: 1,
            sample_count: 3,
            source_refs: ["r".repeat(64)],
          },
        },
      },
      anti_gaming: {
        simulated_dispatch_excluded_from_real_numerator: true,
      },
    },
    issues: [],
    labels: {
      lifecycle: ["validation", "diagnostic", "default", "baseline", "retired"],
      readiness: ["deterministic_ready", "evidence_only", "unavailable"],
      execution: ["real", "simulated", "degraded", "not_applicable"],
      integrity: ["verified", "pending", "missing", "stale", "inconsistent"],
    },
    snapshot_digest: "s".repeat(64),
    evidence_digest: `${start}`.padStart(64, "e").slice(-64),
  }
}

describe("policy evidence admission and incremental projection", () => {
  test("strict v1 admission rejects incompatible lifecycle and ownership", () => {
    const admitted = admitPolicyEvidencePage(page(1, 1))
    expect(admitted.transitions[0]?.mechanism.lifecycle).toBe("validation")
    expect(() => admitPolicyEvidencePage({
      ...page(1, 1),
      canonical_write_allowed: true,
    })).toThrow("ownership")
    expect(() => admitPolicyEvidencePage({
      ...page(1, 1),
      transitions: [{
        ...page(1, 1).transitions[0],
        mechanism: {
          ...page(1, 1).transitions[0]!.mechanism,
          lifecycle: "secret_default",
        },
      }],
    })).toThrow("lifecycle")
  })

  test("2105 transitions append by cursor without a load-all render", () => {
    const store = new PolicyEvidenceStore(
      { taskId: "task-policy" },
      { maximumTransitions: 5_000 },
    )
    for (let start = 1; start <= 2_105; start += 200) {
      const count = Math.min(200, 2_106 - start)
      store.append(page(start, count, {
        hasMore: start + count <= 2_105,
      }))
    }
    const snapshot = store.getSnapshot()
    expect(snapshot.transitions).toHaveLength(2_105)
    expect(snapshot.hasMore).toBeFalse()
    expect(snapshot.snapshotDigest).toBe("s".repeat(64))
    expect(snapshot.evidenceDigests).toHaveLength(11)
    expect(snapshot.transitions[0]?.sequence).toBe(1)
    expect(snapshot.transitions.at(-1)?.sequence).toBe(2_105)
  })

  test("runtime exports original page and snapshot digests", async () => {
    const pages = [
      page(1, 2, { hasMore: true }),
      page(3, 1, { hasMore: false }),
    ]
    const api = {
      async evidence() {
        const selected = pages.shift()
        if (!selected) throw new Error("unexpected extra page")
        return selected
      },
    }
    const runtime = new PolicyEvidenceRuntime({
      api: api as never,
      query: { taskId: "task-policy", reportId: "report-real" },
      pageLimit: 2,
    })
    await runtime.open()
    await runtime.loadNext()
    const exported = JSON.parse(runtime.exportLoaded())
    expect(exported.complete).toBeTrue()
    expect(exported.snapshot_digest).toBe("s".repeat(64))
    expect(exported.evidence_digests).toHaveLength(2)
    expect(exported.transitions).toHaveLength(3)
    expect(exported.canonical_write_allowed).toBeFalse()
  })

  test("simulated dispatch stays visibly simulated with clickable causal refs", () => {
    const runtime = new PolicyEvidenceRuntime({
      api: {} as never,
      query: { taskId: "task-policy" },
    })
    runtime.store.append(page(1, 1, {
      execution: "simulated",
      hasMore: false,
    }))
    const markup = renderToStaticMarkup(
      <PolicyEvidenceView runtime={runtime} />,
    )
    expect(markup).toContain('data-policy-execution="simulated"')
    expect(markup).toContain('data-policy-integrity="verified"')
    expect(markup).toContain('data-evidence-ref-kind="attempt"')
    expect(markup).toContain("deterministic_ready")
    expect(markup).toContain("Export with digest")
  })

  test("adapter and permission-style failures become degraded, never empty success", async () => {
    const api = {
      async evidence() {
        throw new Error("HTTP 403 permission rejected")
      },
    }
    const runtime = new PolicyEvidenceRuntime({
      api: api as never,
      query: { taskId: "task-policy" },
    })
    await runtime.open()
    expect(runtime.getSnapshot().connection).toBe("degraded")
    expect(runtime.getSnapshot().error).toContain("403")
    expect(runtime.getSnapshot().transitions).toHaveLength(0)
  })
})
