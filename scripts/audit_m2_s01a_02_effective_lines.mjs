import { execFileSync } from "node:child_process"
import { resolve } from "node:path"
import ts from "typescript"

const SLICE_BASELINE = "3f51da450bd65fa1f20e53613e1b5bc791b811fb"
const PARENT_BASELINE = "f53bf78287a2b2a6eec74102e7218d664307c137"
const IMPLEMENTATION = "4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682"
const ROOT = resolve(import.meta.dirname, "..")
const parentMode = process.argv.includes("--parent")
const BASELINE = parentMode ? PARENT_BASELINE : SLICE_BASELINE

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

function classified(raw, values = {}) {
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
    docs_comments: 0,
    effective_production: 0,
    ...values,
  }
}

function classifyTypeScript(path, raw, uiBehavior) {
  if (raw.raw_additions === 0) return classified(raw)
  const text = implementationText(path)
  if (!text) return classified(raw)
  const kind = path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, kind)
  const added = addedLines(path)
  const declarations = new Set()
  const commentLines = new Set()
  const codeLines = new Set()

  function visit(node) {
    const excluded =
      ts.isImportDeclaration(node) ||
      ts.isImportEqualsDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isExportDeclaration(node) ||
      (ts.isFunctionDeclaration(node) && !node.body) ||
      (ts.isMethodDeclaration(node) && !node.body)
    if (excluded) {
      mergeInto(declarations, lineRange(source, node.getFullStart(), node.getEnd()))
      return
    }
    ts.forEachChild(node, visit)
  }
  visit(source)

  const language = path.endsWith(".tsx")
    ? ts.LanguageVariant.JSX
    : ts.LanguageVariant.Standard
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, language, text)
  for (let token = scanner.scan(); token !== ts.SyntaxKind.EndOfFileToken; token = scanner.scan()) {
    const tokenLines = lineRange(source, scanner.getTokenPos(), scanner.getTextPos())
    if (
      token === ts.SyntaxKind.SingleLineCommentTrivia ||
      token === ts.SyntaxKind.MultiLineCommentTrivia
    ) {
      mergeInto(commentLines, tokenLines)
      continue
    }
    if (
      token === ts.SyntaxKind.WhitespaceTrivia ||
      token === ts.SyntaxKind.NewLineTrivia
    ) {
      continue
    }
    mergeInto(codeLines, tokenLines)
  }

  let effective = 0
  let typeDeclaration = 0
  let docs = 0
  const sourceLines = text.split(/\r?\n/)
  for (const line of added) {
    if (declarations.has(line)) typeDeclaration += 1
    else if (codeLines.has(line)) effective += 1
    else if (commentLines.has(line) || sourceLines[line - 1]?.trim() === "") docs += 1
    else docs += 1
  }
  return classified(raw, {
    production_runtime: uiBehavior ? 0 : effective,
    ui_behavior: uiBehavior ? effective : 0,
    type_declaration: typeDeclaration,
    docs_comments: docs,
    effective_production: effective,
  })
}

function pythonExcludedRanges(path) {
  const script = [
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
    "for parent in ast.walk(tree):",
    "  if isinstance(parent,ast.ClassDef):",
    "    for node in parent.body:",
    "      if isinstance(node,ast.AnnAssign): mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  return JSON.parse(execFileSync("python", ["-c", script], { cwd: ROOT, encoding: "utf8" }))
}

function classifyPython(path, raw) {
  if (raw.raw_additions === 0) return classified(raw)
  const text = implementationText(path)
  if (!text) return classified(raw)
  const sourceLines = text.split(/\r?\n/)
  const added = addedLines(path)
  const ranges = pythonExcludedRanges(path)
  const types = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docstrings = new Set(ranges.docs)
  let effective = 0
  let typeDeclaration = 0
  let schemaLines = 0
  let docs = 0
  for (const line of added) {
    const value = sourceLines[line - 1]?.trim() ?? ""
    if (types.has(line)) typeDeclaration += 1
    else if (schema.has(line)) schemaLines += 1
    else if (docstrings.has(line) || value === "" || value.startsWith("#")) docs += 1
    else effective += 1
  }
  return classified(raw, {
    production_runtime: effective,
    type_declaration: typeDeclaration,
    schema_dto_data: schemaLines,
    docs_comments: docs,
    effective_production: effective,
  })
}

function excluded(raw, bucket) {
  return classified(raw, { [bucket]: raw.raw_additions })
}

function classify(raw) {
  const path = raw.path
  if (
    path.startsWith("scripts/sync_m2_") &&
    path.endsWith("_source_ledger.py")
  ) return excluded(raw, "docs_comments")
  if (
    path.includes("/test/") ||
    path.endsWith("/src/testing.ts") ||
    path.startsWith("tests/") ||
    path.endsWith(".test.ts") ||
    path.endsWith(".test.tsx") ||
    path.endsWith(".spec.ts") ||
    path.endsWith(".spec.tsx")
  ) return excluded(raw, "test_mock_fixture")
  if (path.endsWith(".css") || path.endsWith(".html") || path.endsWith(".svg")) {
    return excluded(raw, "ui_presentation")
  }
  if (path.endsWith(".md")) return excluded(raw, "docs_comments")
  if (
    path.endsWith(".json") ||
    path.endsWith(".lock") ||
    path.endsWith("bun.lock") ||
    path.endsWith(".yaml") ||
    path.endsWith(".yml")
  ) return excluded(raw, "schema_dto_data")
  if (path.endsWith(".py")) return classifyPython(path, raw)
  if (path.endsWith(".ts") || path.endsWith(".tsx")) {
    return classifyTypeScript(path, raw, path.startsWith("apps/web/src/"))
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
const minimum = parentMode ? 12_000 : 6_000
const payload = {
  audit_scope: parentMode ? "M2-01A-parent" : "M2-S01A-02-slice",
  baseline_commit: BASELINE,
  implementation_commit: IMPLEMENTATION,
  interval: `${BASELINE}..${IMPLEMENTATION}`,
  methodology:
    "Exact Git added-line sets; TypeScript compiler AST excludes imports, interfaces, type aliases, export-only declarations, and overload signatures; Python AST excludes imports, docstrings, and class schema fields; tests, fixtures, manifests, lockfiles, docs, CSS, SVG, and static HTML are excluded.",
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
      `effective_production=${totals.effective_production}`,
      `minimum=${minimum}`,
      `line_count_ok=${payload.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`)
}

if (!payload.line_count_ok) process.exitCode = 1
