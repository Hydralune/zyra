import { describe, expect, test } from "bun:test"
import type {
  ScenarioApi,
  ScenarioCreateInput,
  ScenarioDefinitionProjection,
  ScenarioRegistryProjection,
  ScenarioRunProjection,
  ScenarioStatusProjection,
} from "../src/api/scenario-api.ts"
import { ScenarioApi as TypedScenarioApi } from "../src/api/scenario-api.ts"
import { ZyraApiClient } from "../src/api/client.ts"
import {
  ScenarioProjectionStore,
  ScenarioWorkbenchRuntime,
  assessEvidence,
  assessScenarioAdmission,
  projectSourceAudit,
  validateScenarioRegistry,
} from "../src/features/scenarios/index.ts"

const DIGEST = "a".repeat(64)
const OTHER_DIGEST = "b".repeat(64)

function definition(): ScenarioDefinitionProjection {
  return {
    schema: "zyra.scenario-definition/v1",
    scenario_id: "foundation.short-owner-chain",
    version: "1.0.0",
    title: "Foundation",
    domain: "cross-domain-foundation",
    definition_digest: DIGEST,
    required_owner_stages: [
      "api",
      "task",
      "scheduler",
      "memory",
      "permission",
      "fault",
      "recovery",
      "artifact",
      "evidence",
    ],
    expected_effects: [
      "state_mutation",
      "route",
      "memory",
      "permission",
      "fault",
      "recovery",
      "artifact",
      "verification",
    ],
    minimum_effective_steps: 8,
    metadata: { formal_long_run: false },
  }
}

function registry(): ScenarioRegistryProjection {
  return {
    schema: "zyra.scenario-registry/v1",
    revision: 1,
    definitions: [definition()],
    profiles: [{ profile_id: "foundation.local-sealed" }],
    policies: [{
      policy_id: "sealed-autonomous-foundation",
      policy_digest: DIGEST,
    }],
    registry_digest: DIGEST,
  }
}

function preflight(clean = true, newInput = true) {
  return {
    clean,
    new_input: newInput,
    input_digest: DIGEST,
    receipt_digest: DIGEST,
    checks: ["database", "cache", "index", "artifact", "build"].map(
      (kind) => ({
        kind,
        clean,
        policy: kind === "database"
          ? "sqlite_no_user_rows"
          : "absent_or_empty",
        digest: DIGEST,
      }),
    ),
  }
}

function sourceAudit() {
  return {
    valid: true,
    openclaw: "excluded_forward_only",
    rows: [
      {
        source: "zyra",
        capability: "scenario_runner_evidence_owner",
        role: "zyra_owned_primary",
        status: "active",
        landing: ["packages/evaluation/zyra_evaluation/scenario_runner"],
        reason: "owner",
      },
      {
        source: "opencode",
        capability: "console_patterns",
        role: "role_aware_audit_only",
        status: "inactive",
        landing: ["apps/web/src/features/scenarios"],
        reason: "audit only",
      },
      {
        source: "openclaw",
        capability: "all_forward_roles",
        role: "excluded_forward_only",
        status: "inactive",
        landing: [],
        reason: "forward exclusion",
      },
    ],
    findings: [],
  }
}

function evidence() {
  const effects = {
    state_mutation: 1,
    route: 1,
    memory: 1,
    permission: 1,
    fault: 1,
    recovery: 1,
    artifact: 1,
    verification: 1,
  }
  return {
    manifest_id: "manifest_owner",
    manifest_digest: DIGEST,
    effective_steps: {
      formal_valid: true,
      admitted_step_ids: Object.keys(effects).map((_, index) => `step_${index}`),
      excluded_step_ids: ["step_heartbeat", "step_ui"],
      invalid_step_ids: [],
      effect_counts: effects,
    },
    artifacts: [{
      artifact_id: "artifact_owner",
      sha256: DIGEST,
      size: 100,
    }],
    canonical_events: [{
      sequence: 1,
      event_id: "event_owner",
      event_digest: DIGEST,
      previous_digest: "",
      chain_digest: DIGEST,
    }],
    metrics: {
      summary: {
        raw_sample_count: 18,
        effective_by_effect: effects,
        effective_by_stage: {
          task: 1,
          scheduler: 1,
          memory: 1,
          permission: 1,
          fault: 1,
          recovery: 1,
          artifact: 1,
          verification: 1,
        },
        effective_by_provider: { "zyra-local": 8 },
      },
    },
    claims: {
      human_intervention_count: 0,
      legacy_demo_fallback: false,
      long_live_scenario_complete: false,
      two_thousand_step_gate_complete: false,
      edge_cloud_dispatch_complete: false,
      m2_exit_complete: false,
    },
    source_audit: sourceAudit(),
  }
}

function run(
  patch: Partial<ScenarioRunProjection> = {},
): ScenarioRunProjection {
  return {
    schema: "zyra.scenario-run/v1",
    scenario_run_id: "scenario_owner",
    configuration: {
      scenario_id: "foundation.short-owner-chain",
      definition_version: "1.0.0",
      definition_digest: DIGEST,
      input_digest: DIGEST,
      mode: "sealed",
      expected_policy_digest: DIGEST,
      policy: {
        policy_digest: DIGEST,
        ask_disposition: "deny_and_replan",
        unknown_disposition: "deny_and_replan",
        metadata: { sealed: true },
      },
    },
    phase: "admitted",
    terminal: false,
    revision: 1,
    created_at: "2026-07-25T00:00:00.000Z",
    updated_at: "2026-07-25T00:00:01.000Z",
    started_at: "",
    completed_at: "",
    owner_run_id: "",
    task_id: "",
    preflight_receipt: preflight(),
    policy_decisions: [],
    interventions: [],
    human_intervention_count: 0,
    operator_intervention_attempt_count: 0,
    cancel_requested: false,
    archive_reason: "",
    ...patch,
  }
}

function status(value = run()): ScenarioStatusProjection {
  return {
    schema: "zyra.scenario-status/v1",
    run: value,
    transitions: [],
    receipts: [],
    worker_active: !value.terminal,
    browser_connection_required: false,
    status_digest: value.revision === 1 ? DIGEST : OTHER_DIGEST,
  }
}

class FakeScenarioApi {
  current = run()
  calls: string[] = []
  async registry() {
    this.calls.push("registry")
    return registry()
  }
  async list() {
    this.calls.push("list")
    return {
      schema: "zyra.scenario-run-page/v1",
      offset: 0,
      limit: 100,
      runs: [this.current],
      store: {},
    }
  }
  async get() {
    this.calls.push("get")
    return status(this.current)
  }
  async create(input: ScenarioCreateInput) {
    this.calls.push(`create:${input.input}`)
    this.current = run({ revision: 2 })
    return this.current
  }
  async start() {
    this.calls.push("start")
    this.current = run({
      phase: "running",
      revision: 3,
      started_at: "2026-07-25T00:00:02.000Z",
    })
    return this.current
  }
  async cancel() {
    this.calls.push("cancel")
    this.current = run({
      phase: "cancelled",
      terminal: true,
      revision: 4,
    })
    return this.current
  }
  async archive() {
    this.calls.push("archive")
    this.current = run({
      phase: "archived",
      terminal: true,
      revision: 5,
    })
    return this.current
  }
  async verify() {
    this.calls.push("verify")
    return { verification_receipt: { valid: true } }
  }
}

describe("scenario admission and evidence", () => {
  test("accepts clean sealed admission and rejects dirty/manual state", () => {
    const accepted = assessScenarioAdmission(run())
    expect(accepted.valid).toBe(true)
    expect(accepted.clean).toBe(true)
    expect(accepted.newInput).toBe(true)
    expect(accepted.humanInterventionCount).toBe(0)

    const rejected = assessScenarioAdmission(
      run({
        preflight_receipt: preflight(false, false),
        operator_intervention_attempt_count: 1,
      }),
    )
    expect(rejected.valid).toBe(false)
    expect(rejected.findings.map((item) => item.code)).toContain("preflight_dirty")
    expect(rejected.findings.map((item) => item.code)).toContain("input_not_new")
    expect(rejected.findings.map((item) => item.code)).toContain(
      "operator_attempt_present",
    )
  })

  test("projects semantic evidence and preserves later-gate non-claims", () => {
    const selected = run({
      phase: "succeeded",
      terminal: true,
      revision: 8,
      evidence_manifest: evidence(),
      verification_receipt: { valid: true },
    })
    const assessment = assessEvidence(selected)

    expect(assessment.valid).toBe(true)
    expect(assessment.effectiveStepCount).toBe(8)
    expect(assessment.excludedStepCount).toBe(2)
    expect(assessment.invalidStepCount).toBe(0)
    expect(assessment.effects.permission).toBe(1)
    expect(assessment.providers["zyra-local"]).toBe(8)

    const inflated = assessEvidence({
      ...selected,
      evidence_manifest: {
        ...evidence(),
        effective_steps: {
          ...evidence().effective_steps,
          invalid_step_ids: ["step_no_causation"],
          formal_valid: false,
        },
      },
    })
    expect(inflated.valid).toBe(false)
    expect(inflated.findings.map((item) => item.code)).toContain(
      "effective_step_batch_invalid",
    )
    expect(inflated.findings.map((item) => item.code)).toContain(
      "invalid_steps_present",
    )
  })

  test("projects verified dual-domain live evidence and rejects an incomplete claim", () => {
    const admitted = Array.from(
      { length: 2_100 },
      (_, index) => `live-step-${index + 1}`,
    )
    const claims = {
      human_intervention_count: 0,
      legacy_demo_fallback: false,
      long_live_scenario_complete: true,
      two_thousand_step_gate_complete: true,
      edge_cloud_dispatch_complete: false,
      provider_model_capabilities_complete: false,
      external_provider_execution_excluded: true,
      authenticated_provider_cli_invoked: false,
      fault_change_matrix_complete: true,
      domain_verifier_complete: true,
      causal_archive_complete: true,
      m2_exit_complete: false,
    }
    const selected = run({
      phase: "succeeded",
      terminal: true,
      revision: 9,
      configuration: {
        ...run().configuration,
        scenario_id: "live.software-delivery",
      },
      evidence_manifest: {
        ...evidence(),
        effective_steps: {
          ...evidence().effective_steps,
          admitted_step_ids: admitted,
        },
        claims,
        live_domain: {
          summary: {
            domain: "software_delivery",
            fault_count: 5,
            recovered_fault_count: 5,
          },
          domain_verification: { valid: true },
          placement_verification: { valid: true },
          causal_archive: { manifest_digest: OTHER_DIGEST },
          tier_count: 3,
          provider_model_capability_count: 2,
        },
      },
      verification_receipt: { valid: true },
    })
    const assessment = assessEvidence(selected)
    expect(assessment.valid).toBe(true)
    expect(assessment.live?.complete).toBe(true)
    expect(assessment.live?.effectiveTransitionCount).toBe(2_100)
    expect(assessment.live?.faultCount).toBe(5)
    expect(assessment.live?.tierCount).toBe(3)
    expect(assessment.live?.providerModelCapabilityCount).toBe(0)
    expect(assessment.live?.archiveDigest).toBe(OTHER_DIGEST)

    const incomplete = assessEvidence({
      ...selected,
      evidence_manifest: {
        ...selected.evidence_manifest,
        claims: {
          ...claims,
          external_provider_execution_excluded: false,
        },
      },
    })
    expect(incomplete.valid).toBe(false)
    expect(incomplete.findings.map((item) => item.path)).toContain(
      "evidence_manifest.claims.external_provider_execution_excluded",
    )
  })

  test("distinguishes inactive source roles from missing capability", () => {
    const projection = projectSourceAudit(sourceAudit())

    expect(projection.valid).toBe(true)
    expect(projection.active).toHaveLength(1)
    expect(projection.inactiveNotMissing).toHaveLength(2)
    expect(projection.openclaw).toBe("excluded_forward_only")
  })

  test("rejects conflicting registry revisions and incomplete definitions", () => {
    expect(validateScenarioRegistry(registry())).toHaveLength(0)
    const invalid = validateScenarioRegistry({
      ...registry(),
      registry_digest: "bad",
      definitions: [{
        ...definition(),
        expected_effects: [],
      }],
    })
    expect(invalid.map((item) => item.code)).toContain("registry_digest_invalid")
    expect(invalid.map((item) => item.code)).toContain(
      "definition_effects_missing",
    )
  })
})

describe("scenario durable workbench runtime", () => {
  test("typed API routes registry and create to the scenario owner", async () => {
    const requests: Request[] = []
    const client = new ZyraApiClient({
      baseUrl: "http://scenario.test",
      fetch: (async (input, init) => {
        const request = new Request(input, init)
        requests.push(request)
        const body = request.method === "GET"
          ? registry()
          : { run: run() }
        return new Response(JSON.stringify(body), {
          status: request.method === "GET" ? 200 : 201,
          headers: {
            "Content-Type": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Request-Id": request.headers.get("X-Request-Id") ?? "",
          },
        })
      }) as typeof fetch,
    })
    const api = new TypedScenarioApi(client)

    const catalog = await api.registry()
    const created = await api.create({ input: "typed client scenario input" })

    expect(catalog.registry_digest).toBe(DIGEST)
    expect(created.scenario_run_id).toBe("scenario_owner")
    expect(new URL(requests[0]!.url).pathname).toBe("/scenarios/registry")
    expect(new URL(requests[1]!.url).pathname).toBe("/scenarios/runs")
    expect(requests[1]!.headers.has("Idempotency-Key")).toBe(true)
    client.close()
  })

  test("loads, creates and starts through the scenario API owner", async () => {
    const api = new FakeScenarioApi()
    const runtime = new ScenarioWorkbenchRuntime({
      api: api as unknown as ScenarioApi,
      options: {
        pollIntervalMs: 60_000,
        activePollIntervalMs: 60_000,
      },
    })
    await runtime.open()
    expect(runtime.getSnapshot().connection).toBe("online")
    expect(runtime.getSnapshot().rows).toHaveLength(1)

    await runtime.create({ input: "new workbench input" })
    await runtime.start("scenario_owner")

    expect(api.calls).toContain("create:new workbench input")
    expect(api.calls).toContain("start")
    expect(runtime.getSnapshot().selectedRunId).toBe("scenario_owner")
    expect(runtime.getSnapshot().selected?.run.phase).toBe("running")
    runtime.close()
  })

  test("panel detach and close never cancel backend execution", async () => {
    const api = new FakeScenarioApi()
    const runtime = new ScenarioWorkbenchRuntime({
      api: api as unknown as ScenarioApi,
      options: {
        pollIntervalMs: 60_000,
        activePollIntervalMs: 60_000,
      },
    })
    await runtime.open()
    await runtime.start("scenario_owner")
    runtime.detach("Browser view closed.")
    expect(api.calls).not.toContain("cancel")
    expect(runtime.audit().backendCancellationOnClose).toBe(false)
    expect(runtime.audit().detached).toBe(true)

    api.current = run({
      phase: "succeeded",
      terminal: true,
      revision: 4,
      evidence_manifest: evidence(),
      verification_receipt: { valid: true },
    })
    runtime.attach()
    await runtime.refresh("reconnect")
    expect(runtime.getSnapshot().selected?.run.phase).toBe("succeeded")
    runtime.close("Browser destroyed.")
    expect(api.calls).not.toContain("cancel")
  })

  test("disabled workbench has no local or demo fallback", async () => {
    const api = new FakeScenarioApi()
    const runtime = new ScenarioWorkbenchRuntime({
      api: api as unknown as ScenarioApi,
      options: { disabled: true },
    })

    expect(() => runtime.getSnapshot()).not.toThrow()
    await expect(runtime.open()).rejects.toThrow("disabled")
    await expect(
      runtime.create({ input: "must not execute" }),
    ).rejects.toThrow("disabled")
    expect(api.calls).toHaveLength(0)
    runtime.close()
  })

  test("projection rejects stale and conflicting lifecycle observations", () => {
    const store = new ScenarioProjectionStore()
    const first = run({ revision: 2 })
    expect(store.observeRun(first)).toBe(true)
    expect(store.observeRun(run({ revision: 1 }))).toBe(false)
    expect(
      store.observeRun(
        run({
          revision: 2,
          phase: "running",
          updated_at: "2026-07-25T00:00:02.000Z",
        }),
      ),
    ).toBe(false)
    expect(store.getSnapshot().staleResponses).toBe(1)
    expect(store.getSnapshot().conflictingResponses).toBe(1)
  })
})
