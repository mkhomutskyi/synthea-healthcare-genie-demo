"""Artifact integration tier: needs the verified archive in .cache (pytest -m artifact)."""

from __future__ import annotations

import pytest

from synthea_genie_demo.artifact import (
    read_archive,
    sensitive_literals,
    verify_against_lock,
    verify_staged_files,
)
from synthea_genie_demo.checks import benchmarks, contract, lock
from synthea_genie_demo.jsonio import load_json
from synthea_genie_demo.paths import (
    ARCHIVE_FILENAME,
    DEFAULT_ARCHIVE_DIR,
    DEFAULT_STAGING_DIR,
    REFERENCE_DIGEST_PATH,
    REPO_ROOT,
)
from synthea_genie_demo.payers import PayerMapping
from synthea_genie_demo.reference import (
    FREEZE_COMMAND,
    check_reference_against_digest,
    compute_all,
    load_staged_rows,
)
from synthea_genie_demo.scan import Scanner

pytestmark = pytest.mark.artifact
ARCHIVE = DEFAULT_ARCHIVE_DIR / ARCHIVE_FILENAME
SHA = lock()["artifact"]["sha256"]


@pytest.fixture(scope="module")
def contents():  # type: ignore[no-untyped-def]
    if not ARCHIVE.exists():
        pytest.skip("run scripts/download_and_verify.py first")
    return read_archive(ARCHIVE)


def test_cached_archive_matches_lock(contents) -> None:  # type: ignore[no-untyped-def]
    c = contract()
    assert verify_against_lock(contents, lock(), c, PayerMapping.from_contract(c)) == []


def test_real_payer_names_map_exactly(contents) -> None:  # type: ignore[no-untyped-def]
    mapping = PayerMapping.from_contract(contract())
    mapped = mapping.map_names(r["NAME"] for r in contents.members["payers.csv"].rows)
    assert sorted(set(mapped.values())) == contract()["payer_categories"]


def test_public_outputs_free_of_exact_local_values(contents) -> None:  # type: ignore[no-untyped-def]
    c = contract()
    literals = sensitive_literals(ARCHIVE, PayerMapping.from_contract(c), SHA)
    assert len(literals["private_payer_brand"]) == 6
    findings = Scanner.from_contract(c, exact_literals=literals).scan_tree(REPO_ROOT)
    assert [str(f) for f in findings] == []


def test_hashed_brand_scan_catches_real_names(contents) -> None:  # type: ignore[no-untyped-def]
    c = contract()
    scanner = Scanner.from_contract(c)
    for name in sensitive_literals(ARCHIVE, PayerMapping.from_contract(c), SHA)[
        "private_payer_brand"
    ]:
        assert any(f.rule == "private_payer_brand" for f in scanner.scan_text(f"x {name} y"))


def test_reference_totals_agree_with_lock() -> None:
    if not (DEFAULT_STAGING_DIR / "encounters.csv").exists():
        pytest.skip("run scripts/download_and_verify.py --stage first")
    observed = lock()["observed"]["encounters"]
    results = compute_all(load_staged_rows(DEFAULT_STAGING_DIR))
    by_year: dict[int, int] = {}
    for year, _cls, count in results["i1_annual_encounters_by_class"]["rows"]:
        by_year[year] = by_year.get(year, 0) + count
    assert by_year == {
        2024: observed["rows_by_year"]["2024"],
        2025: observed["rows_by_year"]["2025"],
    }
    quarterly = results["i2_quarterly_encounters_by_class"]["rows"]
    assert sum(r[2] for r in quarterly) == sum(by_year.values())
    i5 = results["i5_patients_and_encounters_per_patient_by_payer_category_2025"]["rows"]
    assert len(i5) == len(contract()["payer_categories"])


def test_verified_staging_reproduces_frozen_reference_digest() -> None:
    if not (DEFAULT_STAGING_DIR / "manifest.json").exists():
        pytest.skip("run scripts/download_and_verify.py --stage first")
    verify_staged_files(DEFAULT_STAGING_DIR, lock(), contract())
    digest = load_json(REFERENCE_DIGEST_PATH)
    assert digest["artifact_sha256"] == SHA
    results = compute_all(load_staged_rows(DEFAULT_STAGING_DIR))
    problems = check_reference_against_digest(
        results,
        digest,
        artifact_sha256=SHA,
        intents=benchmarks()["intents"],
        generated_by=FREEZE_COMMAND,
    )
    assert problems == []
