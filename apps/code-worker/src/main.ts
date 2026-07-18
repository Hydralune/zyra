import {
  runStdioRuntime,
  runtimeContract,
} from "../../../packages/runtime/claude-runtime/src/index.ts";
import { E01RuntimeCoordinator } from "../../../packages/runtime/claude-runtime/src/e01/coordinator.ts";
import { E02CapabilityCoordinator } from "../../../packages/runtime/claude-runtime/src/e02/coordinator.ts";
import { runE02ApiPort } from "../../../packages/runtime/claude-runtime/src/e02/api-port-runtime.ts";
import { runStructuredControlStdio } from "../../../packages/runtime/claude-runtime/src/control/stdio.ts";
import { E03AgentControlCoordinator } from "../../../packages/runtime/claude-runtime/src/e03/coordinator.ts";
import { runCurrentEntryStdioProbe } from "../../../packages/runtime/claude-runtime/src/stdio-probe.ts";

export const DEFAULT_CAPABILITY_ENTRYPOINT =
  E02CapabilityCoordinator.prototype.execute;
export const DEFAULT_AGENT_CONTROL_ENTRYPOINT =
  E03AgentControlCoordinator.prototype.execute;

type ContractSurface =
  | "health"
  | "snapshot"
  | "inventory"
  | "query"
  | "session"
  | "tools";
type JsonWriter = (value: unknown) => void;

const CONTRACT_COMMANDS: Readonly<Record<string, ContractSurface>> =
  Object.freeze({
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
    if (command === "--stdio-probe") {
      const result = await runCurrentEntryStdioProbe();
      this.writeJson(result);
      return result.ok === true ? 0 : 1;
    }
    if (command === "--stdio") return this.runTaskRuntime();
    if (command === "--e02-api") return this.runCapabilityApiPort();
    if (command === "--e03-control") return this.runAgentControlPort();
    if (command === "--e01-inventory") return this.runRuntimeInventory();
    const surface = CONTRACT_COMMANDS[command];
    if (!surface) {
      this.writeJson({
        ok: false,
        error: "unsupported_code_worker_command",
        command,
      });
      return 2;
    }
    const contract = runtimeContract(surface);
    this.writeJson(contract);
    if (surface === "health") {
      const productized = contract.productizedRuntime;
      const e02 = contract.e02CapabilityRuntime;
      const e03 = contract.e03AgentControlRuntime;
      if (
        !productized ||
        typeof productized !== "object" ||
        Array.isArray(productized) ||
        productized.complete !== true ||
        productized.evidenceIntegrity !== true ||
        !e02 ||
        typeof e02 !== "object" ||
        Array.isArray(e02) ||
        e02.implementationReady !== true ||
        e02.canonicalEntrypoint !== "E02CapabilityCoordinator.execute" ||
        e02.stateJournalOwner !== "E02CapabilityCoordinator" ||
        e02.pythonDecisionFallback !== false ||
        !e03 ||
        typeof e03 !== "object" ||
        Array.isArray(e03) ||
        e03.implementationReady !== true ||
        e03.canonicalEntrypoint !== "E03AgentControlCoordinator.execute" ||
        e03.defaultTaskEntrypoint !== "CodeWorkerApplication.runTaskRuntime" ||
        e03.builtControlEntrypoint !==
          "CodeWorkerApplication.runAgentControlPort" ||
        e03.stateJournalOwner !== "DurableTaskRegistry" ||
        e03.pythonLogicalOwner !== false ||
        e03.pythonLogicalFallback !== false
      ) {
        return 1;
      }
    }
    return 0;
  }

  private async runTaskRuntime(): Promise<number> {
    await runStdioRuntime();
    return process.exitCode ?? 0;
  }

  private async runCapabilityApiPort(): Promise<number> {
    await runE02ApiPort();
    return process.exitCode ?? 0;
  }

  private async runAgentControlPort(): Promise<number> {
    await runStructuredControlStdio();
    return process.exitCode ?? 0;
  }

  private async runRuntimeInventory(): Promise<number> {
    const coordinator = new E01RuntimeCoordinator("inventory", "inventory");
    await coordinator.bootstrap();
    this.writeJson({
      ok: true,
      ...coordinator.inventory(),
      snapshot: coordinator.snapshot(),
    });
    return 0;
  }
}

export async function main(
  args: readonly string[] = process.argv.slice(2),
): Promise<number> {
  const application = new CodeWorkerApplication();
  return application.run(args);
}

process.exitCode = await main();
