from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import digest, new_identity, utc_now
from .effective_steps import StepBatch
from .errors import conflict
from .models import EffectiveStep, StepDisposition, StepEffect


class CausalEvidenceValidator:
    def validate(
        self,
        batch: StepBatch,
        *,
        expected_run_id: str,
        expected_task_id: str,
        required_stages: Iterable[str] = (),
    ) -> dict[str, Any]:
        admitted = tuple(
            item
            for item in batch.steps
            if item.disposition is StepDisposition.ADMITTED
        )
        by_id: dict[str, EffectiveStep] = {}
        findings: list[dict[str, Any]] = []
        for step in admitted:
            if step.step_id in by_id:
                findings.append(
                    {
                        "code": "causal_step_identity_duplicate",
                        "step_id": step.step_id,
                    }
                )
                continue
            by_id[step.step_id] = step
            if step.run_id != expected_run_id:
                findings.append(
                    {
                        "code": "causal_run_scope_mismatch",
                        "step_id": step.step_id,
                        "expected": expected_run_id,
                        "observed": step.run_id,
                    }
                )
            if step.task_id != expected_task_id:
                findings.append(
                    {
                        "code": "causal_task_scope_mismatch",
                        "step_id": step.step_id,
                        "expected": expected_task_id,
                        "observed": step.task_id,
                    }
                )
        children: dict[str, set[str]] = defaultdict(set)
        parents: dict[str, set[str]] = defaultdict(set)
        for step in admitted:
            for parent_id in step.causal_parent_ids:
                if parent_id == step.step_id:
                    findings.append(
                        {
                            "code": "causal_self_edge",
                            "step_id": step.step_id,
                        }
                    )
                    continue
                if parent_id not in by_id:
                    findings.append(
                        {
                            "code": "causal_parent_missing",
                            "step_id": step.step_id,
                            "parent_id": parent_id,
                        }
                    )
                    continue
                children[parent_id].add(step.step_id)
                parents[step.step_id].add(parent_id)
                parent = by_id[parent_id]
                if parent.sequence >= step.sequence:
                    findings.append(
                        {
                            "code": "causal_order_invalid",
                            "step_id": step.step_id,
                            "parent_id": parent_id,
                            "parent_sequence": parent.sequence,
                            "step_sequence": step.sequence,
                        }
                    )
        roots = tuple(
            step.step_id for step in admitted if not parents.get(step.step_id)
        )
        leaves = tuple(
            step.step_id for step in admitted if not children.get(step.step_id)
        )
        cycles = self._cycles(by_id, children)
        findings.extend(
            {
                "code": "causal_cycle",
                "step_ids": cycle,
            }
            for cycle in cycles
        )
        reachable = self._reachable(roots, children)
        unreachable = sorted(set(by_id) - reachable)
        if unreachable:
            findings.append(
                {
                    "code": "causal_step_unreachable",
                    "step_ids": unreachable,
                }
            )
        stage_counts: dict[str, int] = defaultdict(int)
        effect_counts: dict[str, int] = defaultdict(int)
        for step in admitted:
            stage_counts[step.stage or "unknown"] += 1
            effect_counts[step.effect.value] += 1
        missing_stages = sorted(
            {
                str(stage).strip().casefold()
                for stage in required_stages
                if str(stage).strip()
            }
            - set(stage_counts)
        )
        if missing_stages:
            findings.append(
                {
                    "code": "causal_stage_missing",
                    "stages": missing_stages,
                }
            )
        semantic_paths = self._semantic_paths(
            roots,
            leaves,
            by_id,
            children,
            maximum_paths=10_000,
        )
        effect_path_coverage = self._effect_path_coverage(
            semantic_paths,
            by_id,
        )
        for effect in (
            StepEffect.FAULT.value,
            StepEffect.RECOVERY.value,
            StepEffect.ARTIFACT.value,
            StepEffect.VERIFICATION.value,
        ):
            if effect_counts.get(effect, 0) and not effect_path_coverage.get(effect):
                findings.append(
                    {
                        "code": "causal_effect_path_missing",
                        "effect": effect,
                    }
                )
        receipt = {
            "schema": "zyra.scenario-causal-verification/v1",
            "receipt_id": new_identity("causal"),
            "valid": not findings,
            "run_id": expected_run_id,
            "task_id": expected_task_id,
            "step_count": len(admitted),
            "edge_count": sum(len(value) for value in children.values()),
            "roots": list(roots),
            "leaves": list(leaves),
            "reachable_step_count": len(reachable),
            "cycles": cycles,
            "stage_counts": dict(sorted(stage_counts.items())),
            "effect_counts": dict(sorted(effect_counts.items())),
            "semantic_paths": semantic_paths,
            "effect_path_coverage": effect_path_coverage,
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def require_valid(
        self,
        batch: StepBatch,
        *,
        expected_run_id: str,
        expected_task_id: str,
        required_stages: Iterable[str] = (),
    ) -> dict[str, Any]:
        receipt = self.validate(
            batch,
            expected_run_id=expected_run_id,
            expected_task_id=expected_task_id,
            required_stages=required_stages,
        )
        if not receipt["valid"]:
            raise conflict(
                "scenario_causal_evidence_invalid",
                "Scenario effective-step causal graph is invalid.",
                phase="causal-evidence",
                detail=receipt,
            )
        return receipt

    def _cycles(
        self,
        by_id: Mapping[str, EffectiveStep],
        children: Mapping[str, set[str]],
    ) -> list[list[str]]:
        color: dict[str, int] = {key: 0 for key in by_id}
        stack: list[str] = []
        position: dict[str, int] = {}
        cycles: list[list[str]] = []

        def visit(step_id: str) -> None:
            color[step_id] = 1
            position[step_id] = len(stack)
            stack.append(step_id)
            for child in sorted(children.get(step_id, ())):
                if color.get(child, 0) == 0:
                    visit(child)
                elif color.get(child) == 1:
                    start = position[child]
                    cycle = [*stack[start:], child]
                    if cycle not in cycles:
                        cycles.append(cycle)
            stack.pop()
            position.pop(step_id, None)
            color[step_id] = 2

        for step_id in sorted(by_id):
            if color[step_id] == 0:
                visit(step_id)
        return cycles

    def _reachable(
        self,
        roots: Iterable[str],
        children: Mapping[str, set[str]],
    ) -> set[str]:
        reached: set[str] = set()
        queue = deque(sorted(set(roots)))
        while queue:
            step_id = queue.popleft()
            if step_id in reached:
                continue
            reached.add(step_id)
            queue.extend(
                child
                for child in sorted(children.get(step_id, ()))
                if child not in reached
            )
        return reached

    def _semantic_paths(
        self,
        roots: Iterable[str],
        leaves: Iterable[str],
        by_id: Mapping[str, EffectiveStep],
        children: Mapping[str, set[str]],
        *,
        maximum_paths: int,
    ) -> list[dict[str, Any]]:
        leaf_set = set(leaves)
        output: list[dict[str, Any]] = []
        stack: list[tuple[str, tuple[str, ...]]] = [
            (root, (root,)) for root in reversed(sorted(set(roots)))
        ]
        while stack and len(output) < maximum_paths:
            step_id, path = stack.pop()
            if step_id in leaf_set or not children.get(step_id):
                steps = [by_id[item] for item in path if item in by_id]
                output.append(
                    {
                        "root_step_id": path[0],
                        "leaf_step_id": path[-1],
                        "step_ids": list(path),
                        "effects": [item.effect.value for item in steps],
                        "stages": [item.stage for item in steps],
                        "path_digest": digest(
                            [
                                {
                                    "step_id": item.step_id,
                                    "effect": item.effect.value,
                                    "semantic_digest": item.semantic_digest,
                                }
                                for item in steps
                            ]
                        ),
                    }
                )
                continue
            for child in reversed(sorted(children.get(step_id, ()))):
                if child in path:
                    continue
                stack.append((child, (*path, child)))
        return output

    def _effect_path_coverage(
        self,
        paths: Iterable[Mapping[str, Any]],
        by_id: Mapping[str, EffectiveStep],
    ) -> dict[str, list[str]]:
        coverage: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            path_digest = str(path.get("path_digest") or "")
            for step_id in path.get("step_ids") or ():
                step = by_id.get(str(step_id))
                if step is None:
                    continue
                coverage[step.effect.value].add(path_digest)
        return {
            effect: sorted(values)
            for effect, values in sorted(coverage.items())
        }


def causal_manifest_projection(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    paths = receipt.get("semantic_paths")
    selected = paths if isinstance(paths, list) else []
    return {
        "schema": "zyra.scenario-causal-manifest-projection/v1",
        "valid": receipt.get("valid") is True,
        "receipt_digest": str(receipt.get("receipt_digest") or ""),
        "root_count": len(receipt.get("roots") or []),
        "leaf_count": len(receipt.get("leaves") or []),
        "step_count": int(receipt.get("step_count") or 0),
        "edge_count": int(receipt.get("edge_count") or 0),
        "path_count": len(selected),
        "path_digests": [
            str(item.get("path_digest") or "")
            for item in selected
            if isinstance(item, Mapping)
        ],
        "effect_path_coverage": dict(
            receipt.get("effect_path_coverage") or {}
        ),
    }
