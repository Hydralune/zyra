/** Shared provider wire primitives deliberately kept free of provider state. */
export interface TokenTaskBudget {
  readonly tokens?: number;
  readonly time_seconds?: number;
  readonly tool_calls?: number;
}
