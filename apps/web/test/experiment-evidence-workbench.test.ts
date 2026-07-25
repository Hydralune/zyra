import { describe, expect, test } from "bun:test"
import {
  OPERATION_NAMES,
  coreProtocolCatalog,
} from "../../../packages/core/typed-api-client/src/index.ts"
import type {
  ExperimentApi,
  ExperimentRegistryProjection,
  ExperimentRunProjection,
  ExperimentStatusProjection,
} from "../src/api/experiment-api.ts"
import {
  ExperimentProjectionStore,
  ExperimentWorkbenchRuntime,
  REQUIRED_ABLATIONS,
  REQUIRED_BASELINES,
  REQUIRED_REQUIREMENTS,
  assessBundle,
  assessRegistry,
  assessReport,
  reviewerGraph,
} from "../src/features/experiments/index.ts"

const DIGEST = "a".repeat(64)

function registry(): ExperimentRegistryProjection {
  const variantIds = [...REQUIRED_BASELINES, ...REQUIRED_ABLATIONS]
  return {
    schema: "zyra.experiment-registry/v1",
    variants: variantIds.map((variantId, index) => ({
      schema: "zyra.experiment-variant/v1",
      variant_id: variantId,
      kind: index < 3 ? "baseline" : "ablation",
      title: variantId,
      description: variantId,
      capabilities: {},
      comparison_anchor: "dynamic_heterogeneous_swarm",
      expected_disabled_capability: index < 3 ? "" : variantId,
      required: true,
      definition_digest: DIGEST,
      metadata: {},
    })),
    metrics: Array.from({ length: 32 }, (_, index) => ({
      metric: `metric_${index}`,
      unit: "ratio",
      direction: "higher_is_better",
      category: "test",
      required: true,
      minimum_samples: 3,
      description: "controlled metric",
      requirement_ids: ["REQ-CLOSE-01"],
    })),
    requirements: REQUIRED_REQUIREMENTS.map((requirementId, index) => ({
      requirement_id: requirementId,
      score: index < 6
        ? [15, 15, 15, 10, 10, 10][index]
        : index < 11
          ? 5
          : 0,
      title: requirementId,
      owner: "Zyra",
      required_metrics: ["metric_0"],
      required_variant_ids: ["dynamic_heterogeneous_swarm"],
      evidence_categories: ["metric"],
      stage_gate: true,
      description: requirementId,
    })),
    capabilities: {
      baseline_matrix: true,
      ablation_matrix: true,
      raw_samples: true,
      p50_p95: true,
      dispersion: true,
      confidence: true,
      tamper_evident_bundle: true,
      reviewer_navigation: true,
      browser_connection_required: false,
      authenticated_provider_cli_allowed: false,
      external_model_request_allowed: false,
    },
  }
}

function run(): ExperimentRunProjection {
  const variants = registry().variants
  const cells = variants.flatMap((variant) =>
    [1, 2, 3].map((repetition) => ({
      schema: "zyra.experiment-matrix-cell/v1",
      cell_id: `cell_${variant.variant_id}_${repetition}`,
      variant_id: variant.variant_id,
      repetition,
      seed: repetition,
      envelope_digest: DIGEST,
      phase: "succeeded" as const,
      terminal: true,
      revision: 2,
      created_at: "2026-07-26T00:00:00.000Z",
      updated_at: "2026-07-26T00:00:01.000Z",
      started_at: "2026-07-26T00:00:00.000Z",
      completed_at: "2026-07-26T00:00:01.000Z",
      scenario_run_id: "scenario_owner",
      owner_run_id: "run_owner",
      task_id: "task_owner",
      observation_digest: DIGEST,
      sample_ids: [`sample_${variant.variant_id}_${repetition}`],
      verification_receipt: { valid: true },
    }))
  )
  return {
    schema: "zyra.experiment-run/v1",
    experiment_id: "experiment_owner",
    title: "Formal matrix",
    phase: "succeeded",
    terminal: true,
    revision: 48,
    repetitions: 3,
    envelope: {
      schema: "zyra.experiment-comparison-envelope/v1",
      envelope_id: "envelope_owner",
      scenario_id: "scenario_owner",
      scenario_definition_digest: DIGEST,
      task_input_digest: DIGEST,
      task_input_bytes: 100,
      task_domain: "software_delivery",
      commit_sha: "b".repeat(40),
      environment_digest: DIGEST,
      source_evidence_digest: DIGEST,
      sealed_policy_digest: DIGEST,
      budget: {},
      hardware: {},
      provider: {},
      verifier: {},
      failure_schedule: {},
      seed_plan: [1, 2, 3],
      created_at: "2026-07-26T00:00:00.000Z",
      envelope_digest: DIGEST,
      labels: {},
      metadata: {},
    },
    variants,
    cells,
    created_at: "2026-07-26T00:00:00.000Z",
    updated_at: "2026-07-26T00:00:02.000Z",
    started_at: "2026-07-26T00:00:00.000Z",
    completed_at: "2026-07-26T00:00:02.000Z",
    requested_by: "test",
    report: { report_digest: DIGEST },
    bundle_manifest: { manifest_digest: DIGEST },
    verification_receipt: { valid: true },
    cancel_requested: false,
    archive_reason: "",
    metadata: {},
    progress: { planned: 21, succeeded: 21, failed: 0, terminal: 21 },
  }
}

function report() {
  const summaries = registry().variants.map((variant) => ({
    metric: "quality_score",
    variant_id: variant.variant_id,
    unit: "ratio",
    count: 3,
    p50: 0.9,
    p95: 0.95,
    mean: 0.91,
    standard_deviation: 0.01,
    median_absolute_deviation: 0.01,
    interquartile_range: 0.02,
    confidence: { lower: 0.89, upper: 0.93 },
  }))
  return {
    schema: "zyra.experiment-report-response/v1",
    experiment_id: "experiment_owner",
    report: {
      schema: "zyra.experiment-final-report/v1",
      report_digest: DIGEST,
      matrix: {
        variant_cards: registry().variants.map((variant) => ({ variant })),
      },
      statistics: {
        raw_sample_count: 672,
        summary_count: summaries.length,
        comparison_count: 6,
        p50_present_count: summaries.length,
        p95_present_count: summaries.length,
        dispersion_present_count: summaries.length,
        confidence_present_count: summaries.length,
        summaries,
        comparisons: [{
          metric: "quality_score",
          baseline_variant_id: "dynamic_heterogeneous_swarm",
          compared_variant_id: "no_scheduler",
          absolute_delta: -0.1,
          relative_delta: -0.11,
          effect_direction: "worse",
        }],
      },
      requirements: {
        requirement_count: REQUIRED_REQUIREMENTS.length,
        verified_count: REQUIRED_REQUIREMENTS.length,
        score_total: 100,
        score_verified: 100,
        rows: registry().requirements.map((item) => ({
          requirement_id: item.requirement_id,
          score: item.score,
          verified: true,
          metric_names: ["quality_score"],
          variant_ids: ["dynamic_heterogeneous_swarm"],
          evidence_ids: ["sample_owner"],
          findings: [],
        })),
      },
      reviewer_navigation: {
        backend_log_required: false,
        nodes: [{
          node_id: "requirement:REQ-CLOSE-01",
          kind: "requirement",
          label: "REQ-CLOSE-01",
          route: "/experiments/experiment_owner/requirements",
          digest: DIGEST,
        }],
        edges: [{
          from: "requirement:REQ-CLOSE-01",
          to: "metric:quality_score",
          relation: "supported_by",
        }],
      },
    },
  }
}

function bundle() {
  const categories = [
    "source_manifest",
    "canonical_event",
    "raw_metric",
    "metric_summary",
    "comparison",
    "requirement",
    "report",
    "configuration",
    "verification",
    "navigation",
  ]
  return {
    schema: "zyra.experiment-bundle-response/v1",
    bundle: {
      path: "artifacts/experiment-evidence/bundle_owner.zip",
      sha256: DIGEST,
      manifest: {
        root_digest: DIGEST,
        members: categories.map((category) => ({
          path: `${category}/evidence.json`,
          category,
          sha256: DIGEST,
          size: 10,
        })),
      },
      verification: { valid: true, receipt_digest: DIGEST },
    },
  }
}

function status(): ExperimentStatusProjection {
  return {
    schema: "zyra.experiment-status/v1",
    run: run(),
    transitions: [],
    receipts: [{
      schema: "zyra.experiment-reverification/v1",
      valid: true,
    }],
    bundles: [],
    sample_count: 672,
    sample_status_counts: { observed: 672 },
    worker_active: false,
    browser_connection_required: false,
    status_digest: DIGEST,
  }
}

class FakeExperimentApi {
  calls: string[] = []
  async registry() {
    this.calls.push("registry")
    return registry()
  }
  async list() {
    this.calls.push("list")
    return {
      schema: "zyra.experiment-run-page/v1",
      offset: 0,
      limit: 500,
      runs: [run()],
      store: {},
    }
  }
  async get() {
    this.calls.push("get")
    return status()
  }
  async report() {
    this.calls.push("report")
    return report()
  }
  async bundle() {
    this.calls.push("bundle")
    return bundle()
  }
  async requirements() {
    this.calls.push("requirements")
    return {
      requirements: report().report.requirements,
      reviewer_navigation: report().report.reviewer_navigation,
    }
  }
  async samples() {
    this.calls.push("samples")
    return {
      schema: "zyra.experiment-raw-sample-page/v1",
      experiment_id: "experiment_owner",
      offset: 0,
      limit: 10_000,
      filters: {},
      samples: [],
      page_digest: DIGEST,
    }
  }
  async start() {
    this.calls.push("start")
    return run()
  }
  async verify() {
    this.calls.push("verify")
    return { verification_receipt: { valid: true } }
  }
  async archive() {
    this.calls.push("archive")
    return { ...run(), phase: "archived" as const }
  }
}

describe("experiment evidence workbench", () => {
  test("registers every typed experiment endpoint", () => {
    const operations = new Set(
      coreProtocolCatalog().list().map((item) => item.operation),
    )
    for (const operation of [
      OPERATION_NAMES.experimentRegistry,
      OPERATION_NAMES.experimentRunList,
      OPERATION_NAMES.experimentRunGet,
      OPERATION_NAMES.experimentRunReport,
      OPERATION_NAMES.experimentRunSamples,
      OPERATION_NAMES.experimentRunBundle,
      OPERATION_NAMES.experimentRunSource,
      OPERATION_NAMES.experimentRunRequirements,
      OPERATION_NAMES.experimentRunCreate,
      OPERATION_NAMES.experimentRunStart,
      OPERATION_NAMES.experimentRunCancel,
      OPERATION_NAMES.experimentRunArchive,
      OPERATION_NAMES.experimentRunVerify,
    ]) {
      expect(operations.has(operation)).toBe(true)
    }
  })

  test("fails closed on missing matrix, score, statistics or bundle categories", () => {
    expect(assessRegistry(registry()).valid).toBe(true)
    expect(assessRegistry({
      ...registry(),
      variants: registry().variants.slice(0, 6),
    }).valid).toBe(false)
    expect(assessReport(report()).valid).toBe(true)
    expect(assessReport({
      report: {
        ...report().report,
        statistics: { raw_sample_count: 0 },
      },
    }).valid).toBe(false)
    expect(assessBundle(bundle()).valid).toBe(true)
    expect(assessBundle({
      bundle: {
        ...bundle().bundle,
        manifest: { root_digest: DIGEST, members: [] },
      },
    }).valid).toBe(false)
  })

  test("projects reviewer path without backend logs", () => {
    const graph = reviewerGraph(report())
    expect(graph.backendLogRequired).toBe(false)
    expect(graph.nodes[0]?.kind).toBe("requirement")
    expect(graph.edges[0]?.relation).toBe("supported_by")

    const store = new ExperimentProjectionStore()
    store.registry(registry())
    store.replaceRuns([run()])
    store.select("experiment_owner")
    store.observeStatus(status())
    store.report(report())
    store.bundle(bundle())
    const projection = store.getSnapshot()
    expect(projection.reportAdmission?.valid).toBe(true)
    expect(projection.bundleAssessment?.valid).toBe(true)
    expect(projection.headline.scoreVerified).toBe(100)
    expect(projection.requirementRows).toHaveLength(REQUIRED_REQUIREMENTS.length)
  })

  test("restores backend-owned status and detach never cancels execution", async () => {
    const api = new FakeExperimentApi()
    const runtime = new ExperimentWorkbenchRuntime({
      api: api as unknown as ExperimentApi,
      pollIntervalMs: 60_000,
      activePollIntervalMs: 60_000,
    })
    const opened = await runtime.open()
    expect(opened.connection).toBe("online")
    expect(opened.selected?.run.experiment_id).toBe("experiment_owner")
    expect(opened.reportAdmission?.valid).toBe(true)
    expect(opened.bundleAssessment?.valid).toBe(true)
    expect(api.calls).toEqual([
      "registry",
      "list",
      "get",
      "report",
      "bundle",
      "requirements",
      "samples",
    ])
    runtime.detach("test browser close")
    expect(runtime.audit().detached).toBe(true)
    expect(runtime.audit().backendCancellationOnClose).toBe(false)
    expect(api.calls.some((item) => item === "cancel")).toBe(false)
    runtime.close()
  })
})
