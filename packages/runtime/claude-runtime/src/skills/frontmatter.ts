import { createHash } from "node:crypto";

import type { JsonObject } from "../contracts.ts";

export interface MarkdownDocument {
  attributes: JsonObject;
  body: string;
  digest: string;
}

export function parseMarkdownDocument(content: string): MarkdownDocument {
  const normalized = content.replaceAll("\r\n", "\n");
  if (!normalized.startsWith("---\n")) {
    return { attributes: {}, body: normalized, digest: digest(normalized) };
  }
  const end = normalized.indexOf("\n---\n", 4);
  if (end < 0) {
    return { attributes: {}, body: normalized, digest: digest(normalized) };
  }
  const frontmatter = normalized.slice(4, end);
  const attributes: JsonObject = {};
  for (const rawLine of frontmatter.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const separator = line.indexOf(":");
    if (separator <= 0) {
      continue;
    }
    const key = line.slice(0, separator).trim();
    const rawValue = line.slice(separator + 1).trim();
    if (rawValue.startsWith("[") && rawValue.endsWith("]")) {
      attributes[key] = rawValue.slice(1, -1).split(",")
        .map((item) => unquote(item.trim()))
        .filter(Boolean);
    } else if (rawValue === "true" || rawValue === "false") {
      attributes[key] = rawValue === "true";
    } else {
      attributes[key] = unquote(rawValue);
    }
  }
  return {
    attributes,
    body: normalized.slice(end + 5),
    digest: digest(normalized),
  };
}

function unquote(value: string): string {
  if ((value.startsWith('"') && value.endsWith('"'))
    || (value.startsWith("'") && value.endsWith("'"))) {
    return value.slice(1, -1);
  }
  return value;
}

function digest(value: string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}
