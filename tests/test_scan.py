"""Leak scanner tests. Sensitive-looking strings are assembled at runtime so this file
itself stays clean under the repository scan."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FICTIONAL_PRIVATE_NAMES, fixture_contract

from synthea_genie_demo.checks import check_public_outputs, contract
from synthea_genie_demo.paths import REPO_ROOT
from synthea_genie_demo.scan import Scanner, normalize_term

AT = "@"
HEX = "01234567" + "89abcdef"
UUID_LIKE = "1b2c3d4e-0000-1111-2222-" + "333344445555"
WH = '{"warehouse_id' + '": "'
SP = '{"space_id' + '": "'
WORKSPACE_DIGITS = "90817263" + "54637281"


def fixture_scanner() -> Scanner:
    return Scanner.from_contract(
        fixture_contract(),
        exact_literals={
            "patient_identifier": ["fixture-patient-123"],
            "facility_name": ["Fixtureville Care Site"],
        },
    )


def rules(text: str) -> set[str]:
    return {f.rule for f in fixture_scanner().scan_text(text)}


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"Covered by {FICTIONAL_PRIVATE_NAMES[0]}.", "private_payer_brand"),
        (f"covered by {FICTIONAL_PRIVATE_NAMES[1].upper()}!", "private_payer_brand"),
        ("Seen at " + "Springfield " + "General " + "Hospital" + " today", "facility_name"),
        ("Visit " + "Lakeside " + "Medical " + "Center" + " records", "facility_name"),
        ("Contact jane.doe" + AT + "example.org", "email"),
        (
            "https://" + "adb-" + "12345678" + "90123456.7.azuredatabricks" + ".net/",
            "workspace_url",
        ),
        ("https://" + "my-team.cloud.databricks" + ".com/sql", "workspace_url"),
        ("open ?o=" + "1234567890123", "workspace_id"),
        (WH + HEX + '"}', "workspace_id"),
        (SP + 'abc123def456"}', "workspace_id"),
        ("id " + HEX + " here", "workspace_id"),
        ("id " + "12345678" + "90123456" + " here", "workspace_id"),
        ("v1." + HEX + " here", "workspace_id"),
        ("rate 0." + WORKSPACE_DIGITS, "workspace_id"),
        ("build v0." + WORKSPACE_DIGITS, "workspace_id"),
        ('rows: [["2025-Q1", "0.' + "62345678" + '90123456"]]', "workspace_id"),
        ("see /" + "Users/jane.doe/Documents/repo", "local_path"),
        ("see file:///" + "Users/jane/repo/data.csv", "local_path"),
        ("see file:/" + "home/runner/work/x", "local_path"),
        ("at /" + "home/runner/work/repo", "local_path"),
        ("C:" + "\\Users\\jane\\repo", "local_path"),
        ("parent /" + "Workspace/Users/jane/folder", "workspace_path"),
        ("parent /" + "Workspace/Shared/demo", "workspace_path"),
        ("copy to dbfs:/" + "Volumes/main/ops/raw/x.csv", "volume_path"),
        ("read /" + "Volumes/main/ops/raw", "volume_path"),
        ("token " + "dapi" + HEX * 2, "token_like"),
        ("ghp_" + "A" * 36, "token_like"),
        ("Authorization: Bearer " + "x" * 30, "token_like"),
        ("-----BEGIN " + "RSA PRIVATE KEY-----", "token_like"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "token_like"),
        ("patient " + UUID_LIKE, "patient_identifier"),
        ("key fixturepatient123 without hyphens", "patient_identifier"),
        ("key fixture-patient-123 leaked", "patient_identifier"),
        ("Fixtureville Care Site was mentioned", "facility_name"),
    ],
)
def test_positive_findings(text: str, rule: str) -> None:
    assert rule in rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "See https://docs.databricks.com/aws/en/genie/ for details.",
        "Digest d61417b551e5b0997c33851b339c157421751f0ea68c18ea686ceb1850907c35",
        "ID " + HEX * 2 + " is 32 hex characters",
        "Do not compare with a real hospital or medical center.",
        "Payer categories: Medicare, Medicaid, Dual Eligible, No Insurance, Private Insurance.",
        WH + '<warehouse-id>"}',
        SP + '{{space_id}}"}',
        "Use --warehouse-id <warehouse-id> and --profile <profile>.",
        "fixture-patient-12 is a different key",
        "Upload to a `dbfs:/Volumes/...` path under /Volumes/{{catalog}}/{{schema}}.",
        "Use --parent-path /Workspace/<your folder> or /Users/<you>.",
        "Rates such as 0.6234 and amounts such as 1234.56 are plain numbers.",
    ],
)
def test_negative_cases(text: str) -> None:
    assert rules(text) == set()


def test_repository_path_is_an_exact_literal() -> None:
    scanner = Scanner.from_contract(fixture_contract())
    for text in (f"from {REPO_ROOT}/data", f"open {REPO_ROOT.as_uri()}/x"):
        assert "repository_path" in {f.rule for f in scanner.scan_text(text)}


def table(*cells: object) -> dict[str, object]:
    return {"columns": ["Quarter", "Rate"], "rows": [["2025-Q1", c] for c in cells]}


def test_structured_scan_masks_only_numeric_result_columns() -> None:
    scanner = fixture_scanner()
    unrounded = "0." + "62345678" + "90123456"
    clean = {"runs": [{"result": table(unrounded, "0.5", float("0.12345678" + "90123456"), None)}]}
    assert scanner.scan_json(clean) == []
    assert "workspace_id" in {f.rule for f in scanner.scan_text(str(clean))}
    for leaky in (
        table(unrounded, "v0." + WORKSPACE_DIGITS),
        table("1" + WORKSPACE_DIGITS[1:], "0.5"),
        table(HEX),
        {"columns": ["Rate"], "rows": [["0." + WORKSPACE_DIGITS]], "extra": HEX},
        {"note": "0." + WORKSPACE_DIGITS, "result": table("0.5")},
    ):
        assert "workspace_id" in {f.rule for f in scanner.scan_json(leaky)}, leaky


def test_structured_scan_still_finds_exact_values_inside_numbers() -> None:
    scanner = Scanner.from_contract(
        fixture_contract(), exact_literals={"workspace_id": [WORKSPACE_DIGITS]}
    )
    findings = scanner.scan_json({"result": table("0." + WORKSPACE_DIGITS)})
    assert {f.rule for f in findings} == {"workspace_id"}
    assert all(WORKSPACE_DIGITS not in str(f) for f in findings)


def test_brand_match_is_by_hash_and_normalized() -> None:
    name = FICTIONAL_PRIVATE_NAMES[2]
    scanner = Scanner([__import__("hashlib").sha256(normalize_term(name).encode()).hexdigest()])
    assert scanner.scan_text(f"- {name.lower()} -")
    assert not scanner.scan_text(name.split()[0])


def test_findings_do_not_echo_secret_values() -> None:
    secret = "dapi" + HEX * 2
    for finding in fixture_scanner().scan_text(f"value {secret}"):
        assert secret not in str(finding)


def test_forbidden_file_types(tmp_path: Path) -> None:
    (tmp_path / "data.zip").write_bytes(b"PK")
    (tmp_path / "notes.md").write_text("clean", encoding="utf-8")
    findings = Scanner([]).scan_tree(tmp_path)
    assert [f.rule for f in findings] == ["forbidden_file_type"]


def test_cache_and_venv_are_excluded(tmp_path: Path) -> None:
    for folder in (".cache", ".venv", ".git"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "x.txt").write_text("mail" + AT + "example.org", encoding="utf-8")
    assert Scanner([]).scan_tree(tmp_path) == []


def test_repository_public_outputs_are_clean() -> None:
    assert check_public_outputs(REPO_ROOT, Scanner.from_contract(contract())) == []
