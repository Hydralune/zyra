from __future__ import annotations

from typing import Any, Mapping

from .errors import SkillRuntimeError


class SkillIntegrationError(SkillRuntimeError):
    code = "skill_integration_error"


class SkillCommandParseError(SkillIntegrationError):
    code = "skill_command_parse_error"


class SkillCommandNotFound(SkillIntegrationError):
    code = "skill_command_not_found"


class SkillCommandNotInvocable(SkillIntegrationError):
    code = "skill_command_not_invocable"


class SkillCommandArgumentError(SkillIntegrationError):
    code = "skill_command_argument_error"


class SkillSessionConflict(SkillIntegrationError):
    code = "skill_session_conflict"


class SkillSessionNotFound(SkillIntegrationError):
    code = "skill_session_not_found"


class SkillSessionClosed(SkillIntegrationError):
    code = "skill_session_closed"


class SkillSessionOwnershipError(SkillIntegrationError):
    code = "skill_session_ownership_error"


class SkillCheckpointConflict(SkillIntegrationError):
    code = "skill_checkpoint_conflict"


class SkillCheckpointCorrupt(SkillIntegrationError):
    code = "skill_checkpoint_corrupt"


class SkillEventAppendError(SkillIntegrationError):
    code = "skill_event_append_error"


class SkillEventSequenceError(SkillIntegrationError):
    code = "skill_event_sequence_error"


class SkillEventCausalityError(SkillIntegrationError):
    code = "skill_event_causality_error"


class SkillPluginError(SkillIntegrationError):
    code = "skill_plugin_error"


class SkillPluginNotFound(SkillPluginError):
    code = "skill_plugin_not_found"


class SkillPluginDisabled(SkillPluginError):
    code = "skill_plugin_disabled"


class SkillPluginConflict(SkillPluginError):
    code = "skill_plugin_conflict"


class SkillPluginReloadRejected(SkillPluginError):
    code = "skill_plugin_reload_rejected"


class SkillPluginSupplyChainRejected(SkillPluginError):
    code = "skill_plugin_supply_chain_rejected"


class SkillPluginSecretRejected(SkillPluginError):
    code = "skill_plugin_secret_rejected"


class SkillPluginUpdatePending(SkillPluginError):
    code = "skill_plugin_update_pending"


class SkillPluginUpdateDenied(SkillPluginError):
    code = "skill_plugin_update_denied"


class SkillPluginRollbackError(SkillPluginError):
    code = "skill_plugin_rollback_error"


class SkillMcpError(SkillIntegrationError):
    code = "skill_mcp_error"


class SkillMcpServerUnavailable(SkillMcpError):
    code = "skill_mcp_server_unavailable"


class SkillMcpAuthenticationRequired(SkillMcpError):
    code = "skill_mcp_authentication_required"


class SkillMcpProjectionInvalid(SkillMcpError):
    code = "skill_mcp_projection_invalid"


class SkillMcpProjectionStale(SkillMcpError):
    code = "skill_mcp_projection_stale"


class SkillMcpResourceRejected(SkillMcpError):
    code = "skill_mcp_resource_rejected"


class SkillMcpResourceMissing(SkillMcpError):
    code = "skill_mcp_resource_missing"


class SkillMcpDigestMismatch(SkillMcpError):
    code = "skill_mcp_digest_mismatch"


class SkillMcpPaginationError(SkillMcpError):
    code = "skill_mcp_pagination_error"


class SkillMcpSamplingRejected(SkillMcpError):
    code = "skill_mcp_sampling_rejected"


class SkillRestoreIntegrationError(SkillIntegrationError):
    code = "skill_restore_integration_error"


class SkillRestoreReferenceRejected(SkillRestoreIntegrationError):
    code = "skill_restore_reference_rejected"


class SkillRestoreBudgetExceeded(SkillRestoreIntegrationError):
    code = "skill_restore_budget_exceeded"


class SkillRestorePolicyRejected(SkillRestoreIntegrationError):
    code = "skill_restore_policy_rejected"


class SkillRestoreMessageRejected(SkillRestoreIntegrationError):
    code = "skill_restore_message_rejected"


class SkillOutcomeError(SkillIntegrationError):
    code = "skill_outcome_error"


class SkillOutcomeConflict(SkillOutcomeError):
    code = "skill_outcome_conflict"


class SkillOutcomeReferenceRejected(SkillOutcomeError):
    code = "skill_outcome_reference_rejected"


class SkillOutcomeNotTerminal(SkillOutcomeError):
    code = "skill_outcome_not_terminal"


class SkillForkScopeError(SkillIntegrationError):
    code = "skill_fork_scope_error"


class SkillForkToolDenied(SkillForkScopeError):
    code = "skill_fork_tool_denied"


class SkillForkReceiptMismatch(SkillForkScopeError):
    code = "skill_fork_receipt_mismatch"


class SkillIntegrationDisabled(SkillIntegrationError):
    code = "skill_integration_disabled"


def error_payload(error: Exception) -> dict[str, Any]:
    detail = getattr(error, "detail", {})
    if not isinstance(detail, Mapping):
        detail = {"raw_detail": str(detail)}
    return {
        "error": str(getattr(error, "code", "skill_integration_error")),
        "message": str(error),
        "detail": dict(detail),
        "exception_type": type(error).__name__,
    }
