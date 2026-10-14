"""Export published contracts: JSON Schemas for inputs/outputs and the OpenAPI document.

python -m lab_result_integrity.contracts            # rewrite schemas/ and docs/api/
python -m lab_result_integrity.contracts --check    # exit 1 if committed files are stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from lab_result_integrity.agents.evidence import EvidencePacket
from lab_result_integrity.api.app import create_app
from lab_result_integrity.api.schemas import DecisionRequest, ProposalRequest, ScreenRequest
from lab_result_integrity.domain.catalogue import AnalyteCatalogue
from lab_result_integrity.domain.models import LabResult, ScreeningOutcome
from lab_result_integrity.domain.review import ReviewCase

MODELS: dict[str, type[BaseModel]] = {
    "lab-result": LabResult,
    "screen.request": ScreenRequest,
    "screening-outcome": ScreeningOutcome,
    "evidence-packet": EvidencePacket,
    "review-case": ReviewCase,
    "decision.request": DecisionRequest,
    "proposal.request": ProposalRequest,
    "analyte-catalogue": AnalyteCatalogue,
}


async def _no_container() -> Any:  # pragma: no cover - never started
    raise RuntimeError("contract export does not start the application")


def documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        f"schemas/{name}.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://github.com/kogunlowo123/lab-result-integrity/schemas/{name}.schema.json",
            **model.model_json_schema(mode="validation"),
        }
        for name, model in MODELS.items()
    }
    app: FastAPI = create_app(_no_container)
    docs["docs/api/openapi.json"] = app.openapi()
    return docs


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = root or Path.cwd()
    stale: list[str] = []
    for relative, document in documents().items():
        path = base / relative
        content = render(document)
        if "--check" in args:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(
            "stale contracts (run python -m lab_result_integrity.contracts): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
