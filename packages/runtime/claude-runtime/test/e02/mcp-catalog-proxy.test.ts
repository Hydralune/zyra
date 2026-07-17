// Bun 1.2.15 does not discover the sibling workspace test root when the frozen
// command passes it without a leading `./`. Import it from the discovered E02
// root so the exact frozen command still executes the MCP behavior matrix.
import "../../../../integrations/claude-mcp/test/e02/mcp-catalog-matrix.test.ts";
