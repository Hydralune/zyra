import { Journal, digest, type O, type Receipt, type Snapshot } from "./kernel.ts";
import {QueryTransitionRuntime} from "../query/transition.ts";
import {TurnLifecycleRuntime} from "../query/turn.ts";
import {CancelRuntimeRuntime} from "../query/cancel.ts";
import {StopHooksRuntime} from "../query/stop.ts";
import {RevisionLoopRuntime} from "../query/revise.ts";
import {InputNormalizeRuntime} from "../input/normalize.ts";
import {InputIntentRuntime} from "../input/intent.ts";
import {InputDedupRuntime} from "../input/dedup.ts";
import {InputEofRuntime} from "../input/eof.ts";
import {ContextAssemblyRuntime} from "../context/assemble.ts";
import {ContextBudgetRuntime} from "../context/budget.ts";
import {ContextPairsRuntime} from "../context/pairs.ts";
import {ContextDisclosureRuntime} from "../context/disclose.ts";
import {ContextSelectionRuntime} from "../context/select.ts";
import {ToolRegistryV2Runtime} from "../tools/registry.ts";
import {ToolSchemaRuntime} from "../tools/schema.ts";
import {ToolConcurrencyRuntime} from "../tools/concurrency.ts";
import {ToolExecutionV2Runtime} from "../tools/execute.ts";
import {ToolResultBudgetRuntime} from "../tools/budget.ts";
import {ToolStreamRuntime} from "../tools/stream.ts";
import {LateResultFenceRuntime} from "../tools/late.ts";
import {CompactTriggerRuntime} from "../compact/trigger.ts";
import {MicrocompactRuntime} from "../compact/micro.ts";
import {CompactRestoreRuntime} from "../compact/restore.ts";
import {CompactCleanupRuntime} from "../compact/cleanup.ts";
import {ProviderRequestRuntime} from "../provider/request.ts";
import {ProviderRetryRuntime} from "../provider/retry.ts";
import {ProviderErrorsRuntime} from "../provider/errors.ts";
import {ProviderUsageRuntime} from "../provider/usage.ts";
import {ProviderCacheBreakRuntime} from "../provider/cache.ts";
import {ProviderTransportRuntime} from "../provider/transport.ts";
import {SessionLifecycleV2Runtime} from "../session/lifecycle.ts";
import {SessionCorrelationRuntime} from "../session/correlation.ts";
import {SessionSnapshotV2Runtime} from "../session/snapshot.ts";
import {SessionResumeRuntime} from "../session/resume.ts";
import {CommitOutboxRuntime} from "../protocol/outbox.ts";
import {CommitRecoveryRuntime} from "../protocol/recovery.ts";
import {SideEffectFenceRuntime} from "../protocol/fence.ts";
import {WriteCensusRuntime} from "../protocol/census.ts";
import {IdempotencyRuntimeRuntime} from "../protocol/idempotency.ts";
export class E01RuntimeCoordinator{readonly journal=new Journal();private sequence=0;private readonly domains:Array<{operations():string[];dispatch(n:string,p?:O):Promise<Receipt>}>;readonly runId:string;readonly sessionId:string;constructor(runId:string,sessionId:string){this.runId=runId;this.sessionId=sessionId;this.domains=[
new QueryTransitionRuntime(this.journal,runId,sessionId),
new TurnLifecycleRuntime(this.journal,runId,sessionId),
new CancelRuntimeRuntime(this.journal,runId,sessionId),
new StopHooksRuntime(this.journal,runId,sessionId),
new RevisionLoopRuntime(this.journal,runId,sessionId),
new InputNormalizeRuntime(this.journal,runId,sessionId),
new InputIntentRuntime(this.journal,runId,sessionId),
new InputDedupRuntime(this.journal,runId,sessionId),
new InputEofRuntime(this.journal,runId,sessionId),
new ContextAssemblyRuntime(this.journal,runId,sessionId),
new ContextBudgetRuntime(this.journal,runId,sessionId),
new ContextPairsRuntime(this.journal,runId,sessionId),
new ContextDisclosureRuntime(this.journal,runId,sessionId),
new ContextSelectionRuntime(this.journal,runId,sessionId),
new ToolRegistryV2Runtime(this.journal,runId,sessionId),
new ToolSchemaRuntime(this.journal,runId,sessionId),
new ToolConcurrencyRuntime(this.journal,runId,sessionId),
new ToolExecutionV2Runtime(this.journal,runId,sessionId),
new ToolResultBudgetRuntime(this.journal,runId,sessionId),
new ToolStreamRuntime(this.journal,runId,sessionId),
new LateResultFenceRuntime(this.journal,runId,sessionId),
new CompactTriggerRuntime(this.journal,runId,sessionId),
new MicrocompactRuntime(this.journal,runId,sessionId),
new CompactRestoreRuntime(this.journal,runId,sessionId),
new CompactCleanupRuntime(this.journal,runId,sessionId),
new ProviderRequestRuntime(this.journal,runId,sessionId),
new ProviderRetryRuntime(this.journal,runId,sessionId),
new ProviderErrorsRuntime(this.journal,runId,sessionId),
new ProviderUsageRuntime(this.journal,runId,sessionId),
new ProviderCacheBreakRuntime(this.journal,runId,sessionId),
new ProviderTransportRuntime(this.journal,runId,sessionId),
new SessionLifecycleV2Runtime(this.journal,runId,sessionId),
new SessionCorrelationRuntime(this.journal,runId,sessionId),
new SessionSnapshotV2Runtime(this.journal,runId,sessionId),
new SessionResumeRuntime(this.journal,runId,sessionId),
new CommitOutboxRuntime(this.journal,runId,sessionId),
new CommitRecoveryRuntime(this.journal,runId,sessionId),
new SideEffectFenceRuntime(this.journal,runId,sessionId),
new WriteCensusRuntime(this.journal,runId,sessionId),
new IdempotencyRuntimeRuntime(this.journal,runId,sessionId),
]} async bootstrap(){for(const d of this.domains)await d.dispatch("bootstrap",{canonical_owner:"typescript",runtime:"e01"})}async observe(phase:string,payload:O={}):Promise<Receipt>{const d=this.domains[this.sequence%this.domains.length];const ops=d.operations();const op=phase.includes("compact")?"compact":phase.includes("restore")?"restore":phase.includes("error")?"recover":ops[(this.sequence+digest(phase).charCodeAt(8))%ops.length];this.sequence++;return d.dispatch(op,{...payload,observed_phase:phase,canonical_owner:"typescript"})}inventory():O{return{owner:"typescript",domains:this.domains.length,operations:this.domains.reduce((n,d)=>n+d.operations().length,0),protocol:"prepare/effect/receipt/commit/ack"}}snapshot():Snapshot{return this.journal.snapshot()}restore(s:Snapshot){this.journal.restore(s)}}
