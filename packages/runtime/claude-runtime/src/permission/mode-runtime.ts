import type { JsonObject } from "../contracts.ts";
import { cloneJson, constantTimeDigestEquals, digest } from "../e02/canonical.ts";
import type { PermissionMode } from "../e02/contracts.ts";

export interface PermissionModeState extends JsonObject {
  mode: PermissionMode;
  revision: number;
  interactive: boolean;
  headless: boolean;
  sealedAutonomous: boolean;
  bypassAvailable: boolean;
  autoClassifierEnabled: boolean;
  changedAt: string;
  changedBy: string;
  reason: string;
}
export interface PermissionModeTransition extends JsonObject {
  from: PermissionMode;
  to: PermissionMode;
  revisionBefore: number;
  revisionAfter: number;
  changed: boolean;
  reason: string;
  changedBy: string;
  changedAt: string;
  transitionDigest: string;
}

const MODES = new Set<PermissionMode>([
  "default",
  "acceptEdits",
  "dontAsk",
  "plan",
  "bypassPermissions",
  "auto",
  "sealed",
]);

const TRANSITIONS: Record<PermissionMode, ReadonlySet<PermissionMode>> = {
  default: new Set(["default", "acceptEdits", "dontAsk", "plan", "bypassPermissions", "auto", "sealed"]),
  acceptEdits: new Set(["acceptEdits", "default", "dontAsk", "plan", "auto", "sealed"]),
  dontAsk: new Set(["dontAsk", "default", "acceptEdits", "plan", "auto", "sealed"]),
  plan: new Set(["plan", "default", "acceptEdits", "dontAsk", "auto", "sealed"]),
  bypassPermissions: new Set(["bypassPermissions", "default", "acceptEdits", "dontAsk", "plan", "auto", "sealed"]),
  auto: new Set(["auto", "default", "acceptEdits", "dontAsk", "plan", "sealed"]),
  sealed: new Set(["sealed", "default"]),
};

export class PermissionModeRuntime {
  private stateValue: PermissionModeState;

  constructor(input: Partial<PermissionModeState> = {}) {
    const mode = normalizeMode(input.mode ?? "default");
    this.stateValue = {
      mode,
      revision: normalizeRevision(input.revision ?? 0),
      interactive: input.interactive ?? (mode !== "sealed"),
      headless: input.headless ?? false,
      sealedAutonomous: input.sealedAutonomous ?? (mode === "sealed"),
      bypassAvailable: input.bypassAvailable ?? false,
      autoClassifierEnabled: input.autoClassifierEnabled ?? false,
      changedAt: input.changedAt ?? new Date().toISOString(),
      changedBy: input.changedBy ?? "bootstrap",
      reason: input.reason ?? "initial permission mode",
    };
    this.enforceInvariants(this.stateValue);
  }

  get mode(): PermissionMode {
    return this.stateValue.mode;
  }

  get revision(): number {
    return this.stateValue.revision;
  }

  get state(): PermissionModeState {
    return cloneJson(this.stateValue);
  }

  transition(
    nextValue: PermissionMode,
    options: {
      expectedRevision?: number;
      changedBy: string;
      reason: string;
      interactive?: boolean;
      headless?: boolean;
      bypassAvailable?: boolean;
      autoClassifierEnabled?: boolean;
      at?: string;
      managedOverride?: boolean;
    },
  ): PermissionModeTransition {
    const next = normalizeMode(nextValue);
    const expectedRevision = options.expectedRevision ?? this.stateValue.revision;
    if (expectedRevision !== this.stateValue.revision) {
      throw new Error(`permission mode revision conflict: expected ${expectedRevision}, observed ${this.stateValue.revision}`);
    }
    if (!TRANSITIONS[this.stateValue.mode].has(next) && !options.managedOverride) {
      throw new Error(`permission mode transition ${this.stateValue.mode} -> ${next} is forbidden`);
    }
    if (this.stateValue.mode === "sealed" && next !== "sealed" && !options.managedOverride) {
      throw new Error("sealed permission mode can only be exited by a managed override");
    }
    const changed = next !== this.stateValue.mode
      || options.interactive !== undefined && options.interactive !== this.stateValue.interactive
      || options.headless !== undefined && options.headless !== this.stateValue.headless
      || options.bypassAvailable !== undefined && options.bypassAvailable !== this.stateValue.bypassAvailable
      || options.autoClassifierEnabled !== undefined && options.autoClassifierEnabled !== this.stateValue.autoClassifierEnabled;
    const changedAt = options.at ?? new Date().toISOString();
    const transitionBase = {
      from: this.stateValue.mode,
      to: next,
      revisionBefore: this.stateValue.revision,
      revisionAfter: changed ? this.stateValue.revision + 1 : this.stateValue.revision,
      changed,
      reason: options.reason,
      changedBy: options.changedBy,
      changedAt,
    };
    if (changed) {
      const nextState: PermissionModeState = {
        mode: next,
        revision: transitionBase.revisionAfter,
        interactive: next === "sealed" ? false : options.interactive ?? this.stateValue.interactive,
        headless: options.headless ?? this.stateValue.headless,
        sealedAutonomous: next === "sealed",
        bypassAvailable: options.bypassAvailable ?? this.stateValue.bypassAvailable,
        autoClassifierEnabled: options.autoClassifierEnabled ?? this.stateValue.autoClassifierEnabled,
        changedAt,
        changedBy: options.changedBy,
        reason: options.reason,
      };
      this.enforceInvariants(nextState);
      this.stateValue = nextState;
    }
    return { ...transitionBase, transitionDigest: digest(transitionBase) };
  }

  canAsk(): boolean {
    return this.stateValue.interactive
      && !this.stateValue.headless
      && !this.stateValue.sealedAutonomous
      && this.stateValue.mode !== "dontAsk";
  }

  convertsAskToDeny(): boolean {
    return !this.canAsk()
      || this.stateValue.mode === "sealed"
      || this.stateValue.mode === "dontAsk";
  }

  permitsBypass(): boolean {
    return this.stateValue.mode === "bypassPermissions" && this.stateValue.bypassAvailable;
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-permission-mode/v1",
      state: this.stateValue,
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  restore(snapshot: JsonObject): void {
    if (snapshot.version !== "zyra.e02-permission-mode/v1") throw new Error("unsupported permission mode snapshot");
    const expectedHash = digest({ version: snapshot.version, state: snapshot.state });
    if (!constantTimeDigestEquals(expectedHash, String(snapshot.snapshot_hash ?? ""))) throw new Error("permission mode snapshot hash mismatch");
    const state = snapshot.state as unknown as PermissionModeState;
    this.enforceInvariants(state);
    this.stateValue = cloneJson(state);
  }

  private enforceInvariants(state: PermissionModeState): void {
    normalizeMode(state.mode);
    normalizeRevision(state.revision);
    if (state.mode === "sealed" && (state.interactive || !state.sealedAutonomous)) {
      throw new Error("sealed permission mode must be non-interactive and autonomous");
    }
    if (state.headless && state.interactive) throw new Error("headless permission mode cannot be interactive");
    if (state.mode === "bypassPermissions" && !state.bypassAvailable) {
      throw new Error("bypassPermissions mode requires an explicit bypassAvailable binding");
    }
  }
}

function normalizeMode(value: unknown): PermissionMode {
  const aliases: Record<string, PermissionMode> = {
    accept_edits: "acceptEdits",
    dont_ask: "dontAsk",
    bypass: "bypassPermissions",
    bypass_permissions: "bypassPermissions",
    autonomous: "auto",
  };
  const normalized = typeof value === "string" ? aliases[value] ?? value : "";
  if (!MODES.has(normalized as PermissionMode)) throw new Error(`unsupported permission mode ${String(value)}`);
  return normalized as PermissionMode;
}

function normalizeRevision(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error("permission mode revision is invalid");
  return value as number;
}
