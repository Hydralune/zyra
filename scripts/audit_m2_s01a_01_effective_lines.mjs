import { execFileSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import ts from "typescript"

const BASELINE = "f53bf78287a2b2a6eec74102e7218d664307c137"
const IMPLEMENTATION = "b4e6b5b554ff6ee8ebd786bddc239d00efa9cbc2"
const ROOT = resolve(import.meta.dirname, "..")

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
      return { path, raw_additions: Number(added), raw_deletions: Number(deleted) }
    })
}

function implementationText(path) {
  return git("show", `${IMPLEMENTATION}:${path}`)
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
    } else if (line.startsWith("-")) {
      continue
    } else {
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

function classifyTypeScript(path, raw, uiBehavior) {
  const text = implementationText(path)
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true)
  const added = addedLines(path)
  const typeDeclaration = new Set()
  const commentLines = new Set()
  const codeLines = new Set()

  function visit(node) {
    const isExcluded =
      ts.isImportDeclaration(node) ||
      ts.isImportEqualsDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isExportDeclaration(node) ||
      (ts.isFunctionDeclaration(node) && !node.body) ||
      (ts.isMethodDeclaration(node) && !node.body)
    if (isExcluded) {
      mergeInto(typeDeclaration, lineRange(source, node.getFullStart(), node.getEnd()))
      return
    }
    ts.forEachChild(node, visit)
  }
  visit(source)

  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, ts.LanguageVariant.Standard, text)
  for (let token = scanner.scan(); token !== ts.SyntaxKind.EndOfFileToken; token = scanner.scan()) {
    const tokenLines = lineRange(source, scanner.getTokenPos(), scanner.getTextPos())
    if (
      token === ts.SyntaxKind.SingleLineCommentTrivia ||
      token === ts.SyntaxKind.MultiLineCommentTrivia ||
      token === ts.SyntaxKind.WhitespaceTrivia ||
      token === ts.SyntaxKind.NewLineTrivia
    ) {
      if (
        token === ts.SyntaxKind.SingleLineCommentTrivia ||
        token === ts.SyntaxKind.MultiLineCommentTrivia
      ) {
        mergeInto(commentLines, tokenLines)
      }
      continue
    }
    mergeInto(codeLines, tokenLines)
  }

  let effective = 0
  let declarations = 0
  let docs = 0
  for (const line of added) {
    if (typeDeclaration.has(line)) declarations += 1
    else if (codeLines.has(line)) effective += 1
    else if (commentLines.has(line) || text.split(/\r?\n/)[line - 1]?.trim() === "") docs += 1
    else docs += 1
  }

  return {
    ...raw,
    production_runtime: uiBehavior ? 0 : effective,
    ui_behavior: uiBehavior ? effective : 0,
    ui_presentation: 0,
    type_declaration: declarations,
    schema_dto_data: 0,
    adapter_only: 0,
    generated: 0,
    test_mock_fixture: 0,
    docs_comments: docs,
    effective_production: effective,
  }
}

function pythonExcludedRanges(path) {
  const script = [
    "import ast,json,subprocess",
    `base=${JSON.stringify(BASELINE)}`,
    `impl=${JSON.stringify(IMPLEMENTATION)}`,
    `path=${JSON.stringify(path)}`,
    "text=subprocess.check_output(['git','show',f'{impl}:{path}'],text=True)",
    "tree=ast.parse(text)",
    "types=set(); schema=set(); docs=set()",
    "def mark(target,node):",
    "  target.update(range(node.lineno,getattr(node,'end_lineno',node.lineno)+1))",
    "for node in ast.walk(tree):",
    "  if isinstance(node,(ast.Import,ast.ImportFrom)): mark(types,node)",
    "  if isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.body:",
    "    first=node.body[0]",
    "    if isinstance(first,ast.Expr) and isinstance(first.value,ast.Constant) and isinstance(first.value.value,str): mark(docs,first)",
    "for parent in ast.walk(tree):",
    "  if isinstance(parent,ast.ClassDef):",
    "    for node in parent.body:",
    "      if isinstance(node,ast.AnnAssign): mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  return JSON.parse(execFileSync("python", ["-c", script], { cwd: ROOT, encoding: "utf8" }))
}

function classifyPython(path, raw) {
  const text = implementationText(path)
  const sourceLines = text.split(/\r?\n/)
  const added = addedLines(path)
  const ranges = pythonExcludedRanges(path)
  const typeDeclaration = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docstrings = new Set(ranges.docs)
  let effective = 0
  let declarations = 0
  let schemaLines = 0
  let docs = 0
  for (const line of added) {
    const value = sourceLines[line - 1]?.trim() ?? ""
    if (typeDeclaration.has(line)) declarations += 1
    else if (schema.has(line)) schemaLines += 1
    else if (docstrings.has(line) || value === "" || value.startsWith("#")) docs += 1
    else effective += 1
  }
  return {
    ...raw,
    production_runtime: effective,
    ui_behavior: 0,
    ui_presentation: 0,
    type_declaration: declarations,
    schema_dto_data: schemaLines,
    adapter_only: 0,
    generated: 0,
    test_mock_fixture: 0,
    docs_comments: docs,
    effective_production: effective,
  }
}

function excluded(raw, bucket) {
  return {
    ...raw,
    production_runtime: 0,
    ui_behavior: 0,
    ui_presentation: bucket === "ui_presentation" ? raw.raw_additions : 0,
    type_declaration: bucket === "type_declaration" ? raw.raw_additions : 0,
    schema_dto_data: bucket === "schema_dto_data" ? raw.raw_additions : 0,
    adapter_only: bucket === "adapter_only" ? raw.raw_additions : 0,
    generated: bucket === "generated" ? raw.raw_additions : 0,
    test_mock_fixture: bucket === "test_mock_fixture" ? raw.raw_additions : 0,
    docs_comments: bucket === "docs_comments" ? raw.raw_additions : 0,
    effective_production: 0,
  }
}

function classify(raw) {
  const path = raw.path
  if (
    path.includes("/test/") ||
    path.startsWith("tests/") ||
    path.endsWith("/testing.ts") ||
    path.endsWith("embedded-client-probe.ts")
  ) {
    return excluded(raw, "test_mock_fixture")
  }
  if (path.endsWith(".html")) return excluded(raw, "ui_presentation")
  if (path.endsWith(".md")) return excluded(raw, "docs_comments")
  if (
    path.endsWith(".json") ||
    path.endsWith(".lock") ||
    path.endsWith("bun.lock") ||
    path.endsWith(".yaml") ||
    path.endsWith(".yml")
  ) {
    return excluded(raw, "schema_dto_data")
  }
  if (path.endsWith(".py")) return classifyPython(path, raw)
  if (path.endsWith(".ts") || path.endsWith(".tsx")) {
    const uiBehavior = path.startsWith("apps/web/src/")
    return classifyTypeScript(path, raw, uiBehavior)
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
  baseline_commit: BASELINE,
  implementation_commit: IMPLEMENTATION,
  interval: `${BASELINE}..${IMPLEMENTATION}`,
  methodology:
    "Exact added-line sets; TypeScript compiler AST excludes imports, type/interface/export-only declarations and overload signatures; Python AST excludes imports, docstrings, and class schema fields; tests, fixtures, manifests, lockfiles, docs, and static HTML are excluded.",
  files,
  totals,
  minimum_effective_production: 6000,
  line_count_ok: totals.effective_production >= 6000,
}

if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `interval=${payload.interval}`,
      `raw_additions=${totals.raw_additions}`,
      `production_runtime=${totals.production_runtime}`,
      `ui_behavior=${totals.ui_behavior}`,
      `effective_production=${totals.effective_production}`,
      `minimum=${payload.minimum_effective_production}`,
      `line_count_ok=${payload.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`)
}
