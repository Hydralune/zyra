import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02CustodyDomain =
  | "permission"
  | "mcp"
  | "skill"
  | "plugin"
  | "command"
  | "agent";

export type E02CustodyOwner =
  | "E02CapabilityCoordinator"
  | "PermissionCoordinator"
  | "McpRuntimeCoordinator"
  | "SkillCoordinator"
  | "PluginCoordinator"
  | "CommandCoordinator"
  | "TypeScriptAgentRuntime";

export interface E02CustodyDomainState extends JsonObject {
  domain: E02CustodyDomain;
  owner: E02CustodyOwner;
  runtimeId: string;
  revision: number;
  stateDigest: string;
  priorStateDigest: string;
  lastCaptureId: string;
  lastCaptureHash: string;
  capturedAt: string;
  runtimeEpoch: number;
  restoredFromEpoch: number | null;
  metadata: JsonObject;
}

export interface E02CustodyCapture extends JsonObject {
  captureId: string;
  sequence: number;
  domain: E02CustodyDomain;
  owner: E02CustodyOwner;
  revisionBefore: number;
  revisionAfter: number;
  stateDigestBefore: string;
  stateDigestAfter: string;
  changed: boolean;
  reason: string;
  runtimeEpoch: number;
  capturedAt: string;
  previousCaptureHash: string;
  captureHash: string;
  metadata: JsonObject;
}

export interface E02PhysicalPortDeclaration extends JsonObject {
  portId: string;
  role:
    | "tool-effect"
    | "checkpoint-store"
    | "event-transport"
    | "approval-transport"
    | "credential-store"
    | "artifact-store";
  canonicalDecisionOwner: "typescript";
  canonicalStateOwner: "typescript";
  mayEvaluatePolicy: false;
  mayOwnLiveClient: false;
  callDirection: "typescript-to-physical-port";
  registeredAt: string;
  metadata: JsonObject;
  declarationDigest: string;
}

export interface E02CustodySnapshot {
  version: "zyra.e02-custody/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  previousCaptureHash: string;
  restoredBeforeBootstrap: boolean;
  bootstrapCaptured: boolean;
  domains: E02CustodyDomainState[];
  captures: E02CustodyCapture[];
  physicalPorts: E02PhysicalPortDeclaration[];
  snapshotHash: string;
}

const EXPECTED_OWNERS: Readonly<Record<E02CustodyDomain, E02CustodyOwner>> = Object.freeze({
  permission: "PermissionCoordinator",
  mcp: "McpRuntimeCoordinator",
  skill: "SkillCoordinator",
  plugin: "PluginCoordinator",
  command: "CommandCoordinator",
  agent: "TypeScriptAgentRuntime",
});

const REQUIRED_DOMAINS = Object.freeze(Object.keys(EXPECTED_OWNERS) as E02CustodyDomain[]);

export class E02CustodyRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private sequence = 0;
  private previousCaptureHash = digest({ genesis: "zyra.e02-custody/v1" });
  private restoredBeforeBootstrap = false;
  private bootstrapCaptured = false;
  private readonly domains = new Map<E02CustodyDomain, E02CustodyDomainState>();
  private readonly captures: E02CustodyCapture[] = [];
  private readonly physicalPorts = new Map<string, E02PhysicalPortDeclaration>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    snapshot?: E02CustodySnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  capture(
    domain: E02CustodyDomain,
    stateValue: JsonValue,
    reason: string,
    metadataValue: JsonObject = {},
  ): E02CustodyCapture {
    const owner = EXPECTED_OWNERS[domain];
    if (!owner) throw custodyError("e02_custody_domain_unknown", `unknown custody domain ${domain}`);
    if (!reason.trim()) throw custodyError("e02_custody_reason_missing", "custody capture reason is required");
    const state = canonicalize(stateValue);
    const stateDigestAfter = digest(state);
    const current = this.domains.get(domain);
    const stateDigestBefore = current?.stateDigest ?? digest({ domain, genesis: true });
    const changed = !constantTimeDigestEquals(stateDigestBefore, stateDigestAfter);
    const revisionBefore = current?.revision ?? 0;
    const revisionAfter = revisionBefore + (changed ? 1 : 0);
    const capturedAt = this.timestamp();
    const sequence = this.sequence + 1;
    const captureBase = {
      sequence,
      domain,
      owner,
      revisionBefore,
      revisionAfter,
      stateDigestBefore,
      stateDigestAfter,
      changed,
      reason: reason.trim(),
      runtimeEpoch: this.runtime.epoch,
      capturedAt,
      previousCaptureHash: this.previousCaptureHash,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const captureId = deterministicId("e02-custody-capture", captureBase, 48);
    const captureHash = hashChain(this.previousCaptureHash, { captureId, ...captureBase });
    const capture: E02CustodyCapture = { captureId, ...captureBase, captureHash };
    this.sequence = sequence;
    this.previousCaptureHash = captureHash;
    this.captures.push(capture);
    this.domains.set(domain, {
      domain,
      owner,
      runtimeId: this.runtime.runtimeId,
      revision: revisionAfter,
      stateDigest: stateDigestAfter,
      priorStateDigest: stateDigestBefore,
      lastCaptureId: captureId,
      lastCaptureHash: captureHash,
      capturedAt,
      runtimeEpoch: this.runtime.epoch,
      restoredFromEpoch: current?.restoredFromEpoch ?? null,
      metadata: canonicalize({
        ...current?.metadata,
        ...metadataValue,
        canonical_owner: "typescript",
        python_shadow_state: false,
      }) as JsonObject,
    });
    return cloneJson(capture);
  }

  captureAll(
    states: Record<E02CustodyDomain, JsonValue>,
    reason: string,
  ): E02CustodyCapture[] {
    const captures: E02CustodyCapture[] = [];
    for (const domain of REQUIRED_DOMAINS) {
      if (!(domain in states)) {
        throw custodyError("e02_custody_domain_missing", `capture set is missing ${domain}`);
      }
      captures.push(this.capture(domain, states[domain], reason, {
        capture_set_size: REQUIRED_DOMAINS.length,
        capture_set_reason: reason,
      }));
    }
    return captures;
  }

  markBootstrapCaptured(): void {
    const missing = REQUIRED_DOMAINS.filter((domain) => !this.domains.has(domain));
    if (missing.length) {
      throw custodyError(
        "e02_custody_bootstrap_incomplete",
        `cannot open E02 before custody captures exist for ${missing.join(", ")}`,
      );
    }
    if (this.restoredBeforeBootstrap) {
      for (const domain of REQUIRED_DOMAINS) {
        const state = this.requireDomain(domain);
        if (state.runtimeEpoch !== this.runtime.epoch) {
          throw custodyError(
            "e02_custody_restore_not_rebased",
            `restored custody domain ${domain} was not captured in target epoch`,
          );
        }
      }
    }
    this.bootstrapCaptured = true;
  }

  assertOwner(domain: E02CustodyDomain, owner: E02CustodyOwner): void {
    const expected = EXPECTED_OWNERS[domain];
    if (owner !== expected) {
      throw custodyError(
        "e02_custody_owner_mismatch",
        `${domain} is owned by ${expected}, not ${owner}`,
      );
    }
    const state = this.requireDomain(domain);
    if (state.owner !== expected || state.runtimeId !== this.runtime.runtimeId) {
      throw custodyError(
        "e02_custody_state_owner_corrupt",
        `${domain} custody state does not belong to the active TypeScript runtime`,
      );
    }
  }

  assertReady(): void {
    if (!this.bootstrapCaptured) {
      throw custodyError("e02_custody_not_ready", "E02 custody was not captured before bootstrap completion");
    }
    for (const domain of REQUIRED_DOMAINS) this.assertOwner(domain, EXPECTED_OWNERS[domain]);
  }

  registerPhysicalPort(
    portIdValue: string,
    role: E02PhysicalPortDeclaration["role"],
    metadataValue: JsonObject = {},
  ): E02PhysicalPortDeclaration {
    const portId = portIdValue.trim();
    if (!portId) throw custodyError("e02_physical_port_id_missing", "physical port id is required");
    const existing = this.physicalPorts.get(portId);
    const registeredAt = existing?.registeredAt ?? this.timestamp();
    const base = {
      portId,
      role,
      canonicalDecisionOwner: "typescript" as const,
      canonicalStateOwner: "typescript" as const,
      mayEvaluatePolicy: false as const,
      mayOwnLiveClient: false as const,
      callDirection: "typescript-to-physical-port" as const,
      registeredAt,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const declaration: E02PhysicalPortDeclaration = {
      ...base,
      declarationDigest: digest(base),
    };
    if (existing && !constantTimeDigestEquals(existing.declarationDigest, declaration.declarationDigest)) {
      throw custodyError(
        "e02_physical_port_conflict",
        `physical port ${portId} was registered with different semantics`,
      );
    }
    this.physicalPorts.set(portId, declaration);
    return cloneJson(declaration);
  }

  domain(domain: E02CustodyDomain): E02CustodyDomainState | null {
    const state = this.domains.get(domain);
    return state ? cloneJson(state) : null;
  }

  captureHistory(domain?: E02CustodyDomain): E02CustodyCapture[] {
    return this.captures
      .filter((capture) => !domain || capture.domain === domain)
      .map(cloneJson);
  }

  changedSince(captureId: string): E02CustodyCapture[] {
    const index = this.captures.findIndex((capture) => capture.captureId === captureId);
    if (index < 0) throw custodyError("e02_custody_cursor_unknown", `unknown custody cursor ${captureId}`);
    return this.captures.slice(index + 1).filter((capture) => capture.changed).map(cloneJson);
  }

  health(): JsonObject {
    const missing = REQUIRED_DOMAINS.filter((domain) => !this.domains.has(domain));
    return {
      canonical_owner: "typescript",
      runtime_id: this.runtime.runtimeId,
      runtime_epoch: this.runtime.epoch,
      ready: this.bootstrapCaptured && missing.length === 0,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      bootstrap_captured: this.bootstrapCaptured,
      domain_count: this.domains.size,
      capture_count: this.captures.length,
      changed_capture_count: this.captures.filter((capture) => capture.changed).length,
      physical_port_count: this.physicalPorts.size,
      missing_domains: missing,
      python_shadow_state: false,
      python_decision_fallback: false,
    };
  }

  snapshot(): E02CustodySnapshot {
    const withoutHash = {
      version: "zyra.e02-custody/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      previousCaptureHash: this.previousCaptureHash,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      bootstrapCaptured: this.bootstrapCaptured,
      domains: REQUIRED_DOMAINS
        .map((domain) => this.domains.get(domain))
        .filter((state): state is E02CustodyDomainState => Boolean(state))
        .map(cloneJson),
      captures: this.captures.map(cloneJson),
      physicalPorts: [...this.physicalPorts.values()]
        .sort((left, right) => left.portId.localeCompare(right.portId))
        .map(cloneJson),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private restore(snapshotValue: E02CustodySnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-custody/v1") {
      throw custodyError("e02_custody_snapshot_version", `unsupported custody snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw custodyError("e02_custody_snapshot_digest", "custody snapshot digest mismatch");
    }
    if (
      snapshot.runtime.runtimeId !== this.runtime.runtimeId
      || snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw custodyError("e02_custody_snapshot_binding", "custody snapshot runtime binding mismatch");
    }
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw custodyError("e02_custody_snapshot_epoch", "custody restore epoch must advance");
    }
    let previousHash = digest({ genesis: "zyra.e02-custody/v1" });
    let previousSequence = 0;
    for (const capture of snapshot.captures) {
      if (capture.sequence !== previousSequence + 1 || capture.previousCaptureHash !== previousHash) {
        throw custodyError("e02_custody_capture_chain", "custody capture chain is not contiguous");
      }
      const { captureHash, ...captureBase } = capture;
      if (!constantTimeDigestEquals(hashChain(previousHash, captureBase), captureHash)) {
        throw custodyError("e02_custody_capture_digest", `custody capture ${capture.captureId} digest mismatch`);
      }
      this.captures.push(cloneJson(capture));
      previousSequence = capture.sequence;
      previousHash = capture.captureHash;
    }
    for (const state of snapshot.domains) {
      if (state.owner !== EXPECTED_OWNERS[state.domain] || this.domains.has(state.domain)) {
        throw custodyError("e02_custody_domain_state_invalid", `invalid restored custody domain ${state.domain}`);
      }
      this.domains.set(state.domain, {
        ...cloneJson(state),
        restoredFromEpoch: snapshot.runtime.epoch,
      });
    }
    for (const port of snapshot.physicalPorts) {
      const { declarationDigest, ...base } = port;
      if (!constantTimeDigestEquals(digest(base), declarationDigest) || this.physicalPorts.has(port.portId)) {
        throw custodyError("e02_physical_port_snapshot_invalid", `invalid restored physical port ${port.portId}`);
      }
      this.physicalPorts.set(port.portId, cloneJson(port));
    }
    this.sequence = snapshot.sequence;
    this.previousCaptureHash = snapshot.previousCaptureHash;
    this.restoredBeforeBootstrap = true;
    this.bootstrapCaptured = false;
  }

  private requireDomain(domain: E02CustodyDomain): E02CustodyDomainState {
    const state = this.domains.get(domain);
    if (!state) throw custodyError("e02_custody_domain_uncaptured", `custody domain ${domain} has no capture`);
    return state;
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function custodyError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02CustodyError",
    code,
    details: cloneJson(details),
  });
}

export function isE02CustodySnapshot(value: JsonValue): value is E02CustodySnapshot & JsonObject {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && (value as { version?: unknown }).version === "zyra.e02-custody/v1");
}
