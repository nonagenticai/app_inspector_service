"""The vendored ``InspectionResultResponse`` contract artifact must stay fresh.

This is the producer half of the app_review dead-signal guard: the orchestrator
vendors a copy of ``contracts/inspection_result.schema.json`` and walks it. That
copy is only as truthful as this file is fresh, so this test fails CI the moment
the model changes without a regenerate. Run
``uv run python scripts/regen_contract_artifact.py`` to refresh.
"""

from __future__ import annotations

from scripts.regen_contract_artifact import ARTIFACT, render


def test_contract_artifact_exists() -> None:
    assert ARTIFACT.is_file(), (
        f"{ARTIFACT} is missing. Run: uv run python scripts/regen_contract_artifact.py"
    )


def test_contract_artifact_is_fresh() -> None:
    committed = ARTIFACT.read_text(encoding="utf-8")
    assert committed == render(), (
        "contracts/inspection_result.schema.json is STALE — the "
        "InspectionResultResponse schema changed but the vendored artifact was "
        "not regenerated. Run: uv run python scripts/regen_contract_artifact.py "
        "and re-vendor the copy into "
        "w2v_orchestrator/tests/contracts/inspection_result.schema.json."
    )
