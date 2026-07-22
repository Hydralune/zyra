import { asBoolean, asObject, asString, type JsonObject } from "../contracts.ts";
import { digest, E03RuntimeError } from "../e03/contracts.ts";

export const PHYSICAL_DISPATCH_SCHEMA = "zyra.worker-pool-dispatch/v1";

export interface PhysicalDispatchProjection extends JsonObject {
  schema: typeof PHYSICAL_DISPATCH_SCHEMA;
  required: true;
  canonical_owner: "python.WorkerPoolStore";
  projection_owner: "typescript.OmpWorkerDispatchRuntime";
  task_id: string;
  attempt_id: string;
  attempt: number;
  lease_id: string;
  worker_id: string;
  backend_id: string;
  fence_epoch: number;
  manifest_digest: string;
  concurrency_limit: number;
  lease_state: "active" | "draining";
  logical_task_not_duplicated: true;
  integration_binding_id: string;
  graph_ref: JsonObject;
  workspace_ref: JsonObject;
  gateway_ref: JsonObject;
  route_ref: JsonObject;
  projection_digest: string;
}

export type OmpProjectionJobPhase =
  | "queued"
  | "running"
  | "parked"
  | "completed"
  | "failed"
  | "cancelled";

export interface OmpProjectionProgress extends JsonObject {
  sequence: number;
  message: string;
  completed_units: number;
  total_units: number;
  usage: JsonObject;
  observed_at: string;
}

export interface OmpProjectionJob extends JsonObject {
  job_id: string;
  owner_session_id: string;
  task_id: string;
  attempt_id: string;
  lease_id: string;
  worker_id: string;
  backend_id: string;
  phase: OmpProjectionJobPhase;
  revision: number;
  progress: OmpProjectionProgress;
  queued_at: string;
  started_at: string | null;
  parked_at: string | null;
  settled_at: string | null;
  error: string;
  result_digest: string;
  projection_only: true;
  canonical_state_owner: "python.WorkerPoolStore";
  digest: string;
}

export interface OmpDispatchSnapshot extends JsonObject {
  version: "zyra.omp-worker-dispatch/v1";
  canonical_state_owner: "python.WorkerPoolStore";
  projection_only: true;
  jobs: OmpProjectionJob[];
  semaphores: Array<JsonObject>;
  session_runtime: JsonObject;
}

export function parsePhysicalDispatch(
  value: unknown,
  expectedTaskId: string,
): PhysicalDispatchProjection | null {
  if (value === undefined || value === null) return null;
  const input = asObject(value);
  if (!Object.keys(input).length) return null;
  if (!asBoolean(input.required))
    throw new E03RuntimeError(
      "physical_dispatch_not_required",
      "a supplied physical dispatch projection must be fail-closed",
    );
  const projection = {
    schema: asString(input.schema) as typeof PHYSICAL_DISPATCH_SCHEMA,
    required: true as const,
    canonical_owner: asString(input.canonical_owner) as "python.WorkerPoolStore",
    projection_owner: asString(input.projection_owner) as "typescript.OmpWorkerDispatchRuntime",
    task_id: asString(input.task_id),
    attempt_id: asString(input.attempt_id),
    attempt: integer(input.attempt, "attempt", 1, Number.MAX_SAFE_INTEGER),
    lease_id: asString(input.lease_id),
    worker_id: asString(input.worker_id),
    backend_id: asString(input.backend_id),
    fence_epoch: integer(input.fence_epoch, "fence_epoch", 1, Number.MAX_SAFE_INTEGER),
    manifest_digest: asString(input.manifest_digest),
    concurrency_limit: integer(input.concurrency_limit, "concurrency_limit", 1, 128),
    lease_state: asString(input.lease_state) as "active" | "draining",
    logical_task_not_duplicated: true as const,
    integration_binding_id: asString(input.integration_binding_id),
    graph_ref: asObject(input.graph_ref),
    workspace_ref: asObject(input.workspace_ref),
    gateway_ref: asObject(input.gateway_ref),
    route_ref: asObject(input.route_ref),
    projection_digest: asString(input.projection_digest),
  } satisfies PhysicalDispatchProjection;
  if (projection.schema !== PHYSICAL_DISPATCH_SCHEMA)
    throw new E03RuntimeError(
      "physical_dispatch_schema_mismatch",
      `unsupported physical dispatch schema ${projection.schema || "<empty>"}`,
    );
  if (projection.canonical_owner !== "python.WorkerPoolStore")
    throw new E03RuntimeError(
      "physical_dispatch_owner_mismatch",
      "physical dispatch must remain owned by python.WorkerPoolStore",
    );
  if (projection.projection_owner !== "typescript.OmpWorkerDispatchRuntime")
    throw new E03RuntimeError(
      "physical_dispatch_projection_owner_mismatch",
      "physical dispatch projection owner is not the OMP TypeScript runtime",
    );
  if (projection.task_id !== expectedTaskId)
    throw new E03RuntimeError(
      "physical_dispatch_task_mismatch",
      `worker lease belongs to ${projection.task_id || "<empty>"}, not ${expectedTaskId}`,
    );
  for (const [field, selected] of Object.entries({
    task_id: projection.task_id,
    attempt_id: projection.attempt_id,
    lease_id: projection.lease_id,
    worker_id: projection.worker_id,
    backend_id: projection.backend_id,
    manifest_digest: projection.manifest_digest,
    projection_digest: projection.projection_digest,
    integration_binding_id: projection.integration_binding_id,
  }))
    if (!selected.trim())
      throw new E03RuntimeError(
        "physical_dispatch_incomplete",
        `physical dispatch ${field} is empty`,
      );
  for (const [field, reference] of Object.entries({
    graph_ref: projection.graph_ref,
    workspace_ref: projection.workspace_ref,
    gateway_ref: projection.gateway_ref,
    route_ref: projection.route_ref,
  }))
    if (!Object.keys(reference).length)
      throw new E03RuntimeError(
        "physical_dispatch_foreign_ref_missing",
        `physical dispatch ${field} is empty`,
      );
  if (projection.lease_state !== "active" && projection.lease_state !== "draining")
    throw new E03RuntimeError(
      "physical_dispatch_lease_inactive",
      `worker lease is ${projection.lease_state || "unknown"}`,
    );
  if (input.logical_task_not_duplicated !== true)
    throw new E03RuntimeError(
      "physical_dispatch_duplicates_logical_task",
      "physical projection cannot claim logical task ownership",
    );
  const { projection_digest: checksum, ...unsigned } = projection;
  if (checksum !== digest(unsigned))
    throw new E03RuntimeError(
      "physical_dispatch_checksum_mismatch",
      "physical dispatch projection changed after canonical lease admission",
    );
  return projection;
}

export function sealProjectionJob(
  input: Omit<OmpProjectionJob, "digest"> & { digest?: string },
): OmpProjectionJob {
  const { digest: _digest, ...payload } = input;
  return { ...payload, digest: digest(payload) } as OmpProjectionJob;
}

export function assertProjectionJob(job: OmpProjectionJob): void {
  const { digest: checksum, ...payload } = job;
  if (checksum !== digest(payload))
    throw new E03RuntimeError(
      "omp_projection_job_checksum",
      `OMP projection job ${job.job_id} checksum mismatch`,
    );
  if (!job.projection_only || job.canonical_state_owner !== "python.WorkerPoolStore")
    throw new E03RuntimeError(
      "omp_projection_owner_violation",
      "OMP job manager attempted to become a canonical state owner",
    );
}

export function terminalProjectionPhase(phase: OmpProjectionJobPhase): boolean {
  return phase === "completed" || phase === "failed" || phase === "cancelled";
}

function integer(value: unknown, field: string, minimum: number, maximum: number): number {
  const selected = typeof value === "number" ? value : Number.NaN;
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum)
    throw new E03RuntimeError(
      "physical_dispatch_invalid_number",
      `${field} must be an integer in ${minimum}..${maximum}`,
    );
  return selected;
}
