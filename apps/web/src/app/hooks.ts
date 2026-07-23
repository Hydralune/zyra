import { useEffect, useMemo, useState, useSyncExternalStore } from "react"
import type { WorkbenchRuntime } from "./runtime.ts"
import type { WorkbenchRoute } from "../shell/router.ts"
import type { ProjectionSelector } from "../state/contracts.ts"

export function useWorkbenchSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.workbench.subscribe,
    runtime.workbench.getSnapshot,
    runtime.workbench.getSnapshot,
  )
}

export function useCommandSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.commands.subscribe,
    runtime.commands.getSnapshot,
    runtime.commands.getSnapshot,
  )
}

export function useQueueSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.queue.subscribe,
    runtime.queue.getSnapshot,
    runtime.queue.getSnapshot,
  )
}

export function useOverlaySnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.overlays.subscribe,
    runtime.overlays.getSnapshot,
    runtime.overlays.getSnapshot,
  )
}

export function useLayoutSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.layout.subscribe,
    runtime.layout.getSnapshot,
    runtime.layout.getSnapshot,
  )
}

export function useAnnouncementSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.announcer.subscribe,
    runtime.announcer.getSnapshot,
    runtime.announcer.getSnapshot,
  )
}

export function useProjectionSelector<T>(
  runtime: WorkbenchRuntime,
  selector: ProjectionSelector<T>,
): T {
  const adapter = useMemo(
    () => runtime.projections.externalSelector(selector),
    [runtime, selector.key],
  )
  return useSyncExternalStore(
    adapter.subscribe,
    adapter.getSnapshot,
    adapter.getSnapshot,
  )
}

export function useRoute(runtime: WorkbenchRuntime): WorkbenchRoute {
  const [route, setRoute] = useState(() => runtime.router.start())
  useEffect(() => runtime.router.listen((next) => setRoute(next)), [runtime])
  return route
}

export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  )
  useEffect(() => {
    const onlineListener = () => setOnline(true)
    const offlineListener = () => setOnline(false)
    window.addEventListener("online", onlineListener)
    window.addEventListener("offline", offlineListener)
    return () => {
      window.removeEventListener("online", onlineListener)
      window.removeEventListener("offline", offlineListener)
    }
  }, [])
  return online
}
