"""Published contracts match the code."""

from __future__ import annotations

from pathlib import Path

from lab_result_integrity import contracts
from tests.conftest import ROOT


def test_committed_contracts_are_current() -> None:
    assert contracts.main(["--check"], root=ROOT) == 0


def test_stale_contracts_are_detected_and_rewritten(tmp_path: Path) -> None:
    assert contracts.main(["--check"], root=tmp_path) == 1
    assert contracts.main([], root=tmp_path) == 0
    assert contracts.main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "schemas" / "lab-result.schema.json").exists()
    assert (tmp_path / "docs" / "api" / "openapi.json").exists()
