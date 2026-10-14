"""Shared fixtures. Fixture data is invented: fictional private payer names, non-UUID keys."""

from __future__ import annotations

import copy
import csv
import importlib.util
import io
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from synthea_genie_demo.jsonio import load_json, sha256_text
from synthea_genie_demo.paths import REPO_ROOT, SCHEMA_CONTRACT_PATH
from synthea_genie_demo.scan import normalize_term

FICTIONAL_PRIVATE_NAMES = (
    "Zephyr Mutual Fixture",
    "Quillon Health Fixture",
    "Orbit Assurance Fixture",
    "Larkspur Benefit Fixture",
    "Tundra Cover Fixture",
    "Nimbus Plan Fixture",
)
PUBLIC_NAMES = ("Dual Eligible", "Medicaid", "Medicare", "NO_INSURANCE")
ENCOUNTER_HEADER = [
    "Id",
    "START",
    "STOP",
    "PATIENT",
    "ORGANIZATION",
    "PROVIDER",
    "PAYER",
    "ENCOUNTERCLASS",
    "CODE",
    "DESCRIPTION",
    "BASE_ENCOUNTER_COST",
    "TOTAL_CLAIM_COST",
    "PAYER_COVERAGE",
    "REASONCODE",
    "REASONDESCRIPTION",
]
PAYER_HEADER = ["Id", "NAME", "OWNERSHIP", "ADDRESS"]
FIXTURE_FACILITY = "Fixturetown Community Care Site"
FIXTURE_PROVIDER = "Dr Fixture Person"
FIXTURE_DESCRIPTION = "Fixture clinical description text"


def payer_rows() -> list[dict[str, str]]:
    names = [*PUBLIC_NAMES, *FICTIONAL_PRIVATE_NAMES]
    return [
        {"Id": f"fixture-payer-{i:02d}", "NAME": n, "OWNERSHIP": "x", "ADDRESS": "1 Fixture Way"}
        for i, n in enumerate(names, start=1)
    ]


def encounter_rows() -> list[dict[str, str]]:
    payers = [r["Id"] for r in payer_rows()]
    classes = ["ambulatory", "emergency", "wellness", "inpatient"]
    rows = []
    for i in range(40):
        year = 2023 + (i % 3)
        month = 1 + (i * 5) % 12
        rows.append(
            {
                "Id": f"fixture-encounter-{i:03d}",
                "START": f"{year}-{month:02d}-{1 + i % 27:02d}T{i % 24:02d}:15:00Z",
                "STOP": f"{year}-{month:02d}-{1 + i % 27:02d}T23:59:00Z",
                "PATIENT": f"fixture-patient-{i % 7:03d}",
                "ORGANIZATION": "fixture-org-001",
                "PROVIDER": "fixture-provider-001",
                "PAYER": payers[i % len(payers)],
                "ENCOUNTERCLASS": classes[i % len(classes)],
                "CODE": "000000",
                "DESCRIPTION": FIXTURE_DESCRIPTION,
                "BASE_ENCOUNTER_COST": "10.00",
                "TOTAL_CLAIM_COST": f"{100 + i * 3}.{i % 100:02d}",
                "PAYER_COVERAGE": f"{(100 + i * 3) // 2}.25" if i % 4 else "0.00",
                "REASONCODE": "",
                "REASONDESCRIPTION": "",
            }
        )
    return rows


def csv_bytes(header: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def build_archive(
    path: Path,
    *,
    encounters: list[dict[str, str]] | None = None,
    payers: list[dict[str, str]] | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    members = {
        "encounters.csv": csv_bytes(ENCOUNTER_HEADER, encounters or encounter_rows()),
        "payers.csv": csv_bytes(PAYER_HEADER, payers or payer_rows()),
        "organizations.csv": csv_bytes(
            ["Id", "NAME"], [{"Id": "fixture-org-001", "NAME": FIXTURE_FACILITY}]
        ),
        "providers.csv": csv_bytes(
            ["Id", "NAME"], [{"Id": "fixture-provider-001", "NAME": FIXTURE_PROVIDER}]
        ),
        **(extra_members or {}),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 18, 12, 0, 0))
            archive.writestr(info, data)
    return path


def fixture_contract() -> dict[str, Any]:
    contract = copy.deepcopy(load_json(SCHEMA_CONTRACT_PATH))
    contract["payer_mapping"]["private_name_sha256"] = sorted(
        sha256_text(n) for n in FICTIONAL_PRIVATE_NAMES
    )
    contract["public_output_scan"]["forbidden_term_sha256"] = sorted(
        sha256_text(normalize_term(n)) for n in FICTIONAL_PRIVATE_NAMES
    )
    return contract


@pytest.fixture
def contract() -> dict[str, Any]:
    return fixture_contract()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return build_archive(tmp_path / "fixture.zip")


@pytest.fixture
def load_script() -> Callable[[str], ModuleType]:
    def _load(name: str) -> ModuleType:
        path = REPO_ROOT / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"script_{name}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    return _load


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Fail the test if anything tries to spawn a process (for example the Databricks CLI)."""
    calls: list[Any] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError(f"subprocess invoked in a non-mutating mode: {args!r}")

    import subprocess

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return calls


PROBE_OUTCOME = {"refuse": "refused", "safe_redirect": "redirected", "clarify": "clarified"}


def completed_review(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Every benchmark correct, every probe doing exactly its expected behavior."""
    done = []
    for r in rows:
        bench = r["item_type"] in ("benchmark", "holdout_benchmark")
        outcome = "correct" if bench else PROBE_OUTCOME[r["expected_behavior"]]
        done.append(
            {
                **r,
                "chat_mode_outcome": outcome,
                "result_matches_canonical": "yes" if bench else "n/a",
                "refused": "yes" if outcome == "refused" else "no",
                "safely_redirected": "yes" if outcome == "redirected" else "no",
                "sql_generated": "yes" if bench else "no",
                "forbidden_field_exposed": "no",
                "unsupported_medical_language": "no",
                "rubric_pass": "yes",
                "reviewer_notes": "",
            }
        )
    return done


def export_for(reference: dict[str, Any], bench: dict[str, Any], runs: int = 3) -> dict[str, Any]:
    """A normalized benchmark export whose every run reproduces ``reference`` exactly."""
    from synthea_genie_demo.evidence import EXPORT_FORMAT, EXPORT_FORMAT_VERSION

    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "suite_version": bench["suite_version"],
        "runs": [
            {
                "run": n,
                "items": [
                    {
                        "key": q["key"],
                        "question": q["question"],
                        "status": "answered",
                        "result": copy.deepcopy(reference[q["intent_id"]]),
                    }
                    for q in bench["questions"]
                ],
            }
            for n in range(1, runs + 1)
        ],
    }


def tracked_snapshot() -> dict[str, str]:
    """Hash of every repository file outside ignored local folders."""
    skip = {".git", ".venv", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    snapshot = {}
    for path in sorted(REPO_ROOT.rglob("*")):
        if path.is_file() and not any(part in skip for part in path.relative_to(REPO_ROOT).parts):
            snapshot[path.relative_to(REPO_ROOT).as_posix()] = sha256_text(
                path.read_bytes().decode("utf-8", errors="replace")
            )
    return snapshot
