import type { JsonObject } from "../contracts.ts";

export interface SkillDescriptor {
  name: string;
  description: string;
  path: string;
  root: string;
  rootKind: string;
  precedence: number;
  allowedTools: string[];
  contentDigest: string;
  metadata: JsonObject;
}

export interface CommandDescriptor {
  name: string;
  description: string;
  path: string;
  root: string;
  namespace: string;
  contentDigest: string;
  metadata: JsonObject;
}

export interface SkillExecutionResult {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}
