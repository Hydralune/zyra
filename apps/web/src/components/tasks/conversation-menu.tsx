import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"

export function ConversationIcon({ kind }: { kind: "rename" | "pin" | "delete" }) {
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === "rename" ? <><path d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14z" /></>
      : kind === "pin" ? <><path d="m8 3 8 0-1 6 4 4v2H5v-2l4-4zM12 15v7" /></>
        : <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" /></>}
  </svg>
}

export function ConversationMenu({ anchor, title, pinned, canDelete, onClose, onRename, onPin, onDelete }: {
  anchor: HTMLElement
  title: string
  pinned: boolean
  canDelete: boolean
  onClose: () => void
  onRename: (title: string) => Promise<void>
  onPin: () => void
  onDelete: () => Promise<void>
}) {
  const root = useRef<HTMLDivElement>(null)
  const [mode, setMode] = useState<"menu" | "rename" | "delete">("menu")
  const [name, setName] = useState(title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [position, setPosition] = useState({ top: 0, left: 0 })
  useLayoutEffect(() => {
    if (!root.current) return
    const rect = anchor.getBoundingClientRect()
    const bounds = root.current.getBoundingClientRect()
    setPosition({ left: Math.max(8, Math.min(rect.right - bounds.width, window.innerWidth - bounds.width - 8)),
      top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - bounds.height - 8)) })
    const control = root.current.querySelector<HTMLInputElement | HTMLButtonElement>("input,button")
    control?.focus()
    if (control instanceof HTMLInputElement) control.select()
  }, [anchor, mode, error])
  useEffect(() => {
    const dismiss = (event: Event) => {
      if (!busy && event.target instanceof Node && !root.current?.contains(event.target) && !anchor.contains(event.target)) onClose()
    }
    document.addEventListener("pointerdown", dismiss)
    document.addEventListener("scroll", dismiss, true)
    const resize = () => { if (!busy) onClose() }
    window.addEventListener("resize", resize)
    return () => {
      document.removeEventListener("pointerdown", dismiss)
      document.removeEventListener("scroll", dismiss, true)
      window.removeEventListener("resize", resize)
    }
  }, [anchor, busy, onClose])
  useEffect(() => () => {
    if (anchor.isConnected) anchor.focus({ preventScroll: true })
    else document.getElementById("workbench-command-input")?.focus({ preventScroll: true })
  }, [anchor])
  const run = async (action: () => void | Promise<void>) => {
    setBusy(true); setError("")
    try { await action(); onClose() }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败，请重试。") }
    finally { setBusy(false) }
  }
  return createPortal(<div ref={root} className="product-conversation-popover" style={position}
    role={mode === "menu" ? "menu" : "dialog"} aria-label={mode === "menu" ? "会话操作" : mode === "rename" ? "重命名会话" : "删除会话"}
    onKeyDown={(event) => {
      if (event.key === "Escape") { event.stopPropagation(); if (!busy) onClose() }
      const controls = [...(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)') ?? [])]
      const index = controls.indexOf(document.activeElement as HTMLElement)
      if (event.key === "Tab" || (mode === "menu" && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key))) {
        event.preventDefault()
        const next = event.key === "Home" ? 0 : event.key === "End" ? controls.length - 1
          : (index + (event.key === "ArrowUp" || (event.key === "Tab" && event.shiftKey) ? -1 : 1) + controls.length) % controls.length
        controls[next]?.focus()
      }
    }}>
    {mode === "menu" ? <>
      <button type="button" role="menuitem" onClick={() => setMode("rename")}><ConversationIcon kind="rename" />重命名</button>
      <button type="button" role="menuitem" onClick={() => void run(onPin)}><ConversationIcon kind="pin" />{pinned ? "取消置顶" : "置顶会话"}</button>
      <div className="conversation-menu-divider" />
      <button type="button" role="menuitem" className="conversation-delete" disabled={!canDelete} title={canDelete ? undefined : "任务结束或停止后可删除"} onClick={() => setMode("delete")}><ConversationIcon kind="delete" />删除</button>
    </> : mode === "rename" ? <form onSubmit={(event) => { event.preventDefault(); void run(() => onRename(name)) }}>
      <label>会话名称<input aria-label="会话名称" value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /></label>
      <div className="conversation-menu-actions"><button type="button" disabled={busy} onClick={onClose}>取消</button><button className="conversation-confirm" type="submit" disabled={busy}>{busy ? "保存中…" : "保存"}</button></div>
    </form> : <>
      <strong>删除此会话？</strong><p className="conversation-delete-title">{title}</p><p>删除后无法恢复此会话。</p>
      <div className="conversation-menu-actions"><button type="button" disabled={busy} onClick={onClose}>取消</button><button className="conversation-delete" type="button" disabled={busy} onClick={() => void run(onDelete)}>{busy ? "删除中…" : "删除"}</button></div>
    </>}
    {error ? <p className="conversation-menu-error" role="alert">{error}</p> : null}
  </div>, document.body)
}
