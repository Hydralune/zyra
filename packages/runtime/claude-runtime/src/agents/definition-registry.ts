import { type E03Clock, SystemE03Clock } from "../e03/contracts.ts";
import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  createId,
  DEFAULT_E03_BUDGET,
  digest,
  E03RuntimeError,
  requireString,
  sealSnapshot,
  stringArray,
  unique,
  type E03AgentDefinition,
  type E03Budget,
  type E03RegistrySnapshot,
  type IsolationMode,
} from "../e03/contracts.ts";

const SOURCE_PRIORITY: Readonly<Record<E03AgentDefinition["source"], number>> =
  Object.freeze({
    builtin: 100,
    plugin: 200,
    user: 300,
    project: 400,
  });

const ISOLATION_MODES = new Set<IsolationMode>([
  "none",
  "workspace",
  "worktree",
  "sandbox",
  "remote",
]);

export interface DefinitionInput extends JsonObject {
  name: string;
  description: string;
}

export class AgentDefinitionRegistry {
  private readonly definitions = new Map<string, E03AgentDefinition[]>();
  readonly provenance = new AgentDefinitionProvenance();
  private revision = 0;

  constructor(initial: readonly E03AgentDefinition[] = []) {
    for (const definition of initial) this.register(definition);
  }

  register(input: E03AgentDefinition | DefinitionInput): E03AgentDefinition {
    const definition = this.normalize(input);
    const candidates = this.definitions.get(definition.name) ?? [];
    const duplicate = candidates.find(
      (candidate) =>
        candidate.source === definition.source &&
        candidate.version === definition.version,
    );
    if (duplicate) {
      if (duplicate.digest !== definition.digest) {
        throw new E03RuntimeError(
          "definition_conflict",
          `agent ${definition.name} ${definition.source}@${definition.version} changed without a new version`,
          {
            name: definition.name,
            source: definition.source,
            version: definition.version,
          },
        );
      }
      return cloneJson(duplicate);
    }
    const next = [...candidates, definition].sort(compareDefinitions);
    this.definitions.set(definition.name, next);
    this.provenance.registered(
      definition,
      next[0] ?? null,
      next[0]?.digest === definition.digest
        ? "definition became active"
        : "higher-priority definition remains active",
    );
    this.revision += 1;
    return cloneJson(definition);
  }

  resolve(
    name: string,
    requestedSource?: E03AgentDefinition["source"],
  ): E03AgentDefinition {
    const normalized = requireString(name, "agent.name", 1, 128);
    const candidates = this.definitions.get(normalized) ?? [];
    const selected = requestedSource
      ? candidates.find((candidate) => candidate.source === requestedSource)
      : candidates[0];
    if (!selected) {
      throw new E03RuntimeError(
        "unknown_agent",
        `unknown agent definition ${normalized}`,
        { name: normalized, requestedSource: requestedSource ?? "" },
      );
    }
    this.provenance.selected(
      selected,
      requestedSource
        ? `explicit source ${requestedSource}`
        : "precedence resolution",
    );
    return cloneJson(selected);
  }

  list(name?: string): E03AgentDefinition[] {
    const values = name
      ? (this.definitions.get(name) ?? [])
      : [...this.definitions.values()].flat();
    return values.map(cloneJson).sort(compareDefinitions);
  }

  remove(
    name: string,
    source: E03AgentDefinition["source"],
    version: string,
  ): boolean {
    const candidates = this.definitions.get(name) ?? [];
    const removed = candidates.find(
      (candidate) =>
        candidate.source === source && candidate.version === version,
    );
    const next = candidates.filter(
      (candidate) =>
        !(candidate.source === source && candidate.version === version),
    );
    if (next.length === candidates.length) return false;
    if (next.length) this.definitions.set(name, next);
    else this.definitions.delete(name);
    if (removed)
      this.provenance.removed(removed, "definition removed from registry");
    this.revision += 1;
    return true;
  }

  restore(snapshot: Pick<E03RegistrySnapshot, "definitions">): void {
    const restored = new Map<string, E03AgentDefinition[]>();
    for (const [name, candidates] of Object.entries(snapshot.definitions)) {
      for (const candidate of candidates) {
        const normalized = this.normalize(candidate);
        if (normalized.name !== name)
          throw new E03RuntimeError(
            "definition_corrupt",
            `definition key ${name} does not match ${normalized.name}`,
          );
        const list = restored.get(name) ?? [];
        if (
          list.some(
            (item) =>
              item.source === normalized.source &&
              item.version === normalized.version,
          )
        ) {
          throw new E03RuntimeError(
            "definition_corrupt",
            `duplicate restored definition ${name}`,
          );
        }
        list.push(normalized);
        restored.set(name, list.sort(compareDefinitions));
      }
    }
    this.definitions.clear();
    for (const [name, candidates] of restored)
      this.definitions.set(name, candidates);
    this.revision += 1;
  }

  project(snapshot: E03RegistrySnapshot): E03RegistrySnapshot {
    const definitions = Object.fromEntries(
      [...this.definitions.entries()].map(([name, candidates]) => [
        name,
        candidates.map(cloneJson),
      ]),
    );
    return sealSnapshot({
      ...snapshot,
      definitions,
      revision: Math.max(snapshot.revision, this.revision),
    });
  }

  generation(): string {
    return digest({
      revision: this.revision,
      definitions: this.list().map((item) => item.digest),
    });
  }

  private normalize(
    input: E03AgentDefinition | DefinitionInput,
  ): E03AgentDefinition {
    const value = input as Record<string, unknown>;
    const name = requireString(value.name, "agent.name", 1, 128);
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name))
      throw new E03RuntimeError(
        "invalid_agent_name",
        `invalid agent name ${name}`,
      );
    const description = requireString(
      value.description,
      "agent.description",
      3,
      4_096,
    );
    const version = requireString(
      value.version ?? "1",
      "agent.version",
      1,
      128,
    );
    const source = normalizeSource(value.source);
    const tools = stringArray(value.tools ?? [], "agent.tools", 2_048);
    const deniedTools = stringArray(
      value.deniedTools ?? value.denied_tools ?? [],
      "agent.deniedTools",
      2_048,
    );
    const overlap = tools.filter((tool) => deniedTools.includes(tool));
    if (overlap.length)
      throw new E03RuntimeError(
        "tool_scope_overlap",
        `agent ${name} allows and denies ${overlap.join(", ")}`,
        { overlap },
      );
    const isolation = String(value.isolation ?? "workspace") as IsolationMode;
    if (!ISOLATION_MODES.has(isolation))
      throw new E03RuntimeError(
        "invalid_isolation",
        `agent ${name} has invalid isolation ${isolation}`,
      );
    const budget = normalizeBudget(
      (value.budget ?? {}) as Record<string, unknown>,
    );
    const payload = {
      name,
      description,
      version,
      source,
      priority: SOURCE_PRIORITY[source],
      model: requireString(value.model ?? "inherit", "agent.model", 1, 256),
      effort: requireString(value.effort ?? "inherit", "agent.effort", 1, 64),
      systemPrompt:
        typeof value.systemPrompt === "string"
          ? value.systemPrompt
          : typeof value.system_prompt === "string"
            ? value.system_prompt
            : "",
      tools,
      deniedTools,
      skills: stringArray(value.skills ?? [], "agent.skills", 2_048),
      mcpServers: stringArray(
        value.mcpServers ?? value.mcp_servers ?? [],
        "agent.mcpServers",
        2_048,
      ),
      permissionMode: requireString(
        value.permissionMode ?? value.permission_mode ?? "inherit",
        "agent.permissionMode",
        1,
        64,
      ),
      isolation,
      background: value.background === true,
      memoryScope: requireString(
        value.memoryScope ?? value.memory_scope ?? "task",
        "agent.memoryScope",
        1,
        128,
      ),
      budget,
      metadata: normalizeMetadata(value.metadata),
    };
    return { ...payload, digest: digest(payload) };
  }
}

function compareDefinitions(
  left: E03AgentDefinition,
  right: E03AgentDefinition,
): number {
  return (
    right.priority - left.priority ||
    right.version.localeCompare(left.version) ||
    left.digest.localeCompare(right.digest)
  );
}

function normalizeSource(value: unknown): E03AgentDefinition["source"] {
  const source = String(value ?? "user") as E03AgentDefinition["source"];
  if (!(source in SOURCE_PRIORITY))
    throw new E03RuntimeError(
      "invalid_definition_source",
      `invalid agent definition source ${source}`,
    );
  return source;
}

function normalizeBudget(raw: Record<string, unknown>): E03Budget {
  const startedAt = typeof raw.startedAt === "string" ? raw.startedAt : "";
  const deadlineAt = typeof raw.deadlineAt === "string" ? raw.deadlineAt : "";
  const integer = (
    camel: keyof E03Budget,
    snake: string,
    fallback: number,
    minimum = 0,
  ): number => {
    const value = raw[camel] ?? raw[snake];
    if (value === undefined) return fallback;
    if (!Number.isSafeInteger(value) || (value as number) < minimum)
      throw new E03RuntimeError(
        "invalid_budget",
        `${String(camel)} must be an integer >= ${minimum}`,
      );
    return value as number;
  };
  const budget: E03Budget = {
    maxTurns: integer("maxTurns", "max_turns", DEFAULT_E03_BUDGET.maxTurns, 1),
    maxToolCalls: integer(
      "maxToolCalls",
      "max_tool_calls",
      DEFAULT_E03_BUDGET.maxToolCalls,
      1,
    ),
    maxInputTokens: integer(
      "maxInputTokens",
      "max_input_tokens",
      DEFAULT_E03_BUDGET.maxInputTokens,
      1,
    ),
    maxOutputTokens: integer(
      "maxOutputTokens",
      "max_output_tokens",
      DEFAULT_E03_BUDGET.maxOutputTokens,
      1,
    ),
    maxResultChars: integer(
      "maxResultChars",
      "max_result_chars",
      DEFAULT_E03_BUDGET.maxResultChars,
      1,
    ),
    maxWallTimeMs: integer(
      "maxWallTimeMs",
      "max_wall_time_ms",
      DEFAULT_E03_BUDGET.maxWallTimeMs,
      1,
    ),
    maxChildren: integer(
      "maxChildren",
      "max_children",
      DEFAULT_E03_BUDGET.maxChildren,
    ),
    maxDepth: integer("maxDepth", "max_depth", DEFAULT_E03_BUDGET.maxDepth),
    maxConcurrency: integer(
      "maxConcurrency",
      "max_concurrency",
      DEFAULT_E03_BUDGET.maxConcurrency,
      1,
    ),
    consumedTurns: integer("consumedTurns", "consumed_turns", 0),
    consumedToolCalls: integer("consumedToolCalls", "consumed_tool_calls", 0),
    consumedInputTokens: integer(
      "consumedInputTokens",
      "consumed_input_tokens",
      0,
    ),
    consumedOutputTokens: integer(
      "consumedOutputTokens",
      "consumed_output_tokens",
      0,
    ),
    consumedResultChars: integer(
      "consumedResultChars",
      "consumed_result_chars",
      0,
    ),
    startedAt,
    deadlineAt,
  };
  if (
    budget.consumedTurns > budget.maxTurns ||
    budget.consumedToolCalls > budget.maxToolCalls
  )
    throw new E03RuntimeError(
      "budget_overdrawn",
      "restored agent budget exceeds its ceiling",
    );
  return budget;
}

function normalizeMetadata(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return structuredClone(value) as JsonObject;
}

export function builtinAgentDefinitions(): E03AgentDefinition[] {
  const registry = new AgentDefinitionRegistry();
  const defaults: DefinitionInput[] = [
    {
      name: "general",
      description: "General bounded Zyra CodeWorker child",
      source: "builtin",
      tools: [],
      deniedTools: [],
      isolation: "workspace",
    },
    {
      name: "explore",
      description: "Read-oriented repository exploration child",
      source: "builtin",
      deniedTools: ["file_write", "file_edit"],
      background: true,
      isolation: "workspace",
    },
    {
      name: "verify",
      description: "Bounded verification and evidence child",
      source: "builtin",
      tools: [],
      deniedTools: [],
      isolation: "workspace",
    },
  ];
  return defaults.map((definition) => registry.register(definition));
}

export function mergeDefinitionTools(
  definition: E03AgentDefinition,
  extraTools: readonly string[],
  deniedTools: readonly string[],
): E03AgentDefinition {
  const denied = unique([...definition.deniedTools, ...deniedTools]);
  const tools = unique([...definition.tools, ...extraTools]).filter(
    (tool) => !denied.includes(tool),
  );
  const { digest: _digest, ...base } = definition;
  const payload = { ...base, tools, deniedTools: denied };
  return { ...payload, digest: digest(payload) };
}

export interface DefinitionProvenanceRecord {
  recordId: string;
  name: string;
  version: string;
  source: E03AgentDefinition["source"];
  priority: number;
  definitionDigest: string;
  action: "registered" | "shadowed" | "selected" | "removed";
  selectedDigest: string;
  reason: string;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

export class AgentDefinitionProvenance {
  private records: DefinitionProvenanceRecord[] = [];

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registered(
    definition: E03AgentDefinition,
    selected: E03AgentDefinition | null,
    reason: string,
  ): DefinitionProvenanceRecord {
    return this.append(
      definition,
      selected?.digest ?? "",
      selected && selected.digest !== definition.digest
        ? "shadowed"
        : "registered",
      reason,
    );
  }

  selected(
    definition: E03AgentDefinition,
    reason: string,
  ): DefinitionProvenanceRecord {
    return this.append(definition, definition.digest, "selected", reason);
  }

  removed(
    definition: E03AgentDefinition,
    reason: string,
  ): DefinitionProvenanceRecord {
    return this.append(definition, "", "removed", reason);
  }

  restore(records: readonly DefinitionProvenanceRecord[]): void {
    let previousDigest = "";
    for (const record of records) {
      this.assertRecord(record, previousDigest);
      previousDigest = record.digest;
    }
    this.records = records.map((record) => structuredClone(record));
  }

  snapshot(): DefinitionProvenanceRecord[] {
    return this.records.map((record) => structuredClone(record));
  }

  forDefinition(name: string): DefinitionProvenanceRecord[] {
    return this.snapshot().filter((record) => record.name === name);
  }

  selectedDigest(name: string): string {
    const records = this.records.filter(
      (record) => record.name === name && record.action === "selected",
    );
    return records.at(-1)?.selectedDigest ?? "";
  }

  verify(): {
    valid: true;
    records: number;
    definitions: number;
    headDigest: string;
  } {
    let previousDigest = "";
    const names = new Set<string>();
    for (const record of this.records) {
      this.assertRecord(record, previousDigest);
      previousDigest = record.digest;
      names.add(record.name);
    }
    return {
      valid: true,
      records: this.records.length,
      definitions: names.size,
      headDigest: previousDigest,
    };
  }

  private append(
    definition: E03AgentDefinition,
    selectedDigest: string,
    action: DefinitionProvenanceRecord["action"],
    reason: string,
  ): DefinitionProvenanceRecord {
    this.assertDefinition(definition);
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      recordId: `definition-provenance-${digest({ name: definition.name, definitionDigest: definition.digest, action, previousDigest }).slice(0, 32)}`,
      name: definition.name,
      version: definition.version,
      source: definition.source,
      priority: definition.priority,
      definitionDigest: definition.digest,
      action,
      selectedDigest,
      reason: reason.trim() || action,
      recordedAt: this.clock.now(),
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    this.records.push(record);
    return structuredClone(record);
  }

  private assertDefinition(definition: E03AgentDefinition): void {
    const { digest: checksum, ...payload } = definition;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "definition_digest_mismatch",
        `definition ${definition.name} digest is invalid`,
      );
    if (!definition.name || !definition.version || !definition.source)
      throw new E03RuntimeError(
        "invalid_agent_definition",
        "definition identity is incomplete",
      );
  }

  private assertRecord(
    record: DefinitionProvenanceRecord,
    previousDigest: string,
  ): void {
    const { digest: checksum, ...payload } = record;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "definition_provenance_digest",
        `provenance ${record.recordId} digest is invalid`,
      );
    if (record.previousDigest !== previousDigest)
      throw new E03RuntimeError(
        "definition_provenance_chain",
        `provenance ${record.recordId} does not extend prior record`,
      );
    if (
      !record.recordId ||
      !record.name ||
      !record.version ||
      !record.definitionDigest ||
      !record.reason
    )
      throw new E03RuntimeError(
        "invalid_definition_provenance",
        "definition provenance identity is incomplete",
      );
    if (
      (record.action === "selected" || record.action === "registered") &&
      !record.selectedDigest
    )
      throw new E03RuntimeError(
        "definition_selection_missing",
        "selected/registered provenance has no selected digest",
      );
  }
}

export interface DefinitionRequirement {
  requirementId: string;
  consumerName: string;
  dependencyName: string;
  acceptedSources: E03AgentDefinition["source"][];
  minimumVersion: string | null;
  maximumVersion: string | null;
  requiredTools: string[];
  requiredSkills: string[];
  requiredMcpServers: string[];
  allowBackground: boolean | null;
  acceptedIsolationModes: IsolationMode[];
  optional: boolean;
  digest: string;
}

export interface DefinitionResolution {
  resolutionId: string;
  requirementId: string;
  consumerName: string;
  dependencyName: string;
  selectedDefinitionDigest: string | null;
  selectedSource: E03AgentDefinition["source"] | null;
  selectedVersion: string | null;
  compatible: boolean;
  findings: string[];
  registryGeneration: string;
  resolvedAt: string;
  digest: string;
}

export interface DefinitionDependencyGraph {
  graphId: string;
  generation: string;
  nodes: string[];
  edges: Array<{
    requirementId: string;
    consumerName: string;
    dependencyName: string;
    optional: boolean;
    compatible: boolean;
    selectedDefinitionDigest: string | null;
  }>;
  unresolvedRequirementIds: string[];
  cyclePaths: string[][];
  createdAt: string;
  digest: string;
}

function assertDefinitionRequirement(requirement: DefinitionRequirement): void {
  const { digest: checksum, ...payload } = requirement;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_requirement_digest",
      `definition requirement ${requirement.requirementId} digest is invalid`,
    );
  if (
    !requirement.requirementId ||
    !requirement.consumerName ||
    !requirement.dependencyName
  )
    throw new E03RuntimeError(
      "definition_requirement_identity",
      "definition requirement identity is incomplete",
    );
  if (
    requirement.minimumVersion !== null &&
    requirement.maximumVersion !== null &&
    compareVersion(requirement.minimumVersion, requirement.maximumVersion) > 0
  )
    throw new E03RuntimeError(
      "definition_requirement_version_range",
      "definition requirement minimum version exceeds maximum version",
    );
}

function assertDefinitionResolution(resolution: DefinitionResolution): void {
  const { digest: checksum, ...payload } = resolution;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_resolution_digest",
      `definition resolution ${resolution.resolutionId} digest is invalid`,
    );
  if (
    !resolution.resolutionId ||
    !resolution.requirementId ||
    !resolution.consumerName ||
    !resolution.dependencyName ||
    !resolution.registryGeneration
  )
    throw new E03RuntimeError(
      "definition_resolution_identity",
      "definition resolution identity is incomplete",
    );
  if (resolution.compatible && !resolution.selectedDefinitionDigest)
    throw new E03RuntimeError(
      "definition_resolution_selection",
      "compatible definition resolution requires a selected definition",
    );
}

export class DefinitionDependencyRuntime {
  private requirements = new Map<string, DefinitionRequirement>();
  private resolutions = new Map<string, DefinitionResolution>();
  private graphs = new Map<string, DefinitionDependencyGraph>();

  constructor(
    private readonly registry: AgentDefinitionRegistry,
    private readonly clock: E03Clock = new SystemE03Clock(),
  ) {}

  require(input: {
    consumerName: string;
    dependencyName: string;
    acceptedSources?: readonly E03AgentDefinition["source"][];
    minimumVersion?: string | null;
    maximumVersion?: string | null;
    requiredTools?: readonly string[];
    requiredSkills?: readonly string[];
    requiredMcpServers?: readonly string[];
    allowBackground?: boolean | null;
    acceptedIsolationModes?: readonly IsolationMode[];
    optional?: boolean;
  }): DefinitionRequirement {
    const consumerName = requireString(
      input.consumerName,
      "consumerName",
      1,
      128,
    );
    const dependencyName = requireString(
      input.dependencyName,
      "dependencyName",
      1,
      128,
    );
    if (consumerName === dependencyName)
      throw new E03RuntimeError(
        "definition_requirement_self",
        "agent definition cannot depend on itself",
      );
    const payload = {
      requirementId: `definition-requirement-${digest({ consumerName, dependencyName, input }).slice(0, 24)}`,
      consumerName,
      dependencyName,
      acceptedSources: unique(
        input.acceptedSources ?? [],
      ) as E03AgentDefinition["source"][],
      minimumVersion: input.minimumVersion?.trim() || null,
      maximumVersion: input.maximumVersion?.trim() || null,
      requiredTools: unique(input.requiredTools ?? []),
      requiredSkills: unique(input.requiredSkills ?? []),
      requiredMcpServers: unique(input.requiredMcpServers ?? []),
      allowBackground: input.allowBackground ?? null,
      acceptedIsolationModes: unique(
        input.acceptedIsolationModes ?? [],
      ) as IsolationMode[],
      optional: input.optional ?? false,
    };
    const requirement = { ...payload, digest: digest(payload) };
    assertDefinitionRequirement(requirement);
    const existing = this.requirements.get(requirement.requirementId);
    if (existing && existing.digest !== requirement.digest)
      throw new E03RuntimeError(
        "definition_requirement_conflict",
        `definition requirement ${requirement.requirementId} changed`,
      );
    this.requirements.set(requirement.requirementId, requirement);
    return cloneJson(requirement);
  }

  resolve(requirementId: string): DefinitionResolution {
    const requirement = this.requireRequirement(requirementId);
    const candidates = this.registry.list(requirement.dependencyName);
    const findings: string[] = [];
    let selected: E03AgentDefinition | null = null;
    for (const candidate of candidates) {
      const candidateFindings = this.compatibilityFindings(
        requirement,
        candidate,
      );
      if (!candidateFindings.length) {
        selected = candidate;
        break;
      }
      findings.push(
        `${candidate.source}@${candidate.version}: ${candidateFindings.join("; ")}`,
      );
    }
    if (!selected && !candidates.length)
      findings.push(
        `definition ${requirement.dependencyName} is not registered`,
      );
    const generation = this.registry.generation();
    const payload = {
      resolutionId: `definition-resolution-${digest({ requirementId, generation }).slice(0, 24)}`,
      requirementId: requirement.requirementId,
      consumerName: requirement.consumerName,
      dependencyName: requirement.dependencyName,
      selectedDefinitionDigest: selected?.digest ?? null,
      selectedSource: selected?.source ?? null,
      selectedVersion: selected?.version ?? null,
      compatible: selected !== null || requirement.optional,
      findings: selected ? [] : findings,
      registryGeneration: generation,
      resolvedAt: this.clock.now(),
    };
    const resolution = { ...payload, digest: digest(payload) };
    assertDefinitionResolution(resolution);
    this.resolutions.set(resolution.resolutionId, resolution);
    return cloneJson(resolution);
  }

  resolveAll(consumerName?: string): DefinitionResolution[] {
    return [...this.requirements.values()]
      .filter(
        (requirement) =>
          !consumerName || requirement.consumerName === consumerName,
      )
      .sort(
        (left, right) =>
          left.consumerName.localeCompare(right.consumerName) ||
          left.dependencyName.localeCompare(right.dependencyName),
      )
      .map((requirement) => this.resolve(requirement.requirementId));
  }

  buildGraph(): DefinitionDependencyGraph {
    const resolutions = this.resolveAll();
    const resolutionByRequirement = new Map(
      resolutions.map((resolution) => [resolution.requirementId, resolution]),
    );
    const edges = [...this.requirements.values()].map((requirement) => {
      const resolution = resolutionByRequirement.get(
        requirement.requirementId,
      )!;
      return {
        requirementId: requirement.requirementId,
        consumerName: requirement.consumerName,
        dependencyName: requirement.dependencyName,
        optional: requirement.optional,
        compatible: resolution.compatible,
        selectedDefinitionDigest: resolution.selectedDefinitionDigest,
      };
    });
    const nodes = unique(
      edges.flatMap((edge) => [edge.consumerName, edge.dependencyName]),
    ).sort();
    const cyclePaths = findDefinitionCycles(nodes, edges);
    const generation = this.registry.generation();
    const payload = {
      graphId: `definition-graph-${digest({ generation, edges }).slice(0, 24)}`,
      generation,
      nodes,
      edges,
      unresolvedRequirementIds: edges
        .filter((edge) => !edge.compatible && !edge.optional)
        .map((edge) => edge.requirementId),
      cyclePaths,
      createdAt: this.clock.now(),
    };
    const graph = { ...payload, digest: digest(payload) };
    this.assertGraph(graph);
    this.graphs.set(graph.graphId, cloneJson(graph));
    return cloneJson(graph);
  }

  assertReady(graphId: string): DefinitionDependencyGraph {
    const graph = this.graphs.get(graphId);
    if (!graph)
      throw new E03RuntimeError(
        "definition_graph_missing",
        `definition dependency graph ${graphId} does not exist`,
      );
    this.assertGraph(graph);
    if (graph.generation !== this.registry.generation())
      throw new E03RuntimeError(
        "definition_graph_stale",
        `definition dependency graph ${graphId} is stale`,
      );
    if (graph.unresolvedRequirementIds.length)
      throw new E03RuntimeError(
        "definition_graph_unresolved",
        `definition dependency graph has unresolved requirements: ${graph.unresolvedRequirementIds.join(", ")}`,
      );
    if (graph.cyclePaths.length)
      throw new E03RuntimeError(
        "definition_graph_cycle",
        `definition dependency graph contains cycles: ${graph.cyclePaths.map((path) => path.join(" -> ")).join("; ")}`,
      );
    return cloneJson(graph);
  }

  remove(requirementId: string): boolean {
    return this.requirements.delete(requirementId);
  }

  snapshot(): {
    requirements: DefinitionRequirement[];
    resolutions: DefinitionResolution[];
    graphs: DefinitionDependencyGraph[];
  } {
    return {
      requirements: [...this.requirements.values()].map(cloneJson),
      resolutions: [...this.resolutions.values()].map(cloneJson),
      graphs: [...this.graphs.values()].map(cloneJson),
    };
  }

  restore(input: {
    requirements: readonly DefinitionRequirement[];
    resolutions: readonly DefinitionResolution[];
    graphs: readonly DefinitionDependencyGraph[];
  }): void {
    const requirements = new Map<string, DefinitionRequirement>();
    const resolutions = new Map<string, DefinitionResolution>();
    const graphs = new Map<string, DefinitionDependencyGraph>();
    for (const raw of input.requirements) {
      const requirement = cloneJson(raw);
      assertDefinitionRequirement(requirement);
      if (requirements.has(requirement.requirementId))
        throw new E03RuntimeError(
          "definition_requirement_restore_duplicate",
          `duplicate definition requirement ${requirement.requirementId}`,
        );
      requirements.set(requirement.requirementId, requirement);
    }
    for (const raw of input.resolutions) {
      const resolution = cloneJson(raw);
      assertDefinitionResolution(resolution);
      if (!requirements.has(resolution.requirementId))
        throw new E03RuntimeError(
          "definition_resolution_restore_orphan",
          `definition resolution ${resolution.resolutionId} has no requirement`,
        );
      if (resolutions.has(resolution.resolutionId))
        throw new E03RuntimeError(
          "definition_resolution_restore_duplicate",
          `duplicate definition resolution ${resolution.resolutionId}`,
        );
      resolutions.set(resolution.resolutionId, resolution);
    }
    for (const raw of input.graphs) {
      const graph = cloneJson(raw);
      this.assertGraph(graph);
      if (graphs.has(graph.graphId))
        throw new E03RuntimeError(
          "definition_graph_restore_duplicate",
          `duplicate definition graph ${graph.graphId}`,
        );
      for (const edge of graph.edges)
        if (!requirements.has(edge.requirementId))
          throw new E03RuntimeError(
            "definition_graph_restore_requirement",
            `definition graph ${graph.graphId} references missing requirement`,
          );
      graphs.set(graph.graphId, graph);
    }
    this.requirements = requirements;
    this.resolutions = resolutions;
    this.graphs = graphs;
  }

  private compatibilityFindings(
    requirement: DefinitionRequirement,
    candidate: E03AgentDefinition,
  ): string[] {
    const findings: string[] = [];
    if (
      requirement.acceptedSources.length &&
      !requirement.acceptedSources.includes(candidate.source)
    )
      findings.push(`source ${candidate.source} is not accepted`);
    if (
      requirement.minimumVersion !== null &&
      compareVersion(candidate.version, requirement.minimumVersion) < 0
    )
      findings.push(
        `version ${candidate.version} is below ${requirement.minimumVersion}`,
      );
    if (
      requirement.maximumVersion !== null &&
      compareVersion(candidate.version, requirement.maximumVersion) > 0
    )
      findings.push(
        `version ${candidate.version} is above ${requirement.maximumVersion}`,
      );
    const missingTools = requirement.requiredTools.filter(
      (tool) => !candidate.tools.includes(tool),
    );
    if (missingTools.length)
      findings.push(`missing tools ${missingTools.join(", ")}`);
    const missingSkills = requirement.requiredSkills.filter(
      (skill) => !candidate.skills.includes(skill),
    );
    if (missingSkills.length)
      findings.push(`missing skills ${missingSkills.join(", ")}`);
    const missingMcp = requirement.requiredMcpServers.filter(
      (server) => !candidate.mcpServers.includes(server),
    );
    if (missingMcp.length)
      findings.push(`missing MCP servers ${missingMcp.join(", ")}`);
    if (
      requirement.allowBackground !== null &&
      candidate.background !== requirement.allowBackground
    )
      findings.push(
        `background=${candidate.background} does not match requirement`,
      );
    if (
      requirement.acceptedIsolationModes.length &&
      !requirement.acceptedIsolationModes.includes(candidate.isolation)
    )
      findings.push(`isolation ${candidate.isolation} is not accepted`);
    return findings;
  }

  private requireRequirement(requirementId: string): DefinitionRequirement {
    const requirement = this.requirements.get(requirementId);
    if (!requirement)
      throw new E03RuntimeError(
        "definition_requirement_missing",
        `definition requirement ${requirementId} does not exist`,
      );
    assertDefinitionRequirement(requirement);
    return requirement;
  }

  private assertGraph(graph: DefinitionDependencyGraph): void {
    const { digest: checksum, ...payload } = graph;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "definition_graph_digest",
        `definition dependency graph ${graph.graphId} digest is invalid`,
      );
    if (!graph.graphId || !graph.generation)
      throw new E03RuntimeError(
        "definition_graph_identity",
        "definition dependency graph identity is incomplete",
      );
    const nodeSet = new Set(graph.nodes);
    for (const edge of graph.edges)
      if (!nodeSet.has(edge.consumerName) || !nodeSet.has(edge.dependencyName))
        throw new E03RuntimeError(
          "definition_graph_edge",
          `definition dependency graph ${graph.graphId} has an orphan edge`,
        );
  }
}

function compareVersion(left: string, right: string): number {
  const normalize = (value: string): Array<number | string> =>
    value
      .split(/[.+-]/)
      .filter(Boolean)
      .map((part) => (/^\d+$/.test(part) ? Number(part) : part));
  const leftParts = normalize(left);
  const rightParts = normalize(right);
  const maximum = Math.max(leftParts.length, rightParts.length);
  for (let index = 0; index < maximum; index += 1) {
    const a = leftParts[index] ?? 0;
    const b = rightParts[index] ?? 0;
    if (a === b) continue;
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b));
  }
  return 0;
}

function findDefinitionCycles(
  nodes: readonly string[],
  edges: readonly DefinitionDependencyGraph["edges"][number][],
): string[][] {
  const adjacency = new Map<string, string[]>();
  for (const node of nodes) adjacency.set(node, []);
  for (const edge of edges)
    adjacency.set(edge.consumerName, [
      ...(adjacency.get(edge.consumerName) ?? []),
      edge.dependencyName,
    ]);
  const cycles = new Map<string, string[]>();
  const visit = (node: string, path: string[], active: Set<string>): void => {
    if (active.has(node)) {
      const start = path.indexOf(node);
      const cycle = [...path.slice(start), node];
      const canonical = canonicalCycle(cycle);
      cycles.set(canonical.join("->"), canonical);
      return;
    }
    const nextActive = new Set(active);
    nextActive.add(node);
    for (const neighbor of adjacency.get(node) ?? [])
      visit(neighbor, [...path, node], nextActive);
  };
  for (const node of nodes) visit(node, [], new Set());
  return [...cycles.values()].sort((left, right) =>
    left.join("->").localeCompare(right.join("->")),
  );
}

function canonicalCycle(cycle: readonly string[]): string[] {
  const body = cycle.slice(0, -1);
  if (!body.length) return [];
  const rotations = body.map((_, index) => [
    ...body.slice(index),
    ...body.slice(0, index),
  ]);
  rotations.sort((left, right) =>
    left.join("->").localeCompare(right.join("->")),
  );
  return [...rotations[0]!, rotations[0]![0]!];
}

export interface DefinitionActivation {
  activationId: string;
  name: string;
  version: string;
  definitionDigest: string;
  state: "candidate" | "active" | "draining" | "retired" | "rejected";
  trafficWeight: number;
  minimumHealthyRuns: number;
  healthyRuns: number;
  failedRuns: number;
  failureThreshold: number;
  activatedAt: string | null;
  drainedAt: string | null;
  retiredAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
export interface DefinitionBinding {
  bindingId: string;
  taskId: string;
  activationId: string;
  definitionName: string;
  definitionVersion: string;
  state: "reserved" | "bound" | "released" | "revoked";
  reservedAt: string;
  boundAt: string | null;
  releasedAt: string | null;
  revision: number;
  digest: string;
}
function assertActivation(value: DefinitionActivation): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_activation_digest",
      `definition activation ${value.activationId} is corrupt`,
    );
  if (
    !value.activationId ||
    !value.name ||
    !value.version ||
    !value.definitionDigest
  )
    throw new E03RuntimeError(
      "definition_activation_identity",
      "definition activation identity is required",
    );
  if (
    !Number.isFinite(value.trafficWeight) ||
    value.trafficWeight < 0 ||
    value.trafficWeight > 1 ||
    !Number.isSafeInteger(value.minimumHealthyRuns) ||
    value.minimumHealthyRuns < 0 ||
    !Number.isSafeInteger(value.healthyRuns) ||
    value.healthyRuns < 0 ||
    !Number.isSafeInteger(value.failedRuns) ||
    value.failedRuns < 0 ||
    !Number.isSafeInteger(value.failureThreshold) ||
    value.failureThreshold < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "definition_activation_counters",
      `definition activation ${value.activationId} counters are invalid`,
    );
  if (value.state === "active" && value.activatedAt === null)
    throw new E03RuntimeError(
      "definition_activation_time",
      `active definition ${value.activationId} lacks activation time`,
    );
}
function assertBinding(value: DefinitionBinding): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_binding_digest",
      `definition binding ${value.bindingId} is corrupt`,
    );
  if (
    !value.bindingId ||
    !value.taskId ||
    !value.activationId ||
    !value.definitionName ||
    !value.definitionVersion ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "definition_binding_identity",
      "definition binding is invalid",
    );
}
export class AgentDefinitionActivationRuntime {
  private activations = new Map<string, DefinitionActivation>();
  private bindings = new Map<string, DefinitionBinding>();
  private activeByName = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  nominate(
    definition: E03AgentDefinition,
    input?: {
      trafficWeight?: number;
      minimumHealthyRuns?: number;
      failureThreshold?: number;
    },
  ): DefinitionActivation {
    const existing = [...this.activations.values()].find(
      (value) =>
        value.name === definition.name &&
        value.version === definition.version &&
        value.definitionDigest === definition.digest &&
        value.state !== "retired" &&
        value.state !== "rejected",
    );
    if (existing) return structuredClone(existing);
    const trafficWeight = input?.trafficWeight ?? 0;
    const minimumHealthyRuns = input?.minimumHealthyRuns ?? 1;
    const failureThreshold = input?.failureThreshold ?? 3;
    const payload = {
      activationId: createId("definition-activation"),
      name: definition.name,
      version: definition.version,
      definitionDigest: definition.digest,
      state: "candidate" as const,
      trafficWeight,
      minimumHealthyRuns,
      healthyRuns: 0,
      failedRuns: 0,
      failureThreshold,
      activatedAt: null,
      drainedAt: null,
      retiredAt: null,
      rejectionReason: null,
      revision: 1,
    };
    const activation = { ...payload, digest: digest(payload) };
    assertActivation(activation);
    this.activations.set(activation.activationId, activation);
    return structuredClone(activation);
  }
  reportRun(
    activationId: string,
    expectedRevision: number,
    healthy: boolean,
  ): DefinitionActivation {
    const activation = this.requireActivation(activationId);
    this.assertActivationRevision(activation, expectedRevision);
    if (activation.state !== "candidate" && activation.state !== "active")
      throw new E03RuntimeError(
        "definition_activation_report_state",
        `definition activation ${activationId} is ${activation.state}`,
      );
    const next = this.transitionActivation(
      activation,
      healthy
        ? { healthyRuns: activation.healthyRuns + 1 }
        : { failedRuns: activation.failedRuns + 1 },
    );
    if (next.failedRuns >= next.failureThreshold)
      return this.reject(
        next.activationId,
        next.revision,
        "failure threshold reached",
      );
    return next;
  }
  activate(
    activationId: string,
    expectedRevision: number,
    trafficWeight = 1,
  ): DefinitionActivation {
    const activation = this.requireActivation(activationId);
    this.assertActivationRevision(activation, expectedRevision);
    if (activation.state !== "candidate")
      throw new E03RuntimeError(
        "definition_activation_state",
        `definition activation ${activationId} is ${activation.state}`,
      );
    if (activation.healthyRuns < activation.minimumHealthyRuns)
      throw new E03RuntimeError(
        "definition_activation_health",
        `definition activation ${activationId} has insufficient healthy runs`,
      );
    if (
      !Number.isFinite(trafficWeight) ||
      trafficWeight <= 0 ||
      trafficWeight > 1
    )
      throw new E03RuntimeError(
        "definition_activation_weight",
        "definition activation traffic weight is invalid",
      );
    const priorId = this.activeByName.get(activation.name);
    if (priorId) {
      const prior = this.requireActivation(priorId);
      if (
        prior.activationId !== activation.activationId &&
        prior.state === "active"
      )
        this.transitionActivation(prior, {
          state: "draining",
          trafficWeight: Math.max(0, 1 - trafficWeight),
          drainedAt: this.clock.now(),
        });
    }
    const next = this.transitionActivation(activation, {
      state: "active",
      trafficWeight,
      activatedAt: this.clock.now(),
    });
    this.activeByName.set(next.name, next.activationId);
    return next;
  }
  adjustTraffic(
    activationId: string,
    expectedRevision: number,
    trafficWeight: number,
  ): DefinitionActivation {
    const activation = this.requireActivation(activationId);
    this.assertActivationRevision(activation, expectedRevision);
    if (activation.state !== "active" && activation.state !== "draining")
      throw new E03RuntimeError(
        "definition_activation_traffic_state",
        `definition activation ${activationId} is ${activation.state}`,
      );
    if (
      !Number.isFinite(trafficWeight) ||
      trafficWeight < 0 ||
      trafficWeight > 1
    )
      throw new E03RuntimeError(
        "definition_activation_traffic_weight",
        "definition activation traffic weight is invalid",
      );
    return this.transitionActivation(activation, {
      trafficWeight,
      state: trafficWeight === 0 ? "draining" : activation.state,
      drainedAt: trafficWeight === 0 ? this.clock.now() : activation.drainedAt,
    });
  }
  reject(
    activationId: string,
    expectedRevision: number,
    reason: string,
  ): DefinitionActivation {
    const activation = this.requireActivation(activationId);
    this.assertActivationRevision(activation, expectedRevision);
    if (activation.state === "active" || activation.state === "retired")
      throw new E03RuntimeError(
        "definition_activation_reject_state",
        `definition activation ${activationId} is ${activation.state}`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "definition_activation_reject_reason",
        "definition activation rejection reason is required",
      );
    return this.transitionActivation(activation, {
      state: "rejected",
      trafficWeight: 0,
      rejectionReason: reason.trim(),
    });
  }
  retire(activationId: string, expectedRevision: number): DefinitionActivation {
    const activation = this.requireActivation(activationId);
    this.assertActivationRevision(activation, expectedRevision);
    if (activation.state !== "draining" && activation.state !== "rejected")
      throw new E03RuntimeError(
        "definition_activation_retire_state",
        `definition activation ${activationId} is ${activation.state}`,
      );
    if (
      [...this.bindings.values()].some(
        (binding) =>
          binding.activationId === activationId &&
          (binding.state === "reserved" || binding.state === "bound"),
      )
    )
      throw new E03RuntimeError(
        "definition_activation_live_binding",
        `definition activation ${activationId} has live bindings`,
      );
    const next = this.transitionActivation(activation, {
      state: "retired",
      trafficWeight: 0,
      retiredAt: this.clock.now(),
    });
    if (this.activeByName.get(next.name) === next.activationId)
      this.activeByName.delete(next.name);
    return next;
  }
  reserve(
    taskId: string,
    definitionName: string,
    entropy: number,
  ): DefinitionBinding {
    if (!taskId.trim() || !definitionName.trim() || !Number.isFinite(entropy))
      throw new E03RuntimeError(
        "definition_binding_input",
        "definition binding input is invalid",
      );
    const existing = [...this.bindings.values()].find(
      (value) =>
        value.taskId === taskId &&
        (value.state === "reserved" || value.state === "bound"),
    );
    if (existing) return structuredClone(existing);
    const candidates = [...this.activations.values()]
      .filter(
        (value) =>
          value.name === definitionName &&
          (value.state === "active" || value.state === "draining") &&
          value.trafficWeight > 0,
      )
      .sort((left, right) =>
        left.activationId.localeCompare(right.activationId),
      );
    if (!candidates.length)
      throw new E03RuntimeError(
        "definition_binding_no_activation",
        `definition ${definitionName} has no active version`,
      );
    const total = candidates.reduce(
      (sum, value) => sum + value.trafficWeight,
      0,
    );
    let cursor = Math.abs(entropy % 1) * total;
    let selected = candidates[candidates.length - 1]!;
    for (const candidate of candidates) {
      cursor -= candidate.trafficWeight;
      if (cursor <= 0) {
        selected = candidate;
        break;
      }
    }
    const payload = {
      bindingId: createId("definition-binding"),
      taskId: taskId.trim(),
      activationId: selected.activationId,
      definitionName: selected.name,
      definitionVersion: selected.version,
      state: "reserved" as const,
      reservedAt: this.clock.now(),
      boundAt: null,
      releasedAt: null,
      revision: 1,
    };
    const binding = { ...payload, digest: digest(payload) };
    assertBinding(binding);
    this.bindings.set(binding.bindingId, binding);
    return structuredClone(binding);
  }
  bind(bindingId: string, expectedRevision: number): DefinitionBinding {
    const binding = this.requireBinding(bindingId);
    this.assertBindingRevision(binding, expectedRevision);
    if (binding.state !== "reserved")
      throw new E03RuntimeError(
        "definition_binding_bind_state",
        `definition binding ${bindingId} is ${binding.state}`,
      );
    const activation = this.requireActivation(binding.activationId);
    if (activation.state !== "active" && activation.state !== "draining")
      return this.transitionBinding(binding, {
        state: "revoked",
        releasedAt: this.clock.now(),
      });
    return this.transitionBinding(binding, {
      state: "bound",
      boundAt: this.clock.now(),
    });
  }
  release(bindingId: string, expectedRevision: number): DefinitionBinding {
    const binding = this.requireBinding(bindingId);
    this.assertBindingRevision(binding, expectedRevision);
    if (binding.state === "released" || binding.state === "revoked")
      return structuredClone(binding);
    return this.transitionBinding(binding, {
      state: "released",
      releasedAt: this.clock.now(),
    });
  }
  rollback(
    name: string,
    targetActivationId: string,
  ): { target: DefinitionActivation; prior: DefinitionActivation | null } {
    const target = this.requireActivation(targetActivationId);
    if (
      target.name !== name ||
      (target.state !== "draining" && target.state !== "active")
    )
      throw new E03RuntimeError(
        "definition_activation_rollback_target",
        `definition activation ${targetActivationId} cannot be rollback target`,
      );
    const priorId = this.activeByName.get(name);
    const prior =
      priorId && priorId !== targetActivationId
        ? this.requireActivation(priorId)
        : null;
    const nextTarget =
      target.state === "active"
        ? target
        : this.transitionActivation(target, {
            state: "active",
            trafficWeight: 1,
            activatedAt: this.clock.now(),
            drainedAt: null,
          });
    if (prior && prior.state === "active")
      this.transitionActivation(prior, {
        state: "draining",
        trafficWeight: 0,
        drainedAt: this.clock.now(),
      });
    this.activeByName.set(name, nextTarget.activationId);
    return { target: nextTarget, prior: prior ? structuredClone(prior) : null };
  }
  snapshot(): {
    activations: DefinitionActivation[];
    bindings: DefinitionBinding[];
    activeByName: Array<[string, string]>;
  } {
    return {
      activations: [...this.activations.values()].map((value) =>
        structuredClone(value),
      ),
      bindings: [...this.bindings.values()].map((value) =>
        structuredClone(value),
      ),
      activeByName: [...this.activeByName.entries()].map(([name, id]) => [
        name,
        id,
      ]),
    };
  }
  restore(snapshot: {
    activations: readonly DefinitionActivation[];
    bindings: readonly DefinitionBinding[];
    activeByName: ReadonlyArray<readonly [string, string]>;
  }): void {
    const activations = new Map<string, DefinitionActivation>();
    const bindings = new Map<string, DefinitionBinding>();
    const activeByName = new Map<string, string>();
    for (const value of snapshot.activations) {
      assertActivation(value);
      if (activations.has(value.activationId))
        throw new E03RuntimeError(
          "definition_activation_restore_duplicate",
          `duplicate definition activation ${value.activationId}`,
        );
      activations.set(value.activationId, structuredClone(value));
    }
    for (const value of snapshot.bindings) {
      assertBinding(value);
      if (bindings.has(value.bindingId) || !activations.has(value.activationId))
        throw new E03RuntimeError(
          "definition_binding_restore",
          `definition binding ${value.bindingId} is invalid`,
        );
      bindings.set(value.bindingId, structuredClone(value));
    }
    for (const [name, id] of snapshot.activeByName) {
      const activation = activations.get(id);
      if (
        !activation ||
        activation.name !== name ||
        activation.state !== "active" ||
        activeByName.has(name)
      )
        throw new E03RuntimeError(
          "definition_activation_restore_index",
          `definition activation index ${name} is invalid`,
        );
      activeByName.set(name, id);
    }
    this.activations = activations;
    this.bindings = bindings;
    this.activeByName = activeByName;
  }
  private requireActivation(id: string): DefinitionActivation {
    const value = this.activations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_activation_missing",
        `definition activation ${id} does not exist`,
      );
    assertActivation(value);
    return value;
  }
  private requireBinding(id: string): DefinitionBinding {
    const value = this.bindings.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_binding_missing",
        `definition binding ${id} does not exist`,
      );
    assertBinding(value);
    return value;
  }
  private assertActivationRevision(
    value: DefinitionActivation,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_activation_stale_revision",
        `definition activation ${value.activationId} revision is stale`,
      );
  }
  private assertBindingRevision(
    value: DefinitionBinding,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_binding_stale_revision",
        `definition binding ${value.bindingId} revision is stale`,
      );
  }
  private transitionActivation(
    value: DefinitionActivation,
    patch: Partial<
      Omit<DefinitionActivation, "activationId" | "revision" | "digest">
    >,
  ): DefinitionActivation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      activationId: value.activationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertActivation(next);
    this.activations.set(next.activationId, next);
    return structuredClone(next);
  }
  private transitionBinding(
    value: DefinitionBinding,
    patch: Partial<
      Omit<DefinitionBinding, "bindingId" | "revision" | "digest">
    >,
  ): DefinitionBinding {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      bindingId: value.bindingId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertBinding(next);
    this.bindings.set(next.bindingId, next);
    return structuredClone(next);
  }
}
