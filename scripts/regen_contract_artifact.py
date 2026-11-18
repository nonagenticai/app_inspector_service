#!/usr/bin/env python
"""Regenerate the vendored contract artifact for ``InspectionResultResponse``.

The orchestrator's ``app_review`` path consumes ``GET
/api/inspect/{job_id}/result``, whose body is
:class:`~src.api.schemas.InspectionResultResponse`. That consumer vendors a COPY
of the JSON Schema emitted here and walks it (dotted-leaf set algebra) to detect
contract drift in BOTH directions — a consumer inventing a field we never send,
and us growing a field no consumer reads (the silently-inert bug this whole
mechanism exists to catch).

This script is the SINGLE SOURCE of that artifact. Refresh it whenever the model
changes; never hand-edit ``contracts/inspection_result.schema.json``::

    uv run python scripts/regen_contract_artifact.py

``tests/unit/test_contract_artifact_fresh.py`` fails CI when the committed file
is stale, so a model change that forgets to regenerate cannot merge. After
regenerating, re-vendor the copy into
``w2v_orchestrator/tests/contracts/inspection_result.schema.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.api.schemas import InspectionResultResponse

ARTIFACT: Path = (
    Path(__file__).resolve().parent.parent / "contracts" / "inspection_result.schema.json"
)


def render() -> str:
    """Return the canonical, deterministic JSON Schema text for the artifact."""
    schema = InspectionResultResponse.model_json_schema()
    return json.dumps(schema, sort_keys=True, indent=2) + "\n"


def main() -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(render(), encoding="utf-8")
    print(f"wrote {ARTIFACT}")


if __name__ == "__main__":
    main()
