#!/usr/bin/env bun

import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
} from "node:fs";
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import * as ts from "typescript";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Obj = Record<string, any>;
type ExecutionId = "01" | "02" | "03";

const EXPECTED_BUN = "1.2.15";
const EXPECTED_TYPESCRIPT = "5.8.3";
const EXPECTED_PYTHON = "3.12";
const EXECUTIONS = new Set(["01", "02", "03"]);
const SOURCE_REPOS = new Set(["claude-code-best", "opencode", "openclaw"]);
const ROOT_REPOS = new Set(["claude-code-best", "opencode", "openclaw", "hermes-agent", "oh-my-pi"]);
const ROOT_REPO_ALIASES: Record<string, string> = {
  "claude-code-best": "claude-code-best",
  claude: "claude-code-best",
  opencode: "opencode",
  openclaw: "openclaw",
  "open-claw": "openclaw",
  hermes: "hermes-agent",
  "hermes-agent": "hermes-agent",
  "oh-my-pi": "oh-my-pi",
  omp: "oh-my-pi",
};
const PRIMARY_ROLE: Record<string, string> = {
  "claude-code-best": "primary",
  opencode: "supplementary",
  openclaw: "supplementary",
};
const FINAL_FLOORS: Record<ExecutionId, number> = { "01": 28000, "02": 38000, "03": 34000 };
const CHANGED_FLOORS: Record<ExecutionId, number> = { "01": 25416, "02": 35366, "03": 31890 };
const TEST_FLOORS: Record<ExecutionId, number> = { "01": 7000, "02": 10000, "03": 9000 };

const scriptDir = dirname(fileURLToPath(import.meta.url));
const defaultZyraRoot = resolve(scriptDir, "..");
const workspaceRoot = resolve(defaultZyraRoot, "..");
const defaultManifestSchema = join(
  workspaceRoot,
  "docs",
  "remediations",
  "M1-R01-claude-source-custody",
  "manifests",
  "manifest-v2.schema.json",
);
const defaultEvidenceSchema = join(
  workspaceRoot,
  "docs",
  "remediations",
  "M1-R01-claude-source-custody",
  "manifests",
  "evidence-v1.schema.json",
);
const defaultGolden = join(scriptDir, "fixtures", "m1_r01_counter_golden.json");

interface Finding {
  code: string;
  location: string;
  message: string;
}

interface Args {
  zyraRoot: string;
  baselineRoot?: string;
  sourceRoots: Map<string, string>;
  manifests: string[];
  evidence: string[];
  manifestSchema: string;
  evidenceSchema: string;
  pythonBin: string;
  goldenPath: string;
  goldenSelfTest: boolean;
  requireCompleteSet: boolean;
  enforceGates: boolean;
  pretty: boolean;
}

interface RuntimeToken {
  kind: number;
  raw: string;
  start: number;
  end: number;
  lines: number[];
}

interface RangeAnalysis {
  executableSloc: number;
  executableLines: number[];
  normalizedNodeHash: string | null;
  canonicalTokens: string[];
  lineCanonical: Map<number, string>;
  tokenCount: number;
  shingles: Set<string>;
  inferredKind: string;
}

interface LoadedManifest {
  path: string;
  metadata: Obj;
  entries: Obj[];
}

function sha256Bytes(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value: any): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
    .join(",")}}`;
}

function sameJson(left: any, right: any): boolean {
  return stableJson(left) === stableJson(right);
}

function codePointCompare(left: string, right: string): number {
  const a = Array.from(left.normalize("NFC"));
  const b = Array.from(right.normalize("NFC"));
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const delta = a[i].codePointAt(0)! - b[i].codePointAt(0)!;
    if (delta !== 0) return delta;
  }
  return a.length - b.length;
}

function parseArgs(argv: string[]): Args {
  const args: Args = {
    zyraRoot: defaultZyraRoot,
    sourceRoots: new Map(),
    manifests: [],
    evidence: [],
    manifestSchema: defaultManifestSchema,
    evidenceSchema: defaultEvidenceSchema,
    pythonBin: "python",
    goldenPath: defaultGolden,
    goldenSelfTest: false,
    requireCompleteSet: false,
    enforceGates: false,
    pretty: false,
  };
  if (argv[0] === "verify") argv = argv.slice(1);
  else if (argv[0] === "golden-self-test") {
    args.goldenSelfTest = true;
    argv = argv.slice(1);
  }
  const take = (index: number, flag: string): string => {
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${flag} requires a value`);
    return value;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--zyra-root") args.zyraRoot = take(i++, flag);
    else if (flag === "--baseline-root") args.baselineRoot = take(i++, flag);
    else if (flag === "--source-root") {
      const value = take(i++, flag);
      const split = value.indexOf("=");
      if (split <= 0) throw new Error("--source-root must be repo=path");
      const requestedRepo = value.slice(0, split).toLowerCase();
      const repo = ROOT_REPO_ALIASES[requestedRepo];
      if (!repo || !ROOT_REPOS.has(repo)) throw new Error(`unsupported source repo: ${requestedRepo}`);
      if (args.sourceRoots.has(repo)) throw new Error(`duplicate source root: ${repo}`);
      args.sourceRoots.set(repo, value.slice(split + 1));
    } else if (flag === "--manifest") args.manifests.push(take(i++, flag));
    else if (flag === "--evidence") args.evidence.push(take(i++, flag));
    else if (flag === "--manifest-schema") args.manifestSchema = take(i++, flag);
    else if (flag === "--evidence-schema") args.evidenceSchema = take(i++, flag);
    else if (flag === "--python-bin") args.pythonBin = take(i++, flag);
    else if (flag === "--golden") args.goldenPath = take(i++, flag);
    else if (flag === "--golden-self-test") args.goldenSelfTest = true;
    else if (flag === "--require-complete-set") args.requireCompleteSet = true;
    else if (flag === "--enforce-gates") args.enforceGates = true;
    else if (flag === "--pretty") args.pretty = true;
    else throw new Error(`unknown argument: ${flag}`);
  }
  args.zyraRoot = resolve(args.zyraRoot);
  if (args.baselineRoot) args.baselineRoot = resolve(args.baselineRoot);
  for (const [repo, root] of args.sourceRoots) args.sourceRoots.set(repo, resolve(root));
  args.manifests = args.manifests.map(resolve);
  args.evidence = args.evidence.map(resolve);
  args.manifestSchema = resolve(args.manifestSchema);
  args.evidenceSchema = resolve(args.evidenceSchema);
  args.goldenPath = resolve(args.goldenPath);
  return args;
}

function resolveRef(root: Obj, reference: string): any {
  if (!reference.startsWith("#/")) throw new Error(`only local JSON Schema refs are supported: ${reference}`);
  return reference
    .slice(2)
    .split("/")
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"))
    .reduce((value, key) => value?.[key], root);
}

function matchesType(value: any, wanted: string): boolean {
  if (wanted === "null") return value === null;
  if (wanted === "array") return Array.isArray(value);
  if (wanted === "object") return value !== null && typeof value === "object" && !Array.isArray(value);
  if (wanted === "integer") return Number.isInteger(value);
  if (wanted === "number") return typeof value === "number" && Number.isFinite(value);
  return typeof value === wanted;
}

function validateSchema(value: any, schema: any, root: Obj, at = "$"): string[] {
  if (typeof schema === "boolean") return schema ? [] : [`${at}: forbidden by schema`];
  if (!schema || typeof schema !== "object") return [`${at}: invalid schema node`];
  if (schema.$ref) return validateSchema(value, resolveRef(root, schema.$ref), root, at);
  const errors: string[] = [];
  if (schema.oneOf) {
    const branches = schema.oneOf.map((item: any) => validateSchema(value, item, root, at));
    const passed = branches.filter((branch: string[]) => branch.length === 0).length;
    if (passed !== 1) errors.push(`${at}: oneOf matched ${passed} branches, expected exactly 1`);
    if (passed === 0) errors.push(...branches.sort((a: string[], b: string[]) => a.length - b.length)[0].slice(0, 4));
    return errors;
  }
  if (schema.anyOf) {
    const branches = schema.anyOf.map((item: any) => validateSchema(value, item, root, at));
    if (!branches.some((branch: string[]) => branch.length === 0)) errors.push(`${at}: no anyOf branch matched`);
  }
  if (schema.allOf) for (const item of schema.allOf) errors.push(...validateSchema(value, item, root, at));
  if (schema.if) {
    const matched = validateSchema(value, schema.if, root, at).length === 0;
    if (matched && schema.then) errors.push(...validateSchema(value, schema.then, root, at));
    if (!matched && schema.else) errors.push(...validateSchema(value, schema.else, root, at));
  }
  if (schema.const !== undefined && !sameJson(value, schema.const)) errors.push(`${at}: does not equal const`);
  if (schema.enum && !schema.enum.some((item: any) => sameJson(item, value))) errors.push(`${at}: is not in enum`);
  if (schema.type) {
    const wanted = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!wanted.some((item: string) => matchesType(value, item))) {
      errors.push(`${at}: expected type ${wanted.join("|")}`);
      return errors;
    }
  }
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) errors.push(`${at}: shorter than minLength`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) errors.push(`${at}: longer than maxLength`);
    if (schema.pattern && !new RegExp(schema.pattern, "u").test(value)) errors.push(`${at}: does not match pattern`);
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${at}: below minimum`);
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${at}: above maximum`);
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(`${at}: fewer than minItems`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${at}: more than maxItems`);
    if (schema.uniqueItems) {
      const keys = value.map(stableJson);
      if (new Set(keys).size !== keys.length) errors.push(`${at}: array items are not unique`);
    }
    if (schema.items) value.forEach((item, index) => errors.push(...validateSchema(item, schema.items, root, `${at}[${index}]`)));
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const required of schema.required ?? []) if (!(required in value)) errors.push(`${at}.${required}: required property missing`);
    for (const [key, item] of Object.entries(value)) {
      if (schema.properties?.[key]) errors.push(...validateSchema(item, schema.properties[key], root, `${at}.${key}`));
      else if (schema.additionalProperties === false) errors.push(`${at}.${key}: additional property forbidden`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
        errors.push(...validateSchema(item, schema.additionalProperties, root, `${at}.${key}`));
      }
    }
  }
  return errors;
}

function readJson(path: string): Obj {
  return JSON.parse(readFileSync(path, "utf8"));
}

function readRecords(path: string): Obj[] {
  const text = readFileSync(path, "utf8").replace(/^\uFEFF/, "");
  const trimmed = text.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[")) return JSON.parse(trimmed);
  if (trimmed.startsWith("{") && !trimmed.includes("\n")) return [JSON.parse(trimmed)];
  return text
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        throw new Error(`${path}:${index + 1}: ${String(error)}`);
      }
    });
}

function normalizeRelativePath(value: string, forbiddenPrefix?: string): string | null {
  if (typeof value !== "string" || !value || value !== value.normalize("NFC")) return null;
  if (isAbsolute(value) || /^[A-Za-z]:/u.test(value) || value.includes("\\") || value.includes("\0")) return null;
  const parts = value.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) return null;
  if (forbiddenPrefix && parts[0].toLowerCase() === forbiddenPrefix.toLowerCase()) return null;
  return parts.join("/");
}

function safeFile(root: string, rel: string): string | null {
  const candidate = resolve(root, ...rel.split("/"));
  const rootReal = realpathSync(root);
  if (!existsSync(candidate) || !lstatSync(candidate).isFile()) return null;
  const fileReal = realpathSync(candidate);
  const prefix = rootReal.endsWith(sep) ? rootReal : `${rootReal}${sep}`;
  if (fileReal !== rootReal && !fileReal.startsWith(prefix)) return null;
  return fileReal;
}

function mergeIntervals(intervals: Array<[number, number]>): Array<[number, number]> {
  const sorted = intervals.filter(([start, end]) => end > start).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: Array<[number, number]> = [];
  for (const item of sorted) {
    const last = merged.at(-1);
    if (!last || item[0] > last[1]) merged.push([...item]);
    else last[1] = Math.max(last[1], item[1]);
  }
  return merged;
}

function intervalContains(intervals: Array<[number, number]>, start: number, end: number): boolean {
  let low = 0;
  let high = intervals.length - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const [left, right] = intervals[mid];
    if (start < left) high = mid - 1;
    else if (start >= right) low = mid + 1;
    else return end <= right;
  }
  return false;
}

function hasDeclareModifier(node: ts.Node): boolean {
  return (ts.canHaveModifiers(node) ? ts.getModifiers(node) : undefined)?.some(
    (modifier) => modifier.kind === ts.SyntaxKind.DeclareKeyword,
  ) ?? false;
}

function collectTypeErasedIntervals(sourceFile: ts.SourceFile): Array<[number, number]> {
  const intervals: Array<[number, number]> = [];
  const add = (node: ts.Node | undefined): void => {
    if (node) intervals.push([node.getFullStart(), node.end]);
  };
  const visit = (node: ts.Node): void => {
    let whole = false;
    const isTypeNode = typeof (ts as any).isTypeNode === "function" && (ts as any).isTypeNode(node);
    if (
      isTypeNode ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isTypeParameterDeclaration(node) ||
      ts.isImportTypeNode(node) ||
      ts.isEmptyStatement(node) ||
      hasDeclareModifier(node)
    ) {
      add(node);
      whole = true;
    } else if (
      ts.isImportDeclaration(node) ||
      ts.isImportEqualsDeclaration(node) ||
      ts.isExportDeclaration(node) ||
      ts.isNamespaceExportDeclaration(node)
    ) {
      // Module wiring alone is not implementation credit. Side-effect imports and
      // re-exports are deliberately excluded with every other import/export-only line.
      add(node);
      whole = true;
    } else if ((ts.isImportSpecifier(node) || ts.isExportSpecifier(node)) && node.isTypeOnly) {
      add(node);
      whole = true;
    } else if (ts.isHeritageClause(node) && node.token === ts.SyntaxKind.ImplementsKeyword) {
      add(node);
      whole = true;
    } else if (ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
      intervals.push([node.expression.end, node.end]);
    } else if (ts.isTypeAssertionExpression(node)) {
      intervals.push([node.getStart(sourceFile), node.expression.getStart(sourceFile)]);
    } else if (ts.isNonNullExpression(node)) {
      intervals.push([node.expression.end, node.end]);
    }
    if (!whole) {
      for (const modifier of ts.canHaveModifiers(node) ? ts.getModifiers(node) ?? [] : []) {
        if (
          modifier.kind === ts.SyntaxKind.ExportKeyword ||
          modifier.kind === ts.SyntaxKind.DefaultKeyword ||
          modifier.kind === ts.SyntaxKind.DeclareKeyword ||
          modifier.kind === ts.SyntaxKind.AbstractKeyword ||
          modifier.kind === ts.SyntaxKind.PublicKeyword ||
          modifier.kind === ts.SyntaxKind.PrivateKeyword ||
          modifier.kind === ts.SyntaxKind.ProtectedKeyword ||
          modifier.kind === ts.SyntaxKind.ReadonlyKeyword ||
          modifier.kind === ts.SyntaxKind.OverrideKeyword
        ) add(modifier);
      }
      const typed = node as ts.Node & { type?: ts.TypeNode; typeParameters?: ts.NodeArray<ts.TypeParameterDeclaration> };
      if (typed.type) {
        const children = node.getChildren(sourceFile);
        const colon = [...children]
          .reverse()
          .find((child) => child.kind === ts.SyntaxKind.ColonToken && child.end <= typed.type!.end);
        intervals.push([colon?.getStart(sourceFile) ?? typed.type.getFullStart(), typed.type.end]);
      }
      if (typed.typeParameters?.length) {
        const children = node.getChildren(sourceFile);
        const first = typed.typeParameters[0];
        const last = typed.typeParameters.at(-1)!;
        const left = [...children].reverse().find((child) => child.kind === ts.SyntaxKind.LessThanToken && child.end <= first.end);
        const right = children.find((child) => child.kind === ts.SyntaxKind.GreaterThanToken && child.getStart(sourceFile) >= last.getStart(sourceFile));
        intervals.push([left?.getStart(sourceFile) ?? first.getFullStart(), right?.end ?? last.end]);
      }
      ts.forEachChild(node, visit);
    }
  };
  visit(sourceFile);
  return mergeIntervals(intervals);
}

function scanRuntimeTokens(text: string, fileName: string): { sourceFile: ts.SourceFile; tokens: RuntimeToken[]; lineCount: number } {
  const isTsx = extname(fileName).toLowerCase() === ".tsx";
  const sourceFile = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.ESNext,
    true,
    isTsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const diagnostics: readonly ts.Diagnostic[] = (sourceFile as any).parseDiagnostics ?? [];
  if (diagnostics.length) {
    const first = diagnostics[0];
    throw new Error(`TypeScript parse error at ${fileName}:${first.start ?? 0}: ${ts.flattenDiagnosticMessageText(first.messageText, " ")}`);
  }
  const erased = collectTypeErasedIntervals(sourceFile);
  const scanner = ts.createScanner(
    ts.ScriptTarget.ESNext,
    true,
    isTsx ? ts.LanguageVariant.JSX : ts.LanguageVariant.Standard,
    text,
  );
  const lineStarts = sourceFile.getLineStarts();
  const tokens: RuntimeToken[] = [];
  for (let kind = scanner.scan(); kind !== ts.SyntaxKind.EndOfFileToken; kind = scanner.scan()) {
    const start = scanner.getTokenPos();
    const end = scanner.getTextPos();
    if (intervalContains(erased, start, end)) continue;
    const startLine = sourceFile.getLineAndCharacterOfPosition(start).line + 1;
    const endLine = sourceFile.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1;
    const lines: number[] = [];
    for (let line = startLine; line <= endLine; line += 1) {
      const lineStart = lineStarts[line - 1] ?? 0;
      const lineEnd = line < lineStarts.length ? lineStarts[line] : text.length;
      const fragment = text.slice(Math.max(start, lineStart), Math.min(end, lineEnd)).replace(/[\r\n]/gu, "");
      if (fragment.trim()) lines.push(line);
    }
    if (lines.length) tokens.push({ kind, raw: text.slice(start, end), start, end, lines });
  }
  return { sourceFile, tokens, lineCount: lineStarts.length };
}

function stableTsKind(sourceFile: ts.SourceFile, startLine: number, endLine: number, fallback: string): string {
  let winner: ts.Node | undefined;
  const visit = (node: ts.Node): void => {
    const start = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    const end = sourceFile.getLineAndCharacterOfPosition(Math.max(node.getStart(sourceFile), node.end - 1)).line + 1;
    if (start === startLine && end === endLine && (!winner || node.end - node.pos < winner.end - winner.pos)) winner = node;
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  const node = winner;
  if (!node) return fallback;
  if (ts.isFunctionDeclaration(node)) return "FunctionDeclaration";
  if (ts.isClassDeclaration(node)) return "ClassDeclaration";
  if (ts.isMethodDeclaration(node)) return "MethodDeclaration";
  if (ts.isConstructorDeclaration(node)) return "Constructor";
  if (ts.isGetAccessorDeclaration(node)) return "GetAccessor";
  if (ts.isSetAccessorDeclaration(node)) return "SetAccessor";
  if (ts.isVariableStatement(node)) return "VariableStatement";
  if (ts.isExpressionStatement(node)) return "ExpressionStatement";
  if (ts.isEnumDeclaration(node)) return "EnumDeclaration";
  return ts.SyntaxKind[node.kind] ?? fallback;
}

function canonicalizeTsTokens(tokens: RuntimeToken[]): string[] {
  const identifiers = new Map<string, string>();
  let next = 0;
  let previous = "";
  return tokens.map((token) => {
    const raw = token.raw.normalize("NFC").replace(/\r\n?/gu, "\n");
    let value: string;
    if (token.kind === ts.SyntaxKind.Identifier) {
      if (previous === "." || previous === "?.") value = `prop:${raw}`;
      else {
        if (!identifiers.has(raw)) identifiers.set(raw, `id${next++}`);
        value = identifiers.get(raw)!;
      }
    } else if (token.kind === ts.SyntaxKind.PrivateIdentifier) value = `prop:${raw}`;
    else if (token.kind === ts.SyntaxKind.NumericLiteral || token.kind === ts.SyntaxKind.BigIntLiteral) {
      value = `num:${raw.replace(/_/gu, "").toLowerCase()}`;
    } else if (
      token.kind === ts.SyntaxKind.StringLiteral ||
      token.kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral ||
      token.kind === ts.SyntaxKind.TemplateHead ||
      token.kind === ts.SyntaxKind.TemplateMiddle ||
      token.kind === ts.SyntaxKind.TemplateTail ||
      token.kind === ts.SyntaxKind.JsxText
    ) value = `str:${raw}`;
    else value = ts.tokenToString(token.kind) ?? raw;
    previous = value;
    return value;
  });
}

function makeShingles(tokens: string[], width = 5): Set<string> {
  if (!tokens.length) return new Set();
  if (tokens.length < width) return new Set([tokens.join("\u001f")]);
  const result = new Set<string>();
  for (let i = 0; i <= tokens.length - width; i += 1) result.add(tokens.slice(i, i + width).join("\u001f"));
  return result;
}

function analyzeTsRange(text: string, fileName: string, startLine: number, endLine: number, requestedKind = "ModuleRange"): RangeAnalysis {
  const scanned = scanRuntimeTokens(text, fileName);
  if (startLine < 1 || endLine < startLine || endLine > scanned.lineCount) throw new Error(`invalid range ${startLine}-${endLine} for ${fileName}`);
  const selected = scanned.tokens.filter((token) => token.lines.some((line) => line >= startLine && line <= endLine));
  const canonical = canonicalizeTsTokens(selected);
  const lineParts = new Map<number, string[]>();
  selected.forEach((token, index) => {
    for (const line of token.lines) {
      if (line < startLine || line > endLine) continue;
      const parts = lineParts.get(line) ?? [];
      parts.push(canonical[index]);
      lineParts.set(line, parts);
    }
  });
  const lineCanonical = new Map<number, string>([...lineParts].map(([line, parts]) => [line, parts.join("\u001f")]));
  const inferredKind = requestedKind === "ModuleRange"
    ? requestedKind
    : stableTsKind(scanned.sourceFile, startLine, endLine, requestedKind);
  const executableLines = [...lineCanonical.keys()].sort((a, b) => a - b);
  const normalizedNodeSerialization = stableJson({
    language: "typescript",
    grammar: "typescript@5.8.3",
    node_kind: inferredKind,
    runtime_tokens: canonical,
  });
  const normalizedNodeHash = canonical.length
    ? sha256Bytes(normalizedNodeSerialization)
    : null;
  return {
    executableSloc: lineCanonical.size,
    executableLines,
    normalizedNodeHash,
    canonicalTokens: canonical,
    lineCanonical,
    tokenCount: canonical.length,
    shingles: makeShingles(canonical),
    inferredKind,
  };
}

const PYTHON_ANALYZER = String.raw`
import ast, hashlib, io, json, keyword, sys, tokenize, unicodedata

if sys.version_info[:2] != (3, 12):
    raise SystemExit("python_version=" + ".".join(map(str, sys.version_info[:3])))

LOCATION_FIELDS = {"lineno", "col_offset", "end_lineno", "end_col_offset"}
TYPE_FIELDS = {"annotation", "returns", "type_comment", "type_params"}


def region(node):
    return range(node.lineno, node.end_lineno + 1)


def is_docstring(node, parent):
    body = getattr(parent, "body", None)
    return (
        isinstance(body, list)
        and bool(body)
        and body[0] is node
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def is_type_checking_test(node):
    return (
        isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"
    ) or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


def classify_erased(tree):
    erased = set()
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    type_alias = getattr(ast, "TypeAlias", ())
    for node in ast.walk(tree):
        parent = parents.get(id(node))
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass, ast.Global, ast.Nonlocal)):
            erased.add(id(node))
        elif type_alias and isinstance(node, type_alias):
            erased.add(id(node))
        elif isinstance(node, ast.AnnAssign) and node.value is None:
            erased.add(id(node))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis:
            erased.add(id(node))
        elif parent is not None and is_docstring(node, parent):
            erased.add(id(node))
        elif isinstance(node, ast.If) and is_type_checking_test(node.test):
            erased.add(id(node))
    return erased


def nearest_statement_children(node):
    result = []
    def visit(child):
        if isinstance(child, ast.stmt):
            result.append(child)
            return
        for nested in ast.iter_child_nodes(child):
            visit(nested)
    for child in ast.iter_child_nodes(node):
        visit(child)
    return result


def nontrivia_lines(source):
    lines = set()
    ignored = {
        tokenize.ENCODING, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT,
        tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT,
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in ignored:
            continue
        for line in range(token.start[0], token.end[0] + 1):
            if token.string.strip():
                lines.add(line)
    return lines


def executable_lines(tree, source, start, end, erased):
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or id(node) in erased:
            continue
        owned = set(region(node))
        for child in nearest_statement_children(node):
            owned.difference_update(region(child))
        lines.update(line for line in owned if start <= line <= end)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                lines.update(line for line in region(decorator) if start <= line <= end)
    return sorted(lines.intersection(nontrivia_lines(source)))


class Canonicalizer:
    def __init__(self, erased):
        self.erased = erased
        self.identifiers = {}

    def identifier(self, value):
        if value not in self.identifiers:
            self.identifiers[value] = "id" + str(len(self.identifiers))
        return self.identifiers[value]

    def value(self, node, field_name=None):
        if isinstance(node, ast.AST):
            if id(node) in self.erased:
                return None
            result = {"node": type(node).__name__}
            for name, value in ast.iter_fields(node):
                if name in LOCATION_FIELDS or name in TYPE_FIELDS or name == "ctx":
                    continue
                if name in {"id", "name", "arg", "asname"} and isinstance(value, str):
                    result[name] = self.identifier(value)
                elif name == "attr" and isinstance(value, str):
                    result[name] = "prop:" + unicodedata.normalize("NFC", value)
                elif name == "module" and isinstance(value, str):
                    result[name] = "module:" + unicodedata.normalize("NFC", value)
                else:
                    normalized = self.value(value, name)
                    if normalized is not None and normalized != []:
                        result[name] = normalized
            return result
        if isinstance(node, list):
            values = [self.value(item, field_name) for item in node]
            return [item for item in values if item is not None]
        if isinstance(node, str):
            return unicodedata.normalize("NFC", node.replace("\r\n", "\n").replace("\r", "\n"))
        if isinstance(node, (int, float, complex, bool)) or node is None:
            return node
        return repr(node)


def select_node(tree, start, end, kind, erased):
    if kind == "ModuleRange":
        body = [
            item for item in tree.body
            if id(item) not in erased and item.end_lineno >= start and item.lineno <= end
        ]
        return ast.Module(body=body, type_ignores=[]), "ModuleRange"
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AST)
        and getattr(node, "lineno", None) == start
        and getattr(node, "end_lineno", None) == end
        and type(node).__name__ == kind
        and id(node) not in erased
    ]
    if matches:
        return matches[-1], kind
    body = [
        item for item in tree.body
        if id(item) not in erased and item.end_lineno >= start and item.lineno <= end
    ]
    return ast.Module(body=body, type_ignores=[]), "ModuleRange"


def analyze(source, start, end, kind):
    tree = ast.parse(source, type_comments=True, feature_version=(3, 12))
    line_count = max(1, len(source.splitlines()))
    if start < 1 or end < start or end > line_count:
        raise ValueError(f"invalid range {start}-{end}, line_count={line_count}")
    erased = classify_erased(tree)
    lines = executable_lines(tree, source, start, end, erased)
    selected, inferred = select_node(tree, start, end, kind, erased)
    canonical_value = Canonicalizer(erased).value(selected)
    serialization = json.dumps(
        {"language": "python", "grammar": "python@3.12", "node_kind": inferred, "ast": canonical_value},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    has_runtime_node = bool(lines) and canonical_value not in (None, {"node": "Module"})
    digest = hashlib.sha256(serialization.encode("utf-8")).hexdigest() if has_runtime_node else None
    source_lines = source.splitlines()
    line_canonical = {
        str(line): unicodedata.normalize("NFC", source_lines[line - 1].strip())
        for line in lines
    }
    return {
        "executable_sloc": len(lines),
        "executable_lines": lines,
        "normalized_node_hash": digest,
        "canonical_serialization": serialization,
        "canonical_tokens": [serialization] if has_runtime_node else [],
        "line_canonical": line_canonical,
        "token_count": 1 if has_runtime_node else 0,
        "inferred_kind": inferred,
    }


payload = json.load(sys.stdin)
output = {"python_version": ".".join(map(str, sys.version_info[:3])), "results": {}}
for file in payload["files"]:
    source = file["source"]
    for item in file["ranges"]:
        output["results"][item["id"]] = analyze(
            source, item["start"], item["end"], item.get("node_kind", "ModuleRange")
        )
json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
`;
function analyzePython(
  pythonBin: string,
  files: Array<{ id: string; source: string; ranges: Array<{ id: string; start: number; end: number; node_kind?: string }> }>,
): Obj {
  const process = Bun.spawnSync([pythonBin, "-c", PYTHON_ANALYZER], {
    stdin: JSON.stringify({ files }),
    stdout: "pipe",
    stderr: "pipe",
  });
  const stderr = new TextDecoder().decode(process.stderr);
  if (process.exitCode !== 0) throw new Error(`Python 3.12 analyzer failed: ${stderr.trim()}`);
  return JSON.parse(new TextDecoder().decode(process.stdout));
}

function snapshot(rows: string[]): string {
  return sha256Bytes(rows.sort(codePointCompare).join(""));
}

function sourceKey(entry: Obj): string {
  const raw = `${entry.source_repo}\0${entry.source_snapshot}\0${entry.source_path}\0${entry.range_start_line}\0${entry.range_end_line}`;
  return `source:${sha256Bytes(raw)}`;
}

function pythonKey(entry: Obj): string {
  const raw = `${entry.baseline_snapshot}\0${entry.python_path}\0${entry.start_line}\0${entry.end_line}`;
  return `python:${sha256Bytes(raw)}`;
}

function sum(items: Obj[], field: string): number {
  return items.reduce((total, item) => total + Number(item[field] ?? 0), 0);
}

function checkOverlap(
  entries: Obj[],
  group: (entry: Obj) => string,
  start: (entry: Obj) => number,
  end: (entry: Obj) => number,
  location: (entry: Obj) => string,
  code: string,
  findings: Finding[],
): void {
  const groups = new Map<string, Obj[]>();
  for (const entry of entries) groups.set(group(entry), [...(groups.get(group(entry)) ?? []), entry]);
  for (const values of groups.values()) {
    values.sort((a, b) => start(a) - start(b) || end(a) - end(b));
    for (let i = 1; i < values.length; i += 1) {
      if (start(values[i]) <= end(values[i - 1])) {
        findings.push({ code, location: location(values[i]), message: `range overlaps ${location(values[i - 1])}` });
      }
    }
  }
}

function loadManifest(path: string, schema: Obj, findings: Finding[]): LoadedManifest | null {
  let records: Obj[];
  try {
    records = readRecords(path);
  } catch (error) {
    findings.push({ code: "manifest_parse", location: path, message: String(error) });
    return null;
  }
  if (!records.length) {
    findings.push({ code: "manifest_empty", location: path, message: "manifest has no records" });
    return null;
  }
  const visitNoSplit = (value: any, at: string): void => {
    if (Array.isArray(value)) value.forEach((item, index) => visitNoSplit(item, `${at}[${index}]`));
    else if (value && typeof value === "object") for (const [key, item] of Object.entries(value)) {
      if (["decision", "disposition", "status"].includes(key) && item === "split") {
        findings.push({ code: "split_forbidden", location: `${at}.${key}`, message: "schema-v2 has no split state" });
      }
      visitNoSplit(item, `${at}.${key}`);
    }
  };
  records.forEach((record, index) => {
    visitNoSplit(record, `${path}:${index + 1}`);
    if ("range_start_line" in record && "source_repo" in record) {
      const expectedRole = PRIMARY_ROLE[record.source_repo];
      if (!expectedRole) findings.push({ code: "production_source_repo", location: `${path}:${index + 1}`, message: `${record.source_repo} is conformance/reference-only` });
      if (record.source_role !== expectedRole) findings.push({ code: "source_role", location: `${path}:${index + 1}`, message: `expected source_role=${expectedRole}` });
      const expectedCredit = record.disposition === "accepted" && Boolean(expectedRole);
      if (record.production_credit !== expectedCredit) findings.push({ code: "production_credit", location: `${path}:${index + 1}`, message: `production_credit must be ${expectedCredit}` });
    }
  });
  records.forEach((record, index) => {
    for (const message of validateSchema(record, schema, schema, `$[${index}]`)) {
      findings.push({ code: "manifest_schema", location: `${path}:${index + 1}`, message });
    }
  });
  const metadata = records[0];
  if (metadata.record_type !== "metadata") findings.push({ code: "metadata_first", location: `${path}:1`, message: "first record must be metadata" });
  if (records.slice(1).some((record) => record.record_type === "metadata")) {
    findings.push({ code: "metadata_multiple", location: path, message: "metadata must occur exactly once" });
  }
  return { path, metadata, entries: records.slice(1) };
}

async function verifyManifests(args: Args, schema: Obj, findings: Finding[]): Promise<Obj> {
  const loaded = args.manifests.map((path) => loadManifest(path, schema, findings)).filter(Boolean) as LoadedManifest[];
  const pairKeys = new Set<string>();
  for (const manifest of loaded) {
    const key = `${manifest.metadata.execution_id}:${manifest.metadata.manifest_kind}`;
    if (pairKeys.has(key)) findings.push({ code: "duplicate_manifest", location: manifest.path, message: `duplicate ${key}` });
    pairKeys.add(key);
    for (const entry of manifest.entries) {
      if (entry.execution_id !== manifest.metadata.execution_id) findings.push({ code: "execution_id_mismatch", location: manifest.path, message: "entry execution_id differs from metadata" });
      const expectedType = manifest.metadata.manifest_kind === "source" ? "source_range" : "python_symbol";
      if (entry.record_type !== expectedType) findings.push({ code: "record_kind_mismatch", location: manifest.path, message: `expected ${expectedType}` });
    }
  }
  if (args.requireCompleteSet) {
    for (const execution of EXECUTIONS) for (const kind of ["source", "python_owner"]) {
      if (!pairKeys.has(`${execution}:${kind}`)) findings.push({ code: "complete_set_missing", location: "--manifest", message: `missing ${execution}:${kind}` });
    }
    if (pairKeys.size !== 6) findings.push({ code: "complete_set_size", location: "--manifest", message: `expected exactly 6 manifests, got ${pairKeys.size}` });
  }
  const commits = new Set(loaded.map((item) => item.metadata.zyra_baseline_commit));
  if (commits.size > 1) findings.push({ code: "baseline_commit_mismatch", location: "manifests", message: "all v2 manifests must share one Zyra baseline commit" });

  const sourceEntries: Obj[] = [];
  const pythonEntries: Obj[] = [];
  const sourceFileCache = new Map<string, { text: string; hash: string; lineCount: number }>();
  const pythonFiles = new Map<string, { id: string; source: string; hash: string; ranges: any[] }>();
  const sourceByExecution: Record<string, any> = {};
  const pythonByExecution: Record<string, any> = {};

  for (const manifest of loaded) {
    const metadata = manifest.metadata;
    if (metadata.manifest_kind === "source") {
      const decisions = metadata.repository_decisions ?? [];
      const decisionRepos = new Set(decisions.map((item: Obj) => item.source_repo));
      if (decisionRepos.size !== 3 || [...SOURCE_REPOS].some((repo) => !decisionRepos.has(repo))) {
        findings.push({ code: "repository_decisions", location: manifest.path, message: "repository_decisions must contain each production repo exactly once" });
      }
      for (const decision of decisions) {
        if (decision.source_role !== PRIMARY_ROLE[decision.source_repo]) findings.push({ code: "source_role", location: manifest.path, message: `${decision.source_repo} must be ${PRIMARY_ROLE[decision.source_repo]}` });
      }
      const comparisons = metadata.comparison_decisions ?? [];
      const comparisonMap = new Map(comparisons.map((item: Obj) => [item.comparison_repo, item]));
      if (comparisonMap.get("hermes-agent")?.source_role !== "conformance_only" || comparisonMap.get("hermes-agent")?.production_credit !== 0) {
        findings.push({ code: "hermes_role", location: manifest.path, message: "Hermes must remain zero-credit conformance_only" });
      }
      if (comparisonMap.get("oh-my-pi")?.source_role !== "reference_only" || comparisonMap.get("oh-my-pi")?.production_credit !== 0) {
        findings.push({ code: "omp_role", location: manifest.path, message: "oh-my-pi must remain zero-credit reference_only" });
      }
      for (const entry of manifest.entries) {
        sourceEntries.push(entry);
        const normalized = normalizeRelativePath(entry.source_path, entry.source_repo);
        const root = args.sourceRoots.get(entry.source_repo);
        if (!normalized || !root) {
          findings.push({ code: "source_path_or_root", location: `${manifest.path}:${entry.source_path}`, message: "invalid root-relative source path or missing --source-root" });
          continue;
        }
        const absolute = safeFile(root, normalized);
        if (!absolute) {
          findings.push({ code: "source_file", location: `${entry.source_repo}:${normalized}`, message: "file missing or escapes source root" });
          continue;
        }
        const cacheKey = `${entry.source_repo}:${normalized}`;
        let file = sourceFileCache.get(cacheKey);
        if (!file) {
          const bytes = readFileSync(absolute);
          const text = bytes.toString("utf8").replace(/^\uFEFF/, "");
          file = { text, hash: sha256Bytes(bytes), lineCount: Math.max(1, text.split(/\r?\n/u).length - (text.endsWith("\n") ? 1 : 0)) };
          sourceFileCache.set(cacheKey, file);
        }
        if (file.hash !== entry.source_file_sha256) findings.push({ code: "source_hash", location: cacheKey, message: `expected ${entry.source_file_sha256}, got ${file.hash}` });
        if ((entry.source_language === "TSX") !== normalized.endsWith(".tsx")) findings.push({ code: "source_language", location: cacheKey, message: "source_language does not match extension" });
        if (entry.source_start_line > entry.range_start_line || entry.source_end_line < entry.range_end_line || entry.range_end_line > file.lineCount) {
          findings.push({ code: "source_range", location: sourceKey(entry), message: "range is outside symbol/file bounds" });
          continue;
        }
        try {
          const actual = analyzeTsRange(file.text, normalized, entry.range_start_line, entry.range_end_line).executableSloc;
          if (actual !== entry.raw_executable_sloc) findings.push({ code: "source_sloc", location: sourceKey(entry), message: `manifest ${entry.raw_executable_sloc}, actual ${actual}` });
          const accepted = entry.disposition === "accepted" ? actual : 0;
          if (entry.accepted_executable_sloc !== accepted) findings.push({ code: "accepted_sloc", location: sourceKey(entry), message: `expected ${accepted}` });
          if (entry.disposition === "accepted" && actual === 0) findings.push({ code: "empty_accepted_range", location: sourceKey(entry), message: "accepted range has zero executable SLOC" });
        } catch (error) {
          findings.push({ code: "source_parse", location: cacheKey, message: String(error) });
        }
      }
      const uniqueFiles = new Map<string, string>();
      for (const entry of manifest.entries) uniqueFiles.set(`${entry.source_repo}:${entry.source_path}`, entry.source_file_sha256);
      const globalSnapshot = snapshot([...uniqueFiles].map(([key, hash]) => {
        const split = key.indexOf(":");
        return `${key.slice(0, split)}\0${key.slice(split + 1)}\0${hash}\n`;
      }));
      if (metadata.snapshot !== globalSnapshot) findings.push({ code: "source_snapshot", location: manifest.path, message: `metadata snapshot must be ${globalSnapshot}` });
      const inputs = new Map((metadata.repository_inputs ?? []).map((item: Obj) => [item.source_repo, item]));
      for (const repo of SOURCE_REPOS) {
        const entries = manifest.entries.filter((item) => item.source_repo === repo);
        const decision = decisions.find((item: Obj) => item.source_repo === repo);
        if (decision?.status === "included" && !entries.length) findings.push({ code: "included_repo_empty", location: manifest.path, message: `${repo} is included but has no ranges` });
        if (decision?.status === "not_applicable" && entries.length) findings.push({ code: "not_applicable_has_ranges", location: manifest.path, message: `${repo} has ranges despite not_applicable` });
        if (!entries.length) {
          if (inputs.has(repo)) findings.push({ code: "unexpected_repository_input", location: manifest.path, message: `${repo} input exists without ranges` });
          continue;
        }
        const files = new Map(entries.map((item) => [item.source_path, item.source_file_sha256]));
        const repoSnapshot = snapshot([...files].map(([path, hash]) => `${path}\0${hash}\n`));
        for (const entry of entries) if (entry.source_snapshot !== repoSnapshot || entry.manifest_snapshot !== globalSnapshot) {
          findings.push({ code: "entry_snapshot", location: sourceKey(entry), message: "entry snapshot does not match recomputed manifest/repository snapshot" });
        }
        const input = inputs.get(repo);
        const expected = {
          file_count: files.size,
          range_count: entries.length,
          raw_executable_sloc: sum(entries, "raw_executable_sloc"),
          accepted_executable_sloc: sum(entries, "accepted_executable_sloc"),
          rejected_executable_sloc: sum(entries.filter((item) => item.disposition === "rejected"), "raw_executable_sloc"),
        };
        if (!input || input.snapshot !== repoSnapshot || input.source_role !== PRIMARY_ROLE[repo] || Object.entries(expected).some(([key, value]) => input[key] !== value)) {
          findings.push({ code: "repository_input_totals", location: `${manifest.path}:${repo}`, message: `repository input must equal ${JSON.stringify({ snapshot: repoSnapshot, ...expected })}` });
        }
      }
      const totals = {
        file_count: uniqueFiles.size,
        entry_count: manifest.entries.length,
        raw_executable_sloc: sum(manifest.entries, "raw_executable_sloc"),
        accepted_executable_sloc: sum(manifest.entries, "accepted_executable_sloc"),
        rejected_executable_sloc: sum(manifest.entries.filter((item) => item.disposition === "rejected"), "raw_executable_sloc"),
      };
      if (!sameJson(metadata.totals, totals)) findings.push({ code: "source_metadata_totals", location: manifest.path, message: `expected ${JSON.stringify(totals)}` });
      sourceByExecution[metadata.execution_id] = totals;
    } else if (metadata.manifest_kind === "python_owner") {
      const root = args.baselineRoot ?? args.zyraRoot;
      for (const entry of manifest.entries) {
        pythonEntries.push(entry);
        const normalized = normalizeRelativePath(entry.python_path, "zyra");
        if (!normalized || !normalized.endsWith(".py")) {
          findings.push({ code: "python_path", location: `${manifest.path}:${entry.python_path}`, message: "python_path must be Zyra-root-relative .py path" });
          continue;
        }
        const absolute = safeFile(root, normalized);
        if (!absolute) {
          findings.push({ code: "python_file", location: normalized, message: "file missing or escapes baseline root" });
          continue;
        }
        const bytes = readFileSync(absolute);
        const hash = sha256Bytes(bytes);
        if (hash !== entry.file_sha256) findings.push({ code: "python_hash", location: normalized, message: `expected ${entry.file_sha256}, got ${hash}` });
        const existing = pythonFiles.get(normalized);
        if (existing && existing.hash !== hash) findings.push({ code: "python_cross_execution_hash", location: normalized, message: "same Python path has different baseline hashes" });
        const file = existing ?? { id: normalized, source: bytes.toString("utf8").replace(/^\uFEFF/, ""), hash, ranges: [] };
        file.ranges.push({ id: pythonKey(entry), start: entry.start_line, end: entry.end_line, node_kind: "ModuleRange" });
        pythonFiles.set(normalized, file);
      }
      const uniqueFiles = new Map(manifest.entries.map((item) => [item.python_path, item.file_sha256]));
      const baselineSnapshot = snapshot([...uniqueFiles].map(([path, hash]) => `${path}\0${hash}\n`));
      if (metadata.snapshot !== baselineSnapshot) findings.push({ code: "python_snapshot", location: manifest.path, message: `metadata snapshot must be ${baselineSnapshot}` });
      for (const entry of manifest.entries) if (entry.baseline_snapshot !== baselineSnapshot) findings.push({ code: "python_entry_snapshot", location: pythonKey(entry), message: "entry baseline_snapshot mismatch" });
      const totals = {
        file_count: uniqueFiles.size,
        entry_count: manifest.entries.length,
        executable_sloc: sum(manifest.entries, "executable_sloc"),
        delete_executable_sloc: sum(manifest.entries.filter((item) => item.decision === "delete"), "executable_sloc"),
        retain_executable_sloc: sum(manifest.entries.filter((item) => item.decision === "retain"), "executable_sloc"),
        blocked_executable_sloc: sum(manifest.entries.filter((item) => item.decision === "blocked"), "executable_sloc"),
      };
      if (!sameJson(metadata.totals, totals)) findings.push({ code: "python_metadata_totals", location: manifest.path, message: `expected ${JSON.stringify(totals)}` });
      pythonByExecution[metadata.execution_id] = totals;
    }
  }

  checkOverlap(sourceEntries, (item) => `${item.source_repo}\0${item.source_path}`, (item) => item.range_start_line, (item) => item.range_end_line, sourceKey, "source_overlap", findings);
  checkOverlap(pythonEntries, (item) => item.python_path, (item) => item.start_line, (item) => item.end_line, pythonKey, "python_overlap", findings);
  for (const execution of ["02", "03"]) for (const repo of ["opencode", "openclaw"]) {
    const accepted = sourceEntries.some(
      (item) => item.execution_id === execution && item.source_repo === repo && item.disposition === "accepted" && item.production_credit === true,
    );
    if (!accepted) findings.push({ code: "mandatory_supplementary_source", location: `execution-${execution}:${repo}`, message: "supplementary source has no accepted production-credit range" });
  }
  if (pythonFiles.size) {
    try {
      const result = analyzePython(args.pythonBin, [...pythonFiles.values()].map((item) => ({ id: item.id, source: item.source, ranges: item.ranges })));
      if (!String(result.python_version).startsWith("3.12.")) findings.push({ code: "python_version", location: args.pythonBin, message: `expected 3.12.x, got ${result.python_version}` });
      for (const entry of pythonEntries) {
        const actual = result.results[pythonKey(entry)]?.executable_sloc;
        if (actual !== entry.executable_sloc) findings.push({ code: "python_sloc", location: pythonKey(entry), message: `manifest ${entry.executable_sloc}, actual ${actual}` });
      }
    } catch (error) {
      findings.push({ code: "python_analyzer", location: args.pythonBin, message: String(error) });
    }
  }
  const sourceKeys = sourceEntries.map(sourceKey);
  const pythonKeys = pythonEntries.map(pythonKey);
  if (new Set(sourceKeys).size !== sourceKeys.length) findings.push({ code: "source_unique_key", location: "manifests", message: "duplicate source unique key" });
  if (new Set(pythonKeys).size !== pythonKeys.length) findings.push({ code: "python_unique_key", location: "manifests", message: "duplicate Python unique key" });
  const acceptedAnalyses: Array<{ key: string; analysis: RangeAnalysis }> = [];
  for (const entry of sourceEntries.filter((item) => item.disposition === "accepted" && item.production_credit === true)) {
    const root = args.sourceRoots.get(entry.source_repo);
    const normalized = normalizeRelativePath(entry.source_path, entry.source_repo);
    if (!root || !normalized) continue;
    const absolute = safeFile(root, normalized);
    if (!absolute) continue;
    try {
      acceptedAnalyses.push({
        key: sourceKey(entry),
        analysis: analyzeTsRange(readFileSync(absolute, "utf8").replace(/^\uFEFF/, ""), normalized, entry.range_start_line, entry.range_end_line, entry.node_kind ?? "ModuleRange"),
      });
    } catch {
      // The primary source pass already emitted the parse/path finding.
    }
  }
  const acceptedDuplicateLosers = duplicateLosers(acceptedAnalyses, 0.90);
  const acceptedExecutableSlocUnion = acceptedAnalyses.reduce(
    (total, item) => total + (acceptedDuplicateLosers.has(item.key) ? 0 : item.analysis.executableSloc),
    0,
  );
  let deleteExecutableSlocUnion = 0;
  const deletedNodeHashes = new Set<string>();
  try {
    const pythonAnalysis = analyzePython(
      args.pythonBin,
      [...pythonFiles.values()].map((item) => ({ id: item.id, source: item.source, ranges: item.ranges })),
    );
    for (const entry of pythonEntries.filter((item) => item.decision === "delete")) {
      const result = pythonAnalysis.results[pythonKey(entry)];
      if (!result) continue;
      const identity = result.normalized_node_hash ?? pythonKey(entry);
      if (deletedNodeHashes.has(identity)) continue;
      deletedNodeHashes.add(identity);
      deleteExecutableSlocUnion += result.executable_sloc;
    }
  } catch {
    // The primary Python pass already emitted the analyzer finding.
  }
  return {
    source: {
      by_execution: sourceByExecution,
      accepted_executable_sloc_union: acceptedExecutableSlocUnion,
      duplicate_or_move_ranges_excluded: acceptedDuplicateLosers.size,
      unique_range_count: new Set(sourceKeys).size,
    },
    python: {
      by_execution: pythonByExecution,
      delete_executable_sloc_union: deleteExecutableSlocUnion,
      duplicate_or_move_ranges_excluded: pythonEntries.filter((item) => item.decision === "delete").length - deletedNodeHashes.size,
      unique_range_count: new Set(pythonKeys).size,
    },
    accepted_source_keys: sourceEntries.filter((item) => item.disposition === "accepted").map(sourceKey),
    python_keys: pythonEntries.map(pythonKey),
  };
}

function jaccard(left: Set<string>, right: Set<string>): number {
  if (!left.size && !right.size) return 1;
  let intersection = 0;
  for (const value of left) if (right.has(value)) intersection += 1;
  return intersection / (left.size + right.size - intersection);
}

class UnionFind {
  parent: number[];
  constructor(size: number) { this.parent = Array.from({ length: size }, (_, index) => index); }
  find(value: number): number { return this.parent[value] === value ? value : (this.parent[value] = this.find(this.parent[value])); }
  union(left: number, right: number): void { const a = this.find(left); const b = this.find(right); if (a !== b) this.parent[Math.max(a, b)] = Math.min(a, b); }
}

function duplicateLosers(items: Array<{ key: string; analysis: RangeAnalysis }>, threshold: number): Set<string> {
  const union = new UnionFind(items.length);
  for (let i = 0; i < items.length; i += 1) for (let j = 0; j < i; j += 1) {
    const a = items[i].analysis;
    const b = items[j].analysis;
    const exactEligible = Math.min(a.tokenCount, b.tokenCount) >= 8 || Math.min(a.executableSloc, b.executableSloc) >= 2;
    const exact = exactEligible && a.normalizedNodeHash === b.normalizedNodeHash;
    const nearEligible = Math.min(a.tokenCount, b.tokenCount) >= 20 && Math.min(a.executableSloc, b.executableSloc) >= 5;
    if (exact || nearEligible && jaccard(a.shingles, b.shingles) >= threshold) union.union(i, j);
  }
  const groups = new Map<number, number[]>();
  items.forEach((_, index) => groups.set(union.find(index), [...(groups.get(union.find(index)) ?? []), index]));
  const losers = new Set<string>();
  for (const values of groups.values()) {
    if (values.length < 2) continue;
    values.sort((a, b) => codePointCompare(items[a].key, items[b].key));
    values.slice(1).forEach((index) => losers.add(items[index].key));
  }
  return losers;
}

function collectBaselineCanonicalHashes(root: string, findings: Finding[]): Set<string> {
  const hashes = new Set<string>();
  const skipped = new Set(["node_modules", "dist", "build", "coverage", "vendor", "vendor-runtimes", "source-pool", "runtime-sources", ".git"]);
  const visitFile = (absolute: string): void => {
    const rel = relative(root, absolute).split(sep).join("/");
    const text = readFileSync(absolute, "utf8").replace(/^\uFEFF/, "");
    const lineCount = Math.max(1, text.split(/\r?\n/u).length - (text.endsWith("\n") ? 1 : 0));
    try {
      const module = analyzeTsRange(text, rel, 1, lineCount, "ModuleRange");
      if (module.normalizedNodeHash) hashes.add(module.normalizedNodeHash);
      const sourceFile = ts.createSourceFile(rel, text, ts.ScriptTarget.ESNext, true, rel.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
      const visitNode = (node: ts.Node): void => {
        const stable =
          ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isMethodDeclaration(node) ||
          ts.isConstructorDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node) ||
          ts.isVariableStatement(node) || ts.isExpressionStatement(node) || ts.isEnumDeclaration(node);
        if (stable) {
          const start = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
          const end = sourceFile.getLineAndCharacterOfPosition(Math.max(node.getStart(sourceFile), node.end - 1)).line + 1;
          const kind = stableTsKind(sourceFile, start, end, ts.SyntaxKind[node.kind] ?? "ModuleRange");
          const analysis = analyzeTsRange(text, rel, start, end, kind);
          if (analysis.normalizedNodeHash) hashes.add(analysis.normalizedNodeHash);
        }
        ts.forEachChild(node, visitNode);
      };
      visitNode(sourceFile);
    } catch (error) {
      findings.push({ code: "baseline_inventory_parse", location: rel, message: String(error) });
    }
  };
  const walk = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue;
      const absolute = join(directory, entry.name);
      if (entry.isDirectory()) {
        if (!skipped.has(entry.name)) walk(absolute);
      } else if (entry.isFile() && /\.tsx?$/u.test(entry.name)) visitFile(absolute);
    }
  };
  walk(root);
  return hashes;
}

function excludedProductionPath(path: string): boolean {
  const lower = `/${path.toLowerCase()}/`;
  if (!/^(apps|packages|skills)\//u.test(path)) return true;
  return ["/vendor/", "/vendor-runtimes/", "/source-pool/", "/runtime-sources/", "/node_modules/", "/dist/", "/build/", "/coverage/", "/generated/", "/fixtures/", "/__snapshots__/", "/docs/"].some((part) => lower.includes(part));
}

async function verifyEvidence(args: Args, schema: Obj, manifestReport: Obj, findings: Finding[]): Promise<Obj | null> {
  if (!args.evidence.length) return null;
  const records: Obj[] = [];
  for (const path of args.evidence) {
    try {
      const loaded = readRecords(path);
      loaded.forEach((record, index) => {
        for (const message of validateSchema(record, schema, schema, `$[${index}]`)) findings.push({ code: "evidence_schema", location: `${path}:${index + 1}`, message });
      });
      records.push(...loaded);
    } catch (error) {
      findings.push({ code: "evidence_parse", location: path, message: String(error) });
    }
  }
  const unique = new Set<string>();
  for (const record of records) {
    if (unique.has(record.unique_key)) findings.push({ code: "evidence_unique_key", location: record.unique_key, message: "duplicate evidence unique_key" });
    unique.add(record.unique_key);
  }
  const acceptedSources = new Set(manifestReport.accepted_source_keys ?? []);
  const pythonKeys = new Set(manifestReport.python_keys ?? []);
  const targetRecords = records.filter((item) => item.record_type === "target_owner");
  const targetKeys = new Set(targetRecords.map((item) => item.unique_key));
  const behaviorIds = new Set(records.filter((item) => item.record_type === "behavior_case").map((item) => item.behavior_id));
  for (const record of records.filter((item) => item.record_type === "source_target_map")) {
    if (!acceptedSources.has(record.source_range_key)) findings.push({ code: "source_target_source_ref", location: record.unique_key, message: "source_range_key is not an accepted manifest key" });
    if (!targetKeys.has(record.target_owner_key) || !targetKeys.has(record.default_callsite_target_key)) findings.push({ code: "source_target_target_ref", location: record.unique_key, message: "target reference missing" });
    for (const id of record.behavior_ids ?? []) if (!behaviorIds.has(id)) findings.push({ code: "source_target_behavior_ref", location: record.unique_key, message: `missing behavior ${id}` });
  }
  for (const record of records.filter((item) => item.record_type === "python_owner_result")) {
    if (!pythonKeys.has(record.python_symbol_key)) findings.push({ code: "python_result_ref", location: record.unique_key, message: "python_symbol_key missing from manifest" });
  }
  const analyses: Array<{ record: Obj; key: string; analysis: RangeAnalysis; lineKeys: Set<string> }> = [];
  const fileHashes = new Map<string, string>();
  for (const record of targetRecords) {
    const normalized = normalizeRelativePath(record.target_path, "zyra");
    if (!normalized || !/\.tsx?$/u.test(normalized)) {
      findings.push({ code: "target_path", location: record.unique_key, message: "target path must be Zyra-root-relative TS/TSX" });
      continue;
    }
    const absolute = safeFile(args.zyraRoot, normalized);
    if (!absolute) {
      findings.push({ code: "target_file", location: record.unique_key, message: "target file missing or root escape" });
      continue;
    }
    const bytes = readFileSync(absolute);
    const hash = sha256Bytes(bytes);
    if (hash !== record.target_file_sha256) findings.push({ code: "target_hash", location: record.unique_key, message: `expected ${record.target_file_sha256}, got ${hash}` });
    if (fileHashes.has(normalized) && fileHashes.get(normalized) !== hash) findings.push({ code: "target_hash_conflict", location: normalized, message: "target file has inconsistent hashes" });
    fileHashes.set(normalized, hash);
    try {
      const analysis = analyzeTsRange(bytes.toString("utf8").replace(/^\uFEFF/, ""), normalized, record.start_line, record.end_line, record.node_kind);
      if (record.node_kind !== "ModuleRange" && analysis.inferredKind !== record.node_kind) findings.push({ code: "target_node_kind", location: record.unique_key, message: `declared ${record.node_kind}, inferred ${analysis.inferredKind}` });
      if (analysis.normalizedNodeHash !== record.normalized_node_hash) findings.push({ code: "target_node_hash", location: record.unique_key, message: `expected ${analysis.normalizedNodeHash}` });
      if (analysis.executableSloc !== record.reported_executable_sloc) findings.push({ code: "target_sloc", location: record.unique_key, message: `reported ${record.reported_executable_sloc}, actual ${analysis.executableSloc}` });
      if (record.bucket === "production" && (excludedProductionPath(normalized) || !record.default_reachable || record.test_classification !== "not_test")) {
        findings.push({ code: "invalid_production_bucket", location: record.unique_key, message: "production range is excluded, unreachable, or test-classified" });
      }
      if (record.bucket === "test" && !["case_body", "case_specific_fault_setup"].includes(record.test_classification)) {
        findings.push({ code: "invalid_test_bucket", location: record.unique_key, message: "effective test range must be a case body or case-specific fault setup" });
      }
      if (record.bucket === "test" && !(record.behavior_ids ?? []).length) findings.push({ code: "test_behavior_missing", location: record.unique_key, message: "test range has no behavior reference" });
      const lineKeys = new Set([...analysis.lineCanonical.keys()].map((line) => `${record.target_snapshot}\0${normalized}\0${line}`));
      analyses.push({ record, key: `${normalized}\0${String(record.start_line).padStart(9, "0")}\0${String(record.end_line).padStart(9, "0")}\0${record.unique_key}`, analysis, lineKeys });
    } catch (error) {
      findings.push({ code: "target_parse", location: record.unique_key, message: String(error) });
    }
  }
  checkOverlap(targetRecords, (item) => item.target_path, (item) => item.start_line, (item) => item.end_line, (item) => item.unique_key, "target_overlap", findings);
  const production = analyses.filter((item) => item.record.bucket === "production" && item.record.default_reachable && item.record.test_classification === "not_test" && !excludedProductionPath(item.record.target_path));
  const tests = analyses.filter((item) => item.record.bucket === "test" && ["case_body", "case_specific_fault_setup"].includes(item.record.test_classification));
  const adapters = analyses.filter((item) => item.record.bucket === "adapter");
  const glue = analyses.filter((item) => item.record.bucket === "glue");
  const errorHandling = analyses.filter((item) => item.record.bucket === "error_handling");
  const prodLosers = duplicateLosers(production, 0.90);
  const testLosers = duplicateLosers(tests, 0.80);
  const finalByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  const testByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  const adapterByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  const glueByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  const errorByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  for (const item of production) if (!prodLosers.has(item.key)) for (const line of item.lineKeys) finalByExecution[item.record.execution_id].add(line);
  for (const item of tests) if (!testLosers.has(item.key)) for (const line of item.lineKeys) testByExecution[item.record.execution_id].add(line);
  for (const item of adapters) for (const line of item.lineKeys) adapterByExecution[item.record.execution_id].add(line);
  for (const item of glue) for (const line of item.lineKeys) glueByExecution[item.record.execution_id].add(line);
  for (const item of errorHandling) for (const line of item.lineKeys) errorByExecution[item.record.execution_id].add(line);

  const changedByExecution: Record<string, Set<string>> = { "01": new Set(), "02": new Set(), "03": new Set() };
  if (args.baselineRoot) {
    const baselineCanonicalHashes = collectBaselineCanonicalHashes(args.baselineRoot, findings);
    for (const item of production) {
      if (prodLosers.has(item.key)) continue;
      const rel = item.record.target_path;
      const baselineFile = safeFile(args.baselineRoot, rel);
      let changedLines = item.analysis.normalizedNodeHash && baselineCanonicalHashes.has(item.analysis.normalizedNodeHash)
        ? new Set<number>()
        : new Set(item.analysis.lineCanonical.keys());
      if (baselineFile && changedLines.size) {
        try {
          const baselineText = readFileSync(baselineFile, "utf8").replace(/^\uFEFF/, "");
          const baselineLineCount = Math.max(1, baselineText.split(/\r?\n/u).length - (baselineText.endsWith("\n") ? 1 : 0));
          const baseline = analyzeTsRange(baselineText, rel, 1, baselineLineCount, item.record.node_kind);
          if (baseline.normalizedNodeHash === item.analysis.normalizedNodeHash) changedLines.clear();
          else if (jaccard(baseline.shingles, item.analysis.shingles) >= 0.60) {
            const available = new Map<string, number>();
            for (const value of baseline.lineCanonical.values()) available.set(value, (available.get(value) ?? 0) + 1);
            changedLines = new Set();
            for (const [line, value] of item.analysis.lineCanonical) {
              const count = available.get(value) ?? 0;
              if (count) available.set(value, count - 1);
              else changedLines.add(line);
            }
          }
        } catch (error) {
          findings.push({ code: "baseline_parse", location: rel, message: String(error) });
        }
      }
      if (changedLines.size !== item.record.reported_changed_sloc) findings.push({ code: "changed_sloc", location: item.record.unique_key, message: `reported ${item.record.reported_changed_sloc}, actual ${changedLines.size}` });
      for (const line of changedLines) changedByExecution[item.record.execution_id].add(`${item.record.target_snapshot}\0${rel}\0${line}`);
    }
  } else if (targetRecords.length) {
    findings.push({ code: "baseline_root_missing", location: "--baseline-root", message: "target changed union requires a clean baseline root" });
  }
  const perExecution: Obj = {};
  for (const execution of EXECUTIONS as Set<ExecutionId>) {
    const changed = changedByExecution[execution].size;
    const adapter = adapterByExecution[execution].size;
    const ratio = changed + adapter === 0 ? 1 : adapter / (changed + adapter);
    perExecution[execution] = {
      effective_final_sloc: finalByExecution[execution].size,
      effective_changed_sloc: changed,
      effective_test_sloc: testByExecution[execution].size,
      adapter_executable_sloc: adapter,
      glue_executable_sloc: glueByExecution[execution].size,
      error_handling_executable_sloc: errorByExecution[execution].size,
      adapter_ratio: ratio,
    };
    if (ratio > 0.10) findings.push({ code: "adapter_ratio", location: `execution-${execution}`, message: `adapter ratio ${ratio} exceeds 0.10` });
    if (args.enforceGates) {
      if (finalByExecution[execution].size < FINAL_FLOORS[execution]) findings.push({ code: "final_floor", location: `execution-${execution}`, message: `${finalByExecution[execution].size} < ${FINAL_FLOORS[execution]}` });
      if (changed < CHANGED_FLOORS[execution]) findings.push({ code: "changed_floor", location: `execution-${execution}`, message: `${changed} < ${CHANGED_FLOORS[execution]}` });
      if (testByExecution[execution].size < TEST_FLOORS[execution]) findings.push({ code: "test_floor", location: `execution-${execution}`, message: `${testByExecution[execution].size} < ${TEST_FLOORS[execution]}` });
    }
  }
  return {
    record_count: records.length,
    target_owner_count: targetRecords.length,
    per_execution: perExecution,
    union: {
      effective_final_sloc: new Set([...finalByExecution["01"], ...finalByExecution["02"], ...finalByExecution["03"]]).size,
      effective_changed_sloc: new Set([...changedByExecution["01"], ...changedByExecution["02"], ...changedByExecution["03"]]).size,
      effective_test_sloc: new Set([...testByExecution["01"], ...testByExecution["02"], ...testByExecution["03"]]).size,
    },
    duplicate_excluded: { production: prodLosers.size, test: testLosers.size },
  };
}

async function runGolden(args: Args, findings: Finding[]): Promise<Obj> {
  const fixture = readJson(args.goldenPath);
  const tsResults = new Map<string, RangeAnalysis>();
  for (const item of fixture.typescript_cases ?? []) {
    const lineCount = Math.max(1, item.source.split(/\r?\n/u).length - (item.source.endsWith("\n") ? 1 : 0));
    const result = analyzeTsRange(item.source, `${item.case_id}.ts`, 1, lineCount, item.node_kind);
    tsResults.set(item.case_id, result);
    if (result.executableSloc !== item.expected_executable_sloc) findings.push({ code: "golden_ts_sloc", location: item.case_id, message: `${result.executableSloc} != ${item.expected_executable_sloc}` });
    if (!sameJson(result.executableLines, item.expected_executable_lines)) findings.push({ code: "golden_ts_lines", location: item.case_id, message: `${JSON.stringify(result.executableLines)} != ${JSON.stringify(item.expected_executable_lines)}` });
    if (typeof item.expected_normalized_node_hash === "string" && result.normalizedNodeHash !== item.expected_normalized_node_hash) {
      findings.push({ code: "golden_ts_hash", location: item.case_id, message: `${result.normalizedNodeHash} != ${item.expected_normalized_node_hash}` });
    }
  }
  const pythonFiles = (fixture.python_cases ?? []).map((item: Obj) => ({
    id: item.case_id,
    source: item.source,
    ranges: [{ id: item.case_id, start: 1, end: Math.max(1, item.source.split(/\r?\n/u).length - (item.source.endsWith("\n") ? 1 : 0)), node_kind: item.node_kind }],
  }));
  let pythonResult: Obj = { results: {} };
  const pythonResults = new Map<string, Obj>();
  try {
    pythonResult = analyzePython(args.pythonBin, pythonFiles);
    for (const item of fixture.python_cases ?? []) {
      const result = pythonResult.results[item.case_id];
      pythonResults.set(item.case_id, result);
      if (result.executable_sloc !== item.expected_executable_sloc) findings.push({ code: "golden_python_sloc", location: item.case_id, message: `${result.executable_sloc} != ${item.expected_executable_sloc}` });
      if (!sameJson(result.executable_lines, item.expected_executable_lines)) findings.push({ code: "golden_python_lines", location: item.case_id, message: `${JSON.stringify(result.executable_lines)} != ${JSON.stringify(item.expected_executable_lines)}` });
      if (typeof item.expected_normalized_node_hash === "string" && result.normalized_node_hash !== item.expected_normalized_node_hash) {
        findings.push({ code: "golden_python_hash", location: item.case_id, message: `${result.normalized_node_hash} != ${item.expected_normalized_node_hash}` });
      }
    }
  } catch (error) {
    findings.push({ code: "golden_python", location: args.pythonBin, message: String(error) });
  }
  for (const relation of fixture.relations ?? []) {
    const language = relation.language ?? "typescript";
    const left = language === "python" ? pythonResults.get(relation.left_case_id) : tsResults.get(relation.left_case_id);
    const right = language === "python" ? pythonResults.get(relation.right_case_id) : tsResults.get(relation.right_case_id);
    if (!left || !right) {
      findings.push({ code: "golden_relation_ref", location: relation.relation_id, message: "case reference missing" });
      continue;
    }
    const leftResult = left as any;
    const rightResult = right as any;
    const leftHash = language === "python" ? leftResult.normalized_node_hash : leftResult.normalizedNodeHash;
    const rightHash = language === "python" ? rightResult.normalized_node_hash : rightResult.normalizedNodeHash;
    const same = leftHash === rightHash;
    if (same !== relation.expected_same_hash) findings.push({ code: "golden_relation_hash", location: relation.relation_id, message: `same_hash=${same}` });
    if (relation.kind === "move" && relation.expected_changed_sloc !== 0) findings.push({ code: "golden_move_fixture", location: relation.relation_id, message: "move expected_changed_sloc must be zero" });
    if (relation.kind === "duplicate") {
      const leftSloc = language === "python" ? leftResult.executable_sloc : leftResult.executableSloc;
      const rightSloc = language === "python" ? rightResult.executable_sloc : rightResult.executableSloc;
      if (relation.expected_raw_sloc !== leftSloc + rightSloc) findings.push({ code: "golden_duplicate_raw", location: relation.relation_id, message: `${leftSloc + rightSloc} != ${relation.expected_raw_sloc}` });
      const uniqueSloc = same ? Math.max(leftSloc, rightSloc) : leftSloc + rightSloc;
      if (uniqueSloc !== relation.expected_unique_sloc) findings.push({ code: "golden_duplicate_union", location: relation.relation_id, message: `${uniqueSloc} != ${relation.expected_unique_sloc}` });
    }
    if (relation.kind === "change") {
      if (same) findings.push({ code: "golden_change_hash", location: relation.relation_id, message: "changed nodes unexpectedly share a hash" });
      const rightSloc = language === "python" ? rightResult.executable_sloc : rightResult.executableSloc;
      if (relation.expected_changed_sloc !== rightSloc) findings.push({ code: "golden_change_sloc", location: relation.relation_id, message: `${rightSloc} != ${relation.expected_changed_sloc}` });
    }
  }
  return {
    fixture_id: fixture.fixture_id,
    typescript_case_count: tsResults.size,
    python_case_count: Object.keys(pythonResult.results ?? {}).length,
    relation_count: (fixture.relations ?? []).length,
  };
}

async function main(): Promise<number> {
  const findings: Finding[] = [];
  let args: Args;
  try {
    args = parseArgs(Bun.argv.slice(2));
  } catch (error) {
    const report = { ok: false, findings: [{ code: "arguments", location: "argv", message: String(error) }] };
    console.log(JSON.stringify(report));
    return 2;
  }
  const bunVersion = (globalThis as any).Bun?.version ?? "not-bun";
  if (bunVersion !== EXPECTED_BUN) findings.push({ code: "bun_version", location: "toolchain", message: `expected ${EXPECTED_BUN}, got ${bunVersion}` });
  if (ts.version !== EXPECTED_TYPESCRIPT) findings.push({ code: "typescript_version", location: "toolchain", message: `expected ${EXPECTED_TYPESCRIPT}, got ${ts.version}` });
  let manifestSchema: Obj = {};
  let evidenceSchema: Obj = {};
  try {
    manifestSchema = readJson(args.manifestSchema);
    evidenceSchema = readJson(args.evidenceSchema);
  } catch (error) {
    findings.push({ code: "schema_read", location: "schema", message: String(error) });
  }
  let manifestReport: Obj | null = null;
  let evidenceReport: Obj | null = null;
  let goldenReport: Obj | null = null;
  try {
    if (args.manifests.length) manifestReport = await verifyManifests(args, manifestSchema, findings);
    else if (!args.goldenSelfTest) findings.push({ code: "manifest_required", location: "--manifest", message: "provide manifests or --golden-self-test" });
    if (manifestReport) evidenceReport = await verifyEvidence(args, evidenceSchema, manifestReport, findings);
    if (args.goldenSelfTest) goldenReport = await runGolden(args, findings);
  } catch (error) {
    findings.push({ code: "verifier_internal", location: "runtime", message: String(error) });
  }
  const report = {
    ok: findings.length === 0,
    verifier: "verify_m1_r01_manifests.ts",
    toolchain: { bun: bunVersion, typescript: ts.version, python_required: EXPECTED_PYTHON },
    schemas: {
      manifest_path: args.manifestSchema,
      manifest_sha256: existsSync(args.manifestSchema) ? sha256Bytes(readFileSync(args.manifestSchema)) : null,
      evidence_path: args.evidenceSchema,
      evidence_sha256: existsSync(args.evidenceSchema) ? sha256Bytes(readFileSync(args.evidenceSchema)) : null,
    },
    inputs: {
      zyra_root: args.zyraRoot,
      baseline_root: args.baselineRoot ?? null,
      source_roots: Object.fromEntries(args.sourceRoots),
      manifests: args.manifests,
      evidence: args.evidence,
    },
    manifest: manifestReport,
    evidence: evidenceReport,
    golden: goldenReport,
    findings,
  };
  console.log(JSON.stringify(report, null, args.pretty ? 2 : 0));
  return report.ok ? 0 : 1;
}

process.exitCode = await main();
