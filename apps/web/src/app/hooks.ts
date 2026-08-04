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

export function useLiveSyncSnapshot(runtime: WorkbenchRuntime) {
  return useSyncExternalStore(
    runtime.liveSync.subscribe,
    runtime.liveSync.getSnapshot,
    runtime.liveSync.getSnapshot,
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

/**
 * Mirrors a CSS media query into React state so JavaScript-owned behaviour
 * (focus trapping, `inert`, drawer semantics) stays aligned with the breakpoint
 * the stylesheet actually uses.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" || typeof window.matchMedia !== "function"
      ? false
      : window.matchMedia(query).matches,
  )
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return
    }
    const list = window.matchMedia(query)
    const update = () => setMatches(list.matches)
    update()
    list.addEventListener("change", update)
    return () => list.removeEventListener("change", update)
  }, [query])
  return matches
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
