import {
  runStdioRuntime,
  runtimeContract,
} from "../../../packages/runtime/claude-runtime/src/index.ts";
import { E01RuntimeCoordinator } from "../../../packages/runtime/claude-runtime/src/e01/coordinator.ts";

type ContractSurface = "health" | "snapshot" | "inventory" | "query" | "session" | "tools";
type JsonWriter = (value: unknown) => void;

const CONTRACT_COMMANDS: Readonly<Record<string, ContractSurface>> = Object.freeze({
  "--health": "health",
  "--snapshot": "snapshot",
  "--inventory": "inventory",
  "--query-contract": "query",
  "--session-contract": "session",
  "--tool-loop-contract": "tools",
});

const stdoutJson: JsonWriter = (value) => {
  process.stdout.write(JSON.stringify(value) + "\n");
};

export class CodeWorkerApplication {
  private readonly writeJson: JsonWriter;

  constructor(writeJson: JsonWriter = stdoutJson) {
    this.writeJson = writeJson;
  }

  async run(args: readonly string[]): Promise<number> {
    const command = args[0] ?? "--health";
    if (command === "--stdio") return this.runTaskRuntime();
    if (command === "--e01-inventory") return this.runRuntimeInventory();
    const surface = CONTRACT_COMMANDS[command];
    if (!surface) {
      this.writeJson({ ok: false, error: "unsupported_code_worker_command", command });
      return 2;
    }
    this.writeJson(runtimeContract(surface));
    return 0;
  }

  private async runTaskRuntime(): Promise<number> {
    await runStdioRuntime();
    return process.exitCode ?? 0;
  }

  private async runRuntimeInventory(): Promise<number> {
    const coordinator = new E01RuntimeCoordinator("inventory", "inventory");
    await coordinator.bootstrap();
    this.writeJson({ ok: true, ...coordinator.inventory(), snapshot: coordinator.snapshot() });
    return 0;
  }
}

export async function main(args: readonly string[] = process.argv.slice(2)): Promise<number> {
  const application = new CodeWorkerApplication();
  return application.run(args);
}

process.exitCode = await main();
