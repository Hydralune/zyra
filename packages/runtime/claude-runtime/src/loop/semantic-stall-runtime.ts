import { createHash } from "node:crypto";

import {
  asBoolean,
  asObject,
  asString,
  type JsonObject,
  type ToolStep,
} from "../contracts.ts";

export const SEMANTIC_STALL_SNAPSHOT_VERSION = "zyra.semantic-stall-supervisor/v1";

const VERBATIM_TAIL_WINDOW = 250;
const VERBATIM_MIN_REPEATED_CHARS = 180;
const VERBATIM_MAX_UNIT = 60;
const SEGMENT_CHARACTER_CAP = 700;
const SEGMENT_MINIMUM_NORMALIZED_CHARACTERS = 60;
const SEGMENT_WINDOW = 16;
const SEGMENT_SIMILARITY = 0.8;
const SEGMENT_MINIMUM_COUNT = 8;
const SEGMENT_MINIMUM_CLUSTER = 4;
const LEXICAL_NOVELTY_WINDOW = 8;
const LEXICAL_STALL_NOVELTY_FLOOR = 0.2;
const LEXICAL_STALL_MINIMUM_RUN = 8;
const REASONING_HEADER_RUNAWAY_THRESHOLD = 24;
const DEFAULT_TOOL_CALL_LOOP_THRESHOLD = 5;
const DEFAULT_EXEMPT_TOOLS = ["agent_wait", "job", "shell_wait", "task_wait"];

const CONCRETE_ANCHOR =
  /`[^`]+`|\b\w{2,}\.[a-zA-Z]\w{0,4}\b|[\w-]+(?:\/[\w-]+){2,}|\b\w+_\w+\b|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b/g;

export type SemanticStallKind =
  | "reasoning_loop"
  | "reasoning_header_runaway"
  | "repeated_tool_call";

export interface SemanticStallDetection {
  kind: SemanticStallKind;
  reason: string;
  toolName: string;
  consecutiveCount: number;
  signatureDigest: string;
}

export interface SemanticStallSnapshot {
  version: typeof SEMANTIC_STALL_SNAPSHOT_VERSION;
  reasoningLoopDetections: number;
  toolLoopDetections: number;
  redirectCount: number;
  consecutiveRedirects: number;
  lastToolSignature: string;
  consecutiveIdenticalToolCalls: number;
  lastToolName: string;
  lastDetectionKind: SemanticStallKind | "";
  lastDetectionReason: string;
}

interface SemanticStallOptions {
  modelName: string;
  constraints?: JsonObject;
  restored?: JsonObject | SemanticStallSnapshot;
  continuity?: JsonObject | SemanticStallSnapshot;
}

/**
 * Detects semantic reasoning loops and repeated tool proposals before they are
 * committed to the executable transcript.
 *
 * The reasoning detector is adapted from oh-my-pi's ThinkingLoopDetector. The
 * cross-turn tool guard follows its canonical argument hashing rule, while the
 * durable snapshot stores only the hash and counters: raw tool arguments and
 * tool output never cross a fenced session through this supervisor.
 */
export class SemanticStallRuntime {
  private readonly constraints: JsonObject;
  private readonly reasoningGuardEnabled: boolean;
  private readonly toolLoopThreshold: number;
  private readonly exemptTools: ReadonlySet<string>;
  private state: SemanticStallSnapshot;

  constructor(options: SemanticStallOptions) {
    this.constraints = options.constraints ?? {};
    this.reasoningGuardEnabled = reasoningGuardEnabled(
      options.modelName,
      this.constraints,
    );
    this.toolLoopThreshold = boundedInteger(
      this.constraints.semantic_stall_tool_call_threshold,
      DEFAULT_TOOL_CALL_LOOP_THRESHOLD,
      2,
      16,
    );
    this.exemptTools = new Set([
      ...DEFAULT_EXEMPT_TOOLS,
      ...stringList(this.constraints.semantic_stall_tool_call_exempt_tools),
    ]);
    const restored = semanticSnapshot(options.restored);
    const continuity = semanticSnapshot(options.continuity);
    const selected = restored ?? continuity;
    this.state = selected
      ? structuredClone(selected)
      : {
        version: SEMANTIC_STALL_SNAPSHOT_VERSION,
        reasoningLoopDetections: 0,
        toolLoopDetections: 0,
        redirectCount: 0,
        consecutiveRedirects: 0,
        lastToolSignature: "",
        consecutiveIdenticalToolCalls: 0,
        lastToolName: "",
        lastDetectionKind: "",
        lastDetectionReason: "",
      };
  }

  inspectProviderRound(
    text: string,
    proposedTools: readonly ToolStep[],
  ): SemanticStallDetection | null {
    const toolDetection = this.inspectToolProposal(proposedTools);
    if (toolDetection) {
      this.state.toolLoopDetections += 1;
      this.recordDetection(toolDetection);
      return toolDetection;
    }

    if (proposedTools.length === 0 && this.reasoningGuardEnabled) {
      const reasoningDetection = detectReasoningStall(text);
      if (reasoningDetection) {
        const detection: SemanticStallDetection = {
          kind: reasoningDetection.kind,
          reason: reasoningDetection.reason,
          toolName: "",
          consecutiveCount: 0,
          signatureDigest: digest(text),
        };
        this.state.reasoningLoopDetections += 1;
        this.recordDetection(detection);
        return detection;
      }
    }

    this.state.consecutiveRedirects = 0;
    return null;
  }

  recordRedirect(): SemanticStallSnapshot {
    this.state.redirectCount += 1;
    this.state.consecutiveRedirects += 1;
    return this.snapshot();
  }

  recoveryMessage(detection: SemanticStallDetection): string {
    if (detection.kind === "repeated_tool_call") {
      return [
        "The semantic stall supervisor rejected the proposed tool call before execution.",
        `The same ${detection.toolName} call with equivalent arguments has been proposed ${detection.consecutiveCount} consecutive times.`,
        "The previous result is already present in the transcript.",
        "Do not repeat that call again. Choose different arguments, use a different tool, make a concrete delivery-driving change, or finish only if the objective completion evidence is satisfied.",
      ].join(" ");
    }
    return [
      "The semantic stall supervisor discarded the previous response because its reasoning was looping without a concrete new anchor.",
      `Detected pattern: ${detection.reason}.`,
      "Do not restate the discarded analysis. Use the newest durable tool evidence, choose one concrete next action, and call a tool now when work remains.",
    ].join(" ");
  }

  snapshot(): SemanticStallSnapshot {
    return structuredClone(this.state);
  }

  private inspectToolProposal(
    proposedTools: readonly ToolStep[],
  ): SemanticStallDetection | null {
    if (
      proposedTools.length !== 1
      || this.exemptTools.has(proposedTools[0].tool_name)
    ) {
      this.resetToolSequence();
      return null;
    }
    const step = proposedTools[0];
    const signature = toolSignature(step);
    if (signature === this.state.lastToolSignature) {
      this.state.consecutiveIdenticalToolCalls += 1;
    } else {
      this.state.lastToolSignature = signature;
      this.state.consecutiveIdenticalToolCalls = 1;
      this.state.lastToolName = step.tool_name;
    }
    if (this.state.consecutiveIdenticalToolCalls < this.toolLoopThreshold) {
      return null;
    }
    return {
      kind: "repeated_tool_call",
      reason: `${this.state.consecutiveIdenticalToolCalls} consecutive equivalent tool proposals`,
      toolName: step.tool_name,
      consecutiveCount: this.state.consecutiveIdenticalToolCalls,
      signatureDigest: signature,
    };
  }

  private resetToolSequence(): void {
    this.state.lastToolSignature = "";
    this.state.consecutiveIdenticalToolCalls = 0;
    this.state.lastToolName = "";
  }

  private recordDetection(detection: SemanticStallDetection): void {
    this.state.lastDetectionKind = detection.kind;
    this.state.lastDetectionReason = detection.reason.slice(0, 500);
  }
}

export class ThinkingLoopDetector {
  private tail = "";
  private pending = "";
  private window: Set<string>[] = [];
  private count = 0;
  private wordWindow: Set<string>[] = [];
  private lexicalStallRun = 0;
  private anchorWindow: Set<string>[] = [];

  push(delta: string): string | null {
    if (!delta) return null;
    this.tail += delta;
    if (this.tail.length > VERBATIM_TAIL_WINDOW) {
      this.tail = this.tail.slice(-VERBATIM_TAIL_WINDOW);
    }
    const verbatim = detectVerbatimRepetition(this.tail);
    if (verbatim) return `repeated \"${verbatim[0].trim()}\" ${verbatim[1]} times back-to-back`;

    this.pending += delta;
    while (true) {
      const boundary = /\n\s*\n/.exec(this.pending);
      let raw: string;
      if (boundary) {
        raw = this.pending.slice(0, boundary.index);
        this.pending = this.pending.slice(boundary.index + boundary[0].length);
      } else if (this.pending.length > SEGMENT_CHARACTER_CAP) {
        raw = this.pending.slice(0, SEGMENT_CHARACTER_CAP);
        this.pending = this.pending.slice(SEGMENT_CHARACTER_CAP);
      } else {
        return null;
      }
      for (let remaining = raw; remaining.length > 0;) {
        const chunk = remaining.slice(0, SEGMENT_CHARACTER_CAP);
        remaining = remaining.slice(chunk.length);
        const hit = this.consumeSegment(chunk);
        if (hit) return hit;
      }
    }
  }

  flush(): string | null {
    let remaining = this.pending;
    this.pending = "";
    while (remaining.length > 0) {
      const chunk = remaining.slice(0, SEGMENT_CHARACTER_CAP);
      remaining = remaining.slice(chunk.length);
      const hit = this.consumeSegment(chunk);
      if (hit) return hit;
    }
    return null;
  }

  private consumeSegment(raw: string): string | null {
    const segment = raw
      .replace(/^[ \t]*#{1,6}[ \t].*$/gm, "")
      .replace(/^[ \t]*\*{2,3}.+?\*{2,3}[ \t]*$/gm, "");
    const normalized = normalizeSegment(segment);
    if (normalized.length < SEGMENT_MINIMUM_NORMALIZED_CHARACTERS) return null;

    const fingerprint = trigramShingles(normalized);
    let cluster = 1;
    for (const previous of this.window) {
      if (jaccard(fingerprint, previous) >= SEGMENT_SIMILARITY) cluster += 1;
    }

    const words = new Set(normalized.split(" ").filter(Boolean));
    const priorVocabulary = new Set<string>();
    for (const previous of this.wordWindow) {
      for (const word of previous) priorVocabulary.add(word);
    }
    let unseen = 0;
    for (const word of words) {
      if (!priorVocabulary.has(word)) unseen += 1;
    }
    const novelty = priorVocabulary.size === 0 ? 1 : unseen / Math.max(1, words.size);

    const anchors = new Set<string>();
    for (const match of segment.matchAll(CONCRETE_ANCHOR)) {
      anchors.add(match[0].replaceAll("`", "").toLowerCase());
    }
    let newAnchor = false;
    for (const anchor of anchors) {
      if (this.anchorWindow.every((seen) => !seen.has(anchor))) {
        newAnchor = true;
        break;
      }
    }
    this.lexicalStallRun = novelty <= LEXICAL_STALL_NOVELTY_FLOOR && !newAnchor
      ? this.lexicalStallRun + 1
      : 0;

    this.window.push(fingerprint);
    if (this.window.length > SEGMENT_WINDOW) this.window.shift();
    this.wordWindow.push(words);
    if (this.wordWindow.length > LEXICAL_NOVELTY_WINDOW) this.wordWindow.shift();
    this.anchorWindow.push(anchors);
    if (this.anchorWindow.length > LEXICAL_NOVELTY_WINDOW) this.anchorWindow.shift();
    this.count += 1;

    if (this.count >= SEGMENT_MINIMUM_COUNT) {
      if (cluster >= SEGMENT_MINIMUM_CLUSTER) {
        return `${cluster} near-identical segments within the last ${SEGMENT_WINDOW}`;
      }
      if (this.lexicalStallRun >= LEXICAL_STALL_MINIMUM_RUN) {
        return `${this.lexicalStallRun} low-information segments recycling recent wording`;
      }
    }
    return null;
  }
}

export function detectReasoningStall(
  text: string,
): { kind: "reasoning_loop" | "reasoning_header_runaway"; reason: string } | null {
  if (!text.trim()) return null;
  const headers = text.split(/\r?\n/u).filter((line) => (
    /^#{1,6}[ \t]+\S/.test(line.trim())
    || /^\*{2,3}.+\*{2,3}$/.test(line.trim())
  )).length;
  if (headers >= REASONING_HEADER_RUNAWAY_THRESHOLD) {
    return {
      kind: "reasoning_header_runaway",
      reason: `${headers} planning headers without a tool call`,
    };
  }
  const detector = new ThinkingLoopDetector();
  const detected = detector.push(text) ?? detector.flush();
  return detected ? { kind: "reasoning_loop", reason: detected } : null;
}

function detectVerbatimRepetition(text: string): [string, number] | null {
  if (text.length < VERBATIM_MIN_REPEATED_CHARS) return null;
  const search = text.slice(-Math.min(text.length, VERBATIM_TAIL_WINDOW));
  for (let length = 2; length <= VERBATIM_MAX_UNIT; length += 1) {
    if (search.length < length * 4) continue;
    const unit = search.slice(-length);
    if (!/[\p{L}\p{Extended_Pictographic}]/u.test(unit)) continue;
    let count = 0;
    let position = search.length;
    while (position >= length && search.slice(position - length, position) === unit) {
      count += 1;
      position -= length;
    }
    if (count >= 4 && length * count >= VERBATIM_MIN_REPEATED_CHARS) {
      return [unit, count];
    }
  }
  return null;
}

function normalizeSegment(segment: string): string {
  const normalized = segment
    .toLowerCase()
    .replace(/`([^`]*)`/g, " $1 ");
  return [...normalized.matchAll(/[\p{Script=Han}]|[\p{L}\p{N}]+/gu)]
    .map((match) => match[0])
    .filter((token) => /\p{L}/u.test(token))
    .join(" ");
}

function trigramShingles(normalized: string): Set<string> {
  const words = normalized.split(" ").filter(Boolean);
  if (words.length < 3) return new Set(words.length > 0 ? [words.join(" ")] : []);
  const result = new Set<string>();
  for (let index = 0; index + 3 <= words.length; index += 1) {
    result.add(`${words[index]} ${words[index + 1]} ${words[index + 2]}`);
  }
  return result;
}

function jaccard(left: Set<string>, right: Set<string>): number {
  if (left.size === 0 || right.size === 0) return 0;
  const [small, large] = left.size < right.size ? [left, right] : [right, left];
  let intersection = 0;
  for (const value of small) {
    if (large.has(value)) intersection += 1;
  }
  return intersection / (left.size + right.size - intersection);
}

function toolSignature(step: ToolStep): string {
  return digest(`${step.tool_name}:${JSON.stringify(canonicalize(step.arguments))}`);
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (!value || typeof value !== "object") return value;
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(value as Record<string, unknown>).sort()) {
    if (key === "intent" || key === "__intent") continue;
    result[key] = canonicalize((value as Record<string, unknown>)[key]);
  }
  return result;
}

function reasoningGuardEnabled(modelName: string, constraints: JsonObject): boolean {
  if (Object.prototype.hasOwnProperty.call(
    constraints,
    "semantic_stall_reasoning_guard_enabled",
  )) {
    return asBoolean(constraints.semantic_stall_reasoning_guard_enabled);
  }
  return /deepseek|gemini/iu.test(modelName);
}

function semanticSnapshot(value: unknown): SemanticStallSnapshot | null {
  const candidate = asObject(value);
  if (candidate.version !== SEMANTIC_STALL_SNAPSHOT_VERSION) return null;
  const lastToolSignature = asString(candidate.lastToolSignature).toLowerCase();
  return {
    version: SEMANTIC_STALL_SNAPSHOT_VERSION,
    reasoningLoopDetections: nonnegativeInteger(candidate.reasoningLoopDetections),
    toolLoopDetections: nonnegativeInteger(candidate.toolLoopDetections),
    redirectCount: nonnegativeInteger(candidate.redirectCount),
    consecutiveRedirects: nonnegativeInteger(candidate.consecutiveRedirects),
    lastToolSignature: /^[0-9a-f]{64}$/.test(lastToolSignature)
      ? lastToolSignature
      : "",
    consecutiveIdenticalToolCalls: nonnegativeInteger(candidate.consecutiveIdenticalToolCalls),
    lastToolName: asString(candidate.lastToolName).slice(0, 160),
    lastDetectionKind: semanticKind(candidate.lastDetectionKind),
    lastDetectionReason: asString(candidate.lastDetectionReason).slice(0, 500),
  };
}

function semanticKind(value: unknown): SemanticStallSnapshot["lastDetectionKind"] {
  return value === "reasoning_loop"
    || value === "reasoning_header_runaway"
    || value === "repeated_tool_call"
    ? value
    : "";
}

function boundedInteger(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (value === null || value === undefined || value === "") return fallback;
  const selected = Number(value);
  return Number.isFinite(selected)
    ? Math.max(minimum, Math.min(maximum, Math.floor(selected)))
    : fallback;
}

function nonnegativeInteger(value: unknown): number {
  const selected = Number(value);
  return Number.isFinite(selected)
    ? Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, Math.floor(selected)))
    : 0;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(String).map((item) => item.trim()).filter(Boolean)
    : asString(value).split(",").map((item) => item.trim()).filter(Boolean);
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
