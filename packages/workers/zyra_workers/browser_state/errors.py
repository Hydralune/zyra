from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class BrowserStateError(RuntimeError):
    code = "browser_state_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": dict(self.details)}


class BrowserDomCaptureDisabled(BrowserStateError):
    code = "browser_dom_state_runtime_disabled"


class BrowserDomCaptureFailed(BrowserStateError):
    code = "browser_dom_capture_failed"


class BrowserDomCaptureStale(BrowserStateError):
    code = "browser_dom_capture_stale"


class BrowserDomRootMissing(BrowserDomCaptureFailed):
    code = "browser_dom_root_missing"


class BrowserSelectorStoreDisabled(BrowserStateError):
    code = "browser_selector_store_disabled"


class BrowserSelectorMapConflict(BrowserStateError):
    code = "browser_selector_map_conflict"


class BrowserSelectorMapMissing(BrowserStateError):
    code = "browser_selector_map_missing"


class BrowserSelectorStale(BrowserStateError):
    code = "browser_selector_stale"


class BrowserSelectorIdentityMismatch(BrowserStateError):
    code = "browser_selector_identity_mismatch"


class BrowserSelectorNotFound(BrowserStateError):
    code = "browser_selector_not_found"


class BrowserStateSerializationError(BrowserStateError):
    code = "browser_state_serialization_failed"


class BrowserStateBudgetExceeded(BrowserStateError):
    code = "browser_state_budget_exceeded"


class BrowserStateExternalizationFailed(BrowserStateError):
    code = "browser_state_externalization_failed"


class BrowserMessageManagerDisabled(BrowserStateError):
    code = "browser_message_manager_disabled"


class BrowserActionProjectionFailed(BrowserStateError):
    code = "browser_action_result_projection_failed"


class BrowserNextContextUnavailable(BrowserStateError):
    code = "browser_next_context_unavailable"
