from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_source_language_custody",
    ROOT / "scripts" / "verify_source_language_custody.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

LEDGER_SPEC = importlib.util.spec_from_file_location(
    "sync_worker_pool_foundation_source_ledger",
    ROOT / "scripts" / "sync_worker_pool_foundation_source_ledger.py",
)
assert LEDGER_SPEC is not None and LEDGER_SPEC.loader is not None
LEDGER_MODULE = importlib.util.module_from_spec(LEDGER_SPEC)
LEDGER_SPEC.loader.exec_module(LEDGER_MODULE)


def _document(path: str, *, minimum: int = 1) -> dict[str, object]:
    return {
        "language_custody": [
            {
                "source_repo": "oh-my-pi",
                "source_role": "supplementary_implementation",
                "migration_mode": "cropped_same_language_migration",
                "expected_production_languages": ["typescript"],
                "minimum_added_lines_by_language": {"typescript": minimum},
                "production_paths": [path],
            }
        ]
    }


def test_required_original_language_passes_with_real_added_production_lines(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "_validate_production_path", lambda path: Path(path))
    monkeypatch.setattr(MODULE, "_numstat_added", lambda path, base, target: 72)

    report = MODULE.verify(
        _document("packages/runtime/worker-control.ts"),
        base="baseline",
        target="target",
    )

    assert report["ok"] is True
    assert report["roles"][0]["actual_production_languages"] == ["typescript"]
    assert report["roles"][0]["added_production_lines"] == {"typescript": 72}


def test_language_collapse_and_zero_additions_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "_validate_production_path", lambda path: Path(path))
    monkeypatch.setattr(MODULE, "_numstat_added", lambda path, base, target: 0)

    collapsed = MODULE.verify(
        _document("packages/scheduler/worker_control.py"),
        base="baseline",
        target="target",
    )
    missing = MODULE.verify(
        _document("packages/runtime/worker-control.ts"),
        base="baseline",
        target="target",
    )

    assert collapsed["ok"] is False
    assert any("unexpected languages: python" in item for item in collapsed["violations"])
    assert any("typescript added production lines 0" in item for item in collapsed["violations"])
    assert missing["ok"] is False
    assert any("typescript added production lines 0" in item for item in missing["violations"])


def test_cross_language_exception_requires_prior_decision_id(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "_validate_production_path", lambda path: Path(path))
    monkeypatch.setattr(MODULE, "_numstat_added", lambda path, base, target: 1)
    document = _document("packages/runtime/worker-control.ts")
    document["language_custody"][0]["cross_language_exception"] = True

    report = MODULE.verify(document, base="baseline", target="target")

    assert report["ok"] is False
    assert any("lacks cross_language_decision_id" in item for item in report["violations"])


def test_same_language_ledger_decision_rejects_inverted_target_language() -> None:
    bad_decision = {
        "source_repo": "oh-my-pi",
        "source_language": "typescript",
        "target_language": "python",
        "migration_mode": "cropped_same_language_worker_control",
    }

    try:
        LEDGER_MODULE._validate_decisions((bad_decision,))
    except ValueError as exc:
        assert "typescript->python" in str(exc)
    else:
        raise AssertionError("inverted same-language custody must fail closed")


def test_independent_policy_rejects_self_consistent_inverted_ledger(monkeypatch) -> None:
    document = _document("packages/runtime/worker-control.ts")
    document["slice_id"] = "M1-SAMPLE"
    document["source_decisions"] = [
        {
            "source_repo": "oh-my-pi",
            "role": "supplementary_implementation",
            "source_language": "typescript",
            "target_language": "typescript",
            "migration_mode": "cropped_same_language_migration",
        }
    ]
    document["ledger_custody"] = {
        "path": "ledger.json",
        "owner_unit": "M1-SAMPLE",
        "expected_entry_count": 1,
    }
    inverted = {
        "entries": [
            {
                "ledger_id": "ledger_sample",
                "owner_unit": "M1-SAMPLE",
                "source_repo": "oh-my-pi",
                "metadata": {
                    "source_role": "supplementary_implementation",
                    "source_language": "typescript",
                    "target_language": "python",
                    "migration_mode": "cropped_same_language_migration",
                },
            }
        ]
    }
    monkeypatch.setattr(MODULE, "_validate_production_path", lambda path: Path(path))
    monkeypatch.setattr(MODULE, "_numstat_added", lambda path, base, target: 1)
    monkeypatch.setattr(MODULE, "_load_ledger", lambda path, target: inverted)

    report = MODULE.verify(document, base="baseline", target="target")

    assert report["ok"] is False
    assert any("ledger target_language disagrees" in item for item in report["violations"])
    assert any("same-language ledger is inverted" in item for item in report["violations"])
