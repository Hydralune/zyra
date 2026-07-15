import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  canonicalJson,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
} from "../core/runtime-primitives.js";

export type StopSignal =
  | "model_stop"
  | "terminal_answer"
  | "explicit_cancel"
  | "maximum_turns"
  | "maximum_tool_calls"
  | "token_budget"
  | "cost_budget"
  | "wall_deadline"
  | "consecutive_failures"
  | "context_overflow"
  | "permission_denied"
  | "provider_exhausted"
  | "invariant_failure"
  | "external_interrupt";

export type StopAction = "continue" | "stop" | "retry" | "compact" | "suspend";
export type StopConditionOperator =
  | "equals"
  | "not_equals"
  | "greater_than"
  | "greater_or_equal"
  | "less_than"
  | "less_or_equal"
  | "includes"
  | "present"
  | "absent";

export interface StopCondition {
  path: string;
  operator: StopConditionOperator;
  value?: JsonValue;
}

export interface StopRule {
  ruleId: string;
  owner: string;
  priority: number;
  enabled: boolean;
  signals: StopSignal[];
  match: "all" | "any";
  conditions: StopCondition[];
  action: StopAction;
  reason: string;
  terminal: boolean;
  retryAfterMilliseconds: number | null;
  requireTerminalAnswer: boolean;
  revision: number;
  metadata: JsonRecord;
}

export interface StopEvaluationInput {
  evaluationId?: string;
  sessionId: string;
  runId: string;
  queryId: string;
  turnId: string | null;
  signal: StopSignal;
  correlationId: string;
  evidence: JsonRecord;
  terminalAnswer: string | null;
  currentRevision: number;
}

export interface StopRuleMatch {
  ruleId: string;
  matched: boolean;
  conditionResults: boolean[];
  action: StopAction;
  priority: number;
}

export interface StopDecision {
  decisionId: string;
  evaluationId: string;
  sessionId: string;
  runId: string;
  queryId: string;
  signal: StopSignal;
  action: StopAction;
  reason: string;
  terminal: boolean;
  retryAfterMilliseconds: number | null;
  matchedRuleId: string | null;
  matches: StopRuleMatch[];
  evidenceDigest: string;
  terminalAnswerDigest: string | null;
  decidedAt: number;
  expectedRevision: number;
  decisionDigest: string;
}

export interface StopRuntimeSnapshot {
  version: "zyra.query-stop/v1";
  rules: StopRule[];
  decisions: StopDecision[];
  checksum: string;
}

export interface StopRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumDecisions?: number;
  defaultAction?: StopAction;
}

export class QueryStopRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumDecisions: number;
  private readonly defaultAction: StopAction;
  private readonly rules = new Map<string, StopRule>();
  private readonly decisions = new Map<string, StopDecision>();

  constructor(options: StopRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumDecisions = options.maximumDecisions ?? 10_000;
    this.defaultAction = options.defaultAction ?? "continue";
    assertNonNegativeInteger(this.maximumDecisions, "maximumDecisions");
  }

  register(rule: StopRule): StopRule {
    const normalized = normalizeRule(rule);
    if (this.rules.has(normalized.ruleId)) {
      throw new RuntimeInvariantError("stop_rule_already_exists", {
        ruleId: normalized.ruleId,
      });
    }
    this.rules.set(normalized.ruleId, normalized);
    return deepClone(normalized);
  }

  configure(
    ruleId: string,
    update: Partial<
      Pick<
        StopRule,
        | "priority"
        | "enabled"
        | "signals"
        | "match"
        | "conditions"
        | "action"
        | "reason"
        | "terminal"
        | "retryAfterMilliseconds"
        | "requireTerminalAnswer"
        | "metadata"
      >
    >,
  ): StopRule {
    const previous = this.requireRule(ruleId);
    const next = normalizeRule({
      ...previous,
      ...deepClone(update),
      ruleId,
      revision: previous.revision + 1,
    });
    this.rules.set(ruleId, next);
    return deepClone(next);
  }

  remove(ruleId: string): StopRule {
    const rule = this.requireRule(ruleId);
    this.rules.delete(ruleId);
    return deepClone(rule);
  }

  evaluate(input: StopEvaluationInput): StopDecision {
    validateEvaluation(input);
    const evaluationId = input.evaluationId ?? this.ids.next("stop-evaluation");
    const previous = [...this.decisions.values()].find(
      (decision) => decision.evaluationId === evaluationId,
    );
    const evidenceDigest = digestJson(input.evidence);
    if (previous !== undefined) {
      if (
        previous.evidenceDigest !== evidenceDigest ||
        previous.signal !== input.signal ||
        previous.expectedRevision !== input.currentRevision
      ) {
        throw new RuntimeInvariantError("stop_evaluation_conflict", {
          evaluationId,
        });
      }
      return deepClone(previous);
    }
    const applicable = [...this.rules.values()]
      .filter(
        (rule) => rule.enabled && rule.signals.includes(input.signal),
      )
      .sort(ruleComparator);
    const matches = applicable.map((rule) => evaluateRule(rule, input.evidence));
    const matchedRule = applicable.find(
      (rule) => matches.find((match) => match.ruleId === rule.ruleId)?.matched,
    );
    let action = matchedRule?.action ?? this.defaultAction;
    let reason = matchedRule?.reason ?? `no_stop_rule_matched:${input.signal}`;
    let terminal = matchedRule?.terminal ?? false;
    let retryAfterMilliseconds = matchedRule?.retryAfterMilliseconds ?? null;
    if (matchedRule?.requireTerminalAnswer === true && input.terminalAnswer === null) {
      action = "continue";
      reason = `terminal_answer_missing:${matchedRule.ruleId}`;
      terminal = false;
      retryAfterMilliseconds = null;
    }
    if (terminal && action !== "stop") {
      throw new RuntimeInvariantError("terminal_stop_rule_wrong_action", {
        ruleId: matchedRule?.ruleId ?? null,
        action,
      });
    }
    const body = {
      decisionId: this.ids.next("stop-decision"),
      evaluationId,
      sessionId: input.sessionId,
      runId: input.runId,
      queryId: input.queryId,
      signal: input.signal,
      action,
      reason,
      terminal,
      retryAfterMilliseconds,
      matchedRuleId: matchedRule?.ruleId ?? null,
      matches,
      evidenceDigest,
      terminalAnswerDigest:
        input.terminalAnswer === null ? null : digestJson(input.terminalAnswer),
      decidedAt: this.clock.now(),
      expectedRevision: input.currentRevision,
    };
    const decision: StopDecision = {
      ...body,
      decisionDigest: digestJson(body),
    };
    this.decisions.set(decision.decisionId, decision);
    this.trimDecisions();
    return deepClone(decision);
  }

  assertApplicable(
    decisionId: string,
    sessionId: string,
    queryId: string,
    currentRevision: number,
  ): StopDecision {
    const decision = this.getDecision(decisionId);
    if (
      decision.sessionId !== sessionId ||
      decision.queryId !== queryId ||
      decision.expectedRevision !== currentRevision
    ) {
      throw new RuntimeInvariantError("stale_stop_decision", {
        decisionId,
        sessionId,
        queryId,
        expectedRevision: decision.expectedRevision,
        currentRevision,
      });
    }
    return decision;
  }

  getDecision(decisionId: string): StopDecision {
    const decision = this.decisions.get(decisionId);
    if (decision === undefined) {
      throw new RuntimeInvariantError("unknown_stop_decision", { decisionId });
    }
    const { decisionDigest, ...body } = decision;
    if (digestJson(body) !== decisionDigest) {
      throw new RuntimeInvariantError("stop_decision_digest_mismatch", {
        decisionId,
      });
    }
    return deepClone(decision);
  }

  listRules(signal?: StopSignal): StopRule[] {
    return [...this.rules.values()]
      .filter((rule) => signal === undefined || rule.signals.includes(signal))
      .sort(ruleComparator)
      .map((rule) => deepClone(rule));
  }

  listDecisions(queryId?: string): StopDecision[] {
    return [...this.decisions.values()]
      .filter((decision) => queryId === undefined || decision.queryId === queryId)
      .sort(
        (left, right) =>
          compareNumbers(left.decidedAt, right.decidedAt) ||
          compareStrings(left.decisionId, right.decisionId),
      )
      .map((decision) => deepClone(decision));
  }

  snapshot(): StopRuntimeSnapshot {
    const body = {
      version: "zyra.query-stop/v1" as const,
      rules: this.listRules(),
      decisions: this.listDecisions(),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: StopRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.query-stop/v1") {
      throw new RuntimeInvariantError("unsupported_stop_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("stop_snapshot_checksum_mismatch");
    }
    this.rules.clear();
    this.decisions.clear();
    for (const source of snapshot.rules) {
      const rule = normalizeRule(source);
      if (this.rules.has(rule.ruleId)) {
        throw new RuntimeInvariantError("duplicate_stop_rule_snapshot", {
          ruleId: rule.ruleId,
        });
      }
      this.rules.set(rule.ruleId, rule);
    }
    for (const decision of snapshot.decisions) {
      const { decisionDigest, ...decisionBody } = decision;
      if (digestJson(decisionBody) !== decisionDigest) {
        throw new RuntimeInvariantError("stop_decision_digest_mismatch", {
          decisionId: decision.decisionId,
        });
      }
      this.decisions.set(decision.decisionId, deepClone(decision));
    }
    this.trimDecisions();
  }

  private requireRule(ruleId: string): StopRule {
    const rule = this.rules.get(ruleId);
    if (rule === undefined) {
      throw new RuntimeInvariantError("unknown_stop_rule", { ruleId });
    }
    return rule;
  }

  private trimDecisions(): void {
    if (this.decisions.size <= this.maximumDecisions) {
      return;
    }
    const ordered = this.listDecisions();
    for (const decision of ordered.slice(
      0,
      this.decisions.size - this.maximumDecisions,
    )) {
      this.decisions.delete(decision.decisionId);
    }
  }
}

function normalizeRule(rule: StopRule): StopRule {
  assertNonEmpty(rule.ruleId, "ruleId");
  assertNonEmpty(rule.owner, "owner");
  assertNonEmpty(rule.reason, "reason");
  if (!Number.isSafeInteger(rule.priority)) {
    throw new RuntimeInvariantError("invalid_stop_rule_priority", {
      ruleId: rule.ruleId,
      priority: rule.priority,
    });
  }
  assertNonNegativeInteger(rule.revision, "revision");
  if (rule.signals.length === 0) {
    throw new RuntimeInvariantError("stop_rule_requires_signal", {
      ruleId: rule.ruleId,
    });
  }
  if (rule.terminal && rule.action !== "stop") {
    throw new RuntimeInvariantError("terminal_stop_rule_wrong_action", {
      ruleId: rule.ruleId,
      action: rule.action,
    });
  }
  if (rule.action === "retry") {
    assertNonNegativeInteger(
      rule.retryAfterMilliseconds ?? 0,
      "retryAfterMilliseconds",
    );
  }
  for (const condition of rule.conditions) {
    validateCondition(condition);
  }
  return {
    ...deepClone(rule),
    signals: uniqueSorted(rule.signals) as StopSignal[],
  };
}

function validateCondition(condition: StopCondition): void {
  if (!condition.path.startsWith("/") && condition.path !== "") {
    throw new RuntimeInvariantError("invalid_stop_condition_path", {
      path: condition.path,
    });
  }
  if (
    !["present", "absent"].includes(condition.operator) &&
    condition.value === undefined
  ) {
    throw new RuntimeInvariantError("stop_condition_value_required", {
      path: condition.path,
      operator: condition.operator,
    });
  }
}

function validateEvaluation(input: StopEvaluationInput): void {
  assertNonEmpty(input.sessionId, "sessionId");
  assertNonEmpty(input.runId, "runId");
  assertNonEmpty(input.queryId, "queryId");
  assertNonEmpty(input.correlationId, "correlationId");
  assertNonNegativeInteger(input.currentRevision, "currentRevision");
}

function evaluateRule(rule: StopRule, evidence: JsonRecord): StopRuleMatch {
  const conditionResults = rule.conditions.map((condition) =>
    evaluateCondition(condition, evidence),
  );
  const matched =
    conditionResults.length === 0 ||
    (rule.match === "all"
      ? conditionResults.every(Boolean)
      : conditionResults.some(Boolean));
  return {
    ruleId: rule.ruleId,
    matched,
    conditionResults,
    action: rule.action,
    priority: rule.priority,
  };
}

function evaluateCondition(
  condition: StopCondition,
  evidence: JsonRecord,
): boolean {
  const actual = readPointer(evidence, condition.path);
  if (condition.operator === "present") {
    return actual.found;
  }
  if (condition.operator === "absent") {
    return !actual.found;
  }
  if (!actual.found || condition.value === undefined) {
    return false;
  }
  if (condition.operator === "equals") {
    return canonicalJson(actual.value) === canonicalJson(condition.value);
  }
  if (condition.operator === "not_equals") {
    return canonicalJson(actual.value) !== canonicalJson(condition.value);
  }
  if (condition.operator === "includes") {
    if (typeof actual.value === "string" && typeof condition.value === "string") {
      return actual.value.includes(condition.value);
    }
    if (Array.isArray(actual.value)) {
      return actual.value.some(
        (value) => canonicalJson(value) === canonicalJson(condition.value),
      );
    }
    return false;
  }
  if (typeof actual.value !== "number" || typeof condition.value !== "number") {
    return false;
  }
  if (condition.operator === "greater_than") {
    return actual.value > condition.value;
  }
  if (condition.operator === "greater_or_equal") {
    return actual.value >= condition.value;
  }
  if (condition.operator === "less_than") {
    return actual.value < condition.value;
  }
  return actual.value <= condition.value;
}

function readPointer(
  source: JsonRecord,
  path: string,
): { found: boolean; value: JsonValue | null } {
  if (path === "") {
    return { found: true, value: source };
  }
  const segments = path
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
  let current: JsonValue = source;
  for (const segment of segments) {
    if (Array.isArray(current)) {
      const index = Number.parseInt(segment, 10);
      if (!Number.isSafeInteger(index) || index < 0 || index >= current.length) {
        return { found: false, value: null };
      }
      current = current[index] ?? null;
    } else if (current !== null && typeof current === "object") {
      if (!(segment in current)) {
        return { found: false, value: null };
      }
      current = current[segment] ?? null;
    } else {
      return { found: false, value: null };
    }
  }
  return { found: true, value: current };
}

function ruleComparator(left: StopRule, right: StopRule): number {
  return (
    compareNumbers(left.priority, right.priority) ||
    compareStrings(left.ruleId, right.ruleId)
  );
}
