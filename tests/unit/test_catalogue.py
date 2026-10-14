"""Catalogue governance and validation: thresholds are configuration with citations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from lab_result_integrity.domain.catalogue import AnalyteCatalogue, Operator
from lab_result_integrity.domain.errors import CatalogueError
from tests.conftest import ROOT

STARTER = (ROOT / "configs" / "analytes.yaml").read_text(encoding="utf-8")


def document() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(STARTER)
    return loaded


def write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_starter_catalogue_refused_unless_allowed(tmp_path: Path) -> None:
    path = write(tmp_path, document())
    with pytest.raises(CatalogueError, match="laboratory-director approved"):
        AnalyteCatalogue.load(path, allow_starter=False)
    assert AnalyteCatalogue.load(path, allow_starter=True).governance.status == "starter"


def test_approved_catalogue_loads_without_override(tmp_path: Path) -> None:
    doc = document()
    doc["governance"].update(
        status="approved", approved_by="Laboratory Director", approved_on="2026-09-30"
    )
    catalogue = AnalyteCatalogue.load(write(tmp_path, doc), allow_starter=False)
    summary = catalogue.summary()
    assert summary["approved_on"] == "2026-09-30"
    assert str(summary["digest"]).startswith("sha256:")


def test_approval_needs_approver(tmp_path: Path) -> None:
    doc = document()
    doc["governance"]["status"] = "approved"
    with pytest.raises(CatalogueError, match="approved_by"):
        AnalyteCatalogue.load(write(tmp_path, doc), allow_starter=False)


def test_retired_catalogue_is_refused(tmp_path: Path) -> None:
    doc = document()
    doc["governance"]["status"] = "retired"
    with pytest.raises(CatalogueError, match="retired"):
        AnalyteCatalogue.load(write(tmp_path, doc), allow_starter=True)


def test_every_threshold_needs_a_known_citation() -> None:
    doc = document()
    doc["analytes"][0]["critical"]["cite"] = ["made-up-source"]
    with pytest.raises(CatalogueError, match="made-up-source"):
        AnalyteCatalogue.from_yaml(yaml.safe_dump(doc))
    doc["analytes"][0]["critical"]["cite"] = []
    with pytest.raises(CatalogueError):
        AnalyteCatalogue.from_yaml(yaml.safe_dump(doc))


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("analytes", 0, "reference_intervals", 0, "low"), 9.0, "low must be below high"),
        (("analytes", 0, "critical", "low"), 9.0, "critical low"),
        (("analytes", 0, "plausible", "min"), 99.0, "plausible min"),
        (("analytes", 0, "pbrtqc", "target"), 9.0, "target must lie"),
        (("analytes", 0, "pbrtqc", "truncation_low"), 9.0, "truncation_low"),
        (("analytes", 0, "delta", "mode"), "all", "mode 'all'"),
        (("analytes", 1, "pbrtqc", "ewma_lambda"), None, "ewma_lambda"),
        (("analytes", 0, "reference_intervals", 1, "age_max_days"), 10, "age_max_days"),
    ],
)
def test_inconsistent_thresholds_are_rejected(
    path: tuple[Any, ...], value: Any, message: str
) -> None:
    doc = document()
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(CatalogueError, match=message):
        AnalyteCatalogue.from_yaml(yaml.safe_dump(doc))


def _rejected(doc: dict[str, Any], message: str) -> None:
    with pytest.raises(CatalogueError, match=message):
        AnalyteCatalogue.from_yaml(yaml.safe_dump(doc))


def test_structural_errors_are_rejected() -> None:
    doc = document()
    doc["analytes"].append(doc["analytes"][0])
    _rejected(doc, "duplicate analyte")

    doc = document()
    doc["combination_rules"].append(doc["combination_rules"][0])
    _rejected(doc, "duplicate combination")

    doc = document()
    doc["combination_rules"][0]["conditions"][0]["analyte"] = "99999-9"
    _rejected(doc, "unknown analytes")

    doc = document()
    doc["combination_rules"][0]["conditions"][1] = {
        "analyte": "2823-3",
        "index": "hemolysis",
        "op": "gt",
        "value": 1,
    }
    _rejected(doc, "exactly one")

    doc = document()
    del doc["review_policy"]["sla_minutes"]["stat"]
    _rejected(doc, "missing priorities")

    doc = document()
    doc["review_policy"]["sla_minutes"]["stat"] = 0
    _rejected(doc, "positive")

    doc = document()
    doc["analytes"][0]["critical"] = {"cite": ["lab-policy"]}
    _rejected(doc, "low and/or high")

    doc = document()
    doc["analytes"][0]["reference_intervals"][0] = {"cite": ["lab-policy"]}
    _rejected(doc, "low and/or high")

    doc = document()
    doc["analytes"][0]["delta"] = {"window_hours": 4, "cite": ["lab-policy"]}
    _rejected(doc, "absolute and/or percent")

    with pytest.raises(CatalogueError):
        AnalyteCatalogue.from_yaml("governance: [unclosed")


def test_operators() -> None:
    assert Operator.GT.holds(2, 1)
    assert not Operator.GT.holds(1, 1)
    assert Operator.GTE.holds(1, 1)
    assert Operator.LT.holds(1, 2)
    assert not Operator.LT.holds(2, 2)
    assert Operator.LTE.holds(2, 2)


def test_lookups_and_condition_description() -> None:
    catalogue = AnalyteCatalogue.from_yaml(STARTER)
    assert catalogue.analyte("0000-0") is None
    rule = catalogue.rules_for("2823-3")[0]
    assert "hemolysis index gte 50" in [c.describe() for c in rule.conditions]
    assert all(c in catalogue.sources for a in catalogue.analytes for c in a.citations())
