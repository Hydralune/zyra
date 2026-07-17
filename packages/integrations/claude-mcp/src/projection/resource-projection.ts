import type { JsonObject } from "../contracts.ts";
import type { McpCatalogServerSnapshot } from "../catalog/capability-catalog.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import type { McpResource, McpResourceTemplate } from "../core/protocol.ts";

export interface McpProjectedResource {
  projectionId: string;
  serverId: string;
  connectionId: string;
  catalogRevision: number;
  uri: string;
  name: string;
  title: string | null;
  description: string | null;
  mimeType: string | null;
  size: number | null;
  operation: "resources/read";
  capabilityDigest: string;
  permissionScope: JsonObject;
  invocation: JsonObject;
  metadata: JsonObject;
}

export interface McpProjectedResourceTemplate {
  projectionId: string;
  serverId: string;
  connectionId: string;
  catalogRevision: number;
  uriTemplate: string;
  variables: string[];
  name: string;
  title: string | null;
  description: string | null;
  mimeType: string | null;
  operation: "resources/read";
  capabilityDigest: string;
  permissionScope: JsonObject;
  invocation: JsonObject;
  metadata: JsonObject;
}

export interface McpResourceProjectionResult {
  serverId: string;
  connectionId: string;
  catalogRevision: number;
  resources: McpProjectedResource[];
  templates: McpProjectedResourceTemplate[];
  uriIndex: Record<string, string>;
  templateIndex: Record<string, string>;
  digest: string;
}

export class McpResourceProjection {
  materialize(server: McpCatalogServerSnapshot): McpResourceProjectionResult {
    const resources = server.resources
      .map((resource) => this.projectResource(server, resource))
      .sort((left, right) => left.uri.localeCompare(right.uri));
    const templates = server.resourceTemplates
      .map((template) => this.projectTemplate(server, template))
      .sort((left, right) => left.uriTemplate.localeCompare(right.uriTemplate));
    const uriIndex: Record<string, string> = {};
    const templateIndex: Record<string, string> = {};
    for (const resource of resources) uriIndex[resource.uri] = resource.projectionId;
    for (const template of templates) templateIndex[template.uriTemplate] = template.projectionId;
    const withoutDigest = {
      serverId: server.serverId,
      connectionId: server.connectionId,
      catalogRevision: server.revision,
      resources,
      templates,
      uriIndex,
      templateIndex,
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  resolveTemplate(template: McpProjectedResourceTemplate, values: Record<string, string>): string {
    let output = template.uriTemplate;
    for (const variable of template.variables) {
      const value = values[variable];
      if (value === undefined) throw new Error(`missing URI template variable ${variable}`);
      output = output.replaceAll(`{${variable}}`, encodeURIComponent(value));
      output = output.replace(new RegExp(`\\{${escapeRegExp(variable)}:[^}]+\\}`, "g"), encodeURIComponent(value));
    }
    if (/\{[^}]+\}/.test(output)) throw new Error(`unresolved URI template expression in ${template.uriTemplate}`);
    return output;
  }

  private projectResource(server: McpCatalogServerSnapshot, resource: McpResource): McpProjectedResource {
    const capabilityDigest = sha256(resource);
    return {
      projectionId: deterministicMcpId("mcp-resource-projection", {
        server_id: server.serverId,
        connection_id: server.connectionId,
        revision: server.revision,
        uri: resource.uri,
        capability_digest: capabilityDigest,
      }),
      serverId: server.serverId,
      connectionId: server.connectionId,
      catalogRevision: server.revision,
      uri: resource.uri,
      name: resource.name,
      title: resource.title,
      description: resource.description,
      mimeType: resource.mimeType,
      size: resource.size,
      operation: "resources/read",
      capabilityDigest,
      permissionScope: canonicalJson({
        namespace: "mcp",
        server_id: server.serverId,
        operation: "resources/read",
        resource_uri: resource.uri,
        read_only: true,
      }) as JsonObject,
      invocation: {
        method: "resources/read",
        params_shape: { uri: resource.uri },
        connection_id: server.connectionId,
        connection_epoch: server.connectionEpoch,
        idempotent: true,
      },
      metadata: {
        annotations: canonicalJson(resource.annotations),
        icons: canonicalJson(resource.icons),
        source_meta: canonicalJson(resource.meta),
      },
    };
  }

  private projectTemplate(server: McpCatalogServerSnapshot, template: McpResourceTemplate): McpProjectedResourceTemplate {
    const capabilityDigest = sha256(template);
    const variables = parseTemplateVariables(template.uriTemplate);
    return {
      projectionId: deterministicMcpId("mcp-resource-template", {
        server_id: server.serverId,
        connection_id: server.connectionId,
        revision: server.revision,
        uri_template: template.uriTemplate,
        capability_digest: capabilityDigest,
      }),
      serverId: server.serverId,
      connectionId: server.connectionId,
      catalogRevision: server.revision,
      uriTemplate: template.uriTemplate,
      variables,
      name: template.name,
      title: template.title,
      description: template.description,
      mimeType: template.mimeType,
      operation: "resources/read",
      capabilityDigest,
      permissionScope: canonicalJson({
        namespace: "mcp",
        server_id: server.serverId,
        operation: "resources/read",
        resource_template: template.uriTemplate,
        read_only: true,
      }) as JsonObject,
      invocation: {
        method: "resources/read",
        params_shape: { uri_template: template.uriTemplate, variables },
        connection_id: server.connectionId,
        connection_epoch: server.connectionEpoch,
        idempotent: true,
      },
      metadata: {
        annotations: canonicalJson(template.annotations),
        icons: canonicalJson(template.icons),
        source_meta: canonicalJson(template.meta),
      },
    };
  }
}

function parseTemplateVariables(template: string): string[] {
  const output: string[] = [];
  for (const match of template.matchAll(/\{([A-Za-z_][A-Za-z0-9_.-]*)(?::[^}]+)?\}/g)) output.push(match[1]);
  return [...new Set(output)];
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
