import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, readFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import type { DoctorCommand } from "../src/contracts.ts"
import {
  DOCTOR_SCHEMA,
  executeDoctor,
  writeDoctorBundle,
  type DoctorReport,
} from "../src/product/diagnostics/doctor.ts"

const temporaryDirectories: string[] = []

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true, force: true })))
})

async function directory(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), "zyra-doctor-"))
  temporaryDirectories.push(path)
  return path
}

function command(bundle?: string): DoctorCommand {
  return {
    kind: "doctor",
    baseUrl: "http://127.0.0.1:18998",
    autoStart: false,
    startupTimeoutMs: 5_000,
    timeoutMs: 0,
    bundle,
  }
}

function report(): DoctorReport {
  return {
    schema: DOCTOR_SCHEMA,
    healthy: false,
    status: "degraded",
    generated_at: "2026-09-01T00:00:00.000Z",
    cli: { version: "0.1.0" },
    workspace: { cwd: "G:\\private\\workspace" },
    daemon: { authorization: "Bearer top-secret", origin: "http://127.0.0.1:8000" },
    runtime: { blockers: ["failed at G:\\private\\workspace\\state.json token=opaque-secret"] },
    providers: { password: "provider-secret" },
    checks: [],
  }
}

describe("product doctor", () => {
  test("does not mistake an API bearer token for a configured model provider", async () => {
    const workspace = await directory()
    const result = await executeDoctor({
      command: command(),
      cwd: workspace,
      token: "api-access-token-is-not-a-model-credential",
      overrides: {
        daemon: {
          reachable: true,
          managed: false,
          baseUrl: "http://127.0.0.1:18998",
          staleState: false,
        },
        readiness: {
          ready: true,
          status: "ready",
          apiVersion: "1.0",
          owners: { task_store: true },
          blockers: [],
          raw: {
            details: {
              provider_configuration: {
                configured: false,
                provider_ids: [],
                selected_provider_id: null,
                selected_model_id: null,
              },
            },
          },
        },
        providerIds: ["deepseek"],
        modelCount: 1,
      },
    })

    expect(result.report.healthy).toBe(false)
    expect(result.report.providers).toMatchObject({
      catalog_available: true,
      configuration_present: false,
      configured_provider_ids: [],
    })
    expect(result.report.checks).toContainEqual(expect.objectContaining({
      id: "providers.configuration",
      status: "fail",
    }))
  })

  test("diagnoses a foreign occupied port without starting or trusting it", async () => {
    const workspace = await directory()
    const result = await executeDoctor({
      command: command(),
      cwd: workspace,
      overrides: {
        daemon: {
          reachable: false,
          managed: false,
          baseUrl: "http://127.0.0.1:18998",
          staleState: false,
        },
        originOccupied: true,
        providerIds: [],
        modelCount: 0,
      },
    })

    expect(result.report.schema).toBe(DOCTOR_SCHEMA)
    expect(result.report.status).toBe("offline")
    expect(result.report.checks).toContainEqual(expect.objectContaining({
      id: "daemon.health",
      status: "fail",
      summary: expect.stringContaining("非 Zyra"),
    }))
    expect(result.report.daemon).toMatchObject({ foreign_origin_occupied: true })
  })

  test("writes a redacted, exclusive workspace-local bundle", async () => {
    const workspace = await directory()
    const result = await writeDoctorBundle(report(), workspace, "doctor.json")
    const material = await readFile(result.path, "utf8")

    expect(result.path).toBe(join(workspace, "doctor.json"))
    expect(material).toContain("zyra.cli-diagnostic-bundle/v1")
    expect(material).not.toContain("top-secret")
    expect(material).not.toContain("opaque-secret")
    expect(material).not.toContain("provider-secret")
    expect(material).not.toContain("G:\\\\private")
    expect(material).toContain("[redacted]")
    expect(material).toContain("[redacted-path]")
    await expect(writeDoctorBundle(report(), workspace, "doctor.json"))
      .rejects.toMatchObject({ code: "doctor_bundle_exists" })
  })

  test("refuses to place a bundle outside the workspace", async () => {
    const workspace = await directory()
    await expect(writeDoctorBundle(report(), workspace, resolve(workspace, "..", "doctor.json")))
      .rejects.toMatchObject({ code: "doctor_bundle_path_outside_workspace" })
  })
})
