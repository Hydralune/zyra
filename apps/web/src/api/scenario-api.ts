import {
  OPERATION_NAMES,
  createIdempotencyKey,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export interface ScenarioDefinitionProjection {
  schema: string
  scenario_id: string
  version: string
  title: string
  domain: string
  definition_digest: string
  required_owner_stages: readonly string[]
  expected_effects: readonly string[]
  minimum_effective_steps: number
  metadata: Readonly<Record<string, unknown>>
}

export interface ScenarioRegistryProjection {
  schema: string
  revision: number
  definitions: readonly ScenarioDefinitionProjection[]
  profiles: readonly Readonly<Record<string, unknown>>[]
  policies: readonly Readonly<Record<string, unknown>>[]
  registry_digest: string
}

export interface ScenarioRunProjection {
  schema: string
  scenario_run_id: string
  configuration: Readonly<Record<string, any>>
  phase:
    | "created"
    | "admitted"
    | "queued"
    | "running"
    | "cancelling"
    | "cancelled"
    | "succeeded"
    | "failed"
    | "archived"
  terminal: boolean
  revision: number
  created_at: string
  updated_at: string
  started_at: string
  completed_at: string
  owner_run_id: string
  task_id: string
  preflight_receipt?: Readonly<Record<string, any>>
  policy_decisions: readonly Readonly<Record<string, any>>[]
  interventions: readonly Readonly<Record<string, any>>[]
  human_intervention_count: number
  operator_intervention_attempt_count: number
  evidence_manifest?: Readonly<Record<string, any>>
  verification_receipt?: Readonly<Record<string, any>>
  failure?: Readonly<Record<string, any>>
  cancel_requested: boolean
  archive_reason: string
}

export interface ScenarioStatusProjection {
  schema: string
  run: ScenarioRunProjection
  transitions: readonly Readonly<Record<string, any>>[]
  receipts: readonly Readonly<Record<string, any>>[]
  worker_active: boolean
  browser_connection_required: false
  status_digest: string
}

export interface ScenarioListProjection {
  schema: string
  offset: number
  limit: number
  runs: readonly ScenarioRunProjection[]
  store: Readonly<Record<string, any>>
}

export interface ScenarioCreateInput {
  scenarioId?: string
  definitionVersion?: string
  profileId?: string
  policyId?: string
  policyDigest?: string
  mode?: "sealed" | "interactive"
  input: string
  seed?: number
  labels?: Readonly<Record<string, string>>
  faults?: readonly Readonly<Record<string, unknown>>[]
  preflight?: readonly Readonly<Record<string, unknown>>[]
  signal?: AbortSignal
  timeoutMs?: number
  idempotencyKey?: string
}

export interface ScenarioMutationOptions {
  signal?: AbortSignal
  timeoutMs?: number
  idempotencyKey?: string
}

function scenarioId(value: string | undefined, fallback = ""): string {
  const selected = String(value ?? fallback).trim()
  if (
    !selected
    || selected.length > 255
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(selected)
  ) {
    throw new TypeError("Scenario identity is invalid.")
  }
  return selected
}

function inputValue(value: string): string {
  const selected = String(value || "").trim()
  const size = new TextEncoder().encode(selected).byteLength
  if (!selected || size > 256 * 1024) {
    throw new TypeError("Scenario input must contain at most 256 KiB.")
  }
  return selected
}

function seedValue(value: number | undefined): number {
  if (value === undefined) return 0
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("Scenario seed must be a non-negative safe integer.")
  }
  return value
}

function limitValue(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value)) {
    throw new TypeError("Scenario list bound must be a safe integer.")
  }
  return value
}

function reasonValue(value: string): string {
  const selected = String(value || "").trim()
  if (!selected) throw new TypeError("Scenario mutation reason is required.")
  if (new TextEncoder().encode(selected).byteLength > 8 * 1024) {
    throw new TypeError("Scenario mutation reason exceeds 8 KiB.")
  }
  return selected
}

export class ScenarioApi {
  readonly #client: ZyraApiClient

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  async registry(options: ScenarioMutationOptions = {}): Promise<ScenarioRegistryProjection> {
    const response = await this.#client.endpoint<ScenarioRegistryProjection>(
      OPERATION_NAMES.scenarioRegistry,
      {
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: "scenario.registry",
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
  } = {}): Promise<ScenarioListProjection> {
    const limit = Math.min(10_000, Math.max(1, limitValue(options.limit, 100)))
    const offset = Math.max(0, limitValue(options.offset, 0))
    const response = await this.#client.endpoint<ScenarioListProjection>(
      OPERATION_NAMES.scenarioRunList,
      {
        query: {
          include_archived: options.includeArchived ? "true" : "false",
          limit,
          offset,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `scenario.list:${options.includeArchived === true}:${limit}:${offset}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async get(
    runId: string,
    options: ScenarioMutationOptions = {},
  ): Promise<ScenarioStatusProjection> {
    const selected = scenarioId(runId)
    const response = await this.#client.endpoint<ScenarioStatusProjection>(
      OPERATION_NAMES.scenarioRunGet,
      {
        path: { scenario_run_id: selected },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `scenario.get:${selected}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async evidence(
    runId: string,
    options: ScenarioMutationOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    const selected = scenarioId(runId)
    const response = await this.#client.endpoint<Readonly<Record<string, any>>>(
      OPERATION_NAMES.scenarioRunEvidence,
      {
        path: { scenario_run_id: selected },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `scenario.evidence:${selected}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async create(input: ScenarioCreateInput): Promise<ScenarioRunProjection> {
    const mode = input.mode ?? "sealed"
    const selectedScenarioId = scenarioId(
      input.scenarioId,
      "foundation.short-owner-chain",
    )
    const defaultProfileId = selectedScenarioId.startsWith("live.")
      ? "live.heterogeneous-sealed"
      : "foundation.local-sealed"
    const body = {
      scenario_id: selectedScenarioId,
      definition_version: input.definitionVersion?.trim() || undefined,
      profile_id: scenarioId(input.profileId, defaultProfileId),
      policy_id: scenarioId(input.policyId, "sealed-autonomous-foundation"),
      policy_digest: input.policyDigest?.trim() || undefined,
      mode,
      input: inputValue(input.input),
      seed: seedValue(input.seed),
      labels: { ...(input.labels ?? {}) },
      faults: input.faults ? [...input.faults] : undefined,
      preflight: input.preflight ? [...input.preflight] : undefined,
    }
    const response = await this.#client.endpoint<{ run: ScenarioRunProjection }>(
      OPERATION_NAMES.scenarioRunCreate,
      {
        body,
        idempotencyKey:
          input.idempotencyKey ?? createIdempotencyKey(
            "scenario-create",
            {},
            body,
          ),
        signal: input.signal,
        timeoutMs: input.timeoutMs,
        coordinationKey: `scenario.create:${body.scenario_id}:${body.mode}:${body.seed}`,
      },
    )
    return response.data.run
  }

  async start(
    runId: string,
    options: ScenarioMutationOptions & { wait?: boolean } = {},
  ): Promise<ScenarioRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.scenarioRunStart,
      runId,
      {
        wait: options.wait === true,
        timeout_seconds: options.timeoutMs
          ? Math.max(1, Math.ceil(options.timeoutMs / 1_000))
          : undefined,
      },
      options,
    )
  }

  async cancel(
    runId: string,
    reason: string,
    options: ScenarioMutationOptions = {},
  ): Promise<ScenarioRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.scenarioRunCancel,
      runId,
      { reason: reasonValue(reason) },
      options,
    )
  }

  async archive(
    runId: string,
    reason: string,
    options: ScenarioMutationOptions = {},
  ): Promise<ScenarioRunProjection> {
    return this.#mutate(
      OPERATION_NAMES.scenarioRunArchive,
      runId,
      { reason: reasonValue(reason) },
      options,
    )
  }

  async verify(
    runId: string,
    options: ScenarioMutationOptions = {},
  ): Promise<Readonly<Record<string, any>>> {
    const selected = scenarioId(runId)
    const response = await this.#client.endpoint<Readonly<Record<string, any>>>(
      OPERATION_NAMES.scenarioRunVerify,
      {
        path: { scenario_run_id: selected },
        body: {},
        idempotencyKey:
          options.idempotencyKey ?? createIdempotencyKey(
            "scenario-verify",
            {},
            { scenario_run_id: selected },
          ),
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `scenario.verify:${selected}`,
      },
    )
    return response.data
  }

  async #mutate(
    operation: string,
    runId: string,
    body: Readonly<Record<string, unknown>>,
    options: ScenarioMutationOptions,
  ): Promise<ScenarioRunProjection> {
    const selected = scenarioId(runId)
    const response = await this.#client.endpoint<{ run: ScenarioRunProjection }>(
      operation,
      {
        path: { scenario_run_id: selected },
        body,
        idempotencyKey:
          options.idempotencyKey ?? createIdempotencyKey(
            operation.replaceAll(".", "-"),
            {},
            { scenario_run_id: selected, ...body },
          ),
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `${operation}:${selected}`,
      },
    )
    return response.data.run
  }
}
