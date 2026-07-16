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

export class GitSemanticGraph {
  private readonly declarations = new Map<string, CallableDeclaration>();
  private readonly declarationByNode = new Map<ts.Node, CallableDeclaration>();
  private readonly outgoing = new Map<string, SemanticCallEdge[]>();
  private readonly dispatchEdges = new Map<string, SemanticCallEdge[]>();
  private readonly assertions = new Map<string, AssertionObservation[]>();

  private constructor(
    private readonly snapshotRoot: string,
    private readonly program: ts.Program,
  ) {
    this.indexDeclarations();
    this.indexInterfaceDispatch();
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
    this.declarationByNode.set(node, declaration);
  }

  private indexDeclarations(): void {
    for (const source of this.program.getSourceFiles()) {
      const path = this.repoPath(source);
      if (!path) continue;
      for (const statement of source.statements) {
        if (!ts.isVariableStatement(statement)) continue;
        for (const node of statement.declarationList.declarations) {
          if (
            ts.isIdentifier(node.name)
            && node.initializer
            && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
          ) {
            this.register(path, node.name.text, node);
            this.declarationByNode.set(node.initializer, this.declarations.get(symbolId(path, node.name.text))!);
          }
        }
      }
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
        if (ts.isInterfaceDeclaration(node)) {
          for (const member of node.members) {
            if (ts.isMethodSignature(member)) {
              this.register(path, `${node.name.text}.${declarationName(member, source)}`, member);
            }
          }
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

  private indexInterfaceDispatch(): void {
    const checker = this.program.getTypeChecker();
    for (const source of this.program.getSourceFiles()) {
      const path = this.repoPath(source);
      if (!path) continue;
      for (const statement of source.statements) {
        if (!ts.isClassDeclaration(statement) || !statement.name) continue;
        for (const heritage of statement.heritageClauses ?? []) {
          if (heritage.token !== ts.SyntaxKind.ImplementsKeyword) continue;
          for (const implemented of heritage.types) {
            let symbol = checker.getSymbolAtLocation(implemented.expression);
            if (!symbol) continue;
            if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) symbol = checker.getAliasedSymbol(symbol);
            for (const declaration of symbol.declarations ?? []) {
              if (!ts.isInterfaceDeclaration(declaration)) continue;
              for (const contractMember of declaration.members) {
                if (!ts.isMethodSignature(contractMember)) continue;
                const contract = this.declarationByNode.get(contractMember);
                if (!contract) continue;
                const name = declarationName(contractMember, declaration.getSourceFile());
                const implementation = statement.members.find((member) =>
                  ts.isMethodDeclaration(member) && declarationName(member, source) === name);
                if (!implementation || !ts.isMethodDeclaration(implementation)) continue;
                const target = this.declarationByNode.get(implementation);
                if (!target) continue;
                const invocation = `dispatch ${statement.name.text}.${name} implements ${declaration.name.text}.${name}`;
                const edge: SemanticCallEdge = {
                  callerPath: contract.path,
                  callerSymbol: contract.symbol,
                  calleePath: target.path,
                  calleeSymbol: target.symbol,
                  invocation,
                  invocationSha256: sha256(invocation),
                  callStart: implementation.getStart(source),
                };
                this.dispatchEdges.set(contract.id, [...(this.dispatchEdges.get(contract.id) ?? []), edge]);
              }
            }
          }
        }
      }
    }
  }

  private resolvedDeclaration(node: ts.CallExpression | ts.NewExpression): CallableDeclaration | null {
    const location = ts.isPropertyAccessExpression(node.expression)
      ? node.expression.name
      : node.expression;
    return this.resolvedLocation(location);
  }

  private resolvedLocation(location: ts.Node): CallableDeclaration | null {
    const checker = this.program.getTypeChecker();
    let symbol = checker.getSymbolAtLocation(location);
    if (!symbol) return null;
    if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) symbol = checker.getAliasedSymbol(symbol);
    for (const declaration of symbol.declarations ?? []) {
      const direct = this.declarationByNode.get(declaration);
      if (direct) return direct;
      if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        const initialized = this.declarationByNode.get(declaration.initializer);
        if (initialized) return initialized;
      }
    }
    return null;
  }

  private indexCallsAndAssertions(): void {
    for (const callable of this.declarations.values()) {
      const edges: SemanticCallEdge[] = [...(this.dispatchEdges.get(callable.id) ?? [])];
      const observations: AssertionObservation[] = [];
      const visit = (node: ts.Node): void => {
        if (node !== callable.node && this.declarationByNode.has(node)) return;
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
          if (
            ts.isCallExpression(node)
            && ts.isPropertyAccessExpression(node.expression)
            && ["map", "flatMap", "filter", "find", "findIndex", "forEach", "reduce", "reduceRight", "some", "every"].includes(node.expression.name.text)
          ) {
            for (const argument of node.arguments) {
              const callback = this.resolvedLocation(argument);
              if (!callback || callback.id === callable.id) continue;
              const invocation = `${node.expression.getText(node.getSourceFile())}(${argument.getText(node.getSourceFile())})`;
              edges.push({
                callerPath: callable.path,
                callerSymbol: callable.symbol,
                calleePath: callback.path,
                calleeSymbol: callback.symbol,
                invocation,
                invocationSha256: sha256(invocation),
                callStart: node.getStart(node.getSourceFile()),
              });
            }
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
        if (
          ts.isCallExpression(node)
          && ts.isPropertyAccessExpression(node.expression)
          && ts.isIdentifier(node.expression.expression)
          && node.expression.expression.text === "assert"
          && node.arguments[0]
        ) {
          const source = node.getSourceFile();
          const subject = node.arguments[0].getText(source);
          const matcher = node.expression.name.text;
          const expected = node.arguments.slice(1).map((argument) => argument.getText(source));
          observations.push({
            fingerprint: sha256(JSON.stringify({ subject, matcher, expected })),
            subject,
            matcher,
            expected,
            callStart: node.getStart(source),
          });
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
