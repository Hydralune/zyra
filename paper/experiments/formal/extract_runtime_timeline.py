#!/usr/bin/env python3
"""Extract a compact, auditable tool/compaction timeline from runtime snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def bounded(value: Any, limit: int = 600) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit // 2]}\n...[omitted]...\n{text[-limit // 2 :]}"


def extract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    calls: dict[str, dict[str, Any]] = {}

    # Tool inputs survive in model-iteration/compaction custody even when the
    # event stream intentionally stores only argument digests.
    for node in walk(data.get("typescript_runtime_snapshot", {})):
        if not isinstance(node, dict) or node.get("type") != "tool_use":
            continue
        call_id = str(node.get("id", "")).strip()
        if call_id:
            calls.setdefault(call_id, {}).update({
                "tool": node.get("name"),
                "arguments": node.get("input", {}),
            })

    compactions: list[dict[str, Any]] = []
    for item in data.get("transcript", []):
        metadata = item.get("metadata") or {}
        if item.get("event_type") == "context_compacted":
            compactions.append({
                "created_at": item.get("created_at"),
                "generation": metadata.get("compact_generation"),
                "removed_messages": metadata.get("removed_message_count"),
                "summary": bounded(metadata.get("compact_summary"), 1_200),
            })
        if item.get("type") != "tool":
            continue
        call_id = str(metadata.get("tool_call_id", "")).strip()
        result = metadata.get("tool_result") or {}
        output = result.get("output") or {}
        result_metadata = result.get("metadata") or {}
        calls.setdefault(call_id, {}).update({
            "created_at": item.get("created_at"),
            "turn_index": metadata.get("turn_index"),
            "tool": metadata.get("tool_name"),
            "ok": result.get("ok", metadata.get("ok")),
            "error": result.get("error", metadata.get("error")),
            "return_code": output.get("return_code", result_metadata.get("return_code")),
            "status": output.get("status"),
            "job_id": output.get("job_id"),
            "workspace_mutation_committed": result_metadata.get("workspace_mutation_committed"),
            "stdout": bounded(output.get("stdout")),
            "stderr": bounded(output.get("stderr")),
            "summary": result.get("summary", item.get("content")),
        })

    ordered = sorted(
        ({"tool_call_id": call_id, **record} for call_id, record in calls.items()),
        key=lambda record: (str(record.get("created_at") or ""), str(record["tool_call_id"])),
    )
    return {
        "source": str(path),
        "run_id": data.get("run_id"),
        "task_id": data.get("task_id"),
        "worker_request_id": data.get("worker_request_id"),
        "status": data.get("status"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "stats": data.get("stats", {}),
        "tools": ordered,
        "compactions": compactions,
    }


def markdown(payloads: list[dict[str, Any]]) -> str:
    lines = ["# Runtime forensic timeline", ""]
    for payload in payloads:
        lines.extend([
            f"## {payload['worker_request_id']}",
            "",
            f"- 状态：`{payload['status']}`",
            f"- 时间：`{payload['created_at']}` → `{payload['updated_at']}`",
            f"- 工具调用：{len(payload['tools'])}；上下文压缩：{len(payload['compactions'])}",
            "",
            "| 时间 | 轮次 | 工具 | 命令/参数 | 结果 | RC | 工作区变更 | 摘要 |",
            "|---|---:|---|---|---|---:|---|---|",
        ])
        for tool in payload["tools"]:
            arguments = json.dumps(tool.get("arguments", {}), ensure_ascii=False)
            diagnostic = tool.get("stderr") or tool.get("stdout") or tool.get("summary") or ""
            clean = bounded(diagnostic, 240).replace("\n", "<br>").replace("|", "\\|")
            command = bounded(arguments, 240).replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| {tool.get('created_at') or ''} | {tool.get('turn_index') if tool.get('turn_index') is not None else ''} "
                f"| `{tool.get('tool') or ''}` | `{command}` | `{tool.get('error') or ('ok' if tool.get('ok') else 'failed')}` "
                f"| {tool.get('return_code') if tool.get('return_code') is not None else ''} "
                f"| {tool.get('workspace_mutation_committed') or ''} | {clean} |"
            )
        lines.extend(["", "### 压缩边界", ""])
        for compaction in payload["compactions"]:
            lines.append(
                f"- `{compaction['created_at']}`：generation {compaction['generation']}，"
                f"移除 {compaction['removed_messages']} 条消息。摘要：{bounded(compaction['summary'], 320).replace(chr(10), ' ')}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshots", nargs="+", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    payloads = [extract(path) for path in args.snapshots]
    if args.json:
        args.json.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(payloads), encoding="utf-8")
    if not args.json and not args.markdown:
        print(json.dumps(payloads, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
