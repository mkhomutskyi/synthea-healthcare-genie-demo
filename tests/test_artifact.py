from __future__ import annotations

import csv
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FICTIONAL_PRIVATE_NAMES,
    FIXTURE_DESCRIPTION,
    build_archive,
    encounter_rows,
    payer_rows,
)

from synthea_genie_demo.artifact import (
    ArtifactDriftError,
    ArtifactError,
    HashMismatchError,
    build_lock,
    download_verified,
    profile,
    read_archive,
    sensitive_literals,
    stage,
    urllib_fetch,
    verify_against_lock,
    verify_file_hash,
)
from synthea_genie_demo.jsonio import sha256_file
from synthea_genie_demo.paths import CACHE_DIR, REPO_ROOT, ensure_local_output_dir
from synthea_genie_demo.payers import PayerMapping, PayerMappingError


def file_fetcher(source: Path) -> Any:
    def fetch(url: str, dest: Path, max_bytes: int) -> None:
        dest.write_bytes(source.read_bytes())

    return fetch


def test_download_keeps_file_only_on_hash_match(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out" / "a.zip"
    download_verified(
        "https://example.invalid/a.zip", sha256_file(archive), dest, fetcher=file_fetcher(archive)
    )
    assert dest.read_bytes() == archive.read_bytes()
    assert not list(dest.parent.glob("*.part"))


def test_download_hash_mismatch_fails_closed_and_cleans_up(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out" / "a.zip"
    with pytest.raises(HashMismatchError) as info:
        download_verified(
            "https://example.invalid/a.zip", "0" * 64, dest, fetcher=file_fetcher(archive)
        )
    assert "data-and-metrics.md#updating-the-source" in str(info.value)
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_download_mismatch_does_not_replace_existing_file(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "a.zip"
    dest.write_bytes(b"previous verified content")
    with pytest.raises(HashMismatchError):
        download_verified(
            "https://example.invalid/a.zip", "0" * 64, dest, fetcher=file_fetcher(archive)
        )
    assert dest.read_bytes() == b"previous verified content"


def test_download_cleans_temp_file_when_fetch_fails(tmp_path: Path) -> None:
    def broken(url: str, dest: Path, max_bytes: int) -> None:
        dest.write_bytes(b"partial")
        raise ArtifactError("network down")

    with pytest.raises(ArtifactError):
        download_verified(
            "https://example.invalid/a.zip", "0" * 64, tmp_path / "a.zip", fetcher=broken
        )
    assert list(tmp_path.iterdir()) == []


def test_file_url_fetch_and_size_limit(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "copy.zip"
    urllib_fetch(archive.as_uri(), dest, 10_000_000)
    assert dest.read_bytes() == archive.read_bytes()
    with pytest.raises(ArtifactError):
        urllib_fetch(archive.as_uri(), tmp_path / "small.zip", 10)


def test_refuses_plain_http() -> None:
    with pytest.raises(ArtifactError):
        urllib_fetch("http://example.invalid/a.zip", Path("unused"), 10)


def test_verify_file_hash(archive: Path) -> None:
    assert verify_file_hash(archive, sha256_file(archive)) == sha256_file(archive)
    with pytest.raises(HashMismatchError):
        verify_file_hash(archive, "f" * 64)


def test_local_output_dir_rule(tmp_path: Path) -> None:
    assert ensure_local_output_dir(tmp_path) == tmp_path.resolve()
    assert ensure_local_output_dir(CACHE_DIR / "x") == (CACHE_DIR / "x").resolve()
    for bad in (REPO_ROOT / "data", REPO_ROOT, REPO_ROOT / "evidence" / "published-run"):
        with pytest.raises(ValueError):
            ensure_local_output_dir(bad)


def test_profile_and_lock_round_trip(archive: Path, contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    contents = read_archive(archive)
    observed = profile(contents, contract, mapping)
    assert observed["encounters"]["row_count"] == 40
    assert observed["encounters"]["distinct_patients"] == 7
    assert sum(observed["encounters"]["rows_by_payer_category"].values()) == 40
    assert observed["payers"]["private_name_count"] == 6
    lock = build_lock(
        source_url="https://example.invalid/a.zip",
        contents=contents,
        observed=observed,
        retrieved_on="2026-10-13",
    )
    assert verify_against_lock(contents, lock, contract, mapping) == []
    assert "No claim is made" in lock["artifact"]["provenance_note"]


def test_verify_detects_drift(tmp_path: Path, archive: Path, contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    contents = read_archive(archive)
    lock = build_lock(
        source_url="u",
        contents=contents,
        observed=profile(contents, contract, mapping),
        retrieved_on="2026-10-13",
    )
    rows = encounter_rows()[:-1]
    drifted = read_archive(build_archive(tmp_path / "drift.zip", encounters=rows))
    problems = verify_against_lock(drifted, lock, contract, mapping)
    assert any("row_count" in p for p in problems)


def test_unknown_payer_fails(tmp_path: Path, contract: dict[str, Any]) -> None:
    payers = payer_rows()
    payers[-1] = {**payers[-1], "NAME": "Unexpected New Payer"}
    contents = read_archive(build_archive(tmp_path / "x.zip", payers=payers))
    with pytest.raises(PayerMappingError) as info:
        profile(contents, contract, PayerMapping.from_contract(contract))
    assert "Unexpected New Payer" not in str(info.value)


@pytest.mark.parametrize(
    ("column", "value"),
    [("START", "2025/01/01 10:00"), ("TOTAL_CLAIM_COST", "12.345"), ("PAYER", "fixture-payer-99")],
)
def test_encounter_drift_fails(
    tmp_path: Path, contract: dict[str, Any], column: str, value: str
) -> None:
    rows = encounter_rows()
    rows[3] = {**rows[3], column: value}
    contents = read_archive(build_archive(tmp_path / "x.zip", encounters=rows))
    with pytest.raises(ArtifactDriftError):
        profile(contents, contract, PayerMapping.from_contract(contract))


def test_unsafe_member_names_rejected(tmp_path: Path) -> None:
    path = build_archive(tmp_path / "x.zip", extra_members={"../evil.csv": b"a\n"})
    with pytest.raises(ArtifactDriftError):
        read_archive(path)


def test_missing_member_rejected(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payers.csv", "Id,NAME\r\n")
    with pytest.raises(ArtifactDriftError):
        read_archive(path)


def test_stage_writes_only_minimized_columns(
    tmp_path: Path, archive: Path, contract: dict[str, Any]
) -> None:
    mapping = PayerMapping.from_contract(contract)
    manifest = stage(read_archive(archive), contract, mapping, tmp_path / "staging")
    staged = (tmp_path / "staging" / "encounters.csv").read_text(encoding="utf-8")
    payers = (tmp_path / "staging" / "payer_categories.csv").read_text(encoding="utf-8")
    header = staged.splitlines()[0].split(",")
    assert header == [c["name"] for c in contract["staging"]["encounters"]["columns"]]
    for forbidden in (
        *FICTIONAL_PRIVATE_NAMES,
        FIXTURE_DESCRIPTION,
        "fixture-org-001",
        "fixture-provider-001",
        "NAME",
    ):
        assert forbidden not in staged + payers
    categories = {r["payer_category"] for r in csv.DictReader(payers.splitlines())}
    assert categories == set(contract["payer_categories"])
    assert manifest["files"]["encounters.csv"]["rows"] == 40
    starts = [line.split(",")[1] for line in staged.splitlines()[1:]]
    assert starts == sorted(starts)


def test_stage_is_deterministic(tmp_path: Path, archive: Path, contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    first = stage(read_archive(archive), contract, mapping, tmp_path / "a")
    second = stage(read_archive(archive), contract, mapping, tmp_path / "b")
    assert first["files"] == second["files"]


def test_sensitive_literals(archive: Path, contract: dict[str, Any]) -> None:
    literals = sensitive_literals(
        archive, PayerMapping.from_contract(contract), sha256_file(archive)
    )
    assert sorted(literals["private_payer_brand"]) == sorted(FICTIONAL_PRIVATE_NAMES)
    assert "fixture-patient-000" in literals["patient_identifier"]
    assert literals["facility_name"] and literals["provider_name"]


def test_sensitive_literals_verifies_hash_before_opening(
    tmp_path: Path, archive: Path, contract: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthea_genie_demo import artifact

    opened: list[Path] = []
    real_open = artifact._open_archive
    monkeypatch.setattr(artifact, "_open_archive", lambda p: opened.append(p) or real_open(p))
    mapping = PayerMapping.from_contract(contract)
    with pytest.raises(HashMismatchError):
        sensitive_literals(archive, mapping, "0" * 64)
    assert opened == []
    not_zip = tmp_path / "not.zip"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(HashMismatchError):
        sensitive_literals(not_zip, mapping, sha256_file(archive))
    with pytest.raises(ArtifactDriftError, match="not a valid ZIP"):
        sensitive_literals(not_zip, mapping, sha256_file(not_zip))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"../escape.csv": b"x"}, "member paths"),
        ({"nested/organizations.csv": b"x"}, "member paths"),
        ({"notes.txt": b"x"}, "member paths"),
        ({f"m{i:02d}.csv": b"x" for i in range(70)}, "more than"),
    ],
)
def test_archive_member_table_limits(
    tmp_path: Path, contract: dict[str, Any], extra: dict[str, bytes], message: str
) -> None:
    path = build_archive(tmp_path / "bad.zip", extra_members=extra)
    mapping = PayerMapping.from_contract(contract)
    with pytest.raises(ArtifactDriftError, match=message):
        read_archive(path)
    with pytest.raises(ArtifactDriftError, match=message):
        sensitive_literals(path, mapping, sha256_file(path))


def test_archive_size_and_flag_limits(
    tmp_path: Path, contract: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthea_genie_demo import artifact

    mapping = PayerMapping.from_contract(contract)
    path = build_archive(tmp_path / "a.zip")
    monkeypatch.setattr(artifact, "MAX_LITERAL_MEMBER_BYTES", 10)
    with pytest.raises(ArtifactDriftError, match="larger than"):
        sensitive_literals(path, mapping, sha256_file(path))
    monkeypatch.undo()

    monkeypatch.setattr(artifact, "MAX_TOTAL_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(ArtifactDriftError, match="uncompressed size"):
        read_archive(path)
    monkeypatch.undo()

    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in ("encounters.csv", "payers.csv"):
            z.writestr(name, b"a" * 2_000_000)
    with pytest.raises(ArtifactDriftError, match="compression ratio"):
        read_archive(bomb)

    with zipfile.ZipFile(path) as z:
        info = z.getinfo("payers.csv")
        info.file_size += 5
        with pytest.raises(ArtifactDriftError, match="declared size"):
            artifact.read_member(z, info, 1024 * 1024)
        z.getinfo("encounters.csv").flag_bits |= 0x1
        with pytest.raises(ArtifactDriftError, match="encrypted"):
            artifact.checked_members(z)
