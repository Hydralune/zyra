#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import * as ts from "typescript";

type Obj = Record<string, any>;
type Unit = { hash: string; structuralHash: string; lines: number; path: string; symbol: string };

const VERIFIED = "c34535a783e88f9481387ced89cba4fbc333dc74";
const zyra = resolve(import.meta.dir, "..");
const workspace = resolve(zyra, "..");
const manifestRoot = join(workspace, "docs", "remediations", "M1-R01-claude-source-custody", "manifests");
const evidenceRoot = join(zyra, "docs", "reviews", "evidence", "M1-R01-v3", "execution-01");
const productionRoots = [
  "apps/code-worker/src",
  "packages/runtime/claude-runtime/src",
];
const testRoot = "packages/runtime/claude-runtime/test/e01";
const semanticallyDormantProductionPaths = new Map<string, string>();
const e01ExcludedProductionPrefixes = [
  "packages/runtime/claude-runtime/src/agents/",
  "packages/runtime/claude-runtime/src/control/",
  "packages/runtime/claude-runtime/src/permission/",
  "packages/runtime/claude-runtime/src/skills/",
] as const;
const e01ExcludedProductionFiles = new Set([
  "packages/runtime/claude-runtime/src/capabilities.ts",
]);
const providerActivationContracts = [
  { path: "packages/runtime/claude-runtime/src/provider/prompt-runtime.ts", owner: "providerPrompt", method: "build", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "providerPrompt", assertion: "providerPrompt.lastPrompt" },
  { path: "packages/runtime/claude-runtime/src/provider/request-runtime.ts", owner: "providerRequests", method: "create", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "providerRequests", assertion: "providerRequests.requests" },
  { path: "packages/runtime/claude-runtime/src/provider/response-runtime.ts", owner: "providerResponses", method: "begin", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "providerResponses", assertion: "providerResponses.responses" },
  { path: "packages/runtime/claude-runtime/src/provider/routing-runtime.ts", owner: "providerRouting", method: "decide", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "providerRouting", assertion: "providerRouting.states" },
  { path: "packages/runtime/claude-runtime/src/provider/rate-limit-runtime.ts", owner: "providerRateLimits", method: "reserve", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "providerRateLimits", assertion: "providerRateLimits.reservations" },
  { path: "packages/runtime/claude-runtime/src/provider/model-runtime.ts", owner: "provider", method: "prepare", caller: "E01RuntimeCoordinator.prepareProviderLifecycle", snapshot: "provider", assertion: "provider.requests" },
  { path: "packages/runtime/claude-runtime/src/provider/model-runtime.ts", owner: "provider", method: "execute", caller: "E01RuntimeCoordinator.executePreparedProvider", snapshot: "provider", assertion: "provider.requests" },
  { path: "packages/runtime/claude-runtime/src/provider/transport-runtime.ts", owner: "providerTransport", method: "execute", caller: "E01RuntimeCoordinator.executePreparedProvider", snapshot: "providerTransport", assertion: "providerTransport.requests" },
  { path: "packages/runtime/claude-runtime/src/provider/credential-runtime.ts", owner: "providerCredentials", method: "register", caller: "E01RuntimeCoordinator.configureProviderRuntime", snapshot: "providerCredentials", assertion: "providerCredentials.records" },
  { path: "packages/runtime/claude-runtime/src/provider/credential-runtime.ts", owner: "providerCredentials", method: "select", caller: "E01RuntimeCoordinator.configureProviderRuntime", snapshot: "providerCredentials", assertion: "providerCredentials.records" },
  { path: "packages/runtime/claude-runtime/src/provider/credential-runtime.ts", owner: "providerCredentials", method: "resolve", caller: "E01RuntimeCoordinator.configureProviderRuntime", snapshot: "providerCredentials", assertion: "providerCredentials.records" },
] as const;

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function jsonl(path: string): Obj[] {
  return readFileSync(path, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", "utf8");
}

function writeJsonl(path: string, values: unknown[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, values.map((value) => JSON.stringify(value)).join("\n") + "\n", "utf8");
}

function walk(relativeRoot: string): string[] {
  const absolute = join(zyra, relativeRoot);
  if (!existsSync(absolute)) return [];
  const output: string[] = [];
  for (const entry of readdirSync(absolute, { withFileTypes: true })) {
    const relativePath = join(relativeRoot, entry.name).replaceAll("\\", "/");
    if (entry.isDirectory()) output.push(...walk(relativePath));
    else output.push(relativePath);
  }
  return output;
}

function physicalLines(text: string): number {
  if (!text) return 0;
  const lines = text.replaceAll("\r", "").split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.length;
}

function normalized(value: string): string {
  return value
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|\n)\s*\/\/[^\n]*/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function structuralNormalized(value: string): string {
  const scanner = ts.createScanner(ts.ScriptTarget.ESNext, true, ts.LanguageVariant.Standard, value);
  const tokens: string[] = [];
  for (let token = scanner.scan(); token !== ts.SyntaxKind.EndOfFileToken; token = scanner.scan()) {
    if (token === ts.SyntaxKind.Identifier || token === ts.SyntaxKind.PrivateIdentifier) {
      tokens.push("Identifier");
    } else if (
      token === ts.SyntaxKind.StringLiteral
      || token === ts.SyntaxKind.NumericLiteral
      || token === ts.SyntaxKind.BigIntLiteral
      || token === ts.SyntaxKind.RegularExpressionLiteral
      || token === ts.SyntaxKind.NoSubstitutionTemplateLiteral
      || token === ts.SyntaxKind.TemplateHead
      || token === ts.SyntaxKind.TemplateMiddle
      || token === ts.SyntaxKind.TemplateTail
    ) {
      tokens.push(ts.SyntaxKind[token]);
    } else {
      tokens.push(ts.SyntaxKind[token]);
    }
  }
  return tokens.join(" ");
}

function astUnits(path: string, text: string): Unit[] {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  const output: Unit[] = [];
  const add = (node: ts.Node, symbol: string): void => {
    const start = node.getStart(source);
    const end = node.end;
    const raw = text.slice(start, end);
    const canonical = normalized(raw);
    if (!canonical) return;
    const startLine = source.getLineAndCharacterOfPosition(start).line;
    const endLine = source.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line;
    output.push({
      hash: hash(canonical),
      structuralHash: hash(structuralNormalized(raw)),
      lines: endLine - startLine + 1,
      path,
      symbol,
    });
  };
  source.statements.forEach((statement, index) => {
    if (
      ts.isImportDeclaration(statement)
      || ts.isExportDeclaration(statement)
      || ts.isInterfaceDeclaration(statement)
      || ts.isTypeAliasDeclaration(statement)
      || (ts.isModuleDeclaration(statement) && (statement.flags & ts.NodeFlags.Ambient) !== 0)
    ) return;
    if (ts.isClassDeclaration(statement)) {
      const className = statement.name?.text ?? `anonymous_class_${index}`;
      if (statement.members.length === 0) add(statement, className);
      else statement.members.forEach((member, memberIndex) => {
        if (
          ts.isPropertyDeclaration(member)
          && !member.initializer
          && !member.decorators
        ) return;
        if (ts.isIndexSignatureDeclaration(member) || ts.isSemicolonClassElement(member)) return;
        const name = member.name && "getText" in member.name ? member.name.getText(source) : `member_${memberIndex}`;
        add(member, `${className}.${name}`);
      });
      return;
    }
    const named = (statement as ts.NamedDeclaration).name;
    add(statement, named && "getText" in named ? named.getText(source) : `statement_${index}`);
  });
  return output;
}

function typeOnlyLines(path: string, text: string): number {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  let lines = 0;
  const count = (node: ts.Node): void => {
    const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line;
    const end = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line;
    lines += end - start + 1;
  };
  for (const statement of source.statements) {
    if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) count(statement);
    if (ts.isClassDeclaration(statement)) {
      for (const member of statement.members) {
        if (ts.isPropertyDeclaration(member) && !member.initializer) count(member);
        if (ts.isIndexSignatureDeclaration(member) || ts.isSemicolonClassElement(member)) count(member);
      }
    }
  }
  return lines;
}

function executableSourceLines(records: Obj[]): number {
  const groups = new Map<string, Obj[]>();
  for (const record of records.filter((item) => item.accepted === true)) {
    groups.set(record.source_path, [...(groups.get(record.source_path) ?? []), record]);
  }
  let total = 0;
  for (const [path, ranges] of groups) {
    const text = readFileSync(join(workspace, "claude-code-best", path), "utf8");
    const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
    const executable = new Set<number>();
    const addNode = (node: ts.Node): void => {
      const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      const end = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line + 1;
      for (let line = start; line <= end; line += 1) executable.add(line);
    };
    for (const statement of source.statements) {
      if (
        ts.isImportDeclaration(statement)
        || ts.isExportDeclaration(statement)
        || ts.isInterfaceDeclaration(statement)
        || ts.isTypeAliasDeclaration(statement)
      ) continue;
      if (ts.isClassDeclaration(statement)) {
        for (const member of statement.members) {
          if (ts.isPropertyDeclaration(member) && !member.initializer) continue;
          if (ts.isIndexSignatureDeclaration(member) || ts.isSemicolonClassElement(member)) continue;
          addNode(member);
        }
      } else {
        addNode(statement);
      }
    }
    for (const line of executable) {
      if (ranges.some((range) => line >= range.start_line && line <= range.end_line)) total += 1;
    }
  }
  return total;
}

function e01ExclusionReason(path: string): string | null {
  if (e01ExcludedProductionFiles.has(path)) {
    return "live capability catalog owned by later permission/MCP/skill/subagent execution units; not counted toward E01 runtime-core SLOC";
  }
  const prefix = e01ExcludedProductionPrefixes.find((candidate) => path.startsWith(candidate));
  if (prefix) {
    return `live ${prefix.split("/").at(-2)} subsystem owned by a later execution unit; excluded from E01-only SLOC`;
  }
  return null;
}

function git(args: string[], encoding: "utf8" | "buffer" = "utf8"): string | Buffer {
  return execFileSync("git", args, {
    cwd: zyra,
    encoding: encoding === "utf8" ? "utf8" : "buffer",
    stdio: ["ignore", "pipe", "ignore"],
  }) as string | Buffer;
}

function baselineText(path: string): string | null {
  try {
    return (git(["show", `${VERIFIED}:${path}`], "buffer") as Buffer).toString("utf8");
  } catch {
    return null;
  }
}

function deduplicatedLines(
  units: Unit[],
  excludedHashes = new Set<string>(),
  excludedStructuralHashes = new Set<string>(),
  useStructuralHashes = true,
): { lines: number; units: Unit[] } {
  const seen = new Set<string>();
  const seenStructures = new Set<string>();
  const accepted: Unit[] = [];
  for (const unit of units) {
    if (
      excludedHashes.has(unit.hash)
      || seen.has(unit.hash)
      || (useStructuralHashes && excludedStructuralHashes.has(unit.structuralHash))
      || (useStructuralHashes && seenStructures.has(unit.structuralHash))
    ) continue;
    seen.add(unit.hash);
    if (useStructuralHashes) seenStructures.add(unit.structuralHash);
    accepted.push(unit);
  }
  return { lines: accepted.reduce((sum, unit) => sum + unit.lines, 0), units: accepted };
}

function testNames(path: string): string[] {
  const text = readFileSync(join(zyra, path), "utf8");
  const names: string[] = [];
  for (const match of text.matchAll(/(?:test|it)\s*\(\s*["'`]([^"'`]+)["'`]/g)) names.push(match[1]);
  return names;
}

const declarationCache = new Map<string, string | null>();
const testBodyCache = new Map<string, string | null>();

function declarationText(path: string, symbol: string): string | null {
  const key = `${path}::${symbol}`;
  if (declarationCache.has(key)) return declarationCache.get(key) ?? null;
  const absolute = join(zyra, path);
  if (!existsSync(absolute)) {
    declarationCache.set(key, null);
    return null;
  }
  const text = readFileSync(absolute, "utf8");
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  const [owner, member] = symbol.split(".");
  let selected: ts.Node | null = null;
  for (const statement of source.statements) {
    if (member && ts.isClassDeclaration(statement) && statement.name?.text === owner) {
      selected = statement.members.find((item) => item.name?.getText(source) === member) ?? null;
      break;
    }
    const named = (statement as ts.NamedDeclaration).name;
    if (!member && named && ts.isIdentifier(named) && named.text === owner) {
      selected = statement;
      break;
    }
  }
  const result = selected ? selected.getText(source) : null;
  declarationCache.set(key, result);
  return result;
}

function escaped(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function declarationInvokes(callerPath: string, callerSymbol: string, calleeSymbol: string): boolean {
  if (callerSymbol === calleeSymbol) return declarationText(callerPath, callerSymbol) !== null;
  const caller = declarationText(callerPath, callerSymbol);
  if (!caller) return false;
  const callee = escaped(calleeSymbol.split(".").at(-1) ?? calleeSymbol);
  return new RegExp(`(?:\\.|\\b)${callee}\\s*\\(`).test(caller)
    || new RegExp(`\\.(?:map|flatMap|forEach|filter|some|find)\\(\\s*${callee}\\b`).test(caller);
}

function testBody(path: string, name: string): string | null {
  const key = `${path}::${name}`;
  if (testBodyCache.has(key)) return testBodyCache.get(key) ?? null;
  const absolute = join(zyra, path);
  if (!existsSync(absolute)) {
    testBodyCache.set(key, null);
    return null;
  }
  const text = readFileSync(absolute, "utf8");
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  let selected: string | null = null;
  const visit = (node: ts.Node): void => {
    if (selected) return;
    if (ts.isCallExpression(node) && /^(?:test|it)$/.test(node.expression.getText(source))) {
      const [title, body] = node.arguments;
      if ((ts.isStringLiteral(title) || ts.isNoSubstitutionTemplateLiteral(title)) && title.text === name && body) {
        selected = body.getText(source);
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  testBodyCache.set(key, selected);
  return selected;
}

function readEvidence(name: string): Obj | null {
  const path = join(evidenceRoot, `${name}-result.json`);
  return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null;
}

function main(): void {
  const enforce = process.argv.includes("--enforce-gates");
  const failures: string[] = [];
  const source = jsonl(join(manifestRoot, "execution-01-source-manifest.jsonl"));
  const python = jsonl(join(manifestRoot, "execution-01-python-owner-baseline.jsonl"));
  const targets = jsonl(join(manifestRoot, "execution-01-target-custody-map.jsonl"));
  const mutations = jsonl(join(manifestRoot, "execution-01-mutation-manifest.jsonl"));
  const profile = JSON.parse(readFileSync(join(manifestRoot, "execution-01-gate-profile.json"), "utf8"));
  const thresholds = profile.thresholds as Obj;
  if (profile.verified_zyra_head !== VERIFIED) failures.push("verified baseline mismatch");
  if (Bun.version !== profile.toolchain.bun_version) failures.push(`Bun ${Bun.version} does not match ${profile.toolchain.bun_version}`);
  const packageJson = JSON.parse(readFileSync(join(zyra, "package.json"), "utf8"));
  if (packageJson.devDependencies.typescript !== profile.toolchain.typescript_version) failures.push("TypeScript pin mismatch");

  const productionFiles = productionRoots.flatMap(walk).filter((path) => path.endsWith(".ts") && !/\.(?:test|spec)\.ts$/.test(path));
  for (const path of productionFiles) {
    const reason = e01ExclusionReason(path);
    if (reason) semanticallyDormantProductionPaths.set(path, reason);
  }
  const countedProductionFiles = productionFiles.filter((path) => !semanticallyDormantProductionPaths.has(path));
  const dormantProductionFiles = productionFiles.filter((path) => semanticallyDormantProductionPaths.has(path));
  const testFiles = walk(testRoot).filter((path) => path.endsWith(".behavior.test.ts"));
  const productionText = new Map(productionFiles.map((path) => [path, readFileSync(join(zyra, path), "utf8")]));
  const rawFinalPhysical = [...productionText.values()].reduce((sum, text) => sum + physicalLines(text), 0);
  const finalPhysical = countedProductionFiles.reduce((sum, path) => sum + physicalLines(productionText.get(path) ?? ""), 0);
  const dormantPhysical = dormantProductionFiles.reduce((sum, path) => sum + physicalLines(productionText.get(path) ?? ""), 0);
  const typeOnlyExcludedPhysical = countedProductionFiles.reduce(
    (sum, path) => sum + typeOnlyLines(path, productionText.get(path) ?? ""),
    0,
  );
  const baselineHashes = new Set<string>();
  const baselineStructuralHashes = new Set<string>();
  for (const path of countedProductionFiles) {
    const previous = baselineText(path);
    if (previous !== null) for (const unit of astUnits(path, previous)) {
      baselineHashes.add(unit.hash);
      baselineStructuralHashes.add(unit.structuralHash);
    }
  }
  const currentUnits = countedProductionFiles.flatMap((path) => astUnits(path, productionText.get(path) ?? ""));
  const exactChanged = deduplicatedLines(currentUnits, baselineHashes, new Set(), false);
  const changed = deduplicatedLines(currentUnits, baselineHashes, baselineStructuralHashes);
  const testUnits = testFiles.flatMap((path) => astUnits(path, readFileSync(join(zyra, path), "utf8")));
  const exactTests = deduplicatedLines(testUnits, new Set(), new Set(), false);
  const effectiveTests = deduplicatedLines(testUnits, new Set(), new Set());
  const adapterFiles = countedProductionFiles.filter((path) => /(?:adapter|bridge|gateway)/i.test(basename(path)));
  const adapterLines = adapterFiles.reduce((sum, path) => sum + physicalLines(productionText.get(path) ?? ""), 0);
  const adapterRatio = finalPhysical === 0 ? 1 : adapterLines / finalPhysical;
  const effectiveChangedGroups = new Map<string, Unit[]>();
  for (const unit of changed.units) {
    effectiveChangedGroups.set(unit.path, [...(effectiveChangedGroups.get(unit.path) ?? []), unit]);
  }
  const effectiveChangedByFile = [...effectiveChangedGroups.entries()]
    .map(([path, units]) => ({
      path,
      ast_units: units.length,
      effective_sloc: units.reduce((sum, unit) => sum + unit.lines, 0),
    }))
    .sort((left, right) => right.effective_sloc - left.effective_sloc || left.path.localeCompare(right.path));

  if (changed.lines < thresholds.effective_changed_typescript_sloc) failures.push(`effective changed TypeScript SLOC ${changed.lines} < ${thresholds.effective_changed_typescript_sloc}`);
  if (finalPhysical < thresholds.final_non_test_typescript_sloc) failures.push(`final non-test TypeScript SLOC ${finalPhysical} < ${thresholds.final_non_test_typescript_sloc}`);
  if (effectiveTests.lines < thresholds.effective_behavior_test_sloc) failures.push(`effective behavior test SLOC ${effectiveTests.lines} < ${thresholds.effective_behavior_test_sloc}`);
  if (adapterRatio > thresholds.adapter_ratio_maximum) failures.push(`adapter ratio ${adapterRatio} > ${thresholds.adapter_ratio_maximum}`);

  const sourcePhysical = source.filter((item) => item.accepted).reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0);
  const sourceExecutable = executableSourceLines(source);
  if (sourceExecutable < thresholds.accepted_source_executable_sloc) failures.push(`accepted source executable SLOC ${sourceExecutable} below gate`);
  if (mutations.length < thresholds.mutation_points_minimum) failures.push("mutation manifest threshold failed");
  const undeletedPython = [...new Set(python.map((item) => item.python_path))].filter((path) => existsSync(join(zyra, path)));
  if (undeletedPython.length) failures.push(`Python canonical owner paths still exist: ${undeletedPython.join(",")}`);

  const coordinatorPath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
  const rootCallsiteText = readFileSync(join(zyra, "packages/runtime/claude-runtime/src/query-engine.ts"), "utf8");
  if (!rootCallsiteText.includes("E01RuntimeCoordinator") || !rootCallsiteText.includes("await e01.bootstrap()")) failures.push("default E01 callsite is not wired");
  const recordProviderDeclaration = declarationText(coordinatorPath, "E01RuntimeCoordinator.recordProvider") ?? "";
  const lifecycleDeclaration = declarationText(coordinatorPath, "E01RuntimeCoordinator.applyProviderLifecycle") ?? "";
  const snapshotDeclarationForActivation = declarationText(coordinatorPath, "E01RuntimeCoordinator.snapshot") ?? "";
  const providerModelExecuteDeclaration = declarationText(
    "packages/runtime/claude-runtime/src/provider/model-runtime.ts",
    "ProviderModelRuntime.execute",
  ) ?? "";
  const queryRunDeclaration = declarationText(
    "packages/runtime/claude-runtime/src/query-engine.ts",
    "ClaudeRuntimeCore.run",
  ) ?? "";
  const providerBehaviorBody = testBody(
    "packages/runtime/claude-runtime/test/runtime.test.ts",
    "runtime commits provider prompt usage and recovery state through default loop",
  ) ?? "";
  const providerActivation = providerActivationContracts.map((contract) => {
    const caller = declarationText(coordinatorPath, contract.caller) ?? "";
    const callerName = contract.caller.split(".").at(-1) ?? contract.caller;
    const entryConnected = callerName === "configureProviderRuntime"
      ? /await\s+e01\.configureProviderRuntime\s*\(/u.test(queryRunDeclaration)
      : callerName === "executePreparedProvider"
        ? /e01\.executePreparedProvider\s*\(/u.test(queryRunDeclaration)
        : new RegExp(`(?:\\.|\\b)${escaped(callerName)}\\s*\\(`).test(lifecycleDeclaration);
    const ownerInvoked = contract.owner === "providerTransport"
      ? /this\.provider\.execute\s*\(/u.test(caller)
        && /transport\.execute\s*\(/u.test(providerModelExecuteDeclaration)
      : new RegExp(`this\\.${escaped(contract.owner)}\\.${escaped(contract.method)}\\s*\\(`).test(caller);
    const snapshotOwned = new RegExp(`\\b${escaped(contract.snapshot)}\\s*:\\s*this\\.${escaped(contract.snapshot)}\\.snapshot\\s*\\(`).test(snapshotDeclarationForActivation);
    const behaviorAsserted = providerBehaviorBody.includes(contract.assertion);
    return { ...contract, entry_connected: entryConnected, owner_invoked: ownerInvoked, snapshot_owned: snapshotOwned, behavior_asserted: behaviorAsserted, complete: entryConnected && ownerInvoked && snapshotOwned && behaviorAsserted };
  });
  if (!recordProviderDeclaration.includes("this.applyProviderLifecycle(")) failures.push("provider lifecycle is disconnected from recordProvider");
  for (const contract of providerActivation) if (!contract.complete) failures.push(`incomplete provider activation contract: ${contract.path}`);
  const mutationEvidencePath = join(evidenceRoot, "mutation-results.json");
  const mutationEvidence = existsSync(mutationEvidencePath) ? JSON.parse(readFileSync(mutationEvidencePath, "utf8")) : null;
  const killedMutationIds = new Set<string>(
    (Array.isArray(mutationEvidence?.results) ? mutationEvidence.results : [])
      .filter((item: Obj) => item.killed === true || String(item.status ?? item.result).toUpperCase() === "KILLED")
      .map((item: Obj) => String(item.mutation_id ?? item.mutationId ?? item.id)),
  );
  const mutationById = new Map(mutations.map((item) => [String(item.mutation_id), item]));
  const snapshotDeclaration = declarationText(coordinatorPath, "E01RuntimeCoordinator.snapshot") ?? "";
  const sourceById = new Map(source.filter((item) => item.accepted === true).map((item) => [item.mapping_id, item]));
  const chains = targets.map((target) => {
    const sourceItem = sourceById.get(target.mapping_id);
    const targetAbsolute = join(zyra, target.target_path);
    const targetText = existsSync(targetAbsolute) ? readFileSync(targetAbsolute, "utf8") : "";
    const targetSymbol = String(target.target_symbol);
    const targetSymbolExists = declarationText(String(target.target_path), targetSymbol) !== null;
    const targetHashMatches = typeof target.target_sha256 === "string"
      && target.target_sha256.length === 64
      && target.target_sha256 === (targetText ? hash(targetText) : null);
    const callsitePath = String(target.default_callsite_path);
    const callsiteSymbol = String(target.default_callsite_symbol);
    const invokesTarget = declarationInvokes(callsitePath, callsiteSymbol, targetSymbol);
    const declaredEdges = Array.isArray(target.default_entry_edges) ? target.default_entry_edges as Obj[] : [];
    const verifiedEdges = declaredEdges.map((item) => {
      const callerPath = String(item.caller_path);
      const callerSymbol = String(item.caller_symbol);
      const calleePath = String(item.callee_path);
      const calleeSymbol = String(item.callee_symbol);
      return {
        ...item,
        caller_exists: declarationText(callerPath, callerSymbol) !== null,
        callee_exists: declarationText(calleePath, calleeSymbol) !== null,
        invokes: declarationInvokes(callerPath, callerSymbol, calleeSymbol),
      };
    });
    const defaultTarget = target.target_path === "packages/runtime/claude-runtime/src/query-engine.ts" && targetSymbol === "ClaudeRuntimeCore.run";
    const entryChainCoversTarget = defaultTarget || verifiedEdges.some((item) => item.callee_path === target.target_path && item.callee_symbol === targetSymbol);
    const entryChainValid = (defaultTarget || verifiedEdges.length > 0)
      && verifiedEdges.every((item) => item.caller_exists && item.callee_exists && item.invokes)
      && entryChainCoversTarget;
    const ownerProperty = String(target.state_snapshot_property ?? "");
    const permissionHostSnapshot = declarationText(
      "packages/runtime/claude-runtime/src/capability-host.ts",
      "PermissionedCapabilityHost.snapshot",
    ) ?? "";
    const stateEffectReachable = Boolean(ownerProperty) && (
      new RegExp(`\\b${escaped(ownerProperty)}\\s*:\\s*this\\.${escaped(ownerProperty)}\\.snapshot\\s*\\(`).test(snapshotDeclaration)
      || (ownerProperty === "modelIteration" && /modelIteration\s*:\s*iteration\.snapshot\s*\(/u.test(queryRunDeclaration))
      || (["enforcement", "settlement"].includes(ownerProperty)
        && new RegExp(`\\b${escaped(ownerProperty)}\\s*:\\s*this\\.${escaped(ownerProperty)}\\.snapshot\\s*\\(`).test(permissionHostSnapshot))
    );
    const declaredBehavior = Array.isArray(target.behavior_tests) ? target.behavior_tests as Obj[] : [];
    const behaviorTests = declaredBehavior.map((item) => {
      const path = String(item.path);
      const name = String(item.name);
      const body = testBody(path, name);
      const anchor = String(item.anchor ?? "");
      const assertionTokens = Array.isArray(item.assertion_tokens) ? item.assertion_tokens.map(String) : [];
      return {
        path,
        test_name: name,
        anchor,
        assertion_tokens: assertionTokens,
        exists: body !== null,
        anchor_present: Boolean(body && anchor && body.includes(anchor)),
        state_assertions_present: Boolean(body && assertionTokens.length > 0 && assertionTokens.every((token) => body.includes(token))),
      };
    });
    const behaviorLimit = Number(thresholds.behavior_tests_per_mapping_maximum ?? 2);
    const behaviorValid = behaviorTests.length > 0
      && behaviorTests.length <= behaviorLimit
      && behaviorTests.every((item) => item.exists && item.anchor_present && item.state_assertions_present);
    const declaredMutationIds = Array.isArray(target.mutation_ids) ? target.mutation_ids.map(String) : [];
    const minimumMutations = Number(thresholds.mutation_ids_per_mapping_minimum ?? 1);
    const mutationLinks = declaredMutationIds.map((mutationId) => ({
      mutation_id: mutationId,
      declared: mutationById.has(mutationId),
      killed: killedMutationIds.has(mutationId),
    }));
    const mutationLinksValid = mutationLinks.length >= minimumMutations
      && mutationLinks.every((item) => item.declared && (!enforce || item.killed));
    const complete = Boolean(sourceItem)
      && targetSymbolExists
      && targetHashMatches
      && invokesTarget
      && entryChainValid
      && stateEffectReachable
      && Boolean(target.state_observation)
      && behaviorValid
      && mutationLinksValid;
    if (!complete) failures.push(`incomplete five-hop chain ${target.mapping_id}`);
    return {
      mapping_id: target.mapping_id,
      complete,
      source: sourceItem ? {
        repo: sourceItem.source_repo,
        snapshot: sourceItem.source_snapshot,
        path: sourceItem.source_path,
        symbol: sourceItem.source_symbol,
        range: [sourceItem.start_line, sourceItem.end_line],
        sha256: sourceItem.source_sha256,
      } : null,
      target: {
        path: target.target_path,
        symbol: target.target_symbol,
        sha256: targetText ? hash(targetText) : null,
        manifest_sha256: target.target_sha256,
        hash_matches: targetHashMatches,
        symbol_exists: targetSymbolExists,
      },
      default_callsite: {
        path: callsitePath,
        symbol: callsiteSymbol,
        coordinator_path: coordinatorPath,
        coordinator_symbol: "E01RuntimeCoordinator",
        invokes_target: invokesTarget,
      },
      default_entry_edges: verifiedEdges,
      state_effect: {
        store: target.state_store,
        kind: target.state_effect_kind,
        coordinator_snapshot_property: ownerProperty,
        observation: target.state_observation,
        reachable: stateEffectReachable,
      },
      behavior_tests: behaviorTests,
      mutations: mutationLinks,
      adaptation: target.adaptation,
    };
  });
  const targetSymbolCounts = new Map<string, number>();
  for (const target of targets) {
    const key = `${target.target_path}::${target.target_symbol}`;
    targetSymbolCounts.set(key, (targetSymbolCounts.get(key) ?? 0) + 1);
  }
  const uniqueTargetSymbolCount = targetSymbolCounts.size;
  const maximumMappingsPerTargetSymbol = Math.max(0, ...targetSymbolCounts.values());
  if (uniqueTargetSymbolCount < Number(thresholds.source_to_target_unique_symbols_minimum ?? 1)) {
    failures.push(`unique target symbol count ${uniqueTargetSymbolCount} below gate`);
  }
  if (maximumMappingsPerTargetSymbol > Number(thresholds.source_to_target_max_mappings_per_symbol ?? Number.MAX_SAFE_INTEGER)) {
    failures.push(`maximum mappings per target symbol ${maximumMappingsPerTargetSymbol} above gate`);
  }
  if (enforce && (!mutationEvidence || mutationEvidence.summary.killed !== mutations.length || mutationEvidence.summary.invalid !== 0)) {
    failures.push(`${mutations.length}/${mutations.length} executable mutation evidence missing or failed`);
  }
  const requiredEvidence = ["runtime-origin", "write-path", "same-session-resume", "lost-ack", "disable", "dependencies", "toolchain", "cleanroom"];
  const evidenceStatus = Object.fromEntries(requiredEvidence.map((name) => [name, readEvidence(name)]));
  if (enforce) for (const [name, value] of Object.entries(evidenceStatus)) if (!value?.ok) failures.push(`required evidence missing or failed: ${name}`);

  const locReport = {
    schema_version: "3.0",
    execution_id: "E01",
    verified_baseline: VERIFIED,
    production_roots: productionRoots,
    production_file_count: productionFiles.length,
    semantically_counted_production_file_count: countedProductionFiles.length,
    semantically_counted_production_files: countedProductionFiles,
    raw_non_test_typescript_physical_sloc: rawFinalPhysical,
    final_non_test_typescript_physical_sloc: finalPhysical,
    semantically_dormant_production_sloc: dormantPhysical,
    semantically_dormant_production: dormantProductionFiles.map((path) => ({ path, reason: semanticallyDormantProductionPaths.get(path) })),
    excluded_type_only_and_uninitialized_declaration_sloc: typeOnlyExcludedPhysical,
    effective_changed_by_file: effectiveChangedByFile,
    line_buckets: {
      production_e01_physical: finalPhysical,
      production_excluded_later_unit_physical: dormantPhysical,
      production_type_only_excluded_physical: typeOnlyExcludedPhysical,
      behavior_test_effective: effectiveTests.lines,
      generated: 0,
      data_as_code: 0,
      vendor_like_source_pool: 0,
      adapter_only: adapterLines,
      mock_fixture: 0,
      docs: 0,
    },
    provider_activation_contracts: providerActivation,
    baseline_ast_unit_hash_count: baselineHashes.size,
    baseline_ast_structural_hash_count: baselineStructuralHashes.size,
    current_ast_unit_count: currentUnits.length,
    exact_changed_ast_unit_count: exactChanged.units.length,
    exact_changed_typescript_sloc: exactChanged.lines,
    effective_changed_ast_unit_count: changed.units.length,
    effective_changed_typescript_sloc: changed.lines,
    structural_clone_excluded_production_sloc: exactChanged.lines - changed.lines,
    behavior_test_files: testFiles,
    behavior_test_ast_unit_count: testUnits.length,
    exact_behavior_test_ast_unit_count: exactTests.units.length,
    exact_behavior_test_sloc: exactTests.lines,
    effective_behavior_test_ast_unit_count: effectiveTests.units.length,
    effective_behavior_test_sloc: effectiveTests.lines,
    structural_clone_excluded_behavior_test_sloc: exactTests.lines - effectiveTests.lines,
    adapter_files: adapterFiles,
    adapter_sloc: adapterLines,
    adapter_ratio: adapterRatio,
    algorithm: "TypeScript AST declaration/member units from semantically active production modules; exact hashes plus identifier/literal-normalized structural hashes; baseline, repeated structural units, and inspect/snapshot/restore-only modules excluded",
  };
  writeJson(join(evidenceRoot, "effective-loc-report.json"), locReport);
  writeJsonl(join(evidenceRoot, "source-to-target-five-hop.jsonl"), chains);
  const result = {
    schema_version: "3.0",
    execution_id: "E01",
    generated_at_utc: new Date().toISOString(),
    verified_baseline: VERIFIED,
    enforce_gates: enforce,
    pass: failures.length === 0,
    failures,
    thresholds,
    actual: {
      accepted_source_physical_sloc: sourcePhysical,
      accepted_source_executable_sloc: sourceExecutable,
      effective_changed_typescript_sloc: changed.lines,
      final_non_test_typescript_sloc: finalPhysical,
      effective_behavior_test_sloc: effectiveTests.lines,
      adapter_ratio: adapterRatio,
      source_to_target_chain_count: chains.length,
      complete_source_to_target_chain_count: chains.filter((item) => item.complete).length,
      unique_target_symbol_count: uniqueTargetSymbolCount,
      maximum_mappings_per_target_symbol: maximumMappingsPerTargetSymbol,
      mutation_declared: mutations.length,
      mutation_killed: mutationEvidence?.summary?.killed ?? 0,
      python_owner_paths_remaining: undeletedPython,
    },
    evidence: Object.fromEntries(Object.entries(evidenceStatus).map(([name, value]) => [name, value ? { ok: value.ok, probe_id: value.probe_id } : null])),
  };
  writeJson(join(evidenceRoot, "candidate-gate-result.json"), result);
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  if (failures.length) process.exitCode = 1;
}

if (process.argv[2] !== "verify") throw new Error("usage: verify_m1_r01_manifests.ts verify [--enforce-gates]");
main();
