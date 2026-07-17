import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { SkillContextPolicy, SkillDescriptor, SkillResourceContent } from "./contracts-v2.ts";

export interface SkillParentContext {
  system: string[];
  conversation: JsonValue[];
  memory: JsonValue[];
  workspaceInstructions: string[];
  mcpInstructions: string[];
  variables: JsonObject;
  metadata: JsonObject;
}

export interface SkillContextSection {
  sectionId: string;
  kind: "skill_body" | "system" | "conversation" | "memory" | "workspace_instruction" | "mcp_instruction" | "resource" | "variables";
  sourceId: string;
  priority: number;
  required: boolean;
  content: JsonValue;
  contentDigest: string;
  tokenEstimate: number;
  included: boolean;
  exclusionReason: string | null;
  metadata: JsonObject;
}

export interface SkillContextComposition {
  compositionId: string;
  skillId: string;
  descriptorDigest: string;
  policyDigest: string;
  parentDigest: string;
  sections: SkillContextSection[];
  rendered: JsonObject;
  inputTokens: number;
  resourceTokens: number;
  maximumInputTokens: number;
  maximumResourceTokens: number;
  truncated: boolean;
  compactionRequested: boolean;
  createdAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface SkillContextSnapshot {
  version: "zyra.skill-context-runtime/v1";
  revision: number;
  compositions: SkillContextComposition[];
  digest: string;
  capturedAt: string;
}

export class SkillContextRuntime {
  private readonly compositions = new Map<string, SkillContextComposition>();
  private readonly now: () => Date;
  private readonly maximumCompositions: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumCompositions?: number; snapshot?: SkillContextSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumCompositions = options.maximumCompositions ?? 10_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  compose(input: {
    descriptor: SkillDescriptor;
    parent: SkillParentContext;
    resources: SkillResourceContent[];
    arguments: JsonObject;
    metadata?: JsonObject;
  }): SkillContextComposition {
    const descriptor = cloneJson(input.descriptor);
    const parent = normalizeParent(input.parent);
    const resources = input.resources.map(cloneJson);
    validateResourceBindings(descriptor, resources);
    const sections = buildSections(descriptor, parent, resources, input.arguments);
    this.applyPolicy(sections, descriptor.context);
    const included = sections.filter((section) => section.included);
    const inputTokens = included.reduce((total, section) => total + section.tokenEstimate, 0);
    const resourceTokens = included.filter((section) => section.kind === "resource").reduce((total, section) => total + section.tokenEstimate, 0);
    const excludedForBudget = sections.some((section) => !section.included && section.exclusionReason?.includes("budget"));
    const compactionRequested = descriptor.context.compactionStrategy === "compact_parent" && excludedForBudget;
    const compositionId = deterministicId("skill-context-composition", {
      skill_id: descriptor.skillId,
      descriptor_digest: descriptor.descriptorDigest,
      parent_digest: digest(parent),
      arguments_digest: digest(input.arguments),
      resource_digests: resources.map((resource) => resource.digest),
    }, 40);
    const existing = this.compositions.get(compositionId);
    const rendered: JsonObject = {
      skill: {
        id: descriptor.skillId,
        name: descriptor.name,
        version: descriptor.version,
        body: sectionContent(included, "skill_body") ?? null,
        arguments: cloneJson(input.arguments),
      },
      system: sectionContents(included, "system"),
      conversation: sectionContents(included, "conversation"),
      memory: sectionContents(included, "memory"),
      workspace_instructions: sectionContents(included, "workspace_instruction"),
      mcp_instructions: sectionContents(included, "mcp_instruction"),
      resources: included.filter((section) => section.kind === "resource").map((section) => ({
        resource_id: section.sourceId,
        content: section.content,
        digest: section.contentDigest,
        tokens: section.tokenEstimate,
      })),
      variables: sectionContent(included, "variables") ?? {},
    };
    const createdAt = existing?.createdAt ?? this.timestamp();
    const base = {
      skillId: descriptor.skillId,
      descriptorDigest: descriptor.descriptorDigest,
      policyDigest: digest(descriptor.context),
      parentDigest: digest(parent),
      sections,
      rendered,
      inputTokens,
      resourceTokens,
      maximumInputTokens: descriptor.context.maximumInputTokens,
      maximumResourceTokens: descriptor.context.maximumResourceTokens,
      truncated: excludedForBudget,
      compactionRequested,
      createdAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const composition: SkillContextComposition = {
      compositionId,
      ...base,
      digest: digest({ compositionId, ...base }),
    };
    if (existing && existing.digest !== composition.digest) throw new Error(`skill context composition ${compositionId} is non-deterministic`);
    this.compositions.set(compositionId, composition);
    this.revision += existing ? 0 : 1;
    this.trim();
    return cloneJson(composition);
  }

  get(compositionId: string): SkillContextComposition | null {
    const value = this.compositions.get(compositionId);
    return value ? cloneJson(value) : null;
  }

  list(skillId?: string): SkillContextComposition[] {
    return [...this.compositions.values()].filter((value) => !skillId || value.skillId === skillId).sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson);
  }

  snapshot(): SkillContextSnapshot {
    const withoutDigest = {
      version: "zyra.skill-context-runtime/v1" as const,
      revision: this.revision,
      compositions: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: SkillContextSnapshot): void {
    if (snapshot.version !== "zyra.skill-context-runtime/v1") throw new Error("unsupported skill context snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("skill context snapshot digest mismatch");
    this.compositions.clear();
    this.revision = snapshot.revision;
    for (const composition of snapshot.compositions) {
      const { digest: compositionDigest, ...payload } = composition;
      if (digest(payload) !== compositionDigest) throw new Error(`skill context ${composition.compositionId} digest mismatch`);
      this.compositions.set(composition.compositionId, cloneJson(composition));
    }
  }

  private applyPolicy(sections: SkillContextSection[], policy: SkillContextPolicy): void {
    for (const section of sections) {
      if (section.kind === "conversation" && !policy.inheritConversation) exclude(section, "conversation_not_inherited");
      if (section.kind === "system" && !policy.inheritSystem) exclude(section, "system_not_inherited");
      if (section.kind === "memory" && !policy.inheritMemory) exclude(section, "memory_not_inherited");
      if (section.kind === "workspace_instruction" && !policy.includeWorkspaceInstructions) exclude(section, "workspace_instructions_disabled");
      if (section.kind === "mcp_instruction" && !policy.includeMcpInstructions) exclude(section, "mcp_instructions_disabled");
    }
    let resourceTokens = sections.filter((section) => section.kind === "resource" && section.included).reduce((total, section) => total + section.tokenEstimate, 0);
    if (resourceTokens > policy.maximumResourceTokens) {
      if (policy.compactionStrategy === "reject") throw new Error(`skill resources require ${resourceTokens} tokens, budget ${policy.maximumResourceTokens}`);
      for (const section of sections.filter((value) => value.kind === "resource" && value.included).sort((left, right) => left.required === right.required ? left.priority - right.priority : left.required ? 1 : -1)) {
        if (resourceTokens <= policy.maximumResourceTokens) break;
        if (section.required) continue;
        exclude(section, "resource_budget_exceeded");
        resourceTokens -= section.tokenEstimate;
      }
      if (resourceTokens > policy.maximumResourceTokens) throw new Error("required skill resources exceed resource token budget");
    }
    let inputTokens = sections.filter((section) => section.included).reduce((total, section) => total + section.tokenEstimate, 0);
    if (inputTokens > policy.maximumInputTokens) {
      if (policy.compactionStrategy === "reject") throw new Error(`skill context requires ${inputTokens} tokens, budget ${policy.maximumInputTokens}`);
      const removable = sections.filter((section) => section.included && !section.required && section.kind !== "skill_body").sort((left, right) => left.priority - right.priority || right.tokenEstimate - left.tokenEstimate);
      for (const section of removable) {
        if (inputTokens <= policy.maximumInputTokens) break;
        exclude(section, "input_budget_exceeded");
        inputTokens -= section.tokenEstimate;
      }
      if (inputTokens > policy.maximumInputTokens) throw new Error("required skill context exceeds input token budget");
    }
  }

  private trim(): void {
    while (this.compositions.size > this.maximumCompositions) {
      const first = this.compositions.keys().next().value as string | undefined;
      if (!first) break;
      this.compositions.delete(first);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function buildSections(descriptor: SkillDescriptor, parent: SkillParentContext, resources: SkillResourceContent[], argumentsValue: JsonObject): SkillContextSection[] {
  const sections: SkillContextSection[] = [];
  const add = (kind: SkillContextSection["kind"], sourceId: string, content: JsonValue, priority: number, required: boolean, metadata: JsonObject = {}): void => {
    const contentValue = canonicalize(content);
    const base = { kind, sourceId, priority, required, content: contentValue, contentDigest: digest(contentValue), tokenEstimate: estimateTokens(contentValue), included: true, exclusionReason: null, metadata };
    sections.push({ sectionId: deterministicId("skill-context-section", base, 32), ...base });
  };
  add("skill_body", descriptor.skillId, renderBody(descriptor.body, argumentsValue), 10_000, true);
  parent.system.forEach((value, index) => add("system", `system:${index}`, value, 8_000 - index, false));
  parent.workspaceInstructions.forEach((value, index) => add("workspace_instruction", `workspace:${index}`, value, 7_500 - index, false));
  parent.mcpInstructions.forEach((value, index) => add("mcp_instruction", `mcp:${index}`, value, 7_000 - index, false));
  parent.memory.forEach((value, index) => add("memory", `memory:${index}`, value, 5_000 - index, false));
  parent.conversation.forEach((value, index) => add("conversation", `conversation:${index}`, value, 4_000 + index, false));
  for (const resource of resources) add("resource", resource.resourceId, resource.text ?? resource.bytesBase64 ?? "", resource.metadata.priority === "high" ? 9_000 : 6_000, descriptor.resources.find((value) => value.resourceId === resource.resourceId)?.required ?? false, { digest: resource.digest, kind: resource.kind, media_type: resource.mediaType });
  add("variables", "variables", { ...parent.variables, arguments: argumentsValue }, 9_500, true);
  return sections;
}

function normalizeParent(value: SkillParentContext): SkillParentContext {
  return {
    system: value.system.filter((item) => typeof item === "string"),
    conversation: value.conversation.map(canonicalize),
    memory: value.memory.map(canonicalize),
    workspaceInstructions: value.workspaceInstructions.filter((item) => typeof item === "string"),
    mcpInstructions: value.mcpInstructions.filter((item) => typeof item === "string"),
    variables: cloneJson(value.variables),
    metadata: cloneJson(value.metadata),
  };
}

function validateResourceBindings(descriptor: SkillDescriptor, resources: SkillResourceContent[]): void {
  const allowed = new Map(descriptor.resources.map((value) => [value.resourceId, value]));
  const seen = new Set<string>();
  for (const resource of resources) {
    const declaration = allowed.get(resource.resourceId);
    if (!declaration) throw new Error(`skill resource ${resource.resourceId} was not declared`);
    if (seen.has(resource.resourceId)) throw new Error(`duplicate skill resource ${resource.resourceId}`);
    seen.add(resource.resourceId);
    if (declaration.digest && declaration.digest !== resource.digest) throw new Error(`skill resource ${resource.resourceId} digest mismatch`);
  }
  for (const declaration of descriptor.resources) if (declaration.required && !seen.has(declaration.resourceId)) throw new Error(`required skill resource ${declaration.resourceId} is missing`);
}

function renderBody(body: string, argumentsValue: JsonObject): string {
  return body.replace(/\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}/g, (_match, key: string) => {
    const value = argumentsValue[key];
    return value === undefined ? "" : typeof value === "string" ? value : JSON.stringify(value);
  });
}

function estimateTokens(value: JsonValue): number {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return Math.max(1, Math.ceil(Buffer.byteLength(text, "utf8") / 4));
}

function exclude(section: SkillContextSection, reason: string): void {
  section.included = false;
  section.exclusionReason = reason;
}

function sectionContent(sections: SkillContextSection[], kind: SkillContextSection["kind"]): JsonValue | undefined {
  return sections.find((section) => section.kind === kind)?.content;
}

function sectionContents(sections: SkillContextSection[], kind: SkillContextSection["kind"]): JsonValue[] {
  return sections.filter((section) => section.kind === kind).map((section) => cloneJson(section.content));
}
