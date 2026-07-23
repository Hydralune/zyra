import { execFileSync } from "node:child_process"
import { resolve } from "node:path"
import ts from "typescript"

const BASELINE = "1f41ab17864dd789948aeebe3723540007ebaf2b"
const IMPLEMENTATION = "6cd3f3fc259a5d39e96f81c79917ce3159d46efe"
const ROOT = resolve(import.meta.dirname, "..")
const MINIMUM = 7_500

const ADAPTER_ONLY = new Set([
  "apps/api/zyra_api/main.py",
  "apps/web/src/api/client.ts",
  "apps/web/src/api/task-api.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
])

const SCHEMA_ONLY = new Set([
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/protocol.ts",
])

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" })
}

function diffRows() {
  return git("diff", "--numstat", BASELINE, IMPLEMENTATION)
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
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
    return git("show", `${IMPLEMENTATION}:${path}`)
  } catch {
    return ""
  }
}

function addedLines(path) {
  const patch = git("diff", "--unified=0", BASELINE, IMPLEMENTATION, "--", path)
  const lines = new Set()
  let nextLine = 0
  for (const line of patch.split(/\r?\n/)) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (header) {
      nextLine = Number(header[1])
      continue
    }
    if (!nextLine || line.startsWith("---") || line.startsWith("+++")) continue
    if (line.startsWith("+")) {
      lines.add(nextLine)
      nextLine += 1
    } else if (!line.startsWith("-")) {
      nextLine += 1
    }
  }
  return lines
}

function lineRange(source, start, end) {
  const first = source.getLineAndCharacterOfPosition(start).line + 1
  const last = source.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1
  const lines = new Set()
  for (let line = first; line <= last; line += 1) lines.add(line)
  return lines
}

function mergeInto(target, source) {
  for (const value of source) target.add(value)
}

function row(raw, values = {}) {
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
    ...values,
  }
}

function excluded(raw, bucket) {
  return row(raw, { [bucket]: raw.raw_additions })
}

function topLevelStaticDeclaration(node) {
  if (!ts.isVariableStatement(node)) return false
  return node.declarationList.declarations.every((declaration) => {
    if (!ts.isIdentifier(declaration.name)) return false
    const name = declaration.name.text
    if (!/^[A-Z][A-Z0-9_]*$/.test(name)) return false
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
    if (node.parent === source && topLevelStaticDeclaration(node)) {
      mergeInto(schema, lineRange(source, node.getFullStart(), node.getEnd()))
      return
    }
    ts.forEachChild(node, visit)
  }
  visit(source)

  const scanner = ts.createScanner(
    ts.ScriptTarget.Latest,
    false,
    path.endsWith(".tsx") ? ts.LanguageVariant.JSX : ts.LanguageVariant.Standard,
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

  let production = 0
  let typeDeclaration = 0
  let schemaData = 0
  let docs = 0
  const sourceLines = text.split(/\r?\n/)
  for (const line of added) {
    if (declarations.has(line)) typeDeclaration += 1
    else if (schema.has(line)) schemaData += 1
    else if (code.has(line)) production += 1
    else if (comments.has(line) || sourceLines[line - 1]?.trim() === "") docs += 1
    else docs += 1
  }
  return row(raw, {
    production_runtime: production,
    type_declaration: typeDeclaration,
    schema_dto_data: schemaData,
    docs_comments_blank: docs,
    effective_production: production,
  })
}

function pythonExcludedRanges(path) {
  const program = [
    "import ast,json,subprocess",
    `impl=${JSON.stringify(IMPLEMENTATION)}`,
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
    "    if isinstance(node,ast.Assign):",
    "      names=[target.id for target in node.targets if isinstance(target,ast.Name)]",
    "    elif isinstance(node.target,ast.Name): names=[node.target.id]",
    "    if names and all(name.upper()==name for name in names): mark(schema,node)",
    "for parent in ast.walk(tree):",
    "  if isinstance(parent,ast.ClassDef):",
    "    is_enum=any((isinstance(base,ast.Name) and base.id.endswith('Enum')) or (isinstance(base,ast.Attribute) and base.attr.endswith('Enum')) for base in parent.bases)",
    "    if is_enum: mark(schema,parent)",
    "    else:",
    "      for node in parent.body:",
    "        if isinstance(node,ast.AnnAssign): mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  return JSON.parse(
    execFileSync("python", ["-c", program], { cwd: ROOT, encoding: "utf8" }),
  )
}

function classifyPython(path, raw) {
  if (raw.raw_additions === 0) return row(raw)
  if (ADAPTER_ONLY.has(path)) return excluded(raw, "adapter_only")
  const text = implementationText(path)
  if (!text) return row(raw)
  const sourceLines = text.split(/\r?\n/)
  const added = addedLines(path)
  const ranges = pythonExcludedRanges(path)
  const types = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docstrings = new Set(ranges.docs)
  let production = 0
  let typeDeclaration = 0
  let schemaData = 0
  let docs = 0
  for (const line of added) {
    const value = sourceLines[line - 1]?.trim() ?? ""
    if (types.has(line)) typeDeclaration += 1
    else if (schema.has(line)) schemaData += 1
    else if (docstrings.has(line) || value === "" || value.startsWith("#")) docs += 1
    else production += 1
  }
  return row(raw, {
    production_runtime: production,
    type_declaration: typeDeclaration,
    schema_dto_data: schemaData,
    docs_comments_blank: docs,
    effective_production: production,
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
  ) return excluded(raw, "test_mock_fixture")
  if (path.endsWith(".md")) return excluded(raw, "docs_comments_blank")
  if (path.endsWith(".css") || path.endsWith(".html") || path.endsWith(".svg")) {
    return excluded(raw, "ui_presentation")
  }
  if (
    path.endsWith(".json") ||
    path.endsWith(".lock") ||
    path.endsWith(".yaml") ||
    path.endsWith(".yml")
  ) return excluded(raw, "schema_dto_data")
  if (
    path.startsWith("vendor/") ||
    path.startsWith("vendor-runtimes/") ||
    path.startsWith("source-pool/") ||
    path.startsWith("runtime-sources/")
  ) return excluded(raw, "vendor_like_source_pool")
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
    if (typeof value === "number") totals[key] = (totals[key] ?? 0) + value
  }
}

const payload = {
  audit_scope: "M2-S01B-01-slice",
  baseline_commit: BASELINE,
  implementation_commit: IMPLEMENTATION,
  interval: `${BASELINE}..${IMPLEMENTATION}`,
  methodology:
    "Exact Git added-line sets. TypeScript compiler AST excludes imports, interfaces, type aliases, export-only declarations, overload signatures and top-level static schema constants. Python AST excludes imports, docstrings, enum bodies, module constants and class DTO fields. Route/client glue is conservatively adapter-only. Tests, fixtures, docs, presentation, generated content and vendor/source-pool material are excluded.",
  files,
  totals,
  minimum_effective_production: MINIMUM,
  line_count_ok: totals.effective_production >= MINIMUM,
}

if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${payload.audit_scope}`,
      `interval=${payload.interval}`,
      `raw_additions=${totals.raw_additions}`,
      `production_runtime=${totals.production_runtime}`,
      `type_declaration=${totals.type_declaration}`,
      `schema_dto_data=${totals.schema_dto_data}`,
      `adapter_only=${totals.adapter_only}`,
      `test_mock_fixture=${totals.test_mock_fixture}`,
      `docs_comments_blank=${totals.docs_comments_blank}`,
      `effective_production=${totals.effective_production}`,
      `minimum=${MINIMUM}`,
      `line_count_ok=${payload.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`)
}

if (!payload.line_count_ok) process.exitCode = 1
