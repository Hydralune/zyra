import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { tmpdir } from "node:os";
import ts from "typescript";

export interface SemanticCallEdge {
  callerPath: string;
  callerSymbol: string;
  calleePath: string;
  calleeSymbol: string;
  invocation: string;
  invocationSha256: string;
  callStart: number;
}

export interface AssertionObservation {
  fingerprint: string;
  subject: string;
  matcher: string;
  expected: string[];
  callStart: number;
}

export interface TestObservation {
  callPath: SemanticCallEdge[];
  assertions: AssertionObservation[];
}

interface CallableDeclaration {
  id: string;
  path: string;
  symbol: string;
  node: ts.Node;
}

const sha256 = (value: string): string => createHash("sha256").update(value).digest("hex");
const normalized = (value: string): string => value.replaceAll("\\", "/");

export const symbolId = (path: string, symbol: string): string => `${normalized(path)}::${symbol}`;
export const testSymbol = (name: string): string => `test:${name}`;

const declarationName = (node: ts.NamedDeclaration, source: ts.SourceFile): string => {
  if (!node.name) return "";
  if (ts.isIdentifier(node.name) || ts.isPrivateIdentifier(node.name)) return node.name.text;
  return node.name.getText(source).replace(/^['"]|['"]$/g, "");
};

const callableKey = (node: ts.Node): string =>
  `${normalized(node.getSourceFile().fileName)}:${node.pos}:${node.end}`;

export class GitSemanticGraph {
  private readonly declarations = new Map<string, CallableDeclaration>();
  private readonly declarationByNode = new Map<string, CallableDeclaration>();
  private readonly outgoing = new Map<string, SemanticCallEdge[]>();
  private readonly assertions = new Map<string, AssertionObservation[]>();

  private constructor(
    private readonly snapshotRoot: string,
    private readonly program: ts.Program,
  ) {
    this.indexDeclarations();
    this.indexCallsAndAssertions();
  }

  static create(repoRoot: string, commit: string): GitSemanticGraph {
    const snapshotRoot = mkdtempSync(resolve(tmpdir(), "zyra-e01-symbol-graph-"));
    const listed = execFileSync("git", ["ls-tree", "-r", "--name-only", commit], {
      cwd: repoRoot,
      encoding: "utf8",
      maxBuffer: 256 * 1024 * 1024,
    });
    const paths = listed.split(/\r?\n/).filter((path) =>
      /\.tsx?$/.test(path)
      && (path.startsWith("apps/code-worker/") || path.startsWith("packages/")));
    for (const path of paths) {
      const target = resolve(snapshotRoot, path);
      mkdirSync(dirname(target), { recursive: true });
      const bytes = execFileSync("git", ["show", `${commit}:${path}`], {
        cwd: repoRoot,
        encoding: "buffer",
        maxBuffer: 256 * 1024 * 1024,
      });
      writeFileSync(target, bytes);
    }
    const program = ts.createProgram(paths.map((path) => resolve(snapshotRoot, path)), {
      allowImportingTsExtensions: true,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      noEmit: true,
      skipLibCheck: true,
      target: ts.ScriptTarget.ES2022,
      typeRoots: [resolve(repoRoot, "node_modules/@types")],
    });
    return new GitSemanticGraph(snapshotRoot, program);
  }

  dispose(): void {
    rmSync(this.snapshotRoot, { recursive: true, force: true });
  }

  hasDeclaration(path: string, symbol: string): boolean {
    return this.declarations.has(symbolId(path, symbol));
  }

  hasEdge(edge: SemanticCallEdge): boolean {
    return (this.outgoing.get(symbolId(edge.callerPath, edge.callerSymbol)) ?? []).some((candidate) =>
      candidate.calleePath === normalized(edge.calleePath)
      && candidate.calleeSymbol === edge.calleeSymbol
      && candidate.invocationSha256 === edge.invocationSha256);
  }

  findPath(from: string, to: string): SemanticCallEdge[] | null {
    if (!this.declarations.has(from) || !this.declarations.has(to)) return null;
    if (from === to) return [];
    const queue: string[] = [from];
    const visited = new Set<string>([from]);
    const previous = new Map<string, { parent: string; edge: SemanticCallEdge }>();
    while (queue.length > 0) {
      const current = queue.shift()!;
      const edges = [...(this.outgoing.get(current) ?? [])].sort((left, right) =>
        symbolId(left.calleePath, left.calleeSymbol).localeCompare(symbolId(right.calleePath, right.calleeSymbol))
        || left.callStart - right.callStart);
      for (const edge of edges) {
        const next = symbolId(edge.calleePath, edge.calleeSymbol);
        if (visited.has(next)) continue;
        visited.add(next);
        previous.set(next, { parent: current, edge });
        if (next === to) {
          const path: SemanticCallEdge[] = [];
          let cursor = to;
          while (cursor !== from) {
            const step = previous.get(cursor);
            if (!step) return null;
            path.push(step.edge);
            cursor = step.parent;
          }
          return path.reverse();
        }
        queue.push(next);
      }
    }
    return null;
  }

  testObservation(path: string, testName: string, target: string): TestObservation | null {
    const start = symbolId(path, testSymbol(testName));
    const callPath = this.findPath(start, target);
    if (!callPath || callPath.length === 0) return null;
    const firstInvocation = callPath[0]!.callStart;
    const assertions = (this.assertions.get(start) ?? []).filter((item) => item.callStart > firstInvocation);
    return assertions.length > 0 ? { callPath, assertions } : null;
  }

  private repoPath(source: ts.SourceFile): string | null {
    const path = normalized(relative(this.snapshotRoot, source.fileName));
    return path.startsWith("../") ? null : path;
  }

  private register(path: string, symbol: string, node: ts.Node): void {
    const id = symbolId(path, symbol);
    if (this.declarations.has(id)) return;
    const declaration = { id, path, symbol, node };
    this.declarations.set(id, declaration);
    this.declarationByNode.set(callableKey(node), declaration);
  }

  private indexDeclarations(): void {
    for (const source of this.program.getSourceFiles()) {
      const path = this.repoPath(source);
      if (!path) continue;
      const visit = (node: ts.Node): void => {
        if (ts.isFunctionDeclaration(node) && node.name && node.body) {
          this.register(path, node.name.text, node);
        }
        if (ts.isClassDeclaration(node) && node.name) {
          for (const member of node.members) {
            if (
              (ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member))
              && member.body
            ) {
              this.register(path, `${node.name.text}.${declarationName(member, source)}`, member);
            }
          }
        }
        if (
          ts.isVariableDeclaration(node)
          && ts.isIdentifier(node.name)
          && node.initializer
          && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
          && node.parent.parent.parent === source
        ) {
          this.register(path, node.name.text, node);
          this.declarationByNode.set(callableKey(node.initializer), this.declarations.get(symbolId(path, node.name.text))!);
        }
        if (ts.isCallExpression(node)) {
          const expression = node.expression;
          const name = ts.isIdentifier(expression)
            ? expression.text
            : ts.isPropertyAccessExpression(expression)
              ? expression.name.text
              : "";
          const title = node.arguments[0];
          const callback = node.arguments[1];
          if (
            (name === "test" || name === "it")
            && title
            && ts.isStringLiteralLike(title)
            && callback
            && (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))
          ) {
            this.register(path, testSymbol(title.text), callback);
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(source);
    }
  }

  private resolvedDeclaration(node: ts.CallExpression | ts.NewExpression): CallableDeclaration | null {
    const checker = this.program.getTypeChecker();
    const location = ts.isPropertyAccessExpression(node.expression)
      ? node.expression.name
      : node.expression;
    let symbol = checker.getSymbolAtLocation(location);
    if (!symbol) return null;
    if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) symbol = checker.getAliasedSymbol(symbol);
    for (const declaration of symbol.declarations ?? []) {
      const direct = this.declarationByNode.get(callableKey(declaration));
      if (direct) return direct;
      if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        const initialized = this.declarationByNode.get(callableKey(declaration.initializer));
        if (initialized) return initialized;
      }
    }
    return null;
  }

  private indexCallsAndAssertions(): void {
    for (const callable of this.declarations.values()) {
      const edges: SemanticCallEdge[] = [];
      const observations: AssertionObservation[] = [];
      const visit = (node: ts.Node): void => {
        if (node !== callable.node && this.declarationByNode.has(callableKey(node))) return;
        if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
          const resolved = this.resolvedDeclaration(node);
          if (resolved && resolved.id !== callable.id) {
            const invocation = node.expression.getText(node.getSourceFile());
            edges.push({
              callerPath: callable.path,
              callerSymbol: callable.symbol,
              calleePath: resolved.path,
              calleeSymbol: resolved.symbol,
              invocation,
              invocationSha256: sha256(invocation),
              callStart: node.getStart(node.getSourceFile()),
            });
          }
        }
        if (
          ts.isCallExpression(node)
          && ts.isPropertyAccessExpression(node.expression)
          && ts.isCallExpression(node.expression.expression)
        ) {
          const expectCall = node.expression.expression;
          if (ts.isIdentifier(expectCall.expression) && expectCall.expression.text === "expect" && expectCall.arguments[0]) {
            const source = node.getSourceFile();
            const subject = expectCall.arguments[0].getText(source);
            const matcher = node.expression.name.text;
            const expected = node.arguments.map((argument) => argument.getText(source));
            observations.push({
              fingerprint: sha256(JSON.stringify({ subject, matcher, expected })),
              subject,
              matcher,
              expected,
              callStart: node.getStart(source),
            });
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(callable.node);
      const unique = new Map(edges.map((edge) => [
        `${edge.calleePath}::${edge.calleeSymbol}::${edge.invocationSha256}`,
        edge,
      ]));
      this.outgoing.set(callable.id, [...unique.values()]);
      this.assertions.set(callable.id, observations);
    }
  }
}
