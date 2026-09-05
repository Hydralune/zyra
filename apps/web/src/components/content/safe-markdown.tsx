import { Fragment, useMemo, type ReactNode } from "react"
import { CopyButton } from "./copy-button.tsx"
import {
  parseSafeMarkdown,
  type MarkdownBlock,
  type MarkdownInline,
} from "../../features/artifacts/viewers.ts"

export interface SafeMarkdownProps {
  text: string
  className?: string
  streaming?: boolean
  truncated?: boolean
}

/**
 * Renders backend-authored Markdown as inert React nodes.  The shared parser
 * refuses raw HTML and unsafe links; no `dangerouslySetInnerHTML` path exists.
 */
export function SafeMarkdown({
  text,
  className,
  streaming = false,
  truncated = false,
}: SafeMarkdownProps) {
  const blocks = useMemo(() => parseSafeMarkdown(text), [text])
  return (
    <article
      className={className}
      data-streaming={streaming || undefined}
      data-truncated={truncated || undefined}
    >
      {truncated ? (
        <p className="product-markdown-notice">
          较早的实时内容已折叠；任务结束后将显示完整回答。
        </p>
      ) : null}
      {blocks.map((block, index) => (
        <MarkdownBlockView
          key={`${block.kind}:${block.sourceLine}:${index}`}
          block={block}
        />
      ))}
      {streaming ? <span className="product-stream-cursor" aria-hidden="true" /> : null}
    </article>
  )
}

function MarkdownBlockView({ block }: { block: MarkdownBlock }) {
  if (block.kind === "heading") {
    const content = renderInline(block.children)
    if (block.level === 1) return <h1>{content}</h1>
    if (block.level === 2) return <h2>{content}</h2>
    if (block.level === 3) return <h3>{content}</h3>
    if (block.level === 4) return <h4>{content}</h4>
    if (block.level === 5) return <h5>{content}</h5>
    return <h6>{content}</h6>
  }
  if (block.kind === "paragraph") return <p>{renderInline(block.children)}</p>
  if (block.kind === "code") {
    return (
      <div className="product-code-block">
      <div className="product-code-toolbar"><span>{block.language || "代码"}</span><CopyButton text={block.text} label="复制代码" /></div>
      <pre data-language={block.language}>
        <code>{block.text}</code>
        {!block.closed ? <span className="product-markdown-holdback">代码块仍在生成</span> : null}
      </pre>
      </div>
    )
  }
  if (block.kind === "quote") return <blockquote>{renderInline(block.children)}</blockquote>
  if (block.kind === "rule") return <hr />
  if (block.kind === "notice") {
    return <p className={`product-markdown-notice tone-${block.tone}`}>{block.text}</p>
  }
  if (block.kind === "list") {
    const items = block.items.map((item) => (
      <li key={`${item.sourceLine}:${inlineText(item.children)}`}>
        {item.checked !== undefined ? (
          <input type="checkbox" checked={item.checked} readOnly tabIndex={-1} />
        ) : null}
        {renderInline(item.children)}
      </li>
    ))
    return block.ordered ? <ol start={block.start}>{items}</ol> : <ul>{items}</ul>
  }
  return (
    <div className="product-markdown-table-wrap">
      <table>
        <thead>
          <tr>
            {block.header.map((cell, index) => (
              <th key={index} style={{ textAlign: block.alignments[index] }}>
                {renderInline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} style={{ textAlign: block.alignments[cellIndex] }}>
                  {renderInline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function renderInline(nodes: readonly MarkdownInline[]): ReactNode {
  return nodes.map((node, index) => {
    const key = `${node.kind}:${index}`
    if (node.kind === "text") return <Fragment key={key}>{node.text}</Fragment>
    if (node.kind === "code") return <code key={key}>{node.text}</code>
    if (node.kind === "break") return <br key={key} />
    if (node.kind === "emphasis") return <em key={key}>{renderInline(node.children)}</em>
    if (node.kind === "strong") return <strong key={key}>{renderInline(node.children)}</strong>
    if (node.kind === "strike") return <del key={key}>{renderInline(node.children)}</del>
    if (!node.link.allowed || !node.link.href) {
      return (
        <span key={key} className="product-link-refused" title={node.link.reason}>
          {renderInline(node.children)}
        </span>
      )
    }
    return (
      <a
        key={key}
        href={node.link.href}
        target={node.link.external ? "_blank" : undefined}
        rel={node.link.external ? "noopener noreferrer" : undefined}
        referrerPolicy="no-referrer"
      >
        {renderInline(node.children)}
      </a>
    )
  })
}

function inlineText(nodes: readonly MarkdownInline[]): string {
  return nodes.map((node) => {
    if (node.kind === "text" || node.kind === "code") return node.text
    if (node.kind === "break") return "\n"
    return inlineText(node.children)
  }).join("")
}
