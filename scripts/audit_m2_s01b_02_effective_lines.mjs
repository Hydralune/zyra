import { execFileSync } from "node:child_process"
import { resolve } from "node:path"
import ts from "typescript"

const COMMITS = Object.freeze({
  slice: "8f6ea956286a6f84f7c20fc46225fa0b9b5ee9d5",
  parent: "1f41ab17864dd789948aeebe3723540007ebaf2b",
  stage: "f53bf78287a2b2a6eec74102e7218d664307c137",
  implementation: "7446f4cb57c4a75f416a5d6707ccf47c03a67cc3",
})
const MINIMUMS = Object.freeze({
  slice: 7_500,
  parent: 15_000,
  stage: 27_000,
})
const ROOT = resolve(import.meta.dirname, "..")
const scope = process.argv.includes("--stage")
  ? "stage"
  : process.argv.includes("--parent")
    ? "parent"
    : "slice"
const baseline = COMMITS[scope]
const minimum = MINIMUMS[scope]

const ADAPTER_ONLY = new Set([
  "apps/api/zyra_api/main.py",
  "apps/web/src/api/client.ts",
  "apps/web/src/api/task-api.ts",
  "apps/web/src/app/hooks.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
])

const SCHEMA_ONLY = new Set([
  "apps/web/package.json",
  "apps/web/tsconfig.json",
  "bun.lock",
  "package.json",
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/protocol.ts",
])

const UI_BEHAVIOR_PREFIXES = Object.freeze([
  "apps/web/src/app/",
  "apps/web/src/command/",
  "apps/web/src/shell/",
])

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" })
}

function diffRows() {
  const output = git(
    "diff",
    "--numstat",
    baseline,
    COMMITS.implementation,
  ).trim()
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
    return git("show", `${COMMITS.implementation}:${path}`)
  } catch {
    return ""
  }
}

function addedLines(path) {
  const patch = git(
    "diff",
    "--unified=0",
    baseline,
    COMMITS.implementation,
    "--",
    path,
  )
  const result = new Set()
  let nextLine = 0
  for (const line of patch.split(/\r?\n/)) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (header) {
      nextLine = Number(header[1])
      continue
    }
    if (!nextLine || line.startsWith("---") || line.startsWith("+++")) {
      continue
    }
    if (line.startsWith("+")) {
      result.add(nextLine)
      nextLine += 1
    } else if (!line.startsWith("-")) {
      nextLine += 1
    }
  }
  return result
}

function lineRange(source, start, end) {
  const first = source.getLineAndCharacterOfPosition(start).line + 1
  const last =
    source.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1
  const result = new Set()
  for (let line = first; line <= last; line += 1) result.add(line)
  return result
}

function mergeInto(target, values) {
  for (const value of values) target.add(value)
}

function row(raw, patch = {}) {
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
    ...patch,
  }
}

function excluded(raw, bucket) {
  return row(raw, { [bucket]: raw.raw_additions })
}

function topLevelStaticDeclaration(node, path) {
  if (!ts.isVariableStatement(node)) return false
  return node.declarationList.declarations.every((declaration) => {
    if (!ts.isIdentifier(declaration.name)) return false
    if (
      path.endsWith("/contracts.ts") ||
      path.endsWith("/constants.ts") ||
      path.endsWith("/protocol.ts")
    ) {
      return true
    }
    if (!/^[A-Z][A-Z0-9_]*$/.test(declaration.name.text)) return false
    const initializer = declaration.initializer
    return (
      !initializer ||
      ts.isObjectLiteralExpression(initializer) ||
      ts.isArrayLiteralExpression(initializer) ||
      ts.isStringLiteral(initializer) ||
      ts.isNumericLiteral(initializer) ||
      initializer.kind === ts.SyntaxKind.TrueKeyword ||
      initializer.kind === ts.SyntaxKind.FalseKeyword
    )
  })
}

function classifyTypeScript(path, raw) {
  if (raw.raw_additions === 0) return row(raw)
  if (ADAPTER_ONLY.has(path)) return excluded(raw, "adapter_only")
  if (SCHEMA_ONLY.has(path)) return excluded(raw, "schema_dto_data")
  const text = implementationText(path)
  if (!text) return row(raw)
  const source = ts.createSourceFile(
    path,
    text,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const added = addedLines(path)
  const declarations = new Set()
  const schema = new Set()
  const jsx = new Set()
  const comments = new Set()
  const code = new Set()
  function visit(node) {
    const declarationOnly =
      ts.isImportDeclaration(node) ||
      ts.isImportEqualsDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isExportDeclaration(node) ||
      (ts.isFunctionDeclaration(node) && !node.body) ||
      (ts.isMethodDeclaration(node) && !node.body)
    if (declarationOnly) {
      mergeInto(declarations, lineRange(source, node.getFullStart(), node.getEnd()))
      return
    }
    if (node.parent === source && topLevelStaticDeclaration(node, path)) {
      mergeInto(schema, lineRange(source, node.getFullStart(), node.getEnd()))
      return
    }
    if (
      ts.isJsxElement(node) ||
      ts.isJsxSelfClosingElement(node) ||
      ts.isJsxFragment(node)
    ) {
      mergeInto(jsx, lineRange(source, node.getStart(source), node.getEnd()))
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  const scanner = ts.createScanner(
    ts.ScriptTarget.Latest,
    false,
    path.endsWith(".tsx")
      ? ts.LanguageVariant.JSX
      : ts.LanguageVariant.Standard,
    text,
  )
  for (
    let token = scanner.scan();
    token !== ts.SyntaxKind.EndOfFileToken;
    token = scanner.scan()
  ) {
    const lines = lineRange(source, scanner.getTokenPos(), scanner.getTextPos())
    if (
      token === ts.SyntaxKind.SingleLineCommentTrivia ||
      token === ts.SyntaxKind.MultiLineCommentTrivia
    ) {
      mergeInto(comments, lines)
    } else if (
      token !== ts.SyntaxKind.WhitespaceTrivia &&
      token !== ts.SyntaxKind.NewLineTrivia
    ) {
      mergeInto(code, lines)
    }
  }
  const uiBehavior = UI_BEHAVIOR_PREFIXES.some((prefix) =>
    path.startsWith(prefix),
  )
  const sourceLines = text.split(/\r?\n/)
  const counts = {
    production_runtime: 0,
    ui_behavior: 0,
    ui_presentation: 0,
    type_declaration: 0,
    schema_dto_data: 0,
    docs_comments_blank: 0,
  }
  for (const line of added) {
    if (declarations.has(line)) counts.type_declaration += 1
    else if (schema.has(line)) counts.schema_dto_data += 1
    else if (jsx.has(line)) counts.ui_presentation += 1
    else if (code.has(line)) {
      if (uiBehavior || path.endsWith(".tsx")) counts.ui_behavior += 1
      else counts.production_runtime += 1
    } else if (
      comments.has(line) ||
      sourceLines[line - 1]?.trim() === ""
    ) {
      counts.docs_comments_blank += 1
    } else {
      counts.docs_comments_blank += 1
    }
  }
  return row(raw, {
    ...counts,
    effective_production:
      counts.production_runtime + counts.ui_behavior,
  })
}

function pythonExcludedRanges(path) {
  const program = [
    "import ast,json,subprocess",
    `impl=${JSON.stringify(COMMITS.implementation)}`,
    `path=${JSON.stringify(path)}`,
    "text=subprocess.check_output(['git','show',f'{impl}:{path}'],text=True)",
    "tree=ast.parse(text)",
    "types=set(); schema=set(); docs=set()",
    "def mark(target,node): target.update(range(node.lineno,getattr(node,'end_lineno',node.lineno)+1))",
    "for node in ast.walk(tree):",
    "  if isinstance(node,(ast.Import,ast.ImportFrom)): mark(types,node)",
    "  if isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.body:",
    "    first=node.body[0]",
    "    if isinstance(first,ast.Expr) and isinstance(first.value,ast.Constant) and isinstance(first.value.value,str): mark(docs,first)",
    "for node in tree.body:",
    "  if isinstance(node,(ast.Assign,ast.AnnAssign)):",
    "    names=[]",
    "    if isinstance(node,ast.Assign): names=[item.id for item in node.targets if isinstance(item,ast.Name)]",
    "    elif isinstance(node.target,ast.Name): names=[node.target.id]",
    "    if names and all(name.upper()==name for name in names): mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  return JSON.parse(
    execFileSync("python", ["-c", program], {
      cwd: ROOT,
      encoding: "utf8",
    }),
  )
}

function classifyPython(path, raw) {
  if (raw.raw_additions === 0) return row(raw)
  if (ADAPTER_ONLY.has(path)) return excluded(raw, "adapter_only")
  const text = implementationText(path)
  if (!text) return row(raw)
  const added = addedLines(path)
  const sourceLines = text.split(/\r?\n/)
  const ranges = pythonExcludedRanges(path)
  const types = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docs = new Set(ranges.docs)
  const counts = {
    production_runtime: 0,
    type_declaration: 0,
    schema_dto_data: 0,
    docs_comments_blank: 0,
  }
  for (const line of added) {
    const value = sourceLines[line - 1]?.trim() ?? ""
    if (types.has(line)) counts.type_declaration += 1
    else if (schema.has(line)) counts.schema_dto_data += 1
    else if (docs.has(line) || !value || value.startsWith("#")) {
      counts.docs_comments_blank += 1
    } else {
      counts.production_runtime += 1
    }
  }
  return row(raw, {
    ...counts,
    effective_production: counts.production_runtime,
  })
}

function classify(raw) {
  const path = raw.path
  if (
    path.includes("/test/") ||
    path.startsWith("tests/") ||
    path.endsWith(".test.ts") ||
    path.endsWith(".test.tsx") ||
    path.endsWith(".spec.ts") ||
    path.endsWith(".spec.tsx")
  ) {
    return excluded(raw, "test_mock_fixture")
  }
  if (path.endsWith(".md")) return excluded(raw, "docs_comments_blank")
  if (
    path.endsWith(".css") ||
    path.endsWith(".html") ||
    path.endsWith(".svg")
  ) {
    return excluded(raw, "ui_presentation")
  }
  if (
    path.endsWith(".json") ||
    path.endsWith(".lock") ||
    path.endsWith(".yaml") ||
    path.endsWith(".yml")
  ) {
    return excluded(raw, "schema_dto_data")
  }
  if (
    path.startsWith("vendor/") ||
    path.startsWith("vendor-runtimes/") ||
    path.startsWith("source-pool/") ||
    path.startsWith("runtime-sources/")
  ) {
    return excluded(raw, "vendor_like_source_pool")
  }
  if (path.endsWith(".py")) return classifyPython(path, raw)
  if (path.endsWith(".ts") || path.endsWith(".tsx")) {
    return classifyTypeScript(path, raw)
  }
  return excluded(raw, "generated")
}

const files = diffRows().map(classify)
const totals = {}
for (const file of files) {
  for (const [key, value] of Object.entries(file)) {
    if (typeof value === "number") {
      totals[key] = (totals[key] ?? 0) + value
    }
  }
}

const payload = {
  audit_scope: `M2-S01B-02-${scope}`,
  baseline_commit: baseline,
  implementation_commit: COMMITS.implementation,
  interval: `${baseline}..${COMMITS.implementation}`,
  methodology:
    "Exact Git added-line sets. TypeScript compiler AST excludes imports, interfaces, type aliases, export-only declarations, overload signatures and static contract/schema constants; TSX JSX is presentation-only while executable app/command/shell code is UI behavior. Python AST excludes imports, docstrings and module schema constants. Tests, fixtures, docs, static presentation, build metadata, generated content, adapters and vendor/source-pool material receive zero production credit.",
  files,
  totals,
  minimum_effective_production: minimum,
  line_count_ok: totals.effective_production >= minimum,
}

if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${payload.audit_scope}`,
      `interval=${payload.interval}`,
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
      `minimum=${minimum}`,
      `line_count_ok=${payload.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`)
}

if (!payload.line_count_ok) process.exitCode = 1
