import { OPERATION_NAMES } from "@zyra/typed-api-client"
import { CliApi } from "./api.ts"
import { CliExitCode, CliVerifierError, type ScenarioCommand } from "./contracts.ts"
import type { CliOutput } from "./output.ts"
import type { CommandOutcome } from "./runner.ts"

export async function executeScenario(input: {
  command: ScenarioCommand
  api: CliApi
  output: CliOutput
}): Promise<CommandOutcome> {
  const command = input.command
  let payload: Readonly<Record<string, unknown>>
  let runId = command.runId
  let taskId: string | undefined
  let runStatus = "completed"

  if (command.action === "registry") {
    payload = await input.api.scenarioRegistry()
  } else if (command.action === "ls") {
    payload = await input.api.scenarioList({
      includeArchived: command.includeArchived,
      limit: command.limit,
      offset: command.offset,
    })
  } else if (command.action === "create") {
    const run = await input.api.scenarioCreate({
      scenarioId: command.scenarioId,
      definitionVersion: command.definitionVersion,
      profileId: command.profileId,
      policyId: command.policyId,
      policyDigest: command.policyDigest,
      mode: command.mode,
      value: command.input!,
      seed: command.seed,
      labels: command.labels,
      preflight: command.preflight,
    })
    payload = run.raw
    runId = run.scenario_run_id
    taskId = run.task_id
    runStatus = run.phase
  } else if (command.action === "start") {
    const run = await input.api.scenarioMutate(
      OPERATION_NAMES.scenarioRunStart,
      command.runId!,
      {
        wait: command.wait,
        timeout_seconds: Math.max(1, Math.ceil(command.timeoutMs / 1_000)),
      },
    )
    payload = run.raw
    runId = run.scenario_run_id
    taskId = run.task_id
    runStatus = run.phase
  } else if (command.action === "cancel") {
    const run = await input.api.scenarioMutate(
      OPERATION_NAMES.scenarioRunCancel,
      command.runId!,
      { reason: command.reason || "Cancelled by Zyra CLI." },
    )
    payload = run.raw
    runId = run.scenario_run_id
    taskId = run.task_id
    runStatus = run.phase
  } else if (command.action === "verify") {
    payload = await input.api.scenarioVerify(command.runId!)
    const receipt = payload.verification_receipt
    if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || (receipt as Record<string, unknown>).valid !== true) {
      throw new CliVerifierError("Scenario verification did not produce a valid receipt.")
    }
  } else {
    payload = await input.api.scenarioEvidence(command.runId!)
  }

  input.output.event(
    {
      schema: "zyra.cli-scenario-event.v1",
      action: command.action,
      response: payload,
    },
    { taskId, runId },
  )
  return {
    exitCode: command.action === "start" && runStatus === "failed"
      ? CliExitCode.TASK_FAILED
      : CliExitCode.SUCCESS,
    status: runStatus,
    taskId,
    runId,
    result: {
      schema: "zyra.cli-scenario-result.v1",
      action: command.action,
      response: payload,
    },
  }
}
