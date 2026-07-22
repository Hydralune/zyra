import type { DispatchMessage, ProviderRouteLease } from "../contracts.ts";
import type { JsonValue } from "../canonical.ts";

// Cropped from oh-my-pi packages/ai/src/providers/transform-messages.ts at
// c6b83c1d. Zyra keeps the pairing/deduplication control flow in TypeScript,
// but maps it to Zyra dispatch messages and route leases instead of importing
// the upstream model registry, agent loop, or storage owner.

export type Api = "openai-chat" | "openai-completions" | "openai-responses" | "anthropic-messages";

interface ModelCompat {
	replayUnsignedThinking?: boolean;
	signingEndpoint?: boolean;
	officialEndpoint?: boolean;
	thinkingFormat?: string;
	requiresThinkingAsText?: boolean;
}

export interface Model<TApi extends Api = Api> {
	readonly provider: string;
	readonly api: TApi;
	readonly id: string;
	readonly reasoning: boolean;
	readonly compat: ModelCompat;
}

export interface TextBlock {
	readonly type: "text";
	readonly text: string;
	readonly [kDemotedThinking]?: boolean;
}

export interface ThinkingBlock {
	readonly type: "thinking";
	readonly thinking: string;
	readonly thinkingSignature?: string;
}

export interface RedactedThinkingBlock {
	readonly type: "redactedThinking";
	readonly data?: string;
}

export interface FallbackBlock {
	readonly type: "fallback";
	readonly reason?: string;
}

export interface ImageBlock {
	readonly type: "image";
	readonly mediaType: string;
	readonly data: string;
}

export interface ToolCall {
	readonly type: "toolCall";
	readonly id: string;
	readonly name: string;
	readonly arguments: Readonly<Record<string, JsonValue>>;
	readonly thoughtSignature?: string;
}

export type AssistantBlock = TextBlock | ThinkingBlock | RedactedThinkingBlock | FallbackBlock | ImageBlock | ToolCall;

export interface AssistantMessage {
	readonly role: "assistant";
	readonly content: AssistantBlock[];
	readonly provider: string;
	readonly api: Api;
	readonly model: string;
	readonly stopReason: "stop" | "toolUse" | "length" | "error" | "aborted";
	readonly timestamp: number;
}

export interface ToolResultMessage {
	readonly role: "toolResult";
	readonly toolCallId: string;
	readonly toolName: string;
	readonly content: readonly TextBlock[];
	readonly isError: boolean;
	readonly timestamp: number;
}

export interface UserMessage {
	readonly role: "user" | "developer" | "system";
	readonly content: string;
	readonly timestamp: number;
}

export type Message = AssistantMessage | ToolResultMessage | UserMessage;

const kDemotedThinking: unique symbol = Symbol("zyra.demoted-thinking");

function isDemotedThinking(value: AssistantBlock): value is TextBlock {
	return value.type === "text" && value[kDemotedThinking] === true;
}

function renderDemotedThinking(_modelId: string, thinking: string): string {
	return `<thinking>\n${thinking.trim()}\n</thinking>`;
}

const ToolCallStatus = {
	Resolved: 1,
	Aborted: 2,
} as const;
type ToolCallStatus = (typeof ToolCallStatus)[keyof typeof ToolCallStatus];

/**
 * Maximum tool-call id length the strictest replay provider accepts.
 *
 * Anthropic requires `^[a-zA-Z0-9_-]+$` with a 64-char cap; Google and Codex
 * `normalizeToolCallId` implementations cap individual id segments to the same
 * 64-char ceiling. Replacement ids minted here flow back through
 * `convertAnthropicMessages` (and friends) unchanged, so the `_dupN` suffix
 * MUST not push a normalized id past this bound.
 */
const MAX_TOOL_CALL_ID_LENGTH = 64;

function appendDuplicateSuffix(originalId: string, suffix: string, maxLength: number): string {
	// Responses-family ids are composites (`callId|itemId`): the wire call_id is
	// the FIRST segment (normalizeResponsesToolCallId splits on `|`), so the
	// suffix must land on every segment or the duplicate collapses back onto the
	// original call_id at encode time. The length budget applies per segment,
	// matching the per-segment caps of the provider normalizers.
	if (originalId.includes("|")) {
		return originalId
			.split("|")
			.map(segment => appendSegmentDuplicateSuffix(segment, suffix, maxLength))
			.join("|");
	}
	return appendSegmentDuplicateSuffix(originalId, suffix, maxLength);
}

function appendSegmentDuplicateSuffix(segment: string, suffix: string, maxLength: number): string {
	if (segment.length + suffix.length <= maxLength) return `${segment}${suffix}`;
	const prefixBudget = Math.max(0, maxLength - suffix.length);
	return `${segment.slice(0, prefixBudget)}${suffix}`;
}

type PendingToolResultRewrite = { replacementId: string } | undefined;

function deduplicateToolCallIds(
	messages: Message[],
	maxToolCallIdLength = MAX_TOOL_CALL_ID_LENGTH,
	duplicateSuffixPrefix = "_dup",
): Message[] {
	const seenToolCallIds = new Map<string, number>();
	const pendingToolResultRewrites = new Map<string, PendingToolResultRewrite[]>();

	return messages.map(msg => {
		if (msg.role === "toolResult") {
			const rewrites = pendingToolResultRewrites.get(msg.toolCallId);
			if (!rewrites || rewrites.length === 0) return msg;

			const rewrite = rewrites.shift();
			if (rewrites.length === 0) pendingToolResultRewrites.delete(msg.toolCallId);
			if (rewrite) return { ...msg, toolCallId: rewrite.replacementId };
			return msg;
		}

		if (msg.role !== "assistant") return msg;

		const enqueueToolResultRewrite = (id: string, rewrite: PendingToolResultRewrite): void => {
			const rewrites = pendingToolResultRewrites.get(id);
			if (rewrites) {
				rewrites.push(rewrite);
				return;
			}
			pendingToolResultRewrites.set(id, [rewrite]);
		};

		// Ids this turn has already touched; used to scope the "drop carried-over
		// pending rewrites" semantics to the FIRST occurrence per turn so multiple
		// blocks of the same id within one turn still accumulate as duplicates.
		const idsTouchedInTurn = new Set<string>();
		let contentChanged = false;
		const content = msg.content.map(block => {
			if (block.type !== "toolCall") return block;

			// Drop any pending rewrites carried over from a prior assistant turn
			// for this id on its first appearance this turn. When a later turn
			// re-emits the same id, the older duplicate call's expected result
			// never landed in time — the second pass synthesizes
			// "No result provided" for it, and the upcoming real result(id) must
			// route to one of THIS turn's calls. Without this guard the older
			// `_dup` id would steal the next result.
			if (!idsTouchedInTurn.has(block.id)) {
				pendingToolResultRewrites.delete(block.id);
				idsTouchedInTurn.add(block.id);
			}

			const previousCount = seenToolCallIds.get(block.id) ?? 0;
			if (previousCount === 0) {
				seenToolCallIds.set(block.id, 1);
				enqueueToolResultRewrite(block.id, undefined);
				return block;
			}

			let duplicateIndex = previousCount;
			let replacementId = appendDuplicateSuffix(
				block.id,
				`${duplicateSuffixPrefix}${duplicateIndex}`,
				maxToolCallIdLength,
			);
			while (seenToolCallIds.has(replacementId)) {
				duplicateIndex += 1;
				replacementId = appendDuplicateSuffix(
					block.id,
					`${duplicateSuffixPrefix}${duplicateIndex}`,
					maxToolCallIdLength,
				);
			}
			seenToolCallIds.set(block.id, duplicateIndex + 1);
			seenToolCallIds.set(replacementId, 1);
			enqueueToolResultRewrite(block.id, { replacementId });
			contentChanged = true;
			return { ...block, id: replacementId };
		});

		if (!contentChanged) return msg;
		return { ...msg, content };
	});
}

/**
 * Drop assistant `toolCall` blocks whose `id` or `name` is empty / whitespace-only,
 * the `toolResult` messages they point at, and any assistant turn that has no
 * replayable content left.
 *
 * Models occasionally emit malformed calls such as `{ "name": "", "arguments": "{}" }`
 * (observed: GLM-5.2 + thinking on long turns, #3458) or a structurally valid
 * `toolCall` whose provider/native passthrough id never materialized (`id: ""`).
 * The agent loop rejects or skips these at execution time, but the malformed block
 * and its error tool-result can stay in `currentContext.messages`, so every
 * subsequent request replays them. Every provider validates the call shape —
 * Anthropic 400s on `tool_use.name` / `tool_use.id` (alongside an orphan
 * `tool_result`), OpenAI Chat Completions 400s on malformed
 * `tool_calls[i].function.*` — wedging the session in a 400 loop until manual
 * `/clear`.
 *
 * Run before any other transform so the rest of the pipeline never sees a
 * malformed call. Idempotent: a re-run on an already-sanitized list returns
 * the input untouched. Provider-agnostic — any wire model could surface this.
 */
function isMalformedToolCallName(name: string | undefined): boolean {
	return !name || name.trim().length === 0;
}

function isMalformedToolCallId(id: string | undefined): boolean {
	return !id || id.trim().length === 0;
}

function isMalformedToolCall(block: { id: string; name: string }): boolean {
	return isMalformedToolCallId(block.id) || isMalformedToolCallName(block.name);
}

function sanitizeMalformedToolCalls(messages: Message[]): Message[] {
	// Fast path: skip the rewrite entirely when nothing is malformed.
	let hasMalformed = false;
	outer: for (const msg of messages) {
		if (msg.role !== "assistant") continue;
		for (const block of msg.content) {
			if (block.type === "toolCall" && isMalformedToolCall(block)) {
				hasMalformed = true;
				break outer;
			}
		}
	}
	if (!hasMalformed) return messages;

	// Positional FIFO pairing within one assistant→tool-result window: a tool-call
	// id can repeat across history when an OpenAI-Responses composite id
	// (`callId|itemId`) collapses on the wire to the same `callId` (see
	// `deduplicateToolCallIds` + `transform-messages-dedup`). A set-based "drop
	// every result for this id" loses the real output for the surviving valid
	// occurrence whenever one duplicate is malformed. Track each `toolCall`
	// occurrence's malformed-ness on a per-id queue and pop on matching
	// `toolResult`, but clear the queues at every non-result boundary so a
	// malformed call whose rejection result never arrived cannot consume a later
	// valid call's real result when the id is reused.
	const dropQueues = new Map<string, boolean[]>();
	const result: Message[] = [];
	for (const msg of messages) {
		if (msg.role === "assistant") {
			dropQueues.clear();
			const filtered: AssistantMessage["content"] = [];
			for (const block of msg.content) {
				if (block.type === "toolCall") {
					const malformed = isMalformedToolCall(block);
					const queue = dropQueues.get(block.id);
					if (queue) queue.push(malformed);
					else dropQueues.set(block.id, [malformed]);
					if (malformed) continue;
				}
				filtered.push(block);
			}
			if (filtered.length === 0) continue;
			result.push(filtered.length === msg.content.length ? msg : { ...msg, content: filtered });
			continue;
		}
		if (msg.role === "toolResult") {
			const queue = dropQueues.get(msg.toolCallId);
			if (queue && queue.length > 0) {
				const drop = queue.shift() === true;
				if (queue.length === 0) dropQueues.delete(msg.toolCallId);
				if (drop) continue;
			}
			result.push(msg);
			continue;
		}
		dropQueues.clear();
		result.push(msg);
	}
	return result;
}

function shouldDropTruncatedThinkingOnlyAssistant(msg: AssistantMessage): boolean {
	const isTruncatedStop = msg.stopReason === "length" || msg.stopReason === "error" || msg.stopReason === "aborted";
	return isTruncatedStop && !msg.content.some(block => block.type === "toolCall" || block.type === "text");
}

function getLatestSurvivingAssistantIndex(messages: readonly Message[]): number {
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		const msg = messages[index]!;
		if (msg.role === "assistant" && !shouldDropTruncatedThinkingOnlyAssistant(msg)) {
			return index;
		}
	}
	return -1;
}

function isAnthropicMessagesModel(model: Model): model is Model<"anthropic-messages"> {
	return model.api === "anthropic-messages";
}

/**
 * Targets that have proven they read unsigned foreign thinking when replayed
 * natively. This is a semantic-carry allowlist only: OpenAI-compatible
 * `reasoning_content` schema requirements and llama.cpp cache-prefix replay are
 * handled by their encoders and MUST NOT make foreign thinking look meaningful.
 */
function targetReadsForeignThinking(model: Model, compat: Model["compat"]): boolean {
	if (compat === undefined) return false;
	if (model.api === "anthropic-messages") {
		return "replayUnsignedThinking" in compat && compat.replayUnsignedThinking === true;
	}
	if (model.api !== "openai-completions") return false;
	if (!("thinkingFormat" in compat)) return false;
	if (compat.requiresThinkingAsText) return false;
	return model.reasoning && compat.thinkingFormat === "zai";
}

const ANTHROPIC_TOOL_CALL_ID_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;

function isValidAnthropicToolCallId(id: string): boolean {
	return ANTHROPIC_TOOL_CALL_ID_PATTERN.test(id);
}

function fallbackAnthropicToolCallId(originalId: string): string {
	return `toolu_${Bun.hash(originalId).toString(36)}`;
}

function normalizeAnthropicTargetToolCallId<TApi extends Api>(
	id: string,
	model: Model<TApi>,
	source: AssistantMessage,
	normalizeToolCallId?: (id: string, model: Model<TApi>, source: AssistantMessage) => string,
): string {
	if (isValidAnthropicToolCallId(id)) return id;
	const normalized =
		normalizeToolCallId?.(id, model, source) ?? id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, MAX_TOOL_CALL_ID_LENGTH);
	if (isValidAnthropicToolCallId(normalized)) return normalized;
	return fallbackAnthropicToolCallId(id);
}

/**
 * Normalize tool call ID for cross-provider compatibility.
 * OpenAI Responses API generates IDs that are 450+ chars with special characters like `|`.
 * Anthropic APIs require IDs matching ^[a-zA-Z0-9_-]+$ (max 64 chars).
 *
 * For aborted/errored turns, this function:
 * - Preserves tool call structure (unlike converting to text summaries)
 * - Injects synthetic "aborted" tool results
 */
export function transformMessages<TApi extends Api>(
	messages: Message[],
	model: Model<TApi>,
	normalizeToolCallId?: (id: string, model: Model<TApi>, source: AssistantMessage) => string,
	maxNormalizedToolCallIdLength = MAX_TOOL_CALL_ID_LENGTH,
	duplicateToolCallIdSuffixPrefix = "_dup",
	targetCompat: Model<TApi>["compat"] = model.compat,
): Message[] {
	// Drop assistant `toolCall` blocks with empty/whitespace `id` or `name`
	// (and their matched `toolResult` messages) before anything else looks at
	// the history. Replays of these would 400 every provider — see
	// `sanitizeMalformedToolCalls`.
	messages = sanitizeMalformedToolCalls(messages);

	// Build a map of original tool call IDs to normalized IDs
	const toolCallIdMap = new Map<string, string>();

	const latestSurvivingAssistantIndex = getLatestSurvivingAssistantIndex(messages);
	// First pass: transform messages (thinking blocks, tool call ID normalization)
	const normalizedMessages = messages.map((msg, index) => {
		// User and developer messages pass through unchanged
		if (msg.role === "user" || msg.role === "developer") {
			return msg;
		}

		// Handle toolResult messages - normalize toolCallId if we have a mapping
		if (msg.role === "toolResult") {
			const normalizedId = toolCallIdMap.get(msg.toolCallId);
			if (normalizedId && normalizedId !== msg.toolCallId) {
				return { ...msg, toolCallId: normalizedId };
			}
			return msg;
		}

		// Assistant messages need transformation check
		if (msg.role === "assistant") {
			const assistantMsg = msg as AssistantMessage;
			const isSameModel =
				assistantMsg.provider === model.provider &&
				assistantMsg.api === model.api &&
				assistantMsg.model === model.id;

			const isAnthropicTarget = isAnthropicMessagesModel(model);
			// Anthropic's all-or-none contract on prior-turn thinking blocks
			// applies to every `anthropic-messages → anthropic-messages` replay,
			// not just the latest assistant turn. The legacy
			// `mustPreserveLatestAnthropicThinking` flag only honored it for the
			// latest turn; every prior turn fell through to the cross-API
			// text-demotion path whenever the conversation crossed a model id,
			// silently dropping the reasoning chain on continuation for custom
			// anthropic-messages providers configured via `models.yaml` and
			// session-level model swaps (#2257).
			const isAnthropicReplay = isAnthropicTarget && assistantMsg.api === "anthropic-messages";
			const isLatestSurvivingAssistant = index === latestSurvivingAssistantIndex;
			// Signature policy is a second axis. Anthropic cryptographically
			// binds reasoning signatures to its key+session+model, so cross-model
			// signatures must be stripped whenever a signing Anthropic endpoint
			// is on either end of the replay:
			//   * official Anthropic (source): the 3p target can't reverify a
			//     foreign signature and keeping it leaks continuation metadata
			//     for no benefit.
			//   * signing Anthropic (target): official Anthropic, GitHub Copilot,
			//     ZenMux, Cloudflare AI Gateway `/anthropic`, and Google Vertex
			//     `publishers/anthropic/…` all forward to signature-enforcing
			//     Anthropic. Any stale/cross-model signature on the wire triggers
			//     `400 Invalid signature in thinking block` — same failure class
			//     whether `officialEndpoint` is true or the endpoint is one of
			//     the known signing proxies (#4297).
			// 3p ↔ 3p replays preserve signatures because compatible providers
			// (Z.AI, DeepSeek, custom `models.yaml` providers) treat them as
			// opaque continuation hints rather than verified material; stripping
			// degrades the reasoning chain into unsigned/text on the next turn
			// (#2265). Source-side official detection uses the canonical catalog
			// provider id `"anthropic"` because assistant messages carry no
			// `baseUrl` — a user who manually points `provider: "anthropic"` at
			// a custom proxy via `models.yaml` will see signatures stripped, the
			// conservative direction (degraded reasoning, not broken requests).
			const isOfficialAnthropicSource = isAnthropicReplay && assistantMsg.provider === "anthropic";
			const isSigningAnthropicTarget = isAnthropicTarget && model.compat.signingEndpoint;
			const signingAnthropicInvolved = isOfficialAnthropicSource || isSigningAnthropicTarget;
			// Compatible Anthropic-messages reasoning targets that accept
			// unsigned thinking natively (Z.AI, DeepSeek, the generic
			// `reasoning && !official` case in the compat builder). Used to keep
			// `redacted_thinking` siblings beside unsigned visible thinking on
			// targets that won't text-demote it.
			const replaysUnsignedAnthropicThinking = isAnthropicTarget && model.compat.replayUnsignedThinking;
			// Thinking signatures can be untrustworthy for two distinct reasons with very
			// different blast radii:
			//
			// 1. Aborted/errored turns: the stream stopped mid-block, so only the block
			//    that was streaming at the abort point — always the FINAL content block —
			//    can carry a partially-streamed (invalid) signature. Every earlier block
			//    completed: Anthropic delivers a block's signature at its
			//    `content_block_stop`, which necessarily fired before the next block began,
			//    so those signatures are whole and valid. Stripping them would needlessly
			//    discard a replayable thinking chain — e.g. interrupting during the visible
			//    text output after thinking already finished leaves a fully-signed thinking
			//    block that must be kept, or Anthropic rejects the replay with HTTP 400
			//    "Invalid `signature` in `thinking` block".
			//
			// 2. Abandoned tool-use turns: a turn that carries toolCall blocks but did NOT
			//    request tool execution (stopReason !== "toolUse" — e.g. adaptive-thinking
			//    Opus emitting tool calls and then ending on `end_turn`/`stop`). The agent
			//    loop pairs those calls with placeholder tool_results to keep the
			//    tool_use/tool_result contract valid. The turn completed cleanly, but its
			//    signatures are end_turn-bound and cannot be replayed in that synthesized
			//    continuation, so EVERY thinking signature is stripped.
			//
			// Latest abandoned turns are exempt because Anthropic requires thinking blocks
			// from its most recent response to remain byte-for-byte unmodified.
			const invalidStopReason = assistantMsg.stopReason === "aborted" || assistantMsg.stopReason === "error";
			const abandonedToolUse =
				!invalidStopReason &&
				assistantMsg.stopReason !== "toolUse" &&
				assistantMsg.content.some(b => b.type === "toolCall");
			const lastBlockIndex = assistantMsg.content.length - 1;

			const anthropicVisibleThinkingSurvivesReplay = (
				candidate: AssistantMessage["content"][number],
				candidateIndex: number,
			): boolean => {
				if (candidate.type !== "thinking") return false;
				if (!isAnthropicReplay) return false;
				if (isLatestSurvivingAssistant && abandonedToolUse) return true;
				const candidateSignatureUntrustworthy =
					abandonedToolUse || (invalidStopReason && candidateIndex === lastBlockIndex);
				const replaySignature =
					candidateSignatureUntrustworthy && candidate.thinkingSignature ? undefined : candidate.thinkingSignature;
				if (!replaySignature && (!candidate.thinking || candidate.thinking.trim() === "")) return false;
				if (isSameModel && isSigningAnthropicTarget && (!replaySignature || replaySignature.trim() === "")) {
					return false;
				}
				return true;
			};
			const hasVisibleAnthropicThinking = assistantMsg.content.some(candidate => candidate.type === "thinking");
			const dropsAllSameModelVisibleThinking =
				isAnthropicReplay &&
				isSameModel &&
				isSigningAnthropicTarget &&
				hasVisibleAnthropicThinking &&
				!assistantMsg.content.some(anthropicVisibleThinkingSurvivesReplay);

			const transformedContent = assistantMsg.content.flatMap((block, blockIndex) => {
				if (block.type === "thinking") {
					// Only an aborted/errored turn's final (mid-stream) block can hold a
					// partial signature; abandoned tool-use turns strip all. Drop the
					// untrustworthy signature so the encoder can downgrade the block to text.
					const signatureUntrustworthy = abandonedToolUse || (invalidStopReason && blockIndex === lastBlockIndex);
					let sanitized: typeof block =
						signatureUntrustworthy && block.thinkingSignature
							? { ...block, thinkingSignature: undefined }
							: block;
					if (isAnthropicReplay) {
						// Latest abandoned turn: Anthropic's byte-for-byte rule forbids
						// even stripping a signature on the latest message.
						if (isLatestSurvivingAssistant && abandonedToolUse) return block;
						// Cross-model prior turns crossing an official Anthropic endpoint
						// must strip the source signature so the downstream encoder
						// applies its `replayUnsignedThinking` policy (unsigned thinking
						// is emitted natively on Anthropic-compatible reasoning endpoints
						// and demoted to text on official Anthropic). 3p ↔ 3p replays
						// keep the signature so the reasoning chain stays signed on
						// continuation (#2265).
						if (
							!isLatestSurvivingAssistant &&
							!isSameModel &&
							signingAnthropicInvolved &&
							sanitized.thinkingSignature
						) {
							sanitized = { ...sanitized, thinkingSignature: undefined };
						}
						// Drop blocks with neither a signature anchor nor any text —
						// nothing for the next turn to replay.
						if (!sanitized.thinkingSignature && (!sanitized.thinking || sanitized.thinking.trim() === "")) {
							return [];
						}
						// Same-model Anthropic replay to a signature-enforcing endpoint
						// requires valid signatures to natively replay thinking blocks.
						// Both undefined and empty string signatures are invalid and must
						// be dropped entirely — not demoted to text. Demotion would cause
						// the reasoning_extraction safety classifier to refuse the response.
						if (
							isSameModel &&
							isSigningAnthropicTarget &&
							(!sanitized.thinkingSignature || sanitized.thinkingSignature.trim() === "")
						) {
							return [];
						}
						return sanitized;
					}
					// Cross-API target: same-model replay keeps signatures untouched
					// (the encoder needs them for native replay; an OpenAI encrypted
					// reasoning blob has empty text but a load-bearing signature).
					if (isSameModel && sanitized.thinkingSignature) return sanitized;
					// Nothing left for the next turn to replay: drop empty/no-anchor
					// thinking blocks before the cross-model paths.
					if (!sanitized.thinking || sanitized.thinking.trim() === "") return [];
					if (isSameModel) return sanitized;
					// Cross-model + cross-API: preserve native thinking only for
					// targets proven to read unsigned foreign reasoning (Z.AI-format
					// OpenAI-compatible targets, plus Anthropic-compatible
					// `replayUnsignedThinking`). Tool-call schema requirements and
					// llama.cpp cache-prefix replay are orthogonal encoder concerns;
					// keeping inert foreign CoT native for those flags loses the
					// canonical visible-text fallback without adding model context.
					if (targetReadsForeignThinking(model, targetCompat)) {
						return sanitized.thinkingSignature ? { ...sanitized, thinkingSignature: undefined } : sanitized;
					}
					// Other cross-API targets (openai-responses encrypted blobs, google
					// thought parts, anthropic-target from a non-Anthropic source, or any
					// reasoning-disabled target) can't replay an unsigned thinking block:
					// the native reasoning slot either rejects a foreign signature or — as
					// verified end-to-end against Gemini 3 — silently discards unsigned
					// thought content (it is neither recalled nor influences generation).
					// Demote to text so the reasoning survives as context, wrapped in the
					// TARGET model's own canonical thinking-block dialect (e.g. a ```thinking
					// fence for Gemini) so it reads as reasoning rather than bare prose the
					// model might mimic.
					// Mark the demoted block (symbol-keyed, never serialized) instead of
					// baking a separator into its text: the openai-completions flatten —
					// the one consumer that joins adjacent text blocks into a single
					// string — inserts a paragraph break after marked blocks, so the
					// bare Anthropic-dialect output (or any dialect's wrapped output
					// whose closing tag isn't a natural word boundary) can't glue onto
					// the following visible-text block, while ordinary adjacent text
					// blocks stitched from streaming / bridges / imported transcripts
					// stay byte-identical. A separator baked into the block text would
					// leak to non-flattening targets: Anthropic/Bedrock reject a
					// terminal assistant message whose text ends with whitespace.
					return {
						type: "text" as const,
						text: renderDemotedThinking(model.id, sanitized.thinking),
						[kDemotedThinking]: true,
					};
				}

				if (block.type === "redactedThinking") {
					// Redacted thinking is native-only. Keep it for same-model
					// signed replay, the latest byte-for-byte Anthropic turn, or
					// compatible targets that will also emit sibling unsigned
					// thinking natively. Drop it when the matching visible thinking
					// was discarded, or when visible thinking was cross-model
					// stripped and will be demoted to text.
					if (isAnthropicReplay) {
						if (dropsAllSameModelVisibleThinking) return [];
						if (isSameModel || isLatestSurvivingAssistant || replaysUnsignedAnthropicThinking) return block;
						return [];
					}
					if (isSameModel) return block;
					return [];
				}

				if (block.type === "fallback") {
					// Server-side-fallback boundary marker (Anthropic beta
					// `server-side-fallback-2026-06-01`). Only the official
					// Anthropic endpoint accepts this block on replay: every
					// other target either rejects unknown content blocks with a
					// 400 (anthropic-compatible endpoints like Umans/Z.AI/MiniMax,
					// and older omp gateways whose schema pre-dates this feature)
					// or throws in its converter (Bedrock). Even the official
					// replay path only accepts the block when the current request
					// itself opts into the beta — but we don't know that here, so
					// keep it and let `convertAnthropicMessages` re-check the
					// per-request opt-in before serializing.
					if (isAnthropicTarget && model.compat.officialEndpoint) return block;
					return [];
				}

				if (block.type === "text") {
					if (isSameModel) return block;
					return {
						type: "text" as const,
						text: block.text,
					};
				}

				if (block.type === "toolCall") {
					const toolCall = block as ToolCall;
					let normalizedToolCall: ToolCall = toolCall;

					if (!isSameModel && toolCall.thoughtSignature) {
						normalizedToolCall = { ...toolCall, thoughtSignature: undefined };
					}

					if (isAnthropicTarget) {
						const normalizedId = normalizeAnthropicTargetToolCallId(
							toolCall.id,
							model,
							assistantMsg,
							normalizeToolCallId,
						);
						if (normalizedId !== toolCall.id) {
							toolCallIdMap.set(toolCall.id, normalizedId);
							normalizedToolCall = { ...normalizedToolCall, id: normalizedId };
						}
					} else if (!isSameModel && normalizeToolCallId) {
						const normalizedId = normalizeToolCallId(toolCall.id, model, assistantMsg);
						if (normalizedId !== toolCall.id) {
							toolCallIdMap.set(toolCall.id, normalizedId);
							normalizedToolCall = { ...normalizedToolCall, id: normalizedId };
						}
					}

					return normalizedToolCall;
				}

				return block;
			});

			// A demoted-thinking block that survived as the message's final block can
			// still end with the thinking text's own trailing whitespace (bare
			// Anthropic-dialect demotion copies it verbatim), and Anthropic rejects a
			// terminal assistant message whose text ends with trailing whitespace
			// ("final assistant content cannot end with trailing whitespace").
			// trimEnd() is safe: demoted text is synthesized context, never
			// byte-exact replay material.
			const finalBlock = transformedContent[transformedContent.length - 1];
			if (finalBlock?.type === "text" && isDemotedThinking(finalBlock)) {
				transformedContent[transformedContent.length - 1] = { ...finalBlock, text: finalBlock.text.trimEnd() };
			}

			return {
				...assistantMsg,
				content: transformedContent,
			};
		}
		return msg;
	});
	const transformed = deduplicateToolCallIds(
		normalizedMessages,
		maxNormalizedToolCallIdLength,
		duplicateToolCallIdSuffixPrefix,
	);
	// All real tool results, keyed by id, in document order. One id can map to
	// more than one result: compaction can fold an assistant `tool_use` into a
	// summary string while its `tool_result` survives, and a later turn may reuse
	// the id. `takeRealToolResult` pulls the earliest unconsumed result positioned
	// AFTER the call's assistant turn, so an orphaned earlier result is never
	// pulled forward onto a later call (which would surface a prior turn's output).
	type IndexedToolResult = { index: number; msg: ToolResultMessage; consumed: boolean };
	const realToolResultsById = new Map<string, IndexedToolResult[]>();
	for (let index = 0; index < transformed.length; index++) {
		const msg = transformed[index];
		if (msg.role === "toolResult") {
			const entry: IndexedToolResult = { index, msg, consumed: false };
			const entries = realToolResultsById.get(msg.toolCallId);
			if (entries) entries.push(entry);
			else realToolResultsById.set(msg.toolCallId, [entry]);
		}
	}
	const takeRealToolResult = (id: string, afterIndex: number): ToolResultMessage | undefined => {
		const entries = realToolResultsById.get(id);
		if (!entries) return undefined;
		for (const entry of entries) {
			if (entry.consumed || entry.index <= afterIndex) continue;
			entry.consumed = true;
			return entry.msg;
		}
		return undefined;
	};

	// Anthropic rejects `tool_result` blocks whose `tool_use_id` does not appear in a prior
	// `tool_use` block. After handoff/compaction folds an assistant turn into a summary
	// string, the user-side `toolResult` for that turn can survive while the originating
	// `tool_use` disappears — leaving an orphan that triggers HTTP 400. Track the set of
	// `tool_use` ids that survive transformation so the second pass can drop orphans cleanly.
	const validToolUseIds = new Set<string>();
	for (const msg of transformed) {
		if (msg.role !== "assistant") continue;
		for (const block of msg.content) {
			if (block.type === "toolCall") validToolUseIds.add(block.id);
		}
	}

	// Second pass: ensure each surviving assistant tool call is immediately
	// followed by exactly one corresponding tool result.
	const result: Message[] = [];
	let pendingToolCalls: ToolCall[] = [];
	// Index of the assistant turn that declared `pendingToolCalls`; a pulled
	// result must be positioned after it (see `takeRealToolResult`).
	let pendingToolCallsStartIndex = -1;
	let pendingAbortedToolCalls = new Map<string, ToolCall>();
	let pendingAbortedTimestamp: number | undefined;
	let pendingAbortedStartIndex = -1;
	// Track which tool calls already have an emitted result so delayed/duplicate
	// toolResult messages cannot create a second provider-visible result.
	const toolCallStatus = new Map<string, ToolCallStatus>();

	const flushPendingToolCalls = (timestamp: number): void => {
		if (pendingToolCalls.length === 0) return;
		for (const tc of pendingToolCalls) {
			if (toolCallStatus.has(tc.id)) continue;
			const realToolResult = takeRealToolResult(tc.id, pendingToolCallsStartIndex);
			if (realToolResult) {
				result.push(realToolResult);
				toolCallStatus.set(tc.id, ToolCallStatus.Resolved);
				continue;
			}
			result.push({
				role: "toolResult",
				toolCallId: tc.id,
				toolName: tc.name,
				content: [{ type: "text", text: "No result provided" }],
				isError: true,
				timestamp,
			} as ToolResultMessage);
			toolCallStatus.set(tc.id, ToolCallStatus.Resolved);
		}
		pendingToolCalls = [];
	};

	const flushPendingAbortedToolCalls = (): void => {
		if (pendingAbortedTimestamp === undefined) return;
		for (const tc of pendingAbortedToolCalls.values()) {
			if (toolCallStatus.has(tc.id)) continue;
			const realToolResult = takeRealToolResult(tc.id, pendingAbortedStartIndex);
			if (realToolResult) {
				result.push(realToolResult);
				toolCallStatus.set(tc.id, ToolCallStatus.Resolved);
				continue;
			}
			result.push({
				role: "toolResult",
				toolCallId: tc.id,
				toolName: tc.name,
				content: [{ type: "text", text: "aborted" }],
				isError: true,
				timestamp: pendingAbortedTimestamp,
			} as ToolResultMessage);
			toolCallStatus.set(tc.id, ToolCallStatus.Aborted);
		}
		pendingAbortedToolCalls = new Map();
		pendingAbortedTimestamp = undefined;
	};

	for (let i = 0; i < transformed.length; i++) {
		const msg = transformed[i];
		const messageTimestamp = "timestamp" in msg && typeof msg.timestamp === "number" ? msg.timestamp : Date.now();

		if (msg.role === "assistant") {
			flushPendingToolCalls(messageTimestamp);
			flushPendingAbortedToolCalls();

			const assistantMsg = msg as AssistantMessage;

			// Drop assistant turns that carry no actionable content (no `text`, no `toolCall`)
			// AND were terminated by a truncating stop reason (`length` / `error` / `aborted`).
			// These are produced when the provider returns `stop_reason: "max_tokens"` (or a
			// stream error) mid-thinking, leaving a `[thinking]`-only message with a valid
			// signature but nothing for the next turn to anchor on. Keeping it creates
			// back-to-back assistant turns once the next response lands, which Anthropic
			// rejects with "messages.X.content.Y: `thinking` blocks in the latest assistant
			// message cannot be modified".
			//
			// `stopReason: "stop"` thinking-only messages are intentionally preserved: they
			// represent reasoning-only assistant turns used for replay round-trips
			// (OpenAI completions `reasoning_text`, Google signed thought parts).
			const originalMsg = messages[i]!;
			if (originalMsg.role === "assistant" && shouldDropTruncatedThinkingOnlyAssistant(originalMsg)) {
				continue;
			}

			const toolCalls = assistantMsg.content.filter(b => b.type === "toolCall") as ToolCall[];

			if (assistantMsg.stopReason === "error" || assistantMsg.stopReason === "aborted") {
				// Keep the assistant message with tool calls intact. Real tool results are
				// emitted immediately if available; otherwise synthesize aborted results
				// before the next turn boundary.
				result.push(msg);
				pendingAbortedToolCalls = new Map(toolCalls.map(toolCall => [toolCall.id, toolCall] as const));
				pendingAbortedTimestamp = assistantMsg.timestamp;
				pendingAbortedStartIndex = i;
				continue;
			}

			if (toolCalls.length > 0) {
				pendingToolCalls = toolCalls;
				pendingToolCallsStartIndex = i;
			}

			result.push(msg);
		} else if (msg.role === "toolResult") {
			if (toolCallStatus.has(msg.toolCallId)) continue;

			if (pendingAbortedToolCalls.has(msg.toolCallId)) {
				pendingAbortedToolCalls.delete(msg.toolCallId);
				toolCallStatus.set(msg.toolCallId, ToolCallStatus.Resolved);
				result.push(msg);
				continue;
			}

			if (pendingToolCalls.some(tc => tc.id === msg.toolCallId)) {
				toolCallStatus.set(msg.toolCallId, ToolCallStatus.Resolved);
				result.push(msg);
				continue;
			}

			if (!validToolUseIds.has(msg.toolCallId)) {
				// Orphan `tool_result`: the originating `tool_use` is not present in the
				// transformed history (typically because handoff/compaction folded the
				// assistant message into a summary string while the user-side result
				// survived). Sending the block as-is would 400 the request, so it must
				// be dropped.
				//
				// If a pending tool-call window is still open (either normal or
				// aborted), the orphan cannot be replaced with a developer note here:
				//
				// * Anthropic requires the next message after an assistant `tool_use`
				//   to be the matching `tool_result`. Inserting a developer message
				//   would break that contiguity.
				// * Flushing pending aborted calls here would wedge synthetic results
				//   between the assistant turn and a real result that may still arrive
				//   inside the current contiguous result window.
				//
				// Drop the orphan silently in that case; the pending calls will be
				// resolved in their own contiguous result window or at the next boundary.
				if (pendingToolCalls.some(tc => !toolCallStatus.has(tc.id)) || pendingAbortedToolCalls.size > 0) {
					continue;
				}
				// No pending tool-call window: safe to preserve the text payload so the
				// model still sees what the tool returned.
				//
				// The note is emitted with `role: "user"` rather than `role: "developer"`
				// because the developer role is elevated by some providers:
				//
				// * Ollama maps `developer` -> `system` (highest instruction priority).
				// * OpenAI chat-completions reasoning models forward `developer` as
				//   `developer` (above-user instruction priority).
				//
				// Stale, model-untrusted tool output must not gain instruction priority
				// above user/developer messages it lived alongside before compaction.
				// `user` role is mapped to plain user content by every provider, so the
				// content survives without ever being treated as an instruction the
				// model should obey.
				const textParts: string[] = [];
				for (const part of msg.content) {
					if (part.type === "text" && part.text.trim() !== "") textParts.push(part.text);
				}
				if (textParts.length > 0) {
					const errorAttr = msg.isError ? ' is-error="true"' : "";
					result.push({
						role: "user",
						content: `<stale-tool-result tool="${msg.toolName}" id="${msg.toolCallId}"${errorAttr}>\n${textParts.join("\n")}\n</stale-tool-result>`,
						timestamp: messageTimestamp,
					} as UserMessage);
				}
			}

			// The matching tool_use exists elsewhere, but this result is not in
			// the currently open result window. Emitting it here would break the
			// provider invariant; the first real result is pulled into the correct
			// slot by the pending-call flush instead.
		} else if (msg.role === "user" || msg.role === "developer") {
			flushPendingToolCalls(messageTimestamp);
			flushPendingAbortedToolCalls();
			result.push(msg);
		} else {
			flushPendingToolCalls(messageTimestamp);
			flushPendingAbortedToolCalls();
			result.push(msg);
		}
	}

	flushPendingToolCalls(Date.now());
	flushPendingAbortedToolCalls();

	return result;
}

export interface MessageNormalizationOptions {
	readonly providerId?: string;
	readonly modelId?: string;
	readonly signingAnthropicEndpoint?: boolean;
	readonly replayUnsignedThinking?: boolean;
	readonly officialAnthropicEndpoint?: boolean;
	readonly maximumToolCallIdLength?: number;
}

export function normalizeDispatchMessages(
	messages: readonly DispatchMessage[],
	lease: ProviderRouteLease,
	options: MessageNormalizationOptions = {},
): Message[] {
	const api: Api = lease.protocol === "openai_chat"
		? "openai-chat"
		: lease.protocol === "openai_responses"
			? "openai-responses"
			: "anthropic-messages";
	const model: Model = {
		provider: options.providerId ?? lease.providerId,
		api,
		id: options.modelId ?? lease.modelId,
		reasoning: true,
		compat: {
			signingEndpoint: options.signingAnthropicEndpoint ?? lease.providerId === "anthropic",
			replayUnsignedThinking: options.replayUnsignedThinking ?? lease.providerId !== "anthropic",
			officialEndpoint: options.officialAnthropicEndpoint ?? lease.providerId === "anthropic",
		},
	};
	const converted = messages.map((message, index) => dispatchToInternal(message, model, index));
	return transformMessages(
		converted,
		model,
		(id) => normalizeToolCallId(id, api),
		options.maximumToolCallIdLength ?? MAX_TOOL_CALL_ID_LENGTH,
	);
}

function dispatchToInternal(message: DispatchMessage, model: Model, index: number): Message {
	const timestamp = index + 1;
	if (message.role === "tool") {
		if (!message.toolCallId?.trim()) throw new TypeError("tool message requires toolCallId");
		return {
			role: "toolResult",
			toolCallId: message.toolCallId,
			toolName: message.name?.trim() || "unknown_tool",
			content: textParts(message.content),
			isError: toolResultIsError(message.content),
			timestamp,
		};
	}
	if (message.role === "assistant") {
		return {
			role: "assistant",
			content: assistantParts(message.content),
			provider: model.provider,
			api: model.api,
			model: model.id,
			stopReason: assistantStopReason(message.content),
			timestamp,
		};
	}
	return {
		role: message.role,
		content: textContent(message.content),
		timestamp,
	};
}

function assistantParts(content: DispatchMessage["content"]): AssistantBlock[] {
	if (typeof content === "string") return content ? [{ type: "text", text: content }] : [];
	const blocks: AssistantBlock[] = [];
	for (const raw of content) {
		if (!isJsonRecord(raw)) {
			blocks.push({ type: "text", text: String(raw) });
			continue;
		}
		const type = String(raw.type ?? "");
		if (type === "text" || type === "output_text") {
			const text = String(raw.text ?? raw.content ?? "");
			if (text) blocks.push({ type: "text", text });
			continue;
		}
		if (type === "thinking" || type === "reasoning") {
			const thinking = String(raw.thinking ?? raw.text ?? raw.content ?? "");
			const signature = stringOrUndefined(raw.signature ?? raw.thinkingSignature);
			blocks.push({ type: "thinking", thinking, ...(signature ? { thinkingSignature: signature } : {}) });
			continue;
		}
		if (type === "redacted_thinking" || type === "redactedThinking") {
			blocks.push({ type: "redactedThinking", data: stringOrUndefined(raw.data) });
			continue;
		}
		if (type === "fallback") {
			blocks.push({ type: "fallback", reason: stringOrUndefined(raw.reason) });
			continue;
		}
		if (type === "image" || type === "image_url") {
			const mediaType = String(raw.mediaType ?? raw.media_type ?? "application/octet-stream");
			const data = imageData(raw);
			if (data) blocks.push({ type: "image", mediaType, data });
			continue;
		}
		if (type === "tool_call" || type === "toolCall" || type === "function_call") {
			blocks.push({
				type: "toolCall",
				id: String(raw.id ?? raw.call_id ?? raw.toolCallId ?? ""),
				name: String(raw.name ?? nestedName(raw.function) ?? ""),
				arguments: toolArguments(raw.arguments ?? nestedArguments(raw.function) ?? raw.input),
				thoughtSignature: stringOrUndefined(raw.thoughtSignature ?? raw.thought_signature),
			});
			continue;
		}
		blocks.push({ type: "text", text: JSON.stringify(raw) });
	}
	return blocks;
}

function textParts(content: DispatchMessage["content"]): TextBlock[] {
	if (typeof content === "string") return [{ type: "text", text: content }];
	const parts: TextBlock[] = [];
	for (const raw of content) {
		if (isJsonRecord(raw) && (raw.type === "text" || raw.type === "output_text")) {
			parts.push({ type: "text", text: String(raw.text ?? raw.content ?? "") });
			continue;
		}
		if (isJsonRecord(raw) && (raw.type === "tool_result" || raw.type === "toolResult")) {
			parts.push({ type: "text", text: textContent(raw.content as JsonValue | readonly JsonValue[]) });
			continue;
		}
		parts.push({ type: "text", text: typeof raw === "string" ? raw : JSON.stringify(raw) });
	}
	return parts.length > 0 ? parts : [{ type: "text", text: "" }];
}

function textContent(content: JsonValue | readonly JsonValue[]): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return JSON.stringify(content);
	const values: string[] = [];
	for (const raw of content) {
		if (typeof raw === "string") values.push(raw);
		else if (isJsonRecord(raw) && (raw.type === "text" || raw.type === "output_text")) values.push(String(raw.text ?? raw.content ?? ""));
		else values.push(JSON.stringify(raw));
	}
	return values.join("\n");
}

function assistantStopReason(content: DispatchMessage["content"]): AssistantMessage["stopReason"] {
	if (!Array.isArray(content)) return "stop";
	for (const raw of content) {
		if (!isJsonRecord(raw)) continue;
		const reason = String(raw.stopReason ?? raw.stop_reason ?? "");
		if (reason === "toolUse" || reason === "tool_use") return "toolUse";
		if (reason === "length" || reason === "max_tokens") return "length";
		if (reason === "error") return "error";
		if (reason === "aborted" || reason === "cancelled") return "aborted";
	}
	return assistantParts(content).some((block) => block.type === "toolCall") ? "toolUse" : "stop";
}

function toolResultIsError(content: DispatchMessage["content"]): boolean {
	if (!Array.isArray(content)) return false;
	return content.some((raw) => isJsonRecord(raw) && (raw.isError === true || raw.is_error === true));
}

function normalizeToolCallId(id: string, api: Api): string {
	const value = id.trim();
	if (api !== "anthropic-messages") return value.slice(0, 256);
	if (ANTHROPIC_TOOL_CALL_ID_PATTERN.test(value)) return value;
	const sanitized = value.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, MAX_TOOL_CALL_ID_LENGTH);
	return sanitized || fallbackAnthropicToolCallId(value || "missing");
}

function toolArguments(value: unknown): Readonly<Record<string, JsonValue>> {
	if (isJsonRecord(value)) return value as Record<string, JsonValue>;
	if (typeof value !== "string" || !value.trim()) return {};
	try {
		const parsed = JSON.parse(value) as unknown;
		return isJsonRecord(parsed) ? parsed as Record<string, JsonValue> : { value: parsed as JsonValue };
	} catch {
		return { _malformed_json: value };
	}
}

function nestedName(value: unknown): string | undefined {
	return isJsonRecord(value) ? stringOrUndefined(value.name) : undefined;
}

function nestedArguments(value: unknown): unknown {
	return isJsonRecord(value) ? value.arguments : undefined;
}

function imageData(value: Readonly<Record<string, JsonValue>>): string {
	const direct = stringOrUndefined(value.data ?? value.url);
	if (direct) return direct;
	const source = value.source;
	if (!isJsonRecord(source)) return "";
	return String(source.data ?? source.url ?? "");
}

function stringOrUndefined(value: unknown): string | undefined {
	return typeof value === "string" && value.length > 0 ? value : undefined;
}

function isJsonRecord(value: unknown): value is Record<string, JsonValue> {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}
