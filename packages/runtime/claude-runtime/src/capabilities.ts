import type {
  JsonObject,
  RuntimeRunInput,
  ToolSpecContract,
} from "./contracts.ts";
import type {
  AgentCapabilityResult,
  AgentExecutionContext,
} from "./agents/index.ts";
import {
  E02CapabilityCoordinator,
  type E02AuthorizationInput,
  type E02AuthorizationResult,
  type E02CapabilityCoordinatorSnapshot,
  type E02CoordinatorPorts,
  type E02ExecutionContext,
  type E02ExecutionReceipt,
} from "./e02/index.ts";
import type { McpCoordinatorExecution } from "../../../integrations/claude-mcp/src/index.ts";
import type { SkillCoordinatorExecution } from "./skills/index.ts";
import type { PluginCoordinatorExecution } from "./plugins/index.ts";
import type { CommandCoordinatorExecution } from "./commands/index.ts";

export type CapabilityExecutionResult =
  | McpCoordinatorExecution
  | SkillCoordinatorExecution
  | PluginCoordinatorExecution
  | CommandCoordinatorExecution
  | AgentCapabilityResult;

export class TypeScriptCapabilityRuntime {
  readonly e02: E02CapabilityCoordinator;

  private constructor(e02: E02CapabilityCoordinator) {
    this.e02 = e02;
  }

  static async open(
    input: RuntimeRunInput,
    ports: E02CoordinatorPorts = {},
  ): Promise<TypeScriptCapabilityRuntime> {
    return new TypeScriptCapabilityRuntime(
      await E02CapabilityCoordinator.open(input, ports),
    );
  }

  mergeToolSpecs(existing: ToolSpecContract[]): ToolSpecContract[] {
    return this.e02.mergeToolSpecs(existing);
  }

  toolSpecs(): ToolSpecContract[] {
    return this.e02.toolSpecs();
  }

  owns(toolName: string): boolean {
    return this.e02.owns(toolName);
  }

  owner(toolName: string): string {
    return this.e02.owner(toolName);
  }

  authorize(input: E02AuthorizationInput): Promise<E02AuthorizationResult> {
    return this.e02.authorize(input);
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    agentContext?: AgentExecutionContext,
    executionContext: Partial<E02ExecutionContext> = {},
  ): Promise<CapabilityExecutionResult> {
    const receipt = await this.executeWithReceipt(
      toolName,
      argumentsValue,
      agentContext,
      executionContext,
    );
    return receipt.result as CapabilityExecutionResult;
  }

  executeWithReceipt(
    toolName: string,
    argumentsValue: JsonObject,
    agentContext?: AgentExecutionContext,
    executionContext: Partial<E02ExecutionContext> = {},
  ): Promise<E02ExecutionReceipt> {
    const toolCallId = executionContext.toolCallId
      || `direct:${toolName}:${this.e02.runtime.workerRequestId}`;
    return this.e02.execute(toolName, argumentsValue, {
      ...executionContext,
      toolCallId,
      agentContext,
    });
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    await this.e02.drainBackground(context);
  }

  snapshot(): E02CapabilityCoordinatorSnapshot {
    return this.e02.snapshot();
  }

  health(): ReturnType<E02CapabilityCoordinator["health"]> {
    return this.e02.health();
  }

  async close(): Promise<void> {
    await this.e02.close();
  }
}
