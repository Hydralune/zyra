from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zyra_runtime.claude_context_window import (
    ClaudeContextBlock,
    ClaudeContextBlockRole,
    ClaudeContextBudget,
    ClaudeContextSource,
    ClaudeContextWindowManager,
)

from ..browser_state.contracts import BrowserContextDisclosure, state_id
from ..browser_state.errors import BrowserNextContextUnavailable
from .models import BrowserNextContextReceipt


class BrowserNextContextPort:
    """Narrow adapter into 02D's context-window types and budget semantics."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._accepted: set[str] = set()
        self._appends = 0
        self._duplicates = 0
        self._blocked = 0

    def append_disclosure(
        self,
        disclosure: BrowserContextDisclosure,
        *,
        context_window: ClaudeContextWindowManager | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        max_chars: int = 32000,
    ) -> BrowserNextContextReceipt:
        if self.disabled:
            self._blocked += 1
            raise BrowserNextContextUnavailable("browser next-context port is disabled")
        source_id = f"browser-disclosure:{disclosure.disclosure_id}"
        duplicate = disclosure.disclosure_id in self._accepted
        if context_window is None:
            context_window = ClaudeContextWindowManager(
                budget=ClaudeContextBudget(
                    max_chars=max(max_chars, disclosure.bytes + 1024),
                    reserve_chars=1024,
                    min_recent_blocks=1,
                ),
                runtime_source="zyra-browser-context-port",
                runtime_id="M1-S04B-01",
                session_id=session_id,
                request_id=worker_request_id,
            )
        existing = [
            block for block in context_window.blocks
            if block.source is not None and block.source.source_id == source_id
        ]
        if existing:
            duplicate = True
            block = existing[-1]
        else:
            block = ClaudeContextBlock(
                role=ClaudeContextBlockRole.USER,
                text=disclosure.text,
                priority=820,
                source=ClaudeContextSource(
                    source_id=source_id,
                    source_kind="browser_context_disclosure",
                    source_path="packages/workers/zyra_workers/browser_context/context_port.py",
                    upstream_source_path="browser_use/agent/message_manager/service.py",
                    runtime_owner="packages/runtime/zyra_runtime/claude_context_window.py",
                    metadata={
                        "trust": disclosure.trust,
                        "read_once": str(disclosure.read_once).lower(),
                        "selector_revision_id": disclosure.selector_revision_id,
                    },
                ),
                artifact_ids=list(disclosure.artifact_ids),
                metadata={
                    "browser_disclosure_id": disclosure.disclosure_id,
                    "browser_capture_id": disclosure.capture_id,
                    "browser_selector_revision_id": disclosure.selector_revision_id,
                    "browser_selector_refs": list(disclosure.selector_refs),
                    "trust_level": disclosure.trust,
                    "secret_redaction_state": "clean",
                    "read_once": disclosure.read_once,
                },
            )
            context_window.add_block(block)
        model_messages = context_window.model_messages(max_chars=max_chars)
        accepted = any(
            isinstance(message, Mapping)
            and isinstance(message.get("metadata"), Mapping)
            and str(message["metadata"].get("browser_disclosure_id") or "") == disclosure.disclosure_id
            and disclosure.text in str(message.get("content") or "")
            for message in model_messages
        )
        if not accepted:
            self._blocked += 1
            raise BrowserNextContextUnavailable(
                "02D context window did not select the browser disclosure",
                details={
                    "disclosure_id": disclosure.disclosure_id,
                    "context_active_chars": context_window.active_chars,
                    "context_budget": context_window.budget.to_dict(),
                },
            )
        self._accepted.add(disclosure.disclosure_id)
        self._appends += not duplicate
        self._duplicates += duplicate
        return BrowserNextContextReceipt(
            receipt_id=state_id("brctxreceipt"),
            disclosure_id=disclosure.disclosure_id,
            source_id=source_id,
            accepted=True,
            duplicate=duplicate,
            context_block=block.to_dict(),
            reason="already_present" if duplicate else "selected_by_02d_context_window",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserNextContextPort",
            "owner_unit": "M1-S04B-01",
            "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
            "disabled": self.disabled,
            "appends": self._appends,
            "duplicates": self._duplicates,
            "blocked": self._blocked,
        }
