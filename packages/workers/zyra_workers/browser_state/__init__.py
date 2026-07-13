"""Zyra-owned browser DOM state, selector generation, and validity runtime."""

from .contracts import (
    BrowserContextDisclosure,
    BrowserDisclosureBudget,
    BrowserDomCapture,
    BrowserDomCaptureRequest,
    BrowserSelectorEntry,
    BrowserSelectorMapRevision,
    BrowserSelectorRef,
    SelectorMapIdentity,
)
from .capture_policy import BrowserCapturePolicyDecision, BrowserDomCapturePolicy
from .runtime import BrowserDomStateRuntime
from .selector_ranker import BrowserSelectorRanker, BrowserSelectorSelection
from .selector_store import BrowserSelectorMapStore
from .semantic_sections import BrowserSemanticOutline, BrowserSemanticOutlineBuilder
from .state_delta import BrowserDomDelta, compare_selector_revisions
from .watchdog import BrowserDomObservation, BrowserDomWatchdog

__all__ = [
    "BrowserContextDisclosure",
    "BrowserCapturePolicyDecision",
    "BrowserDisclosureBudget",
    "BrowserDomCapture",
    "BrowserDomCaptureRequest",
    "BrowserDomCapturePolicy",
    "BrowserDomDelta",
    "BrowserDomObservation",
    "BrowserDomStateRuntime",
    "BrowserDomWatchdog",
    "BrowserSelectorEntry",
    "BrowserSelectorMapRevision",
    "BrowserSelectorMapStore",
    "BrowserSelectorRanker",
    "BrowserSelectorRef",
    "BrowserSelectorSelection",
    "BrowserSemanticOutline",
    "BrowserSemanticOutlineBuilder",
    "SelectorMapIdentity",
    "compare_selector_revisions",
]
