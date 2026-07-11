from __future__ import annotations


class SkillRuntimeError(RuntimeError):
    """Base class for fail-closed skill runtime failures."""

    code = "skill_runtime_error"

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": str(self), "detail": dict(self.detail)}


class SkillRuntimeDisabled(SkillRuntimeError):
    code = "skill_runtime_disabled"


class SkillRegistryDisabled(SkillRuntimeError):
    code = "skill_registry_disabled"


class SkillBodyLoaderDisabled(SkillRuntimeError):
    code = "skill_body_loader_disabled"


class SkillAllowedToolsPolicyDisabled(SkillRuntimeError):
    code = "skill_allowed_tools_policy_disabled"


class SkillNotFound(SkillRuntimeError):
    code = "skill_not_found"


class SkillAmbiguous(SkillRuntimeError):
    code = "skill_ambiguous"


class SkillDisabled(SkillRuntimeError):
    code = "skill_disabled"


class SkillRevoked(SkillRuntimeError):
    code = "skill_revoked"


class SkillSuperseded(SkillRuntimeError):
    code = "skill_superseded"


class SkillRevisionNotFound(SkillRuntimeError):
    code = "skill_revision_not_found"


class SkillRevisionMismatch(SkillRuntimeError):
    code = "skill_revision_mismatch"


class SkillFrontmatterError(SkillRuntimeError):
    code = "skill_frontmatter_invalid"


class SkillNameError(SkillFrontmatterError):
    code = "skill_name_invalid"


class SkillSourceError(SkillRuntimeError):
    code = "skill_source_error"


class SkillPathError(SkillRuntimeError):
    code = "skill_path_invalid"


class SkillContainmentError(SkillPathError):
    code = "skill_path_outside_root"


class SkillSymlinkError(SkillPathError):
    code = "skill_symlink_rejected"


class SkillReadRace(SkillPathError):
    code = "skill_read_race"


class SkillResourceNotFound(SkillPathError):
    code = "skill_resource_not_found"


class SkillResourceTypeError(SkillPathError):
    code = "skill_resource_type_rejected"


class SkillBudgetExceeded(SkillRuntimeError):
    code = "skill_context_budget_exceeded"


class SkillPolicyDenied(SkillRuntimeError):
    code = "skill_policy_denied"


class SkillPermissionPending(SkillRuntimeError):
    code = "skill_permission_pending"


class SkillPermissionDenied(SkillRuntimeError):
    code = "skill_permission_denied"


class SkillHookError(SkillRuntimeError):
    code = "skill_hook_error"


class SkillInvocationConflict(SkillRuntimeError):
    code = "skill_invocation_conflict"


class SkillInvocationStateError(SkillRuntimeError):
    code = "skill_invocation_state_invalid"


class SkillForkUnavailable(SkillRuntimeError):
    code = "skill_fork_runtime_unavailable"


class SkillReloadRejected(SkillRuntimeError):
    code = "skill_reload_rejected"


class SkillRollbackRejected(SkillRuntimeError):
    code = "skill_rollback_rejected"


class PluginManifestError(SkillRuntimeError):
    code = "skill_plugin_manifest_invalid"


class PluginSecretSubstitutionError(SkillRuntimeError):
    code = "skill_plugin_secret_substitution_rejected"


class SkillCompactRestoreError(SkillRuntimeError):
    code = "skill_compact_restore_failed"


class SkillStateCorrupt(SkillRuntimeError):
    code = "skill_state_corrupt"
