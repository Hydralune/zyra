import { isAbsolute, resolve, sep } from "node:path";
import {
  createId,
  digest,
  E03RuntimeError,
  unique,
  type E03AgentDefinition,
  type E03Budget,
  type E03CapabilityScope,
  type E03Clock,
  type IsolationMode,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface ScopeDerivationInput {
  parent: E03CapabilityScope;
  definition: E03AgentDefinition;
  requestedTools?: readonly string[];
  requestedSkills?: readonly string[];
  requestedMcpServers?: readonly string[];
  requestedWorkspaceRoots?: readonly string[];
  requestedIsolation?: IsolationMode;
  requestedBackground?: boolean;
  requestedPermissionMode?: string;
  depth: number;
}

const ISOLATION_RANK: Readonly<Record<IsolationMode, number>> = Object.freeze({
  none: 0,
  workspace: 1,
  worktree: 2,
  sandbox: 3,
  remote: 4,
});

export class AgentScopeLattice {
  private readonly policy = new AgentCapabilityPolicy();
  derive(input: ScopeDerivationInput): E03CapabilityScope {
    const parent = this.normalize(input.parent);
    if (
      input.depth !==
      input.parent.maxDepth - (input.parent.maxDepth - input.depth)
    ) {
      if (!Number.isSafeInteger(input.depth))
        throw new E03RuntimeError(
          "invalid_depth",
          "agent depth must be an integer",
        );
    }
    if (input.depth < 0 || input.depth > parent.maxDepth)
      throw new E03RuntimeError(
        "depth_exceeded",
        `agent depth ${input.depth} exceeds ${parent.maxDepth}`,
      );
    const definition = input.definition;
    const deniedTools = unique([
      ...parent.deniedTools,
      ...definition.deniedTools,
    ]);
    const definitionTools = definition.tools.length
      ? intersection(parent.tools, definition.tools)
      : parent.tools;
    const requestedTools = input.requestedTools?.length
      ? intersection(definitionTools, input.requestedTools)
      : definitionTools;
    const tools = requestedTools.filter((tool) => !deniedTools.includes(tool));
    const skills = definition.skills.length
      ? intersection(parent.skills, definition.skills)
      : parent.skills;
    const selectedSkills = input.requestedSkills?.length
      ? intersection(skills, input.requestedSkills)
      : skills;
    const mcpServers = definition.mcpServers.length
      ? intersection(parent.mcpServers, definition.mcpServers)
      : parent.mcpServers;
    const selectedMcp = input.requestedMcpServers?.length
      ? intersection(mcpServers, input.requestedMcpServers)
      : mcpServers;
    const workspaceRoots = input.requestedWorkspaceRoots?.length
      ? containedRoots(parent.workspaceRoots, input.requestedWorkspaceRoots)
      : [...parent.workspaceRoots];
    const isolation = input.requestedIsolation ?? definition.isolation;
    if (!parent.isolationModes.includes(isolation))
      throw new E03RuntimeError(
        "isolation_scope_exceeded",
        `isolation ${isolation} is not permitted by parent`,
      );
    const isolationModes = parent.isolationModes.filter(
      (mode) => ISOLATION_RANK[mode] >= ISOLATION_RANK[isolation],
    );
    const permissionMode =
      input.requestedPermissionMode ?? definition.permissionMode;
    if (
      parent.permissionMode !== "inherit" &&
      permissionMode !== "inherit" &&
      permissionMode !== parent.permissionMode
    ) {
      throw new E03RuntimeError(
        "permission_ceiling_exceeded",
        `permission mode ${permissionMode} exceeds parent ${parent.permissionMode}`,
      );
    }
    const budget = deriveBudget(parent, definition.budget);
    this.policy.assert(parent, definition, {
      tools,
      skills: selectedSkills,
      mcpServers: selectedMcp,
      workspaceRoots,
      isolation,
      permissionMode:
        permissionMode === "inherit" ? parent.permissionMode : permissionMode,
      background: input.requestedBackground !== false,
      teamMessaging: parent.allowTeamMessaging,
      fanout: parent.allowFanout && budget.maxChildren > 0,
      kill: parent.allowKill,
      depth: input.depth,
      children: 0,
    });
    const payload = {
      tools,
      deniedTools,
      skills: selectedSkills,
      mcpServers: selectedMcp,
      permissionMode:
        permissionMode === "inherit" ? parent.permissionMode : permissionMode,
      permissionCeilingDigest: parent.permissionCeilingDigest,
      workspaceRoots,
      isolationModes,
      allowBackground:
        parent.allowBackground &&
        definition.background &&
        input.requestedBackground !== false,
      allowTeamMessaging: parent.allowTeamMessaging,
      allowFanout: parent.allowFanout && budget.maxChildren > 0,
      allowKill: parent.allowKill,
      maxDepth: Math.min(parent.maxDepth, definition.budget.maxDepth),
      maxChildren: Math.min(parent.maxChildren, definition.budget.maxChildren),
    };
    const scope = { ...payload, digest: digest(payload) };
    this.assertMonotonic(parent, scope);
    return scope;
  }

  assertMonotonic(parent: E03CapabilityScope, child: E03CapabilityScope): void {
    this.policy.assertDerived(parent, child);
    const violations: string[] = [];
    for (const tool of child.tools)
      if (!parent.tools.includes(tool)) violations.push(`tool:${tool}`);
    for (const denied of parent.deniedTools)
      if (!child.deniedTools.includes(denied))
        violations.push(`denied-tool-removed:${denied}`);
    for (const skill of child.skills)
      if (!parent.skills.includes(skill)) violations.push(`skill:${skill}`);
    for (const server of child.mcpServers)
      if (!parent.mcpServers.includes(server)) violations.push(`mcp:${server}`);
    for (const root of child.workspaceRoots)
      if (!isContained(parent.workspaceRoots, root))
        violations.push(`workspace:${root}`);
    for (const mode of child.isolationModes)
      if (!parent.isolationModes.includes(mode))
        violations.push(`isolation:${mode}`);
    if (child.allowBackground && !parent.allowBackground)
      violations.push("background");
    if (child.allowTeamMessaging && !parent.allowTeamMessaging)
      violations.push("team-messaging");
    if (child.allowFanout && !parent.allowFanout) violations.push("fanout");
    if (child.allowKill && !parent.allowKill) violations.push("kill");
    if (child.maxDepth > parent.maxDepth) violations.push("max-depth");
    if (child.maxChildren > parent.maxChildren) violations.push("max-children");
    if (child.permissionCeilingDigest !== parent.permissionCeilingDigest)
      violations.push("permission-ceiling-digest");
    if (violations.length)
      throw new E03RuntimeError(
        "scope_escalation",
        `child scope escalates ${violations.join(", ")}`,
        { violations },
      );
    const normalized = this.normalize(child);
    if (normalized.digest !== child.digest)
      throw new E03RuntimeError(
        "scope_checksum_mismatch",
        "child scope digest does not match its fields",
      );
  }

  normalize(scope: E03CapabilityScope): E03CapabilityScope {
    const payload = {
      tools: unique(scope.tools),
      deniedTools: unique(scope.deniedTools),
      skills: unique(scope.skills),
      mcpServers: unique(scope.mcpServers),
      permissionMode: scope.permissionMode,
      permissionCeilingDigest: scope.permissionCeilingDigest,
      workspaceRoots: unique(scope.workspaceRoots.map(normalizePath)),
      isolationModes: unique(scope.isolationModes) as IsolationMode[],
      allowBackground: scope.allowBackground,
      allowTeamMessaging: scope.allowTeamMessaging,
      allowFanout: scope.allowFanout,
      allowKill: scope.allowKill,
      maxDepth: scope.maxDepth,
      maxChildren: scope.maxChildren,
    };
    return { ...payload, digest: digest(payload) };
  }
}

function deriveBudget(
  parent: E03CapabilityScope,
  requested: E03Budget,
): E03Budget {
  return {
    ...requested,
    maxChildren: Math.min(parent.maxChildren, requested.maxChildren),
    maxDepth: Math.min(parent.maxDepth, requested.maxDepth),
  };
}

function intersection(
  left: readonly string[],
  right: readonly string[],
): string[] {
  const accepted = new Set(right);
  return unique(left.filter((item) => accepted.has(item)));
}

function normalizePath(path: string): string {
  return path.replaceAll("\\", "/").replace(/\/$/, "");
}

function isContained(roots: readonly string[], candidate: string): boolean {
  const path = normalizePath(candidate);
  return roots.some((root) => {
    const normalized = normalizePath(root);
    return path === normalized || path.startsWith(`${normalized}/`);
  });
}

function containedRoots(
  parent: readonly string[],
  requested: readonly string[],
): string[] {
  const roots = unique(requested.map(normalizePath));
  const escaped = roots.filter((root) => !isContained(parent, root));
  if (escaped.length)
    throw new E03RuntimeError(
      "workspace_scope_exceeded",
      `workspace roots escape parent: ${escaped.join(", ")}`,
      { escaped },
    );
  return roots;
}

export function rootCapabilityScope(input: {
  tools: readonly string[];
  deniedTools?: readonly string[];
  skills?: readonly string[];
  mcpServers?: readonly string[];
  permissionMode: string;
  permissionCeilingDigest: string;
  workspaceRoots: readonly string[];
  isolationModes?: readonly IsolationMode[];
  maxDepth?: number;
  maxChildren?: number;
}): E03CapabilityScope {
  const payload = {
    tools: unique(input.tools),
    deniedTools: unique(input.deniedTools ?? []),
    skills: unique(input.skills ?? []),
    mcpServers: unique(input.mcpServers ?? []),
    permissionMode: input.permissionMode,
    permissionCeilingDigest: input.permissionCeilingDigest,
    workspaceRoots: unique(input.workspaceRoots.map(normalizePath)),
    isolationModes: [
      ...(input.isolationModes ?? [
        "workspace",
        "worktree",
        "sandbox",
        "remote",
      ]),
    ] as IsolationMode[],
    allowBackground: true,
    allowTeamMessaging: true,
    allowFanout: true,
    allowKill: true,
    maxDepth: input.maxDepth ?? 3,
    maxChildren: input.maxChildren ?? 8,
  };
  return { ...payload, digest: digest(payload) };
}

export interface CapabilityRequest {
  tools: readonly string[];
  skills: readonly string[];
  mcpServers: readonly string[];
  workspaceRoots: readonly string[];
  isolation: IsolationMode;
  permissionMode: string;
  background: boolean;
  teamMessaging: boolean;
  fanout: boolean;
  kill: boolean;
  depth: number;
  children: number;
}

export interface CapabilityFinding {
  code: string;
  resource: string;
  detail: string;
  fatal: boolean;
}

export interface CapabilityDecision {
  accepted: boolean;
  requested: CapabilityRequest;
  granted: CapabilityRequest;
  findings: CapabilityFinding[];
  parentDigest: string;
  definitionDigest: string;
  decisionDigest: string;
}

const MODE_RANK: Readonly<Record<string, number>> = Object.freeze({
  deny: 0,
  sealed: 0,
  ask: 1,
  prompt: 1,
  inherit: 2,
  allowlist: 2,
  allow: 3,
  bypass: 4,
});

export class AgentCapabilityPolicy {
  evaluate(
    parent: E03CapabilityScope,
    definition: E03AgentDefinition,
    request: CapabilityRequest,
  ): CapabilityDecision {
    this.assertScope(parent);
    this.assertDefinition(definition);
    const findings: CapabilityFinding[] = [];
    const requested = normalizeRequest(request);
    const grantedTools = intersectRequested(
      requested.tools,
      parent.tools,
      definition.tools,
      parent.deniedTools,
      definition.deniedTools,
      findings,
      "tool",
    );
    const grantedSkills = intersectRequested(
      requested.skills,
      parent.skills,
      definition.skills,
      [],
      [],
      findings,
      "skill",
    );
    const grantedMcp = intersectRequested(
      requested.mcpServers,
      parent.mcpServers,
      definition.mcpServers,
      [],
      [],
      findings,
      "mcp_server",
    );
    const workspaceRoots = requested.workspaceRoots.map((root) =>
      resolve(root),
    );
    for (const root of workspaceRoots) {
      if (!isAbsolute(root))
        findings.push(
          finding(
            "relative_workspace",
            root,
            "workspace root must be absolute",
          ),
        );
      if (
        !parent.workspaceRoots.some((allowed) =>
          contains(resolve(allowed), root),
        )
      )
        findings.push(
          finding(
            "workspace_scope_escape",
            root,
            "workspace root escapes parent scope",
          ),
        );
    }
    if (!parent.isolationModes.includes(requested.isolation))
      findings.push(
        finding(
          "isolation_scope_denied",
          requested.isolation,
          "parent scope denies isolation mode",
        ),
      );
    if (definition.isolation !== "none" && requested.isolation === "none")
      findings.push(
        finding(
          "definition_isolation_downgrade",
          requested.isolation,
          `definition requires ${definition.isolation}`,
        ),
      );
    const parentRank = MODE_RANK[parent.permissionMode] ?? 0;
    const requestedRank = MODE_RANK[requested.permissionMode] ?? 0;
    const definitionRank = MODE_RANK[definition.permissionMode] ?? parentRank;
    if (requestedRank > parentRank)
      findings.push(
        finding(
          "permission_escalation",
          requested.permissionMode,
          `parent permission mode is ${parent.permissionMode}`,
        ),
      );
    if (requestedRank > definitionRank)
      findings.push(
        finding(
          "definition_permission_escalation",
          requested.permissionMode,
          `definition permission mode is ${definition.permissionMode}`,
        ),
      );
    if (
      requested.background &&
      (!parent.allowBackground || !definition.background)
    )
      findings.push(
        finding(
          "background_denied",
          "background",
          "parent scope or agent definition denies background execution",
        ),
      );
    if (requested.teamMessaging && !parent.allowTeamMessaging)
      findings.push(
        finding(
          "team_message_denied",
          "team",
          "parent scope denies team messaging",
        ),
      );
    if (requested.fanout && !parent.allowFanout)
      findings.push(
        finding("fanout_denied", "fanout", "parent scope denies fanout"),
      );
    if (requested.kill && !parent.allowKill)
      findings.push(finding("kill_denied", "kill", "parent scope denies kill"));
    if (requested.depth > Math.min(parent.maxDepth, definition.budget.maxDepth))
      findings.push(
        finding(
          "depth_limit",
          String(requested.depth),
          "requested child depth exceeds parent or definition limit",
        ),
      );
    if (
      requested.children >
      Math.min(parent.maxChildren, definition.budget.maxChildren)
    )
      findings.push(
        finding(
          "child_limit",
          String(requested.children),
          "requested children exceed parent or definition limit",
        ),
      );
    const granted: CapabilityRequest = {
      tools: grantedTools,
      skills: grantedSkills,
      mcpServers: grantedMcp,
      workspaceRoots,
      isolation: requested.isolation,
      permissionMode:
        requestedRank <= Math.min(parentRank, definitionRank)
          ? requested.permissionMode
          : parent.permissionMode,
      background:
        requested.background && parent.allowBackground && definition.background,
      teamMessaging: requested.teamMessaging && parent.allowTeamMessaging,
      fanout: requested.fanout && parent.allowFanout,
      kill: requested.kill && parent.allowKill,
      depth: Math.min(
        requested.depth,
        parent.maxDepth,
        definition.budget.maxDepth,
      ),
      children: Math.min(
        requested.children,
        parent.maxChildren,
        definition.budget.maxChildren,
      ),
    };
    const payload = {
      requested,
      granted,
      findings,
      parentDigest: parent.digest,
      definitionDigest: definition.digest,
    };
    return {
      accepted: findings.every((item) => !item.fatal),
      ...payload,
      decisionDigest: digest(payload),
    };
  }

  assert(
    parent: E03CapabilityScope,
    definition: E03AgentDefinition,
    request: CapabilityRequest,
  ): CapabilityDecision {
    const decision = this.evaluate(parent, definition, request);
    if (!decision.accepted)
      throw new E03RuntimeError(
        "capability_scope_denied",
        decision.findings
          .map((item) => `${item.code}:${item.resource}`)
          .join(", "),
        {
          findings:
            decision.findings as unknown as import("../contracts.ts").JsonObject[],
          decisionDigest: decision.decisionDigest,
        },
      );
    return decision;
  }

  assertDerived(parent: E03CapabilityScope, child: E03CapabilityScope): void {
    this.assertScope(parent);
    this.assertScope(child);
    const violations: string[] = [];
    for (const tool of child.tools)
      if (!parent.tools.includes(tool) || parent.deniedTools.includes(tool))
        violations.push(`tool:${tool}`);
    for (const skill of child.skills)
      if (!parent.skills.includes(skill)) violations.push(`skill:${skill}`);
    for (const server of child.mcpServers)
      if (!parent.mcpServers.includes(server)) violations.push(`mcp:${server}`);
    for (const root of child.workspaceRoots)
      if (
        !parent.workspaceRoots.some((allowed) =>
          contains(resolve(allowed), resolve(root)),
        )
      )
        violations.push(`workspace:${root}`);
    for (const mode of child.isolationModes)
      if (!parent.isolationModes.includes(mode))
        violations.push(`isolation:${mode}`);
    if (child.allowBackground && !parent.allowBackground)
      violations.push("background");
    if (child.allowTeamMessaging && !parent.allowTeamMessaging)
      violations.push("team");
    if (child.allowFanout && !parent.allowFanout) violations.push("fanout");
    if (child.allowKill && !parent.allowKill) violations.push("kill");
    if (child.maxDepth > parent.maxDepth) violations.push("depth");
    if (child.maxChildren > parent.maxChildren) violations.push("children");
    if (
      (MODE_RANK[child.permissionMode] ?? 0) >
      (MODE_RANK[parent.permissionMode] ?? 0)
    )
      violations.push("permission");
    if (violations.length)
      throw new E03RuntimeError(
        "capability_scope_escalation",
        `child capability scope escalates ${violations.join(", ")}`,
        { violations, parentDigest: parent.digest, childDigest: child.digest },
      );
  }

  private assertScope(scope: E03CapabilityScope): void {
    const { digest: checksum, ...payload } = scope;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "scope_digest_mismatch",
        "capability scope digest is invalid",
      );
    if (!scope.permissionCeilingDigest || !scope.permissionMode)
      throw new E03RuntimeError(
        "invalid_capability_scope",
        "capability permission boundary is incomplete",
      );
    if (scope.maxDepth < 0 || scope.maxChildren < 0)
      throw new E03RuntimeError(
        "invalid_capability_limit",
        "capability depth or child limit is negative",
      );
  }

  private assertDefinition(definition: E03AgentDefinition): void {
    const { digest: checksum, ...payload } = definition;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "definition_digest_mismatch",
        `agent definition ${definition.name} digest is invalid`,
      );
    if (!definition.name || !definition.version || !definition.description)
      throw new E03RuntimeError(
        "invalid_agent_definition",
        "agent definition identity is incomplete",
      );
  }
}

function normalizeRequest(value: CapabilityRequest): CapabilityRequest {
  if (
    !Number.isSafeInteger(value.depth) ||
    value.depth < 0 ||
    !Number.isSafeInteger(value.children) ||
    value.children < 0
  )
    throw new E03RuntimeError(
      "invalid_capability_request",
      "capability depth and children must be non-negative integers",
    );
  return {
    ...value,
    tools: uniqueCapabilities(value.tools),
    skills: uniqueCapabilities(value.skills),
    mcpServers: uniqueCapabilities(value.mcpServers),
    workspaceRoots: uniqueCapabilities(value.workspaceRoots),
    permissionMode: value.permissionMode.trim() || "inherit",
  };
}

function intersectRequested(
  requested: readonly string[],
  parent: readonly string[],
  definition: readonly string[],
  parentDenied: readonly string[],
  definitionDenied: readonly string[],
  findings: CapabilityFinding[],
  kind: string,
): string[] {
  const denied = new Set([...parentDenied, ...definitionDenied]);
  const allowedByDefinition = definition.length ? new Set(definition) : null;
  const granted: string[] = [];
  for (const value of requested) {
    if (!parent.includes(value))
      findings.push(
        finding(
          `${kind}_parent_denied`,
          value,
          `${kind} is absent from parent scope`,
        ),
      );
    else if (allowedByDefinition && !allowedByDefinition.has(value))
      findings.push(
        finding(
          `${kind}_definition_denied`,
          value,
          `${kind} is absent from agent definition`,
        ),
      );
    else if (denied.has(value))
      findings.push(
        finding(
          `${kind}_explicitly_denied`,
          value,
          `${kind} is explicitly denied`,
        ),
      );
    else granted.push(value);
  }
  return granted;
}

function finding(
  code: string,
  resource: string,
  detail: string,
  fatal = true,
): CapabilityFinding {
  return { code, resource, detail, fatal };
}

function uniqueCapabilities(values: readonly string[]): string[] {
  return [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ].sort();
}

function contains(root: string, candidate: string): boolean {
  return root === candidate || candidate.startsWith(`${root}${sep}`);
}

export type CapabilityLeaseState =
  | "offered"
  | "active"
  | "suspended"
  | "revoked"
  | "expired";

export interface CapabilityLease {
  leaseId: string;
  parentLeaseId: string | null;
  taskId: string;
  sessionId: string;
  holderId: string;
  issuerId: string;
  state: CapabilityLeaseState;
  scope: E03CapabilityScope;
  issuedAt: string;
  activatesAt: string;
  expiresAt: string;
  activatedAt: string | null;
  suspendedAt: string | null;
  revokedAt: string | null;
  revocationReason: string | null;
  revision: number;
  digest: string;
}

export interface CapabilityLeaseUse {
  useId: string;
  leaseId: string;
  taskId: string;
  holderId: string;
  capabilityKind:
    | "tool"
    | "skill"
    | "mcp"
    | "workspace"
    | "isolation"
    | "background";
  capability: string;
  decision: "allowed" | "denied";
  reason: string;
  leaseRevision: number;
  occurredAt: string;
  digest: string;
}

function assertCapabilityLease(lease: CapabilityLease): void {
  const { digest: checksum, ...payload } = lease;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_lease_digest",
      `capability lease ${lease.leaseId} digest is invalid`,
    );
  if (
    !lease.leaseId ||
    !lease.taskId ||
    !lease.sessionId ||
    !lease.holderId ||
    !lease.issuerId
  )
    throw new E03RuntimeError(
      "capability_lease_identity",
      "capability lease identity is incomplete",
    );
  if (!Number.isSafeInteger(lease.revision) || lease.revision < 1)
    throw new E03RuntimeError(
      "capability_lease_revision",
      "capability lease revision is invalid",
    );
  if (Date.parse(lease.activatesAt) >= Date.parse(lease.expiresAt))
    throw new E03RuntimeError(
      "capability_lease_interval",
      "capability lease activation must precede expiry",
    );
  if (lease.state === "active" && lease.activatedAt === null)
    throw new E03RuntimeError(
      "capability_lease_state",
      "active capability lease requires activatedAt",
    );
  if (
    lease.state === "revoked" &&
    (!lease.revokedAt || !lease.revocationReason)
  )
    throw new E03RuntimeError(
      "capability_lease_state",
      "revoked capability lease requires revocation metadata",
    );
}

function assertCapabilityLeaseUse(use: CapabilityLeaseUse): void {
  const { digest: checksum, ...payload } = use;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_lease_use_digest",
      `capability lease use ${use.useId} digest is invalid`,
    );
  if (
    !use.useId ||
    !use.leaseId ||
    !use.taskId ||
    !use.holderId ||
    !use.capability
  )
    throw new E03RuntimeError(
      "capability_lease_use_identity",
      "capability lease use identity is incomplete",
    );
  if (!Number.isSafeInteger(use.leaseRevision) || use.leaseRevision < 1)
    throw new E03RuntimeError(
      "capability_lease_use_revision",
      "capability lease use revision is invalid",
    );
}

export class CapabilityLeaseRuntime {
  private leases = new Map<string, CapabilityLease>();
  private uses = new Map<string, CapabilityLeaseUse>();
  private byTask = new Map<string, Set<string>>();
  private children = new Map<string, Set<string>>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  offer(input: {
    taskId: string;
    sessionId: string;
    holderId: string;
    issuerId: string;
    scope: E03CapabilityScope;
    parentLeaseId?: string | null;
    activatesAt?: string;
    expiresAt: string;
  }): CapabilityLease {
    const now = this.clock.now();
    const parent = input.parentLeaseId
      ? this.requireLease(input.parentLeaseId)
      : null;
    if (parent) {
      if (parent.state !== "active")
        throw new E03RuntimeError(
          "capability_lease_parent_inactive",
          `parent capability lease ${parent.leaseId} is not active`,
        );
      if (parent.sessionId !== input.sessionId)
        throw new E03RuntimeError(
          "capability_lease_parent_session",
          "child capability lease belongs to another session",
        );
      new AgentScopeLattice().assertMonotonic(parent.scope, input.scope);
      if (Date.parse(input.expiresAt) > Date.parse(parent.expiresAt))
        throw new E03RuntimeError(
          "capability_lease_parent_expiry",
          "child capability lease outlives its parent",
        );
    }
    const payload = {
      leaseId: createId("capability-lease"),
      parentLeaseId: parent?.leaseId ?? null,
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      holderId: input.holderId.trim(),
      issuerId: input.issuerId.trim(),
      state: "offered" as const,
      scope: structuredClone(input.scope),
      issuedAt: now,
      activatesAt: input.activatesAt ?? now,
      expiresAt: input.expiresAt,
      activatedAt: null,
      suspendedAt: null,
      revokedAt: null,
      revocationReason: null,
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertCapabilityLease(lease);
    this.leases.set(lease.leaseId, structuredClone(lease));
    const taskLeases = this.byTask.get(lease.taskId) ?? new Set<string>();
    taskLeases.add(lease.leaseId);
    this.byTask.set(lease.taskId, taskLeases);
    if (lease.parentLeaseId) {
      const childLeases =
        this.children.get(lease.parentLeaseId) ?? new Set<string>();
      childLeases.add(lease.leaseId);
      this.children.set(lease.parentLeaseId, childLeases);
    }
    return structuredClone(lease);
  }

  activate(leaseId: string, holderId: string): CapabilityLease {
    const lease = this.requireLease(leaseId);
    if (lease.holderId !== holderId)
      throw new E03RuntimeError(
        "capability_lease_holder",
        `capability lease ${leaseId} belongs to another holder`,
      );
    if (lease.state !== "offered" && lease.state !== "suspended")
      throw new E03RuntimeError(
        "capability_lease_activate_state",
        `capability lease ${leaseId} cannot be activated from ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) < Date.parse(lease.activatesAt))
      throw new E03RuntimeError(
        "capability_lease_not_started",
        `capability lease ${leaseId} has not reached its activation time`,
      );
    if (Date.parse(now) >= Date.parse(lease.expiresAt))
      return this.transition(lease, {
        state: "expired",
      });
    if (lease.parentLeaseId) {
      const parent = this.requireLease(lease.parentLeaseId);
      if (parent.state !== "active")
        throw new E03RuntimeError(
          "capability_lease_parent_inactive",
          `parent capability lease ${parent.leaseId} is not active`,
        );
    }
    return this.transition(lease, {
      state: "active",
      activatedAt: lease.activatedAt ?? now,
      suspendedAt: null,
    });
  }

  suspend(leaseId: string, issuerId: string): CapabilityLease {
    const lease = this.requireIssuer(leaseId, issuerId);
    if (lease.state !== "active")
      throw new E03RuntimeError(
        "capability_lease_suspend_state",
        `capability lease ${leaseId} is not active`,
      );
    const next = this.transition(lease, {
      state: "suspended",
      suspendedAt: this.clock.now(),
    });
    for (const childId of this.descendants(leaseId)) {
      const child = this.requireLease(childId);
      if (child.state === "active")
        this.transition(child, {
          state: "suspended",
          suspendedAt: this.clock.now(),
        });
    }
    return next;
  }

  revoke(leaseId: string, issuerId: string, reason: string): CapabilityLease {
    const lease = this.requireIssuer(leaseId, issuerId);
    if (lease.state === "revoked") return structuredClone(lease);
    if (lease.state === "expired")
      throw new E03RuntimeError(
        "capability_lease_revoke_state",
        `expired capability lease ${leaseId} cannot be revoked`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "capability_lease_revoke_reason",
        "capability lease revocation reason is required",
      );
    const now = this.clock.now();
    const next = this.transition(lease, {
      state: "revoked",
      revokedAt: now,
      revocationReason: reason.trim(),
    });
    for (const childId of this.descendants(leaseId)) {
      const child = this.requireLease(childId);
      if (child.state === "revoked" || child.state === "expired") continue;
      this.transition(child, {
        state: "revoked",
        revokedAt: now,
        revocationReason: `parent ${leaseId} revoked: ${reason.trim()}`,
      });
    }
    return next;
  }

  evaluate(input: {
    leaseId: string;
    holderId: string;
    taskId: string;
    capabilityKind: CapabilityLeaseUse["capabilityKind"];
    capability: string;
  }): CapabilityLeaseUse {
    let lease = this.requireLease(input.leaseId);
    const now = this.clock.now();
    if (
      lease.state === "active" &&
      Date.parse(now) >= Date.parse(lease.expiresAt)
    )
      lease = this.transition(lease, { state: "expired" });
    let decision: CapabilityLeaseUse["decision"] = "allowed";
    let reason = "capability is within active lease";
    if (lease.holderId !== input.holderId || lease.taskId !== input.taskId) {
      decision = "denied";
      reason = "lease custody mismatch";
    } else if (lease.state !== "active") {
      decision = "denied";
      reason = `lease state is ${lease.state}`;
    } else if (
      !this.scopeContains(lease.scope, input.capabilityKind, input.capability)
    ) {
      decision = "denied";
      reason = "capability is outside lease scope";
    }
    const payload = {
      useId: createId("capability-use"),
      leaseId: lease.leaseId,
      taskId: input.taskId,
      holderId: input.holderId,
      capabilityKind: input.capabilityKind,
      capability: input.capability,
      decision,
      reason,
      leaseRevision: lease.revision,
      occurredAt: now,
    };
    const use = { ...payload, digest: digest(payload) };
    assertCapabilityLeaseUse(use);
    this.uses.set(use.useId, use);
    return structuredClone(use);
  }

  active(taskId: string, holderId?: string): CapabilityLease[] {
    return [...(this.byTask.get(taskId) ?? [])]
      .map((leaseId) => this.requireLease(leaseId))
      .filter((lease) => lease.state === "active")
      .filter((lease) => !holderId || lease.holderId === holderId)
      .map((lease) => structuredClone(lease));
  }

  usage(leaseId: string): CapabilityLeaseUse[] {
    this.requireLease(leaseId);
    return [...this.uses.values()]
      .filter((use) => use.leaseId === leaseId)
      .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
      .map((use) => structuredClone(use));
  }

  snapshot(): { leases: CapabilityLease[]; uses: CapabilityLeaseUse[] } {
    return {
      leases: [...this.leases.values()].map((lease) => structuredClone(lease)),
      uses: [...this.uses.values()].map((use) => structuredClone(use)),
    };
  }

  restore(input: {
    leases: readonly CapabilityLease[];
    uses: readonly CapabilityLeaseUse[];
  }): void {
    const leases = new Map<string, CapabilityLease>();
    const uses = new Map<string, CapabilityLeaseUse>();
    const byTask = new Map<string, Set<string>>();
    const children = new Map<string, Set<string>>();
    for (const raw of input.leases) {
      const lease = structuredClone(raw);
      assertCapabilityLease(lease);
      if (leases.has(lease.leaseId))
        throw new E03RuntimeError(
          "capability_lease_restore_duplicate",
          `duplicate capability lease ${lease.leaseId}`,
        );
      leases.set(lease.leaseId, lease);
      const taskLeases = byTask.get(lease.taskId) ?? new Set<string>();
      taskLeases.add(lease.leaseId);
      byTask.set(lease.taskId, taskLeases);
      if (lease.parentLeaseId) {
        const childLeases =
          children.get(lease.parentLeaseId) ?? new Set<string>();
        childLeases.add(lease.leaseId);
        children.set(lease.parentLeaseId, childLeases);
      }
    }
    for (const lease of leases.values()) {
      if (!lease.parentLeaseId) continue;
      const parent = leases.get(lease.parentLeaseId);
      if (!parent)
        throw new E03RuntimeError(
          "capability_lease_restore_parent",
          `capability lease ${lease.leaseId} has missing parent`,
        );
      new AgentScopeLattice().assertMonotonic(parent.scope, lease.scope);
    }
    for (const raw of input.uses) {
      const use = structuredClone(raw);
      assertCapabilityLeaseUse(use);
      const lease = leases.get(use.leaseId);
      if (!lease)
        throw new E03RuntimeError(
          "capability_lease_restore_use_orphan",
          `capability use ${use.useId} has missing lease`,
        );
      if (use.taskId !== lease.taskId || use.holderId !== lease.holderId)
        throw new E03RuntimeError(
          "capability_lease_restore_use_custody",
          `capability use ${use.useId} custody does not match lease`,
        );
      if (uses.has(use.useId))
        throw new E03RuntimeError(
          "capability_lease_restore_use_duplicate",
          `duplicate capability use ${use.useId}`,
        );
      uses.set(use.useId, use);
    }
    this.leases = leases;
    this.uses = uses;
    this.byTask = byTask;
    this.children = children;
  }

  private scopeContains(
    scope: E03CapabilityScope,
    kind: CapabilityLeaseUse["capabilityKind"],
    capability: string,
  ): boolean {
    switch (kind) {
      case "tool":
        return (
          scope.tools.includes(capability) &&
          !scope.deniedTools.includes(capability)
        );
      case "skill":
        return scope.skills.includes(capability);
      case "mcp":
        return scope.mcpServers.includes(capability);
      case "workspace":
        return containsAny(scope.workspaceRoots, capability);
      case "isolation":
        return scope.isolationModes.includes(capability as IsolationMode);
      case "background":
        return scope.allowBackground && capability === "background";
      default:
        return false;
    }
  }

  private descendants(leaseId: string): string[] {
    const output: string[] = [];
    const queue = [...(this.children.get(leaseId) ?? [])];
    const seen = new Set<string>();
    while (queue.length) {
      const childId = queue.shift()!;
      if (seen.has(childId))
        throw new E03RuntimeError(
          "capability_lease_cycle",
          `capability lease hierarchy contains a cycle at ${childId}`,
        );
      seen.add(childId);
      output.push(childId);
      queue.push(...(this.children.get(childId) ?? []));
    }
    return output;
  }

  private requireIssuer(leaseId: string, issuerId: string): CapabilityLease {
    const lease = this.requireLease(leaseId);
    if (lease.issuerId !== issuerId)
      throw new E03RuntimeError(
        "capability_lease_issuer",
        `capability lease ${leaseId} belongs to another issuer`,
      );
    return lease;
  }

  private requireLease(leaseId: string): CapabilityLease {
    const lease = this.leases.get(leaseId);
    if (!lease)
      throw new E03RuntimeError(
        "capability_lease_missing",
        `capability lease ${leaseId} does not exist`,
      );
    assertCapabilityLease(lease);
    return lease;
  }

  private transition(
    lease: CapabilityLease,
    patch: Partial<Omit<CapabilityLease, "leaseId" | "revision" | "digest">>,
  ): CapabilityLease {
    const { digest: _, ...prior } = lease;
    const payload = {
      ...prior,
      ...patch,
      leaseId: lease.leaseId,
      revision: lease.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityLease(next);
    this.leases.set(next.leaseId, structuredClone(next));
    return structuredClone(next);
  }
}

export interface CapabilityEscalation {
  escalationId: string;
  taskId: string;
  sessionId: string;
  requesterId: string;
  approverId: string | null;
  parentScopeDigest: string;
  requestedScopeDigest: string;
  requestedCapabilities: string[];
  reason: string;
  risk: "low" | "medium" | "high" | "critical";
  state: "pending" | "approved" | "denied" | "expired" | "cancelled";
  requestedAt: string;
  expiresAt: string;
  decidedAt: string | null;
  decisionReason: string | null;
  digest: string;
}

function assertCapabilityEscalation(escalation: CapabilityEscalation): void {
  const { digest: checksum, ...payload } = escalation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_escalation_digest",
      `capability escalation ${escalation.escalationId} digest is invalid`,
    );
  if (
    !escalation.escalationId ||
    !escalation.taskId ||
    !escalation.sessionId ||
    !escalation.requesterId ||
    !escalation.reason ||
    !escalation.parentScopeDigest ||
    !escalation.requestedScopeDigest
  )
    throw new E03RuntimeError(
      "capability_escalation_identity",
      "capability escalation identity is incomplete",
    );
  if (!escalation.requestedCapabilities.length)
    throw new E03RuntimeError(
      "capability_escalation_empty",
      "capability escalation requires requested capabilities",
    );
  if (escalation.state !== "pending" && escalation.decidedAt === null)
    throw new E03RuntimeError(
      "capability_escalation_state",
      "decided capability escalation requires a decision timestamp",
    );
}

export class CapabilityEscalationRuntime {
  private escalations = new Map<string, CapabilityEscalation>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  request(input: {
    taskId: string;
    sessionId: string;
    requesterId: string;
    parentScope: E03CapabilityScope;
    requestedScope: E03CapabilityScope;
    requestedCapabilities: readonly string[];
    reason: string;
    risk: CapabilityEscalation["risk"];
    expiresAt: string;
  }): CapabilityEscalation {
    const requestedCapabilities = unique(input.requestedCapabilities).filter(
      Boolean,
    );
    const now = this.clock.now();
    if (Date.parse(input.expiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "capability_escalation_expiry",
        "capability escalation expiry must be in the future",
      );
    const payload = {
      escalationId: createId("capability-escalation"),
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      requesterId: input.requesterId.trim(),
      approverId: null,
      parentScopeDigest: digest(input.parentScope),
      requestedScopeDigest: digest(input.requestedScope),
      requestedCapabilities,
      reason: input.reason.trim(),
      risk: input.risk,
      state: "pending" as const,
      requestedAt: now,
      expiresAt: input.expiresAt,
      decidedAt: null,
      decisionReason: null,
    };
    const escalation = { ...payload, digest: digest(payload) };
    assertCapabilityEscalation(escalation);
    this.escalations.set(escalation.escalationId, escalation);
    return structuredClone(escalation);
  }

  decide(input: {
    escalationId: string;
    approverId: string;
    decision: "approved" | "denied";
    reason: string;
  }): CapabilityEscalation {
    let escalation = this.require(input.escalationId);
    if (escalation.state !== "pending")
      throw new E03RuntimeError(
        "capability_escalation_decided",
        `capability escalation ${input.escalationId} is already ${escalation.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(escalation.expiresAt)) {
      escalation = this.reseal(escalation, {
        state: "expired",
        decidedAt: now,
        decisionReason: "decision deadline elapsed",
      });
      this.escalations.set(escalation.escalationId, escalation);
      return structuredClone(escalation);
    }
    if (!input.approverId.trim() || !input.reason.trim())
      throw new E03RuntimeError(
        "capability_escalation_decision",
        "capability escalation decision requires approver and reason",
      );
    const next = this.reseal(escalation, {
      approverId: input.approverId.trim(),
      state: input.decision,
      decidedAt: now,
      decisionReason: input.reason.trim(),
    });
    this.escalations.set(next.escalationId, next);
    return structuredClone(next);
  }

  cancel(
    escalationId: string,
    requesterId: string,
    reason: string,
  ): CapabilityEscalation {
    const escalation = this.require(escalationId);
    if (escalation.requesterId !== requesterId)
      throw new E03RuntimeError(
        "capability_escalation_requester",
        `capability escalation ${escalationId} belongs to another requester`,
      );
    if (escalation.state !== "pending")
      throw new E03RuntimeError(
        "capability_escalation_cancel_state",
        `capability escalation ${escalationId} is already ${escalation.state}`,
      );
    const next = this.reseal(escalation, {
      state: "cancelled",
      decidedAt: this.clock.now(),
      decisionReason: reason.trim() || "cancelled by requester",
    });
    this.escalations.set(next.escalationId, next);
    return structuredClone(next);
  }

  pending(taskId?: string): CapabilityEscalation[] {
    const now = this.clock.now();
    const output: CapabilityEscalation[] = [];
    for (const current of this.escalations.values()) {
      let escalation = current;
      if (
        escalation.state === "pending" &&
        Date.parse(now) >= Date.parse(escalation.expiresAt)
      ) {
        escalation = this.reseal(escalation, {
          state: "expired",
          decidedAt: now,
          decisionReason: "decision deadline elapsed",
        });
        this.escalations.set(escalation.escalationId, escalation);
      }
      if (
        escalation.state === "pending" &&
        (!taskId || escalation.taskId === taskId)
      )
        output.push(structuredClone(escalation));
    }
    return output.sort((left, right) =>
      left.requestedAt.localeCompare(right.requestedAt),
    );
  }

  snapshot(): CapabilityEscalation[] {
    return [...this.escalations.values()].map((value) =>
      structuredClone(value),
    );
  }

  restore(values: readonly CapabilityEscalation[]): void {
    const next = new Map<string, CapabilityEscalation>();
    for (const value of values) {
      assertCapabilityEscalation(value);
      if (next.has(value.escalationId))
        throw new E03RuntimeError(
          "capability_escalation_restore_duplicate",
          `duplicate capability escalation ${value.escalationId}`,
        );
      next.set(value.escalationId, structuredClone(value));
    }
    this.escalations = next;
  }

  private require(escalationId: string): CapabilityEscalation {
    const escalation = this.escalations.get(escalationId);
    if (!escalation)
      throw new E03RuntimeError(
        "capability_escalation_missing",
        `capability escalation ${escalationId} does not exist`,
      );
    assertCapabilityEscalation(escalation);
    return escalation;
  }

  private reseal(
    escalation: CapabilityEscalation,
    patch: Partial<Omit<CapabilityEscalation, "escalationId" | "digest">>,
  ): CapabilityEscalation {
    const { digest: _, ...prior } = escalation;
    const payload = {
      ...prior,
      ...patch,
      escalationId: escalation.escalationId,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityEscalation(next);
    return next;
  }
}

export type DelegatedCapabilityKind =
  | "tool"
  | "skill"
  | "mcp"
  | "workspace"
  | "background"
  | "fanout"
  | "team_message"
  | "isolation";
export interface CapabilityDelegation {
  delegationId: string;
  parentDelegationId: string | null;
  issuerTaskId: string;
  subjectTaskId: string;
  state: "issued" | "accepted" | "revoked" | "expired" | "exhausted";
  scope: E03CapabilityScope;
  allowedKinds: DelegatedCapabilityKind[];
  maximumUses: number;
  consumedUses: number;
  depth: number;
  issuedAt: string;
  acceptedAt: string | null;
  expiresAt: string;
  revokedAt: string | null;
  revokeReason: string | null;
  revision: number;
  digest: string;
}
export interface CapabilityDelegationUse {
  useId: string;
  delegationId: string;
  subjectTaskId: string;
  kind: DelegatedCapabilityKind;
  resource: string;
  outcome: "allowed" | "denied";
  reason: string;
  sequence: number;
  usedAt: string;
  previousDigest: string;
  digest: string;
}
function assertDelegation(value: CapabilityDelegation): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_delegation_digest",
      `capability delegation ${value.delegationId} is corrupt`,
    );
  if (
    !value.delegationId ||
    !value.issuerTaskId ||
    !value.subjectTaskId ||
    !value.allowedKinds.length
  )
    throw new E03RuntimeError(
      "capability_delegation_identity",
      "capability delegation identity is required",
    );
  if (
    !Number.isSafeInteger(value.maximumUses) ||
    value.maximumUses < 1 ||
    !Number.isSafeInteger(value.consumedUses) ||
    value.consumedUses < 0 ||
    value.consumedUses > value.maximumUses ||
    !Number.isSafeInteger(value.depth) ||
    value.depth < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "capability_delegation_counters",
      `capability delegation ${value.delegationId} is invalid`,
    );
  if (value.state === "accepted" && value.acceptedAt === null)
    throw new E03RuntimeError(
      "capability_delegation_accept_time",
      `accepted delegation ${value.delegationId} lacks time`,
    );
  if (value.state === "revoked" && (!value.revokedAt || !value.revokeReason))
    throw new E03RuntimeError(
      "capability_delegation_revoke_time",
      `revoked delegation ${value.delegationId} lacks reason or time`,
    );
}
function assertDelegationUse(value: CapabilityDelegationUse): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_delegation_use_digest",
      `capability delegation use ${value.useId} is corrupt`,
    );
  if (
    !value.useId ||
    !value.delegationId ||
    !value.subjectTaskId ||
    !value.resource ||
    !value.reason ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "capability_delegation_use_identity",
      `capability delegation use ${value.useId} is invalid`,
    );
}
export class CapabilityDelegationRuntime {
  private delegations = new Map<string, CapabilityDelegation>();
  private uses = new Map<string, CapabilityDelegationUse[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  issue(input: {
    issuerTaskId: string;
    subjectTaskId: string;
    parentDelegationId?: string | null;
    issuerScope: E03CapabilityScope;
    delegatedScope: E03CapabilityScope;
    allowedKinds: readonly DelegatedCapabilityKind[];
    maximumUses: number;
    ttlMs: number;
  }): CapabilityDelegation {
    if (
      !input.issuerTaskId.trim() ||
      !input.subjectTaskId.trim() ||
      input.issuerTaskId === input.subjectTaskId
    )
      throw new E03RuntimeError(
        "capability_delegation_parties",
        "capability delegation parties are invalid",
      );
    if (
      !Number.isSafeInteger(input.maximumUses) ||
      input.maximumUses < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "capability_delegation_limits",
        "capability delegation limits are invalid",
      );
    const allowedKinds: DelegatedCapabilityKind[] = [
      ...new Set(input.allowedKinds),
    ];
    if (!allowedKinds.length)
      throw new E03RuntimeError(
        "capability_delegation_kinds",
        "capability delegation kinds are required",
      );
    this.assertAttenuated(input.issuerScope, input.delegatedScope);
    let depth = 0;
    if (input.parentDelegationId) {
      const parent = this.requireDelegation(input.parentDelegationId);
      if (
        parent.subjectTaskId !== input.issuerTaskId ||
        parent.state !== "accepted"
      )
        throw new E03RuntimeError(
          "capability_delegation_parent_state",
          `parent delegation ${parent.delegationId} cannot delegate`,
        );
      if (Date.parse(parent.expiresAt) <= Date.parse(this.clock.now()))
        throw new E03RuntimeError(
          "capability_delegation_parent_expired",
          `parent delegation ${parent.delegationId} expired`,
        );
      if (allowedKinds.some((kind) => !parent.allowedKinds.includes(kind)))
        throw new E03RuntimeError(
          "capability_delegation_parent_kind",
          "child delegation expands parent kinds",
        );
      this.assertAttenuated(parent.scope, input.delegatedScope);
      depth = parent.depth + 1;
      if (depth > parent.scope.maxDepth)
        throw new E03RuntimeError(
          "capability_delegation_depth",
          "capability delegation exceeds depth",
        );
    }
    const payload = {
      delegationId: createId("capability-delegation"),
      parentDelegationId: input.parentDelegationId ?? null,
      issuerTaskId: input.issuerTaskId.trim(),
      subjectTaskId: input.subjectTaskId.trim(),
      state: "issued" as const,
      scope: structuredClone(input.delegatedScope),
      allowedKinds,
      maximumUses: input.maximumUses,
      consumedUses: 0,
      depth,
      issuedAt: this.clock.now(),
      acceptedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      revokedAt: null,
      revokeReason: null,
      revision: 1,
    };
    const delegation = { ...payload, digest: digest(payload) };
    assertDelegation(delegation);
    this.delegations.set(delegation.delegationId, delegation);
    return structuredClone(delegation);
  }
  accept(
    delegationId: string,
    expectedRevision: number,
    subjectTaskId: string,
  ): CapabilityDelegation {
    const delegation = this.requireDelegation(delegationId);
    this.assertRevision(delegation, expectedRevision);
    if (delegation.subjectTaskId !== subjectTaskId)
      throw new E03RuntimeError(
        "capability_delegation_subject",
        `task ${subjectTaskId} cannot accept delegation ${delegationId}`,
      );
    if (delegation.state !== "issued")
      throw new E03RuntimeError(
        "capability_delegation_accept_state",
        `capability delegation ${delegationId} is ${delegation.state}`,
      );
    if (Date.parse(delegation.expiresAt) <= Date.parse(this.clock.now()))
      return this.transition(delegation, { state: "expired" });
    return this.transition(delegation, {
      state: "accepted",
      acceptedAt: this.clock.now(),
    });
  }
  authorize(input: {
    delegationId: string;
    subjectTaskId: string;
    kind: DelegatedCapabilityKind;
    resource: string;
  }): { delegation: CapabilityDelegation; use: CapabilityDelegationUse } {
    const delegation = this.requireDelegation(input.delegationId);
    if (delegation.subjectTaskId !== input.subjectTaskId)
      return this.recordUse(
        delegation,
        input,
        false,
        "delegation subject mismatch",
      );
    if (delegation.state !== "accepted")
      return this.recordUse(
        delegation,
        input,
        false,
        `delegation is ${delegation.state}`,
      );
    if (Date.parse(delegation.expiresAt) <= Date.parse(this.clock.now())) {
      const expired = this.transition(delegation, { state: "expired" });
      return this.recordUse(expired, input, false, "delegation expired");
    }
    if (delegation.consumedUses >= delegation.maximumUses) {
      const exhausted = this.transition(delegation, { state: "exhausted" });
      return this.recordUse(
        exhausted,
        input,
        false,
        "delegation use budget exhausted",
      );
    }
    if (!delegation.allowedKinds.includes(input.kind))
      return this.recordUse(
        delegation,
        input,
        false,
        `delegation denies ${input.kind}`,
      );
    const reason = this.resourceAllowed(
      delegation.scope,
      input.kind,
      input.resource,
    );
    if (reason !== null)
      return this.recordUse(delegation, input, false, reason);
    const next = this.transition(delegation, {
      consumedUses: delegation.consumedUses + 1,
      state:
        delegation.consumedUses + 1 >= delegation.maximumUses
          ? "exhausted"
          : delegation.state,
    });
    return this.recordUse(next, input, true, "delegated capability allowed");
  }
  revoke(
    delegationId: string,
    expectedRevision: number,
    issuerTaskId: string,
    reason: string,
  ): CapabilityDelegation[] {
    const delegation = this.requireDelegation(delegationId);
    this.assertRevision(delegation, expectedRevision);
    if (delegation.issuerTaskId !== issuerTaskId)
      throw new E03RuntimeError(
        "capability_delegation_revoker",
        `task ${issuerTaskId} cannot revoke delegation ${delegationId}`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "capability_delegation_revoke_reason",
        "capability delegation revoke reason is required",
      );
    const revoked: CapabilityDelegation[] = [];
    const cascade = (value: CapabilityDelegation): void => {
      if (value.state === "revoked" || value.state === "expired") return;
      const next = this.transition(value, {
        state: "revoked",
        revokedAt: this.clock.now(),
        revokeReason: reason.trim(),
      });
      revoked.push(next);
      for (const child of this.delegations.values())
        if (child.parentDelegationId === value.delegationId) cascade(child);
    };
    cascade(delegation);
    return revoked;
  }
  ancestry(delegationId: string): CapabilityDelegation[] {
    const values: CapabilityDelegation[] = [];
    const seen = new Set<string>();
    let cursor: CapabilityDelegation | null =
      this.requireDelegation(delegationId);
    while (cursor) {
      if (seen.has(cursor.delegationId))
        throw new E03RuntimeError(
          "capability_delegation_cycle",
          `capability delegation cycles at ${cursor.delegationId}`,
        );
      seen.add(cursor.delegationId);
      values.push(structuredClone(cursor));
      cursor = cursor.parentDelegationId
        ? this.requireDelegation(cursor.parentDelegationId)
        : null;
    }
    return values;
  }
  snapshot(): {
    delegations: CapabilityDelegation[];
    uses: CapabilityDelegationUse[];
  } {
    return {
      delegations: [...this.delegations.values()].map((value) =>
        structuredClone(value),
      ),
      uses: [...this.uses.values()]
        .flat()
        .map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    delegations: readonly CapabilityDelegation[];
    uses: readonly CapabilityDelegationUse[];
  }): void {
    const delegations = new Map<string, CapabilityDelegation>();
    const uses = new Map<string, CapabilityDelegationUse[]>();
    for (const value of snapshot.delegations) {
      assertDelegation(value);
      if (delegations.has(value.delegationId))
        throw new E03RuntimeError(
          "capability_delegation_restore_duplicate",
          `duplicate capability delegation ${value.delegationId}`,
        );
      delegations.set(value.delegationId, structuredClone(value));
    }
    for (const value of delegations.values())
      if (
        value.parentDelegationId &&
        !delegations.has(value.parentDelegationId)
      )
        throw new E03RuntimeError(
          "capability_delegation_restore_parent",
          `capability delegation ${value.delegationId} has no parent`,
        );
    for (const value of snapshot.uses) {
      assertDelegationUse(value);
      if (!delegations.has(value.delegationId))
        throw new E03RuntimeError(
          "capability_delegation_use_restore",
          `capability delegation use ${value.useId} has no delegation`,
        );
      const entries = uses.get(value.delegationId) ?? [];
      const prior = entries[entries.length - 1];
      if (
        value.sequence !== entries.length + 1 ||
        value.previousDigest !== (prior?.digest ?? "root")
      )
        throw new E03RuntimeError(
          "capability_delegation_use_chain",
          `capability delegation use ${value.useId} breaks chain`,
        );
      entries.push(structuredClone(value));
      uses.set(value.delegationId, entries);
    }
    this.delegations = delegations;
    this.uses = uses;
    for (const value of delegations.values()) this.ancestry(value.delegationId);
  }
  private resourceAllowed(
    scope: E03CapabilityScope,
    kind: DelegatedCapabilityKind,
    resource: string,
  ): string | null {
    if (!resource.trim()) return "delegated resource is empty";
    if (kind === "tool" && !scope.tools.includes(resource))
      return `tool ${resource} is outside delegated scope`;
    if (kind === "skill" && !scope.skills.includes(resource))
      return `skill ${resource} is outside delegated scope`;
    if (kind === "mcp" && !scope.mcpServers.includes(resource))
      return `MCP server ${resource} is outside delegated scope`;
    if (kind === "workspace") {
      const target = resolve(resource);
      if (
        !scope.workspaceRoots.some(
          (root) =>
            target === resolve(root) ||
            target.startsWith(`${resolve(root)}${sep}`),
        )
      )
        return `workspace ${resource} is outside delegated scope`;
    }
    if (kind === "background" && !scope.allowBackground)
      return "background execution is outside delegated scope";
    if (kind === "fanout" && !scope.allowFanout)
      return "fanout is outside delegated scope";
    if (kind === "team_message" && !scope.allowTeamMessaging)
      return "team messaging is outside delegated scope";
    if (
      kind === "isolation" &&
      !scope.isolationModes.includes(resource as IsolationMode)
    )
      return `isolation mode ${resource} is outside delegated scope`;
    return null;
  }
  private assertAttenuated(
    parent: E03CapabilityScope,
    child: E03CapabilityScope,
  ): void {
    const subset = (values: readonly string[], allowed: readonly string[]) =>
      values.every((value) => allowed.includes(value));
    if (
      !subset(child.tools, parent.tools) ||
      !subset(child.skills, parent.skills) ||
      !subset(child.mcpServers, parent.mcpServers) ||
      !subset(
        child.workspaceRoots.map((value) => resolve(value)),
        parent.workspaceRoots.map((value) => resolve(value)),
      )
    )
      throw new E03RuntimeError(
        "capability_delegation_expansion",
        "delegated scope expands list capability",
      );
    if (
      (child.allowBackground && !parent.allowBackground) ||
      (child.allowFanout && !parent.allowFanout) ||
      (child.allowTeamMessaging && !parent.allowTeamMessaging) ||
      child.maxDepth > parent.maxDepth ||
      child.maxChildren > parent.maxChildren
    )
      throw new E03RuntimeError(
        "capability_delegation_expansion",
        "delegated scope expands scalar capability",
      );
    if (!subset(child.isolationModes, parent.isolationModes))
      throw new E03RuntimeError(
        "capability_delegation_isolation",
        "delegated scope expands isolation modes",
      );
  }
  private recordUse(
    delegation: CapabilityDelegation,
    input: {
      delegationId: string;
      subjectTaskId: string;
      kind: DelegatedCapabilityKind;
      resource: string;
    },
    allowed: boolean,
    reason: string,
  ): { delegation: CapabilityDelegation; use: CapabilityDelegationUse } {
    const entries = this.uses.get(delegation.delegationId) ?? [];
    const payload = {
      useId: createId("capability-delegation-use"),
      delegationId: delegation.delegationId,
      subjectTaskId: input.subjectTaskId,
      kind: input.kind,
      resource: input.resource.trim(),
      outcome: allowed ? ("allowed" as const) : ("denied" as const),
      reason,
      sequence: entries.length + 1,
      usedAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const use = { ...payload, digest: digest(payload) };
    assertDelegationUse(use);
    entries.push(use);
    this.uses.set(delegation.delegationId, entries);
    return {
      delegation: structuredClone(delegation),
      use: structuredClone(use),
    };
  }
  private requireDelegation(id: string): CapabilityDelegation {
    const value = this.delegations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "capability_delegation_missing",
        `capability delegation ${id} does not exist`,
      );
    assertDelegation(value);
    return value;
  }
  private assertRevision(value: CapabilityDelegation, expected: number): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "capability_delegation_stale_revision",
        `capability delegation ${value.delegationId} revision is stale`,
      );
  }
  private transition(
    value: CapabilityDelegation,
    patch: Partial<
      Omit<CapabilityDelegation, "delegationId" | "revision" | "digest">
    >,
  ): CapabilityDelegation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      delegationId: value.delegationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDelegation(next);
    this.delegations.set(next.delegationId, next);
    return structuredClone(next);
  }
}

export interface CapabilityAuditEvent {
  eventId: string;
  taskId: string;
  actorId: string;
  action:
    | "derive"
    | "issue"
    | "accept"
    | "allow"
    | "deny"
    | "escalate"
    | "revoke"
    | "expire";
  capabilityKind: string;
  resource: string;
  decisionId: string | null;
  metadata: Record<string, string | number | boolean | null>;
  sequence: number;
  occurredAt: string;
  previousDigest: string;
  digest: string;
}
function assertCapabilityAuditEvent(value: CapabilityAuditEvent): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_audit_digest",
      `capability audit event ${value.eventId} is corrupt`,
    );
  if (
    !value.eventId ||
    !value.taskId ||
    !value.actorId ||
    !value.capabilityKind ||
    !value.resource ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "capability_audit_identity",
      `capability audit event ${value.eventId} is invalid`,
    );
}
export class CapabilityAuditLedger {
  private events = new Map<string, CapabilityAuditEvent[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  append(
    input: Omit<
      CapabilityAuditEvent,
      "eventId" | "sequence" | "occurredAt" | "previousDigest" | "digest"
    >,
  ): CapabilityAuditEvent {
    if (
      !input.taskId.trim() ||
      !input.actorId.trim() ||
      !input.capabilityKind.trim() ||
      !input.resource.trim()
    )
      throw new E03RuntimeError(
        "capability_audit_input",
        "capability audit input is invalid",
      );
    const entries = this.events.get(input.taskId) ?? [];
    const payload = {
      ...structuredClone(input),
      eventId: createId("capability-audit-event"),
      taskId: input.taskId.trim(),
      actorId: input.actorId.trim(),
      capabilityKind: input.capabilityKind.trim(),
      resource: input.resource.trim(),
      sequence: entries.length + 1,
      occurredAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const event = { ...payload, digest: digest(payload) };
    assertCapabilityAuditEvent(event);
    entries.push(event);
    this.events.set(event.taskId, entries);
    return structuredClone(event);
  }
  history(taskId: string, afterSequence = 0): CapabilityAuditEvent[] {
    if (!Number.isSafeInteger(afterSequence) || afterSequence < 0)
      throw new E03RuntimeError(
        "capability_audit_cursor",
        "capability audit cursor is invalid",
      );
    return (this.events.get(taskId) ?? [])
      .filter((value) => value.sequence > afterSequence)
      .map((value) => structuredClone(value));
  }
  verify(taskId?: string): void {
    const groups = taskId
      ? [[taskId, this.events.get(taskId) ?? []] as const]
      : [...this.events.entries()];
    for (const [key, values] of groups) {
      let previousDigest = "root";
      let sequence = 1;
      for (const event of values) {
        assertCapabilityAuditEvent(event);
        if (
          event.taskId !== key ||
          event.sequence !== sequence ||
          event.previousDigest !== previousDigest
        )
          throw new E03RuntimeError(
            "capability_audit_chain",
            `capability audit chain for ${key} is invalid`,
          );
        previousDigest = event.digest;
        sequence += 1;
      }
    }
  }
  projection(taskId: string): {
    total: number;
    allowed: number;
    denied: number;
    escalated: number;
    revoked: number;
    latestDigest: string;
  } {
    const values = this.events.get(taskId) ?? [];
    this.verify(taskId);
    return {
      total: values.length,
      allowed: values.filter((value) => value.action === "allow").length,
      denied: values.filter((value) => value.action === "deny").length,
      escalated: values.filter((value) => value.action === "escalate").length,
      revoked: values.filter((value) => value.action === "revoke").length,
      latestDigest: values[values.length - 1]?.digest ?? "root",
    };
  }
  snapshot(): CapabilityAuditEvent[] {
    this.verify();
    return [...this.events.values()]
      .flat()
      .map((value) => structuredClone(value));
  }
  restore(events: readonly CapabilityAuditEvent[]): void {
    const next = new Map<string, CapabilityAuditEvent[]>();
    for (const event of events) {
      assertCapabilityAuditEvent(event);
      const values = next.get(event.taskId) ?? [];
      if (values.some((value) => value.eventId === event.eventId))
        throw new E03RuntimeError(
          "capability_audit_restore_duplicate",
          `duplicate capability audit event ${event.eventId}`,
        );
      values.push(structuredClone(event));
      next.set(event.taskId, values);
    }
    for (const values of next.values())
      values.sort((left, right) => left.sequence - right.sequence);
    this.events = next;
    this.verify();
  }
}

export interface CapabilityQuotaAccount {
  accountId: string;
  taskId: string;
  state: "open" | "exhausted" | "closed";
  limits: Record<string, number>;
  consumed: Record<string, number>;
  reserved: Record<string, number>;
  openedAt: string;
  closedAt: string | null;
  revision: number;
  digest: string;
}
export interface CapabilityQuotaReservation {
  reservationId: string;
  accountId: string;
  taskId: string;
  capability: string;
  amount: number;
  state: "held" | "consumed" | "released" | "expired";
  expiresAt: string;
  settledAt: string | null;
  revision: number;
  digest: string;
}
function assertCapabilityQuota(value: CapabilityQuotaAccount): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_quota_digest",
      `capability quota ${value.accountId} is corrupt`,
    );
  if (
    !value.accountId ||
    !value.taskId ||
    Object.keys(value.limits).length === 0 ||
    Object.entries(value.limits).some(
      ([key, limit]) =>
        !key ||
        !Number.isSafeInteger(limit) ||
        limit < 0 ||
        !Number.isSafeInteger(value.consumed[key] ?? 0) ||
        (value.consumed[key] ?? 0) < 0 ||
        !Number.isSafeInteger(value.reserved[key] ?? 0) ||
        (value.reserved[key] ?? 0) < 0 ||
        (value.consumed[key] ?? 0) + (value.reserved[key] ?? 0) > limit,
    ) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "capability_quota",
      `capability quota ${value.accountId} is invalid`,
    );
}
function assertCapabilityQuotaReservation(
  value: CapabilityQuotaReservation,
): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_quota_reservation_digest",
      `capability quota reservation ${value.reservationId} is corrupt`,
    );
  if (
    !value.reservationId ||
    !value.accountId ||
    !value.taskId ||
    !value.capability ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "capability_quota_reservation",
      `capability quota reservation ${value.reservationId} is invalid`,
    );
}
export interface CapabilityPolicyVersion {
  policyVersionId: string;
  policyName: string;
  version: number;
  ruleDigest: string;
  allowedCapabilities: string[];
  deniedCapabilities: string[];
  maximumDelegationDepth: number;
  state:
    | "draft"
    | "validating"
    | "canary"
    | "active"
    | "deprecated"
    | "rejected";
  createdAt: string;
  activatedAt: string;
  revision: number;
  digest: string;
}

export interface CapabilityPolicyEvaluation {
  evaluationId: string;
  policyVersionId: string;
  subjectId: string;
  scopeId: string;
  capability: string;
  delegationDepth: number;
  expectedDecision: "allow" | "deny";
  observedDecision: "allow" | "deny";
  accepted: boolean;
  evidenceDigest: string;
  evaluatedAt: string;
  previousDigest: string;
  digest: string;
}

export interface CapabilityPolicyRollout {
  rolloutId: string;
  policyVersionId: string;
  policyName: string;
  previousPolicyVersionId: string;
  canaryPercent: number;
  minimumEvaluations: number;
  maximumMismatchRatio: number;
  state: "planned" | "canary" | "promoted" | "rolled_back" | "failed";
  startedAt: string;
  updatedAt: string;
  terminalReason: string;
  revision: number;
  digest: string;
}

export interface CapabilityPolicyRolloutSnapshot {
  versions: CapabilityPolicyVersion[];
  evaluations: CapabilityPolicyEvaluation[];
  rollouts: CapabilityPolicyRollout[];
  activeVersionByPolicy: [string, string][];
  activeRolloutByPolicy: [string, string][];
}

function assertCapabilityPolicyVersion(value: CapabilityPolicyVersion): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.policyVersionId ||
    !value.policyName ||
    value.version < 1 ||
    !value.ruleDigest ||
    value.maximumDelegationDepth < 0 ||
    new Set(value.allowedCapabilities).size !==
      value.allowedCapabilities.length ||
    new Set(value.deniedCapabilities).size !==
      value.deniedCapabilities.length ||
    value.allowedCapabilities.some((capability) =>
      value.deniedCapabilities.includes(capability),
    ) ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "capability_policy_version_corrupt",
      `capability policy version ${value.policyVersionId || "<empty>"} is corrupt`,
    );
}

function assertCapabilityPolicyEvaluation(
  value: CapabilityPolicyEvaluation,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.evaluationId ||
    !value.policyVersionId ||
    !value.subjectId ||
    !value.scopeId ||
    !value.capability ||
    value.delegationDepth < 0 ||
    !value.evidenceDigest ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "capability_policy_evaluation_corrupt",
      `capability policy evaluation ${value.evaluationId || "<empty>"} is corrupt`,
    );
}

function assertCapabilityPolicyRollout(value: CapabilityPolicyRollout): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.rolloutId ||
    !value.policyVersionId ||
    !value.policyName ||
    value.canaryPercent < 1 ||
    value.canaryPercent > 100 ||
    value.minimumEvaluations < 1 ||
    value.maximumMismatchRatio < 0 ||
    value.maximumMismatchRatio > 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "capability_policy_rollout_corrupt",
      `capability policy rollout ${value.rolloutId || "<empty>"} is corrupt`,
    );
}

export class CapabilityPolicyRolloutRuntime {
  private versions = new Map<string, CapabilityPolicyVersion>();
  private evaluations = new Map<string, CapabilityPolicyEvaluation[]>();
  private rollouts = new Map<string, CapabilityPolicyRollout>();
  private activeVersionByPolicy = new Map<string, string>();
  private activeRolloutByPolicy = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  register(input: {
    policyVersionId?: string;
    policyName: string;
    version: number;
    ruleDigest: string;
    allowedCapabilities: readonly string[];
    deniedCapabilities: readonly string[];
    maximumDelegationDepth: number;
  }): CapabilityPolicyVersion {
    if (
      [...this.versions.values()].some(
        (value) =>
          value.policyName === input.policyName &&
          value.version === input.version,
      )
    )
      throw new E03RuntimeError(
        "capability_policy_version_duplicate",
        `capability policy ${input.policyName} version ${input.version} exists`,
      );
    const policyVersionId =
      input.policyVersionId ?? createId("capability-policy-version");
    const payload = {
      policyVersionId,
      policyName: input.policyName,
      version: input.version,
      ruleDigest: input.ruleDigest,
      allowedCapabilities: [...new Set(input.allowedCapabilities)].sort(),
      deniedCapabilities: [...new Set(input.deniedCapabilities)].sort(),
      maximumDelegationDepth: input.maximumDelegationDepth,
      state: "draft" as const,
      createdAt: this.clock.now(),
      activatedAt: "",
      revision: 1,
    };
    const version = { ...payload, digest: digest(payload) };
    assertCapabilityPolicyVersion(version);
    this.versions.set(policyVersionId, version);
    this.evaluations.set(policyVersionId, []);
    return structuredClone(version);
  }

  beginRollout(input: {
    rolloutId?: string;
    policyVersionId: string;
    expectedRevision: number;
    canaryPercent: number;
    minimumEvaluations: number;
    maximumMismatchRatio: number;
  }): CapabilityPolicyRollout {
    const version = this.requireVersion(input.policyVersionId);
    this.assertVersionRevision(version, input.expectedRevision);
    if (
      version.state !== "draft" ||
      this.activeRolloutByPolicy.has(version.policyName)
    )
      throw new E03RuntimeError(
        "capability_policy_rollout_active",
        `capability policy ${version.policyName} cannot start rollout`,
      );
    const activeVersionId =
      this.activeVersionByPolicy.get(version.policyName) ?? "";
    if (activeVersionId) {
      const active = this.requireVersion(activeVersionId);
      if (active.version >= version.version)
        throw new E03RuntimeError(
          "capability_policy_rollout_version_regression",
          `capability policy ${version.policyName} version is stale`,
        );
    }
    const rolloutId = input.rolloutId ?? createId("capability-policy-rollout");
    const now = this.clock.now();
    const payload = {
      rolloutId,
      policyVersionId: version.policyVersionId,
      policyName: version.policyName,
      previousPolicyVersionId: activeVersionId,
      canaryPercent: input.canaryPercent,
      minimumEvaluations: input.minimumEvaluations,
      maximumMismatchRatio: input.maximumMismatchRatio,
      state: "canary" as const,
      startedAt: now,
      updatedAt: now,
      terminalReason: "",
      revision: 1,
    };
    const rollout = { ...payload, digest: digest(payload) };
    assertCapabilityPolicyRollout(rollout);
    this.rollouts.set(rolloutId, rollout);
    this.activeRolloutByPolicy.set(version.policyName, rolloutId);
    this.transitionVersion(version, { state: "canary" });
    return structuredClone(rollout);
  }

  choose(policyName: string, subjectId: string): CapabilityPolicyVersion {
    const rolloutId = this.activeRolloutByPolicy.get(policyName);
    if (rolloutId) {
      const rollout = this.requireRollout(rolloutId);
      const bucket =
        Number.parseInt(
          digest({ policyName, subjectId, rolloutId }).slice(0, 8),
          16,
        ) % 100;
      if (bucket < rollout.canaryPercent)
        return structuredClone(this.requireVersion(rollout.policyVersionId));
    }
    const activeId = this.activeVersionByPolicy.get(policyName);
    if (!activeId)
      throw new E03RuntimeError(
        "capability_policy_active_missing",
        `capability policy ${policyName} has no active version`,
      );
    return structuredClone(this.requireVersion(activeId));
  }

  evaluate(input: {
    policyVersionId: string;
    subjectId: string;
    scopeId: string;
    capability: string;
    delegationDepth: number;
    expectedDecision: CapabilityPolicyEvaluation["expectedDecision"];
    evidenceDigest: string;
  }): CapabilityPolicyEvaluation {
    const version = this.requireVersion(input.policyVersionId);
    if (!["canary", "active"].includes(version.state))
      throw new E03RuntimeError(
        "capability_policy_evaluation_version_state",
        `capability policy version ${version.policyVersionId} is ${version.state}`,
      );
    const observedDecision: CapabilityPolicyEvaluation["observedDecision"] =
      version.deniedCapabilities.includes(input.capability) ||
      input.delegationDepth > version.maximumDelegationDepth ||
      (!version.allowedCapabilities.includes("*") &&
        !version.allowedCapabilities.includes(input.capability))
        ? "deny"
        : "allow";
    const entries = this.evaluationEntries(version.policyVersionId);
    const payload = {
      evaluationId: createId("capability-policy-evaluation"),
      policyVersionId: version.policyVersionId,
      subjectId: input.subjectId,
      scopeId: input.scopeId,
      capability: input.capability,
      delegationDepth: input.delegationDepth,
      expectedDecision: input.expectedDecision,
      observedDecision,
      accepted: observedDecision === input.expectedDecision,
      evidenceDigest: input.evidenceDigest,
      evaluatedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const evaluation = { ...payload, digest: digest(payload) };
    assertCapabilityPolicyEvaluation(evaluation);
    entries.push(evaluation);
    this.evaluations.set(version.policyVersionId, entries);
    return structuredClone(evaluation);
  }

  promote(
    rolloutId: string,
    expectedRevision: number,
  ): CapabilityPolicyRollout {
    const rollout = this.requireRollout(rolloutId);
    this.assertRolloutRevision(rollout, expectedRevision);
    if (rollout.state !== "canary")
      throw new E03RuntimeError(
        "capability_policy_rollout_promote_state",
        `capability policy rollout ${rolloutId} is ${rollout.state}`,
      );
    const evaluations = this.evaluationEntries(rollout.policyVersionId);
    if (evaluations.length < rollout.minimumEvaluations)
      throw new E03RuntimeError(
        "capability_policy_rollout_evaluations_missing",
        `capability policy rollout ${rolloutId} lacks evaluations`,
      );
    const mismatchRatio =
      evaluations.filter((value) => !value.accepted).length /
      evaluations.length;
    if (mismatchRatio > rollout.maximumMismatchRatio)
      throw new E03RuntimeError(
        "capability_policy_rollout_mismatch_ratio",
        `capability policy rollout ${rolloutId} mismatch ratio is too high`,
      );
    const version = this.requireVersion(rollout.policyVersionId);
    if (rollout.previousPolicyVersionId) {
      const previous = this.requireVersion(rollout.previousPolicyVersionId);
      this.transitionVersion(previous, { state: "deprecated" });
    }
    this.transitionVersion(version, {
      state: "active",
      activatedAt: this.clock.now(),
    });
    this.activeVersionByPolicy.set(rollout.policyName, rollout.policyVersionId);
    this.activeRolloutByPolicy.delete(rollout.policyName);
    return this.transitionRollout(rollout, {
      state: "promoted",
      terminalReason: "evaluation_threshold_met",
    });
  }

  rollback(
    rolloutId: string,
    expectedRevision: number,
    reason: string,
  ): CapabilityPolicyRollout {
    const rollout = this.requireRollout(rolloutId);
    this.assertRolloutRevision(rollout, expectedRevision);
    if (rollout.state !== "canary")
      throw new E03RuntimeError(
        "capability_policy_rollout_rollback_state",
        `capability policy rollout ${rolloutId} is ${rollout.state}`,
      );
    const version = this.requireVersion(rollout.policyVersionId);
    this.transitionVersion(version, { state: "rejected" });
    this.activeRolloutByPolicy.delete(rollout.policyName);
    return this.transitionRollout(rollout, {
      state: "rolled_back",
      terminalReason: reason,
    });
  }

  snapshot(): CapabilityPolicyRolloutSnapshot {
    return {
      versions: [...this.versions.values()].map((value) =>
        structuredClone(value),
      ),
      evaluations: [...this.evaluations.values()]
        .flat()
        .map((value) => structuredClone(value)),
      rollouts: [...this.rollouts.values()].map((value) =>
        structuredClone(value),
      ),
      activeVersionByPolicy: [...this.activeVersionByPolicy.entries()],
      activeRolloutByPolicy: [...this.activeRolloutByPolicy.entries()],
    };
  }

  restore(snapshot: CapabilityPolicyRolloutSnapshot): void {
    const versions = new Map<string, CapabilityPolicyVersion>();
    const evaluations = new Map<string, CapabilityPolicyEvaluation[]>();
    const rollouts = new Map<string, CapabilityPolicyRollout>();
    for (const value of snapshot.versions) {
      assertCapabilityPolicyVersion(value);
      versions.set(value.policyVersionId, structuredClone(value));
      evaluations.set(value.policyVersionId, []);
    }
    for (const value of snapshot.evaluations) {
      assertCapabilityPolicyEvaluation(value);
      const entries = evaluations.get(value.policyVersionId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "capability_policy_restore_evaluation_chain",
          `evaluation ${value.evaluationId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.rollouts) {
      assertCapabilityPolicyRollout(value);
      if (!versions.has(value.policyVersionId) || rollouts.has(value.rolloutId))
        throw new E03RuntimeError(
          "capability_policy_restore_rollout",
          `rollout ${value.rolloutId} invalid`,
        );
      rollouts.set(value.rolloutId, structuredClone(value));
    }
    const activeVersionByPolicy = new Map(snapshot.activeVersionByPolicy);
    const activeRolloutByPolicy = new Map(snapshot.activeRolloutByPolicy);
    if (
      activeVersionByPolicy.size !== snapshot.activeVersionByPolicy.length ||
      activeRolloutByPolicy.size !== snapshot.activeRolloutByPolicy.length
    )
      throw new E03RuntimeError(
        "capability_policy_restore_index_duplicate",
        "capability policy indexes duplicate",
      );
    for (const [name, versionId] of activeVersionByPolicy) {
      const value = versions.get(versionId);
      if (!value || value.policyName !== name || value.state !== "active")
        throw new E03RuntimeError(
          "capability_policy_restore_active_version",
          `policy index ${name} invalid`,
        );
    }
    for (const [name, rolloutId] of activeRolloutByPolicy) {
      const value = rollouts.get(rolloutId);
      if (!value || value.policyName !== name || value.state !== "canary")
        throw new E03RuntimeError(
          "capability_policy_restore_active_rollout",
          `rollout index ${name} invalid`,
        );
    }
    this.versions = versions;
    this.evaluations = evaluations;
    this.rollouts = rollouts;
    this.activeVersionByPolicy = activeVersionByPolicy;
    this.activeRolloutByPolicy = activeRolloutByPolicy;
  }

  private evaluationEntries(versionId: string): CapabilityPolicyEvaluation[] {
    return this.evaluations.get(versionId) ?? [];
  }

  private requireVersion(id: string): CapabilityPolicyVersion {
    const value = this.versions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "capability_policy_version_missing",
        `version ${id} missing`,
      );
    assertCapabilityPolicyVersion(value);
    return value;
  }

  private requireRollout(id: string): CapabilityPolicyRollout {
    const value = this.rollouts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "capability_policy_rollout_missing",
        `rollout ${id} missing`,
      );
    assertCapabilityPolicyRollout(value);
    return value;
  }

  private assertVersionRevision(
    value: CapabilityPolicyVersion,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "capability_policy_version_stale_revision",
        `version ${value.policyVersionId} stale`,
      );
  }

  private assertRolloutRevision(
    value: CapabilityPolicyRollout,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "capability_policy_rollout_stale_revision",
        `rollout ${value.rolloutId} stale`,
      );
  }

  private transitionVersion(
    value: CapabilityPolicyVersion,
    patch: Partial<
      Omit<CapabilityPolicyVersion, "policyVersionId" | "revision" | "digest">
    >,
  ): CapabilityPolicyVersion {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      policyVersionId: value.policyVersionId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityPolicyVersion(next);
    this.versions.set(next.policyVersionId, next);
    return structuredClone(next);
  }

  private transitionRollout(
    value: CapabilityPolicyRollout,
    patch: Partial<
      Omit<CapabilityPolicyRollout, "rolloutId" | "revision" | "digest">
    >,
  ): CapabilityPolicyRollout {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      rolloutId: value.rolloutId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityPolicyRollout(next);
    this.rollouts.set(next.rolloutId, next);
    return structuredClone(next);
  }
}

export class CapabilityQuotaRuntime {
  private accounts = new Map<string, CapabilityQuotaAccount>();
  private reservations = new Map<string, CapabilityQuotaReservation>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  open(
    taskId: string,
    limits: Readonly<Record<string, number>>,
  ): CapabilityQuotaAccount {
    if (!taskId.trim() || !Object.keys(limits).length)
      throw new E03RuntimeError(
        "capability_quota_open",
        "capability quota input is invalid",
      );
    const existing = [...this.accounts.values()].find(
      (value) => value.taskId === taskId && value.state !== "closed",
    );
    if (existing) return structuredClone(existing);
    const normalized = Object.fromEntries(
      Object.entries(limits).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    );
    const consumed = Object.fromEntries(
      Object.keys(normalized).map((key) => [key, 0]),
    );
    const reserved = Object.fromEntries(
      Object.keys(normalized).map((key) => [key, 0]),
    );
    const payload = {
      accountId: createId("capability-quota-account"),
      taskId: taskId.trim(),
      state: "open" as const,
      limits: normalized,
      consumed,
      reserved,
      openedAt: this.clock.now(),
      closedAt: null,
      revision: 1,
    };
    const account = { ...payload, digest: digest(payload) };
    assertCapabilityQuota(account);
    this.accounts.set(account.accountId, account);
    return structuredClone(account);
  }
  reserve(input: {
    accountId: string;
    expectedRevision: number;
    capability: string;
    amount?: number;
    ttlMs: number;
  }): {
    account: CapabilityQuotaAccount;
    reservation: CapabilityQuotaReservation;
  } {
    const account = this.requireAccount(input.accountId);
    this.assertAccountRevision(account, input.expectedRevision);
    if (account.state !== "open")
      throw new E03RuntimeError(
        "capability_quota_reserve_state",
        `capability quota ${account.accountId} is ${account.state}`,
      );
    const amount = input.amount ?? 1;
    if (
      !(input.capability in account.limits) ||
      !Number.isSafeInteger(amount) ||
      amount < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "capability_quota_reserve_input",
        "capability quota reservation is invalid",
      );
    if (
      (account.consumed[input.capability] ?? 0) +
        (account.reserved[input.capability] ?? 0) +
        amount >
      account.limits[input.capability]!
    )
      throw new E03RuntimeError(
        "capability_quota_exceeded",
        `capability quota ${input.capability} is exhausted`,
      );
    const payload = {
      reservationId: createId("capability-quota-reservation"),
      accountId: account.accountId,
      taskId: account.taskId,
      capability: input.capability,
      amount,
      state: "held" as const,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      settledAt: null,
      revision: 1,
    };
    const reservation = { ...payload, digest: digest(payload) };
    assertCapabilityQuotaReservation(reservation);
    this.reservations.set(reservation.reservationId, reservation);
    const reserved = {
      ...account.reserved,
      [input.capability]: (account.reserved[input.capability] ?? 0) + amount,
    };
    const nextAccount = this.transitionAccount(account, { reserved });
    return { account: nextAccount, reservation: structuredClone(reservation) };
  }
  consume(
    reservationId: string,
    expectedRevision: number,
  ): {
    account: CapabilityQuotaAccount;
    reservation: CapabilityQuotaReservation;
  } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "held")
      throw new E03RuntimeError(
        "capability_quota_consume_state",
        `capability quota reservation ${reservationId} is ${reservation.state}`,
      );
    if (Date.parse(reservation.expiresAt) <= Date.parse(this.clock.now()))
      return this.expire(reservationId, expectedRevision);
    const account = this.requireAccount(reservation.accountId);
    const reserved = {
      ...account.reserved,
      [reservation.capability]:
        (account.reserved[reservation.capability] ?? 0) - reservation.amount,
    };
    const consumed = {
      ...account.consumed,
      [reservation.capability]:
        (account.consumed[reservation.capability] ?? 0) + reservation.amount,
    };
    const exhausted = Object.keys(account.limits).every(
      (key) => (consumed[key] ?? 0) >= account.limits[key]!,
    );
    const nextAccount = this.transitionAccount(account, {
      reserved,
      consumed,
      state: exhausted ? "exhausted" : account.state,
    });
    const nextReservation = this.transitionReservation(reservation, {
      state: "consumed",
      settledAt: this.clock.now(),
    });
    return { account: nextAccount, reservation: nextReservation };
  }
  release(
    reservationId: string,
    expectedRevision: number,
  ): {
    account: CapabilityQuotaAccount;
    reservation: CapabilityQuotaReservation;
  } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "held")
      return {
        account: structuredClone(this.requireAccount(reservation.accountId)),
        reservation: structuredClone(reservation),
      };
    const account = this.requireAccount(reservation.accountId);
    const reserved = {
      ...account.reserved,
      [reservation.capability]:
        (account.reserved[reservation.capability] ?? 0) - reservation.amount,
    };
    return {
      account: this.transitionAccount(account, { reserved }),
      reservation: this.transitionReservation(reservation, {
        state: "released",
        settledAt: this.clock.now(),
      }),
    };
  }
  expire(
    reservationId: string,
    expectedRevision: number,
  ): {
    account: CapabilityQuotaAccount;
    reservation: CapabilityQuotaReservation;
  } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "held")
      return {
        account: structuredClone(this.requireAccount(reservation.accountId)),
        reservation: structuredClone(reservation),
      };
    const account = this.requireAccount(reservation.accountId);
    const reserved = {
      ...account.reserved,
      [reservation.capability]:
        (account.reserved[reservation.capability] ?? 0) - reservation.amount,
    };
    return {
      account: this.transitionAccount(account, { reserved }),
      reservation: this.transitionReservation(reservation, {
        state: "expired",
        settledAt: this.clock.now(),
      }),
    };
  }
  close(accountId: string, expectedRevision: number): CapabilityQuotaAccount {
    const account = this.requireAccount(accountId);
    this.assertAccountRevision(account, expectedRevision);
    if (
      [...this.reservations.values()].some(
        (value) => value.accountId === accountId && value.state === "held",
      )
    )
      throw new E03RuntimeError(
        "capability_quota_live_reservation",
        `capability quota ${accountId} has live reservations`,
      );
    if (account.state === "closed") return structuredClone(account);
    return this.transitionAccount(account, {
      state: "closed",
      closedAt: this.clock.now(),
    });
  }
  snapshot(): {
    accounts: CapabilityQuotaAccount[];
    reservations: CapabilityQuotaReservation[];
  } {
    return {
      accounts: [...this.accounts.values()].map((value) =>
        structuredClone(value),
      ),
      reservations: [...this.reservations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    accounts: readonly CapabilityQuotaAccount[];
    reservations: readonly CapabilityQuotaReservation[];
  }): void {
    const accounts = new Map<string, CapabilityQuotaAccount>();
    const reservations = new Map<string, CapabilityQuotaReservation>();
    for (const value of snapshot.accounts) {
      assertCapabilityQuota(value);
      if (accounts.has(value.accountId))
        throw new E03RuntimeError(
          "capability_quota_restore_duplicate",
          `duplicate capability quota ${value.accountId}`,
        );
      accounts.set(value.accountId, structuredClone(value));
    }
    for (const value of snapshot.reservations) {
      assertCapabilityQuotaReservation(value);
      const account = accounts.get(value.accountId);
      if (
        !account ||
        account.taskId !== value.taskId ||
        reservations.has(value.reservationId)
      )
        throw new E03RuntimeError(
          "capability_quota_reservation_restore",
          `capability quota reservation ${value.reservationId} is invalid`,
        );
      reservations.set(value.reservationId, structuredClone(value));
    }
    this.accounts = accounts;
    this.reservations = reservations;
  }
  private requireAccount(id: string): CapabilityQuotaAccount {
    const value = this.accounts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "capability_quota_missing",
        `capability quota ${id} does not exist`,
      );
    assertCapabilityQuota(value);
    return value;
  }
  private requireReservation(id: string): CapabilityQuotaReservation {
    const value = this.reservations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "capability_quota_reservation_missing",
        `capability quota reservation ${id} does not exist`,
      );
    assertCapabilityQuotaReservation(value);
    return value;
  }
  private assertAccountRevision(
    value: CapabilityQuotaAccount,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "capability_quota_stale_revision",
        `capability quota ${value.accountId} revision is stale`,
      );
  }
  private assertReservationRevision(
    value: CapabilityQuotaReservation,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "capability_quota_reservation_stale_revision",
        `capability quota reservation ${value.reservationId} revision is stale`,
      );
  }
  private transitionAccount(
    value: CapabilityQuotaAccount,
    patch: Partial<
      Omit<CapabilityQuotaAccount, "accountId" | "revision" | "digest">
    >,
  ): CapabilityQuotaAccount {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      accountId: value.accountId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityQuota(next);
    this.accounts.set(next.accountId, next);
    return structuredClone(next);
  }
  private transitionReservation(
    value: CapabilityQuotaReservation,
    patch: Partial<
      Omit<CapabilityQuotaReservation, "reservationId" | "revision" | "digest">
    >,
  ): CapabilityQuotaReservation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      reservationId: value.reservationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityQuotaReservation(next);
    this.reservations.set(next.reservationId, next);
    return structuredClone(next);
  }
}

function containsAny(roots: readonly string[], candidate: string): boolean {
  return roots.some((root) => contains(root, candidate));
}
