import {
  OPERATION_NAMES,
  createIdempotencyKey,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export type ExperimentPhase =
  | "created"
  | "admitted"
  | "queued"
  | "running"
  | "aggregating"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "archived"

export interface ExperimentVariantProjection {
  schema: string
  variant_id: string
  kind: "baseline" | "ablation"
  title: string
  description: string
  capabilities: Readonly<Record<string, unknown>>
  comparison_anchor: string
  expected_disabled_capability: string
  required: boolean
  definition_digest: string
  metadata: Readonly<Record<string, unknown>>
}

export interface ExperimentMetricProjection {
  metric: string
  unit: string
  direction:
    | "higher_is_better"
    | "lower_is_better"
    | "target_is_better"
    | "descriptive"
  category: string
  required: boolean
  minimum_samples: number
  target?: number
  bounded_minimum?: number
  bounded_maximum?: number
  description: string
  requirement_ids: readonly string[]
}

export interface ExperimentRequirementProjection {
  requirement_id: string
  score: number
  title: string
  owner: string
  required_metrics: readonly string[]
  required_variant_ids: readonly string[]
  evidence_categories: readonly string[]
  stage_gate: boolean
  description: string
}

export interface ExperimentRegistryProjection {
  schema: string
  variants: readonly ExperimentVariantProjection[]
  metrics: readonly ExperimentMetricProjection[]
  requirements: readonly ExperimentRequirementProjection[]
  capabilities: Readonly<Record<string, boolean>>
}

export interface ExperimentCellProjection {
  schema: string
  cell_id: string
  variant_id: string
  repetition: number
  seed: number
  envelope_digest: string
  phase:
    | "planned"
    | "queued"
    | "running"
    | "succeeded"
    | "failed"
    | "rejected"
    | "cancelled"
  terminal: boolean
  revision: number
  created_at: string
  updated_at: string
  started_at: string
  completed_at: string
  scenario_run_id: string
  owner_run_id: string
  task_id: string
  observation_digest: string
  sample_ids: readonly string[]
  verification_receipt?: Readonly<Record<string, any>>
  failure?: Readonly<Record<string, any>>
}

export interface ExperimentEnvelopeProjection {
  schema: string
  envelope_id: string
  scenario_id: string
  scenario_definition_digest: string
  task_input_digest: string
  task_input_bytes: number
  task_domain: string
  commit_sha: string
  environment_digest: string
  source_evidence_digest: string
  sealed_policy_digest: string
  budget: Readonly<Record<string, any>>
  hardware: Readonly<Record<string, any>>
  provider: Readonly<Record<string, any>>
  verifier: Readonly<Record<string, any>>
  failure_schedule: Readonly<Record<string, any>>
  seed_plan: readonly number[]
  created_at: string
  envelope_digest: string
  labels: Readonly<Record<string, string>>
  metadata: Readonly<Record<string, any>>
}

export interface ExperimentRunProjection {
  schema: string
  experiment_id: string
  title: string
  phase: ExperimentPhase
  terminal: boolean
  revision: number
  repetitions: number
  envelope: ExperimentEnvelopeProjection
  variants: readonly ExperimentVariantProjection[]
  cells: readonly ExperimentCellProjection[]
  created_at: string
  updated_at: string
  started_at: string
  completed_at: string
  requested_by: string
  report?: Readonly<Record<string, any>>
  bundle_manifest?: Readonly<Record<string, any>>
  verification_receipt?: Readonly<Record<string, any>>
  failure?: Readonly<Record<string, any>>
  cancel_requested: boolean
  archive_reason: string
  metadata: Readonly<Record<string, any>>
  progress: {
    planned: number
    succeeded: number
    failed: number
    terminal: number
  }
}

export interface ExperimentStatusProjection {
  schema: string
  run: ExperimentRunProjection
  transitions: readonly Readonly<Record<string, any>>[]
  receipts: readonly Readonly<Record<string, any>>[]
  bundles: readonly Readonly<Record<string, any>>[]
  sample_count: number
  sample_status_counts: Readonly<Record<string, number>>
  worker_active: boolean
  browser_connection_required: false
  status_digest: string
}

export interface ExperimentRunPageProjection {
  schema: string
  offset: number
  limit: number
  runs: readonly ExperimentRunProjection[]
  store: Readonly<Record<string, any>>
}

export interface ExperimentRawSampleProjection {
  schema: string
  sample_id: string
  experiment_id: string
  cell_id: string
  variant_id: string
  repetition: number
  metric: string
  value: number | null
  unit: string
  status: "observed" | "unavailable" | "rejected" | "anomalous"
  observed_at: string
  source_kind: string
  source_ids: readonly string[]
  source_digest: string
  dimensions: Readonly<Record<string, string>>
  anomaly_reason: string
  unavailable_reason: string
  sequence: number
  sample_digest: string
}

export interface ExperimentSamplePageProjection {
  schema: string
  experiment_id: string
  offset: number
  limit: number
  filters: Readonly<Record<string, string>>
  samples: readonly ExperimentRawSampleProjection[]
  page_digest: string
}

export interface ExperimentCreateInput {
  title: string
  sourceArchivePath: string
  envelope: Readonly<Record<string, any>>
  externalEvidence: Readonly<Record<string, Readonly<Record<string, any>>>>
  screenshotIndex?: Readonly<Record<string, any>>
  signal?: AbortSignal
  timeoutMs?: number
  idempotencyKey?: string
}

export interface ExperimentRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
  idempotencyKey?: string
}

function identityValue(value: string, label: string): string {
  const selected = String(value || "").trim()
  if (
    !selected
    || selected.length > 255
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(selected)
  ) {
    throw new TypeError(`${label} is invalid.`)
  }
  return selected
}

function textValue(
  value: string,
  label: string,
  maximumBytes: number,
): string {
  const selected = String(value || "").trim()
  const size = new TextEncoder().encode(selected).byteLength
  if (!selected || size > maximumBytes) {
    throw new TypeError(`${label} must contain at most ${maximumBytes} UTF-8 bytes.`)
  }
  return selected
}

function listBound(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("Experiment list bound must be a non-negative safe integer.")
  }
  return value
}

function mutationReason(value: string): string {
  return textValue(value, "Experiment mutation reason", 8 * 1024)
}

function immutableBody(input: ExperimentCreateInput): Readonly<Record<string, any>> {
  const title = textValue(input.title, "Experiment title", 4 * 1024)
  const sourceArchivePath = textValue(
    input.sourceArchivePath,
    "Source archive path",
    32 * 1024,
  )
  if (!input.envelope || typeof input.envelope !== "object") {
    throw new TypeError("Experiment comparison envelope is required.")
  }
  const seeds = input.envelope.seeds
  if (
    !Array.isArray(seeds)
    || seeds.length < 3
    || seeds.some((value) => !Number.isSafeInteger(value) || value < 0)
    || new Set(seeds).size !== seeds.length
  ) {
    throw new TypeError("Experiment envelope requires at least three unique seeds.")
  }
  return {
    title,
    source_archive_path: sourceArchivePath,
    envelope: structuredClone(input.envelope),
    external_evidence: structuredClone(input.externalEvidence ?? {}),
    screenshot_index: structuredClone(input.screenshotIndex ?? {}),
  }
}

export class ExperimentApi {
  readonly #client: ZyraApiClient

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  async registry(
    options: ExperimentRequestOptions = {},
  ): Promise<ExperimentRegistryProjection> {
    const response = await this.#client.endpoint<ExperimentRegistryProjection>(
      OPERATION_NAMES.experimentRegistry,
      {
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: "experiment.registry",
        deduplicate: true,
      },
    )
    return response.data
  }

  async list(options: {
    includeArchived?: boolean
    limit?: number
    offset?: number
    signal?: AbortSignal
    timeoutMs?: number
  } = {}): Promise<ExperimentRunPageProjection> {
    const limit = Math.min(10_000, Math.max(1, listBound(options.limit, 100)))
    const offset = listBound(options.offset, 0)
    const response = await this.#client.endpoint<ExperimentRunPageProjection>(
      OPERATION_NAMES.experimentRunList,
      {
        query: {
          include_archived: options.includeArchived === true ? "true" : "false",
          limit,
          offset,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `experiment.list:${options.includeArchived === true}:${limit}:${offset}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async get(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<ExperimentStatusProjection> {
    const selected = identityValue(experimentId, "Experiment id")
    const response = await this.#client.endpoint<ExperimentStatusProjection>(
      OPERATION_NAMES.experimentRunGet,
      {
        path: { experiment_id: selected },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `experiment.get:${selected}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async report(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    return this.#resource(
      OPERATION_NAMES.experimentRunReport,
      experimentId,
      options,
    )
  }

  async bundle(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    return this.#resource(
      OPERATION_NAMES.experimentRunBundle,
      experimentId,
      options,
    )
  }

  async source(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    return this.#resource(
      OPERATION_NAMES.experimentRunSource,
      experimentId,
      options,
    )
  }

  async requirements(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    return this.#resource(
      OPERATION_NAMES.experimentRunRequirements,
      experimentId,
      options,
    )
  }

  async samples(
    experimentId: string,
    options: {
      metric?: string
      variantId?: string
      cellId?: string
      status?: string
      limit?: number
      offset?: number
      signal?: AbortSignal
      timeoutMs?: number
    } = {},
  ): Promise<ExperimentSamplePageProjection> {
    const selected = identityValue(experimentId, "Experiment id")
    const limit = Math.min(100_000, Math.max(1, listBound(options.limit, 1000)))
    const offset = listBound(options.offset, 0)
    const response = await this.#client.endpoint<ExperimentSamplePageProjection>(
      OPERATION_NAMES.experimentRunSamples,
      {
        path: { experiment_id: selected },
        query: {
          metric: options.metric?.trim() || undefined,
          variant_id: options.variantId?.trim() || undefined,
          cell_id: options.cellId?.trim() || undefined,
          status: options.status?.trim() || undefined,
          limit,
          offset,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `experiment.samples:${selected}:${options.metric ?? ""}:` +
          `${options.variantId ?? ""}:${options.cellId ?? ""}:` +
          `${options.status ?? ""}:${limit}:${offset}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async create(input: ExperimentCreateInput): Promise<ExperimentRunProjection> {
    const body = immutableBody(input)
    const response = await this.#client.endpoint<{ run: ExperimentRunProjection }>(
      OPERATION_NAMES.experimentRunCreate,
      {
        body,
        idempotencyKey:
          input.idempotencyKey ??
          createIdempotencyKey("experiment-create", {}, body),
        signal: input.signal,
        timeoutMs: input.timeoutMs,
        coordinationKey:
          `experiment.create:${body.source_archive_path}:` +
          `${String(body.envelope?.source_evidence_digest ?? "")}`,
      },
    )
    return response.data.run
  }

  async start(
    experimentId: string,
    options: ExperimentRequestOptions & { wait?: boolean } = {},
  ): Promise<ExperimentRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.experimentRunStart,
      experimentId,
      {
        wait: options.wait === true,
        timeout_seconds: options.timeoutMs
          ? Math.max(1, Math.ceil(options.timeoutMs / 1000))
          : undefined,
      },
      options,
    )
  }

  async archive(
    experimentId: string,
    reason: string,
    options: ExperimentRequestOptions = {},
  ): Promise<ExperimentRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.experimentRunArchive,
      experimentId,
      { reason: mutationReason(reason) },
      options,
    )
  }

  async cancel(
    experimentId: string,
    reason: string,
    options: ExperimentRequestOptions = {},
  ): Promise<ExperimentRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.experimentRunCancel,
      experimentId,
      { reason: mutationReason(reason) },
      options,
    )
  }

  async verify(
    experimentId: string,
    options: ExperimentRequestOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    const selected = identityValue(experimentId, "Experiment id")
    const body = { experiment_id: selected }
    const response = await this.#client.endpoint<Readonly<Record<string, any>>>(
      OPERATION_NAMES.experimentRunVerify,
      {
        path: { experiment_id: selected },
        body: {},
        idempotencyKey:
          options.idempotencyKey ??
          createIdempotencyKey("experiment-verify", {}, body),
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `experiment.verify:${selected}`,
      },
    )
    return response.data
  }

  async #resource(
    operation: string,
    experimentId: string,
    options: ExperimentRequestOptions,
  ): Promise<Readonly<Record<string, any>>> {
    const selected = identityValue(experimentId, "Experiment id")
    const response = await this.#client.endpoint<Readonly<Record<string, any>>>(
      operation,
      {
        path: { experiment_id: selected },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `${operation}:${selected}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async #mutate(
    operation: string,
    experimentId: string,
    body: Readonly<Record<string, unknown>>,
    options: ExperimentRequestOptions,
  ): Promise<ExperimentRunProjection> {
    const selected = identityValue(experimentId, "Experiment id")
    const response = await this.#client.endpoint<{ run: ExperimentRunProjection }>(
      operation,
      {
        path: { experiment_id: selected },
        body,
        idempotencyKey:
          options.idempotencyKey ??
          createIdempotencyKey(
            operation.replaceAll(".", "-"),
            {},
            { experiment_id: selected, ...body },
          ),
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `${operation}:${selected}`,
      },
    )
    return response.data.run
  }
}
