import { execFileSync, spawnSync } from "node:child_process"
import { resolve } from "node:path"
import { writeFileSync } from "node:fs"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv.find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length)
  || "92537deca86376e147feb6c85248b0d3ff2298a6"
const TARGET =
  process.argv.find((value) => value.startsWith("--target="))
    ?.slice("--target=".length)
  || "198087a83102e0457e201114d14549373bea9924"
const MINIMUM = Number(
  process.argv.find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length)
  || 6_500,
)
const OUTPUT =
  process.argv.find((value) => value.startsWith("--output="))
    ?.slice("--output=".length)
const SCOPE =
  process.argv.find((value) => value.startsWith("--scope="))
    ?.slice("--scope=".length)
  || "M2-S05-01"

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" })
}

function implementationText(path) {
  return git("show", `${TARGET}:${path}`)
}

function addedLines(path) {
  const patch = git("diff", "--unified=0", BASELINE, TARGET, "--", path)
  const output = new Set()
  let lineNumber = 0
  for (const line of patch.split(/\r?\n/)) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (header) {
      lineNumber = Number(header[1])
      continue
    }
    if (!lineNumber || line.startsWith("---") || line.startsWith("+++")) continue
    if (line.startsWith("+")) {
      output.add(lineNumber)
      lineNumber += 1
    } else if (!line.startsWith("-")) {
      lineNumber += 1
    }
  }
  return output
}

function classifyPython(entry) {
  const script = [
    "import ast,json,subprocess",
    `target=${JSON.stringify(TARGET)}`,
    `path=${JSON.stringify(entry.path)}`,
    "text=subprocess.check_output(['git','show',f'{target}:{path}'],text=True)",
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
    "for node in tree.body:",
    "  targets=[]",
    "  if isinstance(node,ast.Assign): targets=[target.id for target in node.targets if isinstance(target,ast.Name)]",
    "  elif isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name): targets=[node.target.id]",
    "  if (path.endswith('/scenario_runner/registry.py') or path.endswith('/scenario_runner/source_audit.py')) and targets and all(name.isupper() for name in targets): mark(schema,node)",
    "  if path.endswith('/scenario_runner/registry.py') and isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in {'foundation_definition','default_profile','default_policy'}: mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  const ranges = JSON.parse(
    execFileSync("python", ["-c", script], { cwd: ROOT, encoding: "utf8" }),
  )
  const types = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docs = new Set(ranges.docs)
  const lines = implementationText(entry.path).split(/\r?\n/)
  const result = {
    ...entry,
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
  for (const line of addedLines(entry.path)) {
    const value = lines[line - 1]?.trim() ?? ""
    if (types.has(line)) result.type_declaration += 1
    else if (schema.has(line)) result.schema_dto_data += 1
    else if (docs.has(line) || value === "" || value.startsWith("#")) {
      result.docs_comments_blank += 1
    } else {
      result.production_runtime += 1
    }
  }
  result.effective_production = result.production_runtime
  return result
}

function sourceDecision(path) {
  const language = path.endsWith(".py") ? "python" : "typescript/tsx"
  if (
    path.startsWith("tests/") ||
    path.includes("/test/") ||
    path.endsWith(".test.ts")
  ) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "test_or_conformance",
    }
  }
  if (
    path.startsWith("packages/evaluation/zyra_evaluation/scenario_runner/") ||
    path === "scripts/run_first_stage_scenarios.py" ||
    path.startsWith("apps/web/src/features/scenarios/")
  ) {
    return {
      source_role: "zyra_owned_primary",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "new_owned_runtime_and_same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/api/") ||
    path.startsWith("apps/web/src/api/") ||
    path.startsWith("apps/web/src/app/") ||
    path.startsWith("packages/core/typed-api-client/")
  ) {
    return {
      source_role: "existing_owner_integration",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "typed_api_and_application_composition",
    }
  }
  return {
    source_role: "evidence_or_build",
    source_repo: "zyra",
    source_language: language,
    target_language: language,
    migration_mode: "excluded_support",
  }
}

const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s02b_01_effective_lines.mjs"),
    `--baseline=${BASELINE}`,
    `--target=${TARGET}`,
    "--minimum=0",
  ],
  {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  },
)
if (child.status !== 0 || !child.stdout.trim()) {
  process.stderr.write(child.stderr)
  process.exit(child.status ?? 1)
}

const result = JSON.parse(child.stdout)
result.files = result.files.map((entry) =>
  entry.path.endsWith(".py") &&
  !entry.path.startsWith("tests/") &&
  !entry.path.includes("/test/")
    ? classifyPython(entry)
    : entry,
)

function file(path) {
  const selected = result.files.find((entry) => entry.path === path)
  if (!selected) throw new Error(`Effective-line audit is missing ${path}`)
  return selected
}

function move(path, from, to, requested = "all") {
  const selected = file(path)
  const count =
    requested === "all"
      ? selected[from]
      : Math.min(Number(requested), selected[from])
  selected[from] -= count
  selected[to] += count
  selected.effective_production =
    selected.production_runtime + selected.ui_behavior
}

function moveExecutable(path, to, requested = "all") {
  move(path, "production_runtime", to, requested)
  move(path, "ui_behavior", to, requested)
}

// These files bind existing state owners or transport contracts into the new
// owner. Their lines remain required but cannot satisfy the production quota.
moveExecutable("apps/api/zyra_api/main.py", "adapter_only")
moveExecutable("apps/api/zyra_api/scenario_api.py", "adapter_only")
moveExecutable("apps/web/src/api/index.ts", "adapter_only")
moveExecutable("apps/web/src/api/scenario-api.ts", "adapter_only")
moveExecutable("apps/web/src/app/runtime.ts", "adapter_only")
moveExecutable("apps/web/src/app/workbench-app.tsx", "adapter_only")

// Typed endpoint names and endpoint manifests are protocol schema. The small
// response normalizer registration is executable fail-closed transport code.
moveExecutable(
  "packages/core/typed-api-client/src/constants.ts",
  "schema_dto_data",
)
moveExecutable(
  "packages/core/typed-api-client/src/protocol.ts",
  "schema_dto_data",
)

// The entire model file is DTO/state schema, including serialization helpers.
moveExecutable(
  "packages/evaluation/zyra_evaluation/scenario_runner/models.py",
  "schema_dto_data",
)
moveExecutable(
  "packages/evaluation/zyra_evaluation/__init__.py",
  "schema_dto_data",
)
moveExecutable(
  "packages/evaluation/zyra_evaluation/scenario_runner/__init__.py",
  "schema_dto_data",
)

// Python static defaults and role rows are excluded by exact AST ranges above.
// The TypeScript role sets occupy twelve executable-looking constant lines.
move(
  "apps/web/src/features/scenarios/source-audit.ts",
  "ui_behavior",
  "schema_dto_data",
  12,
)

result.audit_scope = SCOPE
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets between frozen commits. The shared TypeScript compiler-AST scanner excludes imports/exports, interfaces, type aliases, declaration-only signatures, comments/blanks and complete JSX presentation ranges. Python AST classification excludes imports, annotations, docstrings, comments and blanks. This slice additionally excludes all model DTO/state schema, typed endpoint catalogs, static registry/source-role data, API/application composition adapters, tests, fixtures, docs, generated material, ledger data and vendor/source-pool content. Effective production is executable scenario runtime plus non-JSX scenario UI behavior only."
result.files = result.files.map((entry) => ({
  ...entry,
  ...sourceDecision(entry.path),
}))

const numericKeys = [
  "raw_additions",
  "raw_deletions",
  "production_runtime",
  "ui_behavior",
  "ui_presentation",
  "type_declaration",
  "schema_dto_data",
  "adapter_only",
  "generated",
  "test_mock_fixture",
  "docs_comments_blank",
  "vendor_like_source_pool",
  "effective_production",
]
result.totals = Object.fromEntries(
  numericKeys.map((key) => [
    key,
    result.files.reduce((total, entry) => total + Number(entry[key] || 0), 0),
  ]),
)
result.minimum_effective_production = MINIMUM
result.line_count_ok = result.totals.effective_production >= MINIMUM
result.large_file_triggers = result.files
  .filter((entry) =>
    entry.raw_additions > 500 ||
    entry.effective_production > result.totals.effective_production * 0.2 ||
    entry.type_declaration > entry.raw_additions * 0.3 ||
    entry.schema_dto_data > entry.raw_additions * 0.3 ||
    entry.generated > entry.raw_additions * 0.3 ||
    entry.adapter_only > entry.raw_additions * 0.3 ||
    entry.test_mock_fixture > entry.raw_additions * 0.3 ||
    entry.ui_presentation > entry.raw_additions * 0.3)
  .map((entry) => entry.path)

const serialized = `${JSON.stringify(result, null, 2)}\n`
if (OUTPUT) writeFileSync(resolve(ROOT, OUTPUT), serialized)
if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      ...numericKeys.map((key) => `${key}=${result.totals[key]}`),
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
      `large_file_triggers=${result.large_file_triggers.join(",")}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(serialized)
}
if (!result.line_count_ok) process.exitCode = 1
