import { describe, expect, test } from "bun:test"
import { renderToStaticMarkup } from "react-dom/server"
import type { ApiHealth } from "../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../src/app/runtime.ts"
import { LongHorizonLaunchPanel, LONG_HORIZON_TASK_PROFILES } from "../src/features/long-horizon/launch/view.tsx"

function store<T>(value: T) {
  const snapshot = () => value
  return { subscribe: () => () => {}, getSnapshot: snapshot }
}

/**
 * The panel reads only the workbench snapshot, so the runtime stub carries the
 * benchmark report through `runtime.health`.  `bound` decides whether the
 * launch button is offered at all.
 */
function fakeRuntime(options: {
  bound?: boolean
  transportEnabled?: boolean
} = {}): WorkbenchRuntime {
  const health: ApiHealth = {
    status: "ok",
    phase: "runtime",
    service: "zyra-api",
    apiVersion: "1.0",
    capabilities: ["typed_transport"],
    benchmark: {
      bound: options.bound ?? true,
      longHorizon: options.bound ?? true,
    },
    raw: {},
  }
  return {
    workbench: store({
      runtime: { phase: "ready", health },
      transportEnabled: options.transportEnabled ?? true,
      list: { tasks: [] },
      detail: {},
    }),
    api: {
      lifecycle: {
        create: async () => {
          throw new Error("create must not be called in a render test")
        },
        resume: async () => {
          throw new Error("resume must not be called in a render test")
        },
      },
    },
    router: { openTask: () => {} },
  } as unknown as WorkbenchRuntime
}

function render(runtime: WorkbenchRuntime): string {
  return renderToStaticMarkup(<LongHorizonLaunchPanel runtime={runtime} />)
}

describe("long-horizon launch panel", () => {
  test("offers every task profile when a benchmark container is bound", () => {
    const markup = render(fakeRuntime({ bound: true }))
    expect(markup).toContain("基准容器已绑定")
    expect(markup).toContain("运行此任务")
    // A bound panel must not carry the unavailable hint.
    expect(markup).not.toContain("没有绑定基准容器")
    expect(markup).not.toContain("disabled")
  })

  test("offers one button per profile, each with its own goal", () => {
    // Two profiles sharing a container and a repository is the point: the
    // workbench can only bind one container per API process, so a second real
    // task has to differ by method rather than by repository.
    const markup = render(fakeRuntime({ bound: true }))
    const buttons = markup.match(/运行此任务/g) ?? []
    expect(buttons.length).toBe(LONG_HORIZON_TASK_PROFILES.length)
    expect(LONG_HORIZON_TASK_PROFILES.length).toBeGreaterThan(1)
    const goals = LONG_HORIZON_TASK_PROFILES.map((profile) => profile.goal("nonce"))
    expect(new Set(goals).size).toBe(goals.length)
    // Every profile must demand a delivered change, not a description of one.
    for (const goal of goals) expect(goal).toContain("do not only describe a patch")
    // The profiles must be told apart by their method, not merely by their name.
    expect(goals[1]).toContain("failing test before changing any production code")
  })

  test("names the profiles so the difference is visible before launching", () => {
    const markup = render(fakeRuntime({ bound: true }))
    for (const profile of LONG_HORIZON_TASK_PROFILES) {
      expect(markup).toContain(profile.title)
      expect(markup).toContain(profile.detail)
    }
  })

  test("refuses to promise a launch when no container is bound", () => {
    // Starting a sealed task without a container fails closed on the backend, so
    // the panel has to disable the affordance and name the reason rather than
    // render a button that appears broken when clicked.
    const markup = render(fakeRuntime({ bound: false }))
    expect(markup).toContain("未绑定基准容器")
    expect(markup).toContain("没有绑定基准容器")
    expect(markup).toContain("dev_up_swebench.py")
    expect(markup).toContain("disabled")
  })

  test("stays unavailable when the typed transport is down", () => {
    const markup = render(fakeRuntime({ bound: true, transportEnabled: false }))
    expect(markup).toContain("disabled")
  })

  test("names the click phase instead of leaving a silent button", () => {
    // The launch pauses on two network calls before anything visibly happens.
    const markup = render(fakeRuntime({ bound: true }))
    expect(markup).not.toContain("aria-busy=\"true\"")
    expect(markup).not.toContain("正在创建任务")
  })
})
