import { useRef, type PointerEvent } from "react"
import type { WorkbenchRuntime } from "../../app/runtime.ts"

export function PaneDivider({ runtime }: { runtime: WorkbenchRuntime }) {
  const origin = useRef<{ x: number; width: number } | undefined>(undefined)
  const pointerMove = (event: globalThis.PointerEvent) => {
    const value = origin.current
    if (!value) return
    runtime.layout.setTaskListWidth(value.width + event.clientX - value.x)
  }
  const stop = () => {
    origin.current = undefined
    window.removeEventListener("pointermove", pointerMove)
    window.removeEventListener("pointerup", stop)
    window.removeEventListener("pointercancel", stop)
  }
  const start = (event: PointerEvent<HTMLDivElement>) => {
    if (runtime.layout.getSnapshot().mode === "mobile") return
    origin.current = {
      x: event.clientX,
      width: runtime.layout.getSnapshot().taskListWidth,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
    window.addEventListener("pointermove", pointerMove)
    window.addEventListener("pointerup", stop)
    window.addEventListener("pointercancel", stop)
  }
  return (
    <div
      className="pane-divider"
      role="separator"
      aria-label="Resize task list"
      aria-orientation="vertical"
      aria-valuemin={280}
      aria-valuemax={Math.max(280, runtime.layout.getSnapshot().viewportWidth - 360)}
      aria-valuenow={runtime.layout.getSnapshot().taskListWidth}
      tabIndex={0}
      onPointerDown={start}
      onDoubleClick={() => runtime.layout.resetTaskListWidth()}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault()
          runtime.layout.resizeTaskList(event.shiftKey ? -40 : -10)
        } else if (event.key === "ArrowRight") {
          event.preventDefault()
          runtime.layout.resizeTaskList(event.shiftKey ? 40 : 10)
        } else if (event.key === "Home") {
          event.preventDefault()
          runtime.layout.setTaskListWidth(280)
        }
      }}
    />
  )
}
