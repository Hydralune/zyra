import { execFileSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import ts from "typescript"

const BASELINE =
  process.argv
    .find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length) ||
  "a2fa483a75ccae4c14bf9cd94aa0cda41346c28e"
const TARGET =
  process.argv
    .find((value) => value.startsWith("--target="))
    ?.slice("--target=".length) || "WORKTREE"
const MINIMUM = Number(
  process.argv
    .find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length) || 6_500,
)
const AUDIT_SCOPE =
  process.argv
    .find((value) => value.startsWith("--scope="))
    ?.slice("--scope=".length) || "M2-S02A-02"
const ROOT = resolve(import.meta.dirname, "..")

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" })
}

function diffArguments(...tail) {
  return TARGET === "WORKTREE"
    ? ["diff", ...tail, BASELINE]
    : ["diff", ...tail, BASELINE, TARGET]
}

function rawRows() {
  const output = git(...diffArguments("--numstat")).trim()
  if (!output) return []
  return output.split(/\r?\n/).map((line) => {
    const [added, deleted, path] = line.split("\t")
    return {
      path,
      raw_additions: added === "-" ? 0 : Number(added),
      raw_deletions: deleted === "-" ? 0 : Number(deleted),
    }
  })
}

function implementationText(path) {
  try {
    return TARGET === "WORKTREE"
      ? readFileSync(resolve(ROOT, path), "utf8")
      : git("show", `${TARGET}:${path}`)
  } catch {
    return ""
  }
}

function addedLines(path) {
  const args =
    TARGET === "WORKTREE"
      ? ["diff", "--unified=0", BASELINE, "--", path]
      : ["diff", "--unified=0", BASELINE, TARGET, "--", path]
  const patch = git(...args)
  const output = new Set()
  let lineNumber = 0
  for (const line of patch.split(/\r?\n/)) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (header) {
      lineNumber = Number(header[1])
      continue
    }
    if (!lineNumber || line.startsWith("---") || line.startsWith("+++")) {
      continue
    }
    if (line.startsWith("+")) {
      output.add(lineNumber)
      lineNumber += 1
    } else if (!line.startsWith("-")) {
      lineNumber += 1
    }
  }
  return output
}

function range(source, start, end) {
  const first = source.getLineAndCharacterOfPosition(start).line + 1
  const last =
    source.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1
  const output = new Set()
  for (let line = first; line <= last; line += 1) output.add(line)
  return output
}

function addAll(target, values) {
  for (const value of values) target.add(value)
}

function emptyRow(raw) {
  return {
    ...raw,
    production_runtime: 0,
    ui_behavior: 0,
    ui_presentation: 0,
    type_declaration: 0,
    schema_dto_data: 0,
    adapter_only: 0,
    generated: 0,
    test_mock_fixture: 0,
    docs_comments_blank: 0,
    vendor_like_source_pool: 0,
    effective_production: 0,
  }
}

function exclude(raw, bucket) {
  return {
    ...emptyRow(raw),
    [bucket]: raw.raw_additions,
  }
}

function classifyTypeScript(raw) {
  const text = implementationText(raw.path)
  if (!text) return emptyRow(raw)
  const source = ts.createSourceFile(
    raw.path,
    text,
    ts.ScriptTarget.Latest,
    true,
    raw.path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const declarations = new Set()
  const schema = new Set()
  const presentation = new Set()
  const comments = new Set()
  const code = new Set()
  const visit = (node) => {
    const declarationOnly =
      ts.isImportDeclaration(node) ||
      ts.isImportEqualsDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isExportDeclaration(node) ||
      (ts.isFunctionDeclaration(node) && !node.body) ||
      (ts.isMethodDeclaration(node) && !node.body)
    if (declarationOnly) {
      addAll(
        declarations,
        range(source, node.getFullStart(), node.getEnd()),
      )
      return
    }
    if (
      raw.path.endsWith("/contracts.ts") &&
      node.parent === source &&
      ts.isVariableStatement(node)
    ) {
      addAll(schema, range(source, node.getFullStart(), node.getEnd()))
      return
    }
    if (
      ts.isJsxElement(node) ||
      ts.isJsxSelfClosingElement(node) ||
      ts.isJsxFragment(node)
    ) {
      addAll(
        presentation,
        range(source, node.getStart(source), node.getEnd()),
      )
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  const scanner = ts.createScanner(
    ts.ScriptTarget.Latest,
    false,
    raw.path.endsWith(".tsx")
      ? ts.LanguageVariant.JSX
      : ts.LanguageVariant.Standard,
    text,
  )
  for (
    let token = scanner.scan();
    token !== ts.SyntaxKind.EndOfFileToken;
    token = scanner.scan()
  ) {
    const lines = range(source, scanner.getTokenPos(), scanner.getTextPos())
    if (
      token === ts.SyntaxKind.SingleLineCommentTrivia ||
      token === ts.SyntaxKind.MultiLineCommentTrivia
    ) {
      addAll(comments, lines)
    } else if (
      token !== ts.SyntaxKind.WhitespaceTrivia &&
      token !== ts.SyntaxKind.NewLineTrivia
    ) {
      addAll(code, lines)
    }
  }
  const added = addedLines(raw.path)
  const sourceLines = text.split(/\r?\n/)
  const result = emptyRow(raw)
  const isUiBehavior =
    raw.path.includes("/components/") ||
    raw.path.includes("/app/") ||
    raw.path.endsWith(".tsx")
  for (const line of added) {
    if (declarations.has(line)) result.type_declaration += 1
    else if (schema.has(line)) result.schema_dto_data += 1
    else if (presentation.has(line)) result.ui_presentation += 1
    else if (code.has(line)) {
      if (isUiBehavior) result.ui_behavior += 1
      else result.production_runtime += 1
    } else if (
      comments.has(line) ||
      sourceLines[line - 1]?.trim() === ""
    ) {
      result.docs_comments_blank += 1
    } else {
      result.docs_comments_blank += 1
    }
  }
  result.effective_production =
    result.production_runtime + result.ui_behavior
  return result
}

function classify(raw) {
  const path = raw.path
  if (
    path === "apps/web/src/api/task-api.ts" ||
    path === "packages/core/typed-api-client/src/constants.ts" ||
    path === "packages/core/typed-api-client/src/normalizers.ts" ||
    path === "packages/core/typed-api-client/src/protocol.ts"
  ) {
    return exclude(raw, "adapter_only")
  }
  if (
    path.startsWith("tests/") ||
    path.includes("/test/") ||
    path.endsWith(".test.ts") ||
    path.endsWith(".spec.ts")
  ) {
    return exclude(raw, "test_mock_fixture")
  }
  if (path.endsWith(".md")) return exclude(raw, "docs_comments_blank")
  if (
    path.endsWith(".css") ||
    path.endsWith(".html") ||
    path.endsWith(".svg")
  ) {
    return exclude(raw, "ui_presentation")
  }
  if (
    path.endsWith(".json") ||
    path.endsWith(".lock") ||
    path.endsWith(".yaml") ||
    path.endsWith(".yml")
  ) {
    return exclude(raw, "schema_dto_data")
  }
  if (
    path.startsWith("vendor/") ||
    path.startsWith("vendor-runtimes/") ||
    path.startsWith("source-pool/") ||
    path.startsWith("runtime-sources/")
  ) {
    return exclude(raw, "vendor_like_source_pool")
  }
  if (path.endsWith(".ts") || path.endsWith(".tsx")) {
    return classifyTypeScript(raw)
  }
  return exclude(raw, "generated")
}

const files = rawRows().map(classify)
const totals = {}
for (const file of files) {
  for (const [key, value] of Object.entries(file)) {
    if (typeof value === "number") {
      totals[key] = (totals[key] ?? 0) + value
    }
  }
}
const result = {
  audit_scope: AUDIT_SCOPE,
  baseline_commit: BASELINE,
  implementation_commit: TARGET,
  interval:
    TARGET === "WORKTREE"
      ? `${BASELINE}..WORKTREE`
      : `${BASELINE}..${TARGET}`,
  methodology:
    "Exact Git added-line sets. TypeScript compiler AST excludes imports, interfaces, type aliases, export-only declarations, declaration-only signatures and contracts.ts schema constants. Scanner classification excludes comments and blank lines. Tests, fixtures, docs, static presentation, data, generated material, adapters and vendor/source-pool content receive zero production credit.",
  files,
  totals,
  minimum_effective_production: MINIMUM,
  line_count_ok: totals.effective_production >= MINIMUM,
}

if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      `raw_additions=${totals.raw_additions}`,
      `production_runtime=${totals.production_runtime}`,
      `ui_behavior=${totals.ui_behavior}`,
      `ui_presentation=${totals.ui_presentation}`,
      `type_declaration=${totals.type_declaration}`,
      `schema_dto_data=${totals.schema_dto_data}`,
      `adapter_only=${totals.adapter_only}`,
      `test_mock_fixture=${totals.test_mock_fixture}`,
      `docs_comments_blank=${totals.docs_comments_blank}`,
      `vendor_like_source_pool=${totals.vendor_like_source_pool}`,
      `effective_production=${totals.effective_production}`,
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

if (!result.line_count_ok) process.exitCode = 1
