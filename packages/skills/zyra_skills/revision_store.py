from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .errors import (
    SkillRevisionMismatch,
    SkillRevisionNotFound,
    SkillRevoked,
    SkillRollbackRejected,
    SkillStateCorrupt,
)
from .models import SkillRevision, SkillRevisionLifecycle, SkillVersionRef, utc_now


@dataclass(frozen=True, slots=True)
class RevisionStatus:
    immutable_ref: str
    lifecycle: SkillRevisionLifecycle
    revocation_epoch: int
    reason: str = ""
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "immutable_ref": self.immutable_ref,
            "lifecycle": str(self.lifecycle),
            "revocation_epoch": self.revocation_epoch,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }


class SkillRevisionStore:
    """Content-addressed immutable revision custody owned by M1-03C.

    The source files remain the product-owned asset location. This store owns
    revision identity, lifecycle, revocation epoch, and rollback selection. It
    never rewrites source files and does not become a second session store.
    """

    def __init__(self, *, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path).resolve() if state_path else None
        self._lock = RLock()
        self._revisions: dict[str, SkillRevision] = {}
        self._status: dict[str, RevisionStatus] = {}
        self._history_by_skill: dict[str, list[str]] = {}
        self._revocation_epoch_by_skill: dict[str, int] = {}
        self._active_ref_by_skill: dict[str, str] = {}
        if self.state_path and self.state_path.exists():
            self._restore_lifecycle_state()

    def stage(self, revisions: Iterable[SkillRevision]) -> tuple[SkillRevision, ...]:
        staged: list[SkillRevision] = []
        with self._lock:
            for revision in revisions:
                ref = revision.version_ref.immutable_ref
                existing = self._revisions.get(ref)
                if existing is not None:
                    self._assert_same_revision(existing, revision)
                    staged.append(existing)
                    continue
                epoch = self._revocation_epoch_by_skill.get(revision.version_ref.skill_id, 0)
                version_ref = replace(revision.version_ref, revocation_epoch=epoch)
                staged_revision = replace(revision, version_ref=version_ref)
                self._revisions[ref] = staged_revision
                restored_status = self._status.get(ref)
                self._status[ref] = (
                    replace(restored_status, revocation_epoch=max(epoch, restored_status.revocation_epoch))
                    if restored_status is not None
                    else RevisionStatus(
                        immutable_ref=ref,
                        lifecycle=SkillRevisionLifecycle.VALIDATED,
                        revocation_epoch=epoch,
                    )
                )
                history = self._history_by_skill.setdefault(version_ref.skill_id, [])
                if ref not in history:
                    history.append(ref)
                staged.append(staged_revision)
            self._persist_lifecycle_state()
        return tuple(staged)

    def commit_generation(
        self,
        revisions: Iterable[SkillRevision],
        *,
        active_refs: Iterable[str],
    ) -> tuple[SkillRevision, ...]:
        """Atomically stage and activate one complete registry generation.

        No lifecycle mutation is visible until every collision, revocation,
        and activation check has succeeded.  On failure the exact previous
        in-memory state is restored and no state file is replaced.
        """

        incoming = tuple(revisions)
        selected = set(active_refs)
        with self._lock:
            backup = (
                dict(self._revisions),
                dict(self._status),
                {key: list(value) for key, value in self._history_by_skill.items()},
                dict(self._revocation_epoch_by_skill),
                dict(self._active_ref_by_skill),
            )
            try:
                staged: list[SkillRevision] = []
                by_ref: dict[str, SkillRevision] = {}
                for revision in incoming:
                    ref = revision.version_ref.immutable_ref
                    existing = self._revisions.get(ref)
                    if existing is not None:
                        self._assert_same_revision(existing, revision)
                        stored = existing
                    else:
                        epoch = self._revocation_epoch_by_skill.get(revision.version_ref.skill_id, 0)
                        stored = replace(
                            revision,
                            version_ref=replace(revision.version_ref, revocation_epoch=epoch),
                        )
                        self._revisions[ref] = stored
                    status = self._status.get(ref)
                    if status is not None and status.lifecycle is SkillRevisionLifecycle.REVOKED:
                        if ref in selected:
                            raise SkillRevoked(
                                "revoked skill revision cannot be activated",
                                detail={"ref": ref},
                            )
                    if status is None:
                        status = RevisionStatus(
                            immutable_ref=ref,
                            lifecycle=SkillRevisionLifecycle.VALIDATED,
                            revocation_epoch=stored.version_ref.revocation_epoch,
                        )
                        self._status[ref] = status
                    history = self._history_by_skill.setdefault(stored.version_ref.skill_id, [])
                    if ref not in history:
                        history.append(ref)
                    staged.append(stored)
                    by_ref[ref] = stored

                missing = selected - set(by_ref)
                if missing:
                    raise SkillRevisionNotFound(
                        "registry generation activates an unstaged revision",
                        detail={"refs": sorted(missing)},
                    )
                next_active_by_skill: dict[str, str] = {}
                for ref in selected:
                    revision = by_ref[ref]
                    skill_id = revision.version_ref.skill_id
                    other = next_active_by_skill.get(skill_id)
                    if other and other != ref:
                        raise SkillRevisionMismatch(
                            "registry generation selected multiple active revisions for one skill",
                            detail={"skill_id": skill_id, "refs": sorted((other, ref))},
                        )
                    next_active_by_skill[skill_id] = ref

                now = utc_now()
                for skill_id, previous_ref in tuple(self._active_ref_by_skill.items()):
                    next_ref = next_active_by_skill.get(skill_id)
                    if next_ref == previous_ref:
                        continue
                    previous = self._status.get(previous_ref)
                    if previous is not None and previous.lifecycle is not SkillRevisionLifecycle.REVOKED:
                        self._status[previous_ref] = replace(
                            previous,
                            lifecycle=SkillRevisionLifecycle.SUPERSEDED,
                            updated_at=now,
                        )
                for skill_id, ref in next_active_by_skill.items():
                    status = self._status[ref]
                    self._status[ref] = replace(
                        status,
                        lifecycle=SkillRevisionLifecycle.AVAILABLE,
                        updated_at=now,
                    )
                self._active_ref_by_skill = next_active_by_skill
                self._persist_lifecycle_state()
                return tuple(
                    replace(
                        revision,
                        lifecycle=self._status[revision.version_ref.immutable_ref].lifecycle,
                        version_ref=replace(
                            revision.version_ref,
                            revocation_epoch=self._status[revision.version_ref.immutable_ref].revocation_epoch,
                        ),
                    )
                    for revision in staged
                )
            except Exception:
                (
                    self._revisions,
                    self._status,
                    self._history_by_skill,
                    self._revocation_epoch_by_skill,
                    self._active_ref_by_skill,
                ) = backup
                raise

    def activate(self, revision: SkillRevision) -> SkillRevision:
        ref = revision.version_ref.immutable_ref
        with self._lock:
            stored = self._revisions.get(ref)
            if stored is None:
                raise SkillRevisionNotFound("cannot activate an unstaged skill revision", detail={"ref": ref})
            status = self._status[ref]
            if status.lifecycle is SkillRevisionLifecycle.REVOKED:
                raise SkillRevoked("revoked skill revision cannot be activated", detail={"ref": ref})
            previous_ref = self._active_ref_by_skill.get(stored.version_ref.skill_id)
            if previous_ref and previous_ref != ref:
                previous = self._status[previous_ref]
                if previous.lifecycle is not SkillRevisionLifecycle.REVOKED:
                    self._status[previous_ref] = replace(
                        previous,
                        lifecycle=SkillRevisionLifecycle.SUPERSEDED,
                        updated_at=utc_now(),
                    )
            self._status[ref] = replace(
                status,
                lifecycle=SkillRevisionLifecycle.AVAILABLE,
                updated_at=utc_now(),
            )
            self._active_ref_by_skill[stored.version_ref.skill_id] = ref
            self._persist_lifecycle_state()
            return replace(stored, lifecycle=SkillRevisionLifecycle.AVAILABLE)

    def get(self, version_ref: SkillVersionRef | str, *, allow_superseded: bool = True) -> SkillRevision:
        ref = version_ref.immutable_ref if isinstance(version_ref, SkillVersionRef) else str(version_ref)
        with self._lock:
            revision = self._revisions.get(ref)
            if revision is None:
                raise SkillRevisionNotFound("skill revision was not found", detail={"ref": ref})
            status = self._status[ref]
            if status.lifecycle is SkillRevisionLifecycle.REVOKED:
                raise SkillRevoked("skill revision is revoked", detail={"ref": ref, "reason": status.reason})
            if status.lifecycle is SkillRevisionLifecycle.SUPERSEDED and not allow_superseded:
                raise SkillRevisionMismatch("skill revision is superseded", detail={"ref": ref})
            return replace(
                revision,
                version_ref=replace(revision.version_ref, revocation_epoch=status.revocation_epoch),
                lifecycle=status.lifecycle,
            )

    def status(self, version_ref: SkillVersionRef | str) -> RevisionStatus:
        ref = version_ref.immutable_ref if isinstance(version_ref, SkillVersionRef) else str(version_ref)
        with self._lock:
            if ref not in self._status:
                raise SkillRevisionNotFound("skill revision status was not found", detail={"ref": ref})
            return self._status[ref]

    def revoke(self, version_ref: SkillVersionRef | str, *, reason: str) -> RevisionStatus:
        ref = version_ref.immutable_ref if isinstance(version_ref, SkillVersionRef) else str(version_ref)
        with self._lock:
            revision = self._revisions.get(ref)
            if revision is None:
                raise SkillRevisionNotFound("skill revision was not found", detail={"ref": ref})
            skill_id = revision.version_ref.skill_id
            epoch = self._revocation_epoch_by_skill.get(skill_id, 0) + 1
            self._revocation_epoch_by_skill[skill_id] = epoch
            status = RevisionStatus(
                immutable_ref=ref,
                lifecycle=SkillRevisionLifecycle.REVOKED,
                revocation_epoch=epoch,
                reason=reason,
            )
            self._status[ref] = status
            if self._active_ref_by_skill.get(skill_id) == ref:
                self._active_ref_by_skill.pop(skill_id, None)
            self._persist_lifecycle_state()
            return status

    def rollback(self, *, skill_id: str, immutable_ref: str) -> SkillRevision:
        with self._lock:
            if immutable_ref not in self._history_by_skill.get(skill_id, []):
                raise SkillRollbackRejected("rollback target is not part of this skill history", detail={"skill_id": skill_id, "ref": immutable_ref})
            status = self._status[immutable_ref]
            if status.lifecycle is SkillRevisionLifecycle.REVOKED:
                raise SkillRollbackRejected("rollback target is revoked", detail={"ref": immutable_ref})
            revision = self._revisions[immutable_ref]
            return self.activate(revision)

    def history(self, skill_id: str) -> tuple[RevisionStatus, ...]:
        with self._lock:
            return tuple(self._status[ref] for ref in self._history_by_skill.get(skill_id, ()))

    def active_ref(self, skill_id: str) -> str:
        with self._lock:
            return self._active_ref_by_skill.get(skill_id, "")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "status": {ref: item.to_dict() for ref, item in self._status.items()},
                "history_by_skill": {key: list(value) for key, value in self._history_by_skill.items()},
                "revocation_epoch_by_skill": dict(self._revocation_epoch_by_skill),
                "active_ref_by_skill": dict(self._active_ref_by_skill),
            }

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        if int(snapshot.get("version") or 0) != 1:
            raise SkillStateCorrupt("unsupported skill revision lifecycle snapshot version")
        history = {
            str(key): [str(value) for value in values]
            for key, values in dict(snapshot.get("history_by_skill") or {}).items()
            if isinstance(values, list)
        }
        epochs = {
            str(key): int(value)
            for key, value in dict(snapshot.get("revocation_epoch_by_skill") or {}).items()
        }
        active = {
            str(key): str(value)
            for key, value in dict(snapshot.get("active_ref_by_skill") or {}).items()
        }
        statuses: dict[str, RevisionStatus] = {}
        for ref, raw in dict(snapshot.get("status") or {}).items():
            if not isinstance(raw, dict):
                raise SkillStateCorrupt("skill revision lifecycle status is malformed")
            statuses[str(ref)] = RevisionStatus(
                immutable_ref=str(ref),
                lifecycle=SkillRevisionLifecycle(str(raw.get("lifecycle") or "validated")),
                revocation_epoch=int(raw.get("revocation_epoch") or 0),
                reason=str(raw.get("reason") or ""),
                updated_at=str(raw.get("updated_at") or utc_now()),
            )
        with self._lock:
            # Revision objects are rebuilt from trusted product/source assets.
            # Restore only lifecycle records that will be reconciled when the
            # next source scan stages the matching immutable refs.
            self._history_by_skill = history
            self._revocation_epoch_by_skill = epochs
            self._active_ref_by_skill = active
            self._status = statuses

    def _assert_same_revision(self, left: SkillRevision, right: SkillRevision) -> None:
        if left.version_ref.content_digest != right.version_ref.content_digest:
            raise SkillRevisionMismatch("immutable skill ref collision", detail={"ref": left.version_ref.immutable_ref})
        if left.body.digest != right.body.digest:
            raise SkillRevisionMismatch("skill body digest changed under immutable ref", detail={"ref": left.version_ref.immutable_ref})

    def _restore_lifecycle_state(self) -> None:
        assert self.state_path is not None
        try:
            item = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SkillStateCorrupt("skill revision lifecycle state is unreadable") from error
        if int(item.get("version") or 0) != 1:
            raise SkillStateCorrupt("unsupported skill revision lifecycle state version")
        self.restore_snapshot(item)

    def _persist_lifecycle_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.state_path)
