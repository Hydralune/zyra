import { useEffect, useState } from "react"

export function CopyButton({ text, label = "复制回复" }: { text: string; label?: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle")
  useEffect(() => setStatus("idle"), [text])
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setStatus("copied")
    } catch { setStatus("failed") }
  }
  return <span className="copy-control">
    <button type="button" className="product-button product-button-quiet" aria-label={label} onClick={() => void copy()}>
      {status === "copied" ? "已复制" : label}
    </button>
    <span className={status === "failed" ? "product-error-copy" : "sr-only"} role="status">
      {status === "copied" ? `${label}成功` : status === "failed" ? "复制失败，请选中文字手动复制。" : ""}
    </span>
  </span>
}
