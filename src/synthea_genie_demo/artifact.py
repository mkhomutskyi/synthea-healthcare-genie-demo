"""Download, verify, profile and stage the official Synthea CSV archive.

Everything here fails closed: a hash, filename, header, count or payer drift stops the run.
Nothing in this module writes inside the repository except through caller-chosen paths
that have passed :func:`synthea_genie_demo.paths.ensure_local_output_dir`.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .jsonio import sha256_bytes, sha256_file, write_json
from .payers import PayerMapping

OFFICIAL_SOURCE_URL = (
    "https://synthetichealth.github.io/synthea-sample-data/downloads/latest/"
    "synthea_sample_data_csv_latest.zip"
)
OFFICIAL_SHA256 = "d61417b551e5b0997c33851b339c157421751f0ea68c18ea686ceb1850907c35"
USED_MEMBERS = ("encounters.csv", "payers.csv")
LITERAL_MEMBERS = (("organizations.csv", "facility_name"), ("providers.csv", "provider_name"))
STAGED_FILES = ("encounters.csv", "payer_categories.csv")
PROFILE_YEARS = ("2023", "2024", "2025", "2026")
ARTIFACT_LABEL = (
    "Synthea sample data CSV archive (upstream 100-patient sample label; 'latest' download)"
)
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 64
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_LITERAL_MEMBER_BYTES = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
USER_AGENT = "synthea-healthcare-genie-demo/0.1 (+artifact verification)"

Fetcher = Callable[[str, Path, int], None]


class ArtifactError(RuntimeError):
    """Base error for artifact verification failures."""


class HashMismatchError(ArtifactError):
    def __init__(self, observed: str, expected: str) -> None:
        super().__init__(
            f"SHA-256 mismatch: expected {expected}, observed {observed}. "
            "The upstream 'latest' archive may have changed. Nothing was used. "
            "See docs/data-and-metrics.md#updating-the-source."
        )
        self.observed = observed
        self.expected = expected


class ArtifactDriftError(ArtifactError):
    """Archive contents differ from the frozen lock or contract."""


def urllib_fetch(url: str, dest: Path, max_bytes: int) -> None:
    if not url.startswith(("https://", "file://")):
        raise ArtifactError(f"Refusing non-HTTPS source URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        total = 0
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise ArtifactError(f"Download exceeded {max_bytes} bytes; aborting.")
            out.write(chunk)


def download_verified(
    url: str,
    expected_sha256: str,
    dest_path: Path,
    *,
    fetcher: Fetcher = urllib_fetch,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> Path:
    """Download to a temporary file and move it into place only if the hash matches."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=dest_path.parent)
    os.close(handle)
    tmp_path = Path(tmp_name)
    try:
        fetcher(url, tmp_path, max_bytes)
        observed = sha256_file(tmp_path)
        if observed != expected_sha256:
            raise HashMismatchError(observed=observed, expected=expected_sha256)
        os.replace(tmp_path, dest_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return dest_path


def verify_file_hash(path: Path, expected_sha256: str) -> str:
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise HashMismatchError(observed=observed, expected=expected_sha256)
    return observed


@dataclass(frozen=True)
class Member:
    name: str
    size_bytes: int
    sha256: str
    zip_timestamp: str
    header: list[str]
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class ArchiveContents:
    path: Path
    sha256: str
    size_bytes: int
    member_names: list[str]
    members: dict[str, Member]


def _safe_member_name(name: str) -> bool:
    return "/" not in name and "\\" not in name and ".." not in name and name.endswith(".csv")


def _open_archive(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArtifactDriftError(f"{path} is not a valid ZIP archive") from exc


def checked_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Validate the member table before reading anything (names, count, sizes, flags)."""
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ArtifactDriftError(f"Archive has more than {MAX_MEMBERS} members")
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise ArtifactDriftError("Archive has duplicate member names")
    unsafe = sorted(n for n in names if not _safe_member_name(n))
    if unsafe:
        raise ArtifactDriftError(f"Unexpected archive member paths: {unsafe}")
    if sum(info.file_size for info in infos) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ArtifactDriftError("Archive expands beyond the uncompressed size limit")
    if any(info.flag_bits & 0x1 for info in infos):
        raise ArtifactDriftError("Archive has encrypted members")
    return {info.filename: info for info in infos}


def read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    """Read one member with a hard byte cap, independent of the declared size."""
    if info.file_size > max_bytes:
        raise ArtifactDriftError(f"{info.filename} is larger than {max_bytes} bytes")
    if info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
        raise ArtifactDriftError(f"{info.filename} has an implausible compression ratio")
    with archive.open(info) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes or len(data) != info.file_size:
        raise ArtifactDriftError(f"{info.filename} size differs from its declared size")
    return data


def read_archive(path: Path, expected_sha256: str | None = None) -> ArchiveContents:
    """Read the two in-scope members into memory without extracting the archive.

    With ``expected_sha256`` the file digest is verified before the ZIP is parsed.
    """
    if expected_sha256 is not None:
        verify_file_hash(path, expected_sha256)
    with _open_archive(path) as archive:
        infos = checked_members(archive)
        names = sorted(infos)
        members: dict[str, Member] = {}
        for name in USED_MEMBERS:
            if name not in infos:
                raise ArtifactDriftError(f"Archive is missing required member {name}")
            info = infos[name]
            data = read_member(archive, info, MAX_MEMBER_BYTES)
            header, rows = _parse_csv(name, data)
            year, month, day, hour, minute, second = info.date_time
            members[name] = Member(
                name=name,
                size_bytes=info.file_size,
                sha256=sha256_bytes(data),
                zip_timestamp=f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}",
                header=header,
                rows=rows,
            )
    return ArchiveContents(
        path=path,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        member_names=names,
        members=members,
    )


def _parse_csv(name: str, data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactDriftError(f"{name} is not UTF-8") from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ArtifactDriftError(f"{name} is empty") from exc
    rows: list[dict[str, str]] = []
    for line_number, record in enumerate(reader, start=2):
        if not record:
            continue
        if len(record) != len(header):
            raise ArtifactDriftError(f"{name} line {line_number} has {len(record)} fields")
        rows.append(dict(zip(header, record, strict=True)))
    return header, rows


def _money(value: str, column: str, max_places: int) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ArtifactDriftError(f"Non-numeric {column} value") from exc
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or -exponent > max_places:
        raise ArtifactDriftError(f"{column} has more than {max_places} decimal places")
    return amount


def _require_columns(member: Member, required: list[str]) -> None:
    missing = [c for c in required if c not in member.header]
    if missing:
        raise ArtifactDriftError(f"{member.name} is missing required columns {missing}")


def payer_categories_by_id(
    contents: ArchiveContents, contract: Mapping[str, Any], mapping: PayerMapping
) -> dict[str, str]:
    payers = contents.members["payers.csv"]
    _require_columns(payers, contract["sources"]["payers.csv"]["required_columns"])
    ids = [row["Id"] for row in payers.rows]
    if len(set(ids)) != len(ids):
        raise ArtifactDriftError("payers.csv has duplicate Id values")
    by_name = mapping.map_names(row["NAME"] for row in payers.rows)
    return {row["Id"]: by_name[row["NAME"]] for row in payers.rows}


def profile(
    contents: ArchiveContents, contract: Mapping[str, Any], mapping: PayerMapping
) -> dict[str, Any]:
    """Return the observed facts recorded in data/artifact.lock.json."""
    source_rules = contract["sources"]["encounters.csv"]
    encounters = contents.members["encounters.csv"]
    _require_columns(encounters, source_rules["required_columns"])
    category_by_payer = payer_categories_by_id(contents, contract, mapping)
    start_pattern = re.compile(source_rules["start_timestamp_pattern"])
    max_places = int(source_rules["max_money_decimal_places"])

    class_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    category_year_counts: dict[str, Counter[str]] = {y: Counter() for y in PROFILE_YEARS}
    claim_total = Decimal("0")
    coverage_total = Decimal("0")
    coverage_exceeds_claim = 0
    negative_amounts = 0
    dates: list[str] = []
    for row in encounters.rows:
        start = row["START"]
        if not start_pattern.match(start):
            raise ArtifactDriftError("encounters.csv has a START value with an unexpected format")
        day = start[:10]
        date.fromisoformat(day)
        dates.append(day)
        payer_id = row["PAYER"]
        if payer_id not in category_by_payer:
            raise ArtifactDriftError(
                "encounters.csv references a payer key missing from payers.csv"
            )
        category = category_by_payer[payer_id]
        claim = _money(row["TOTAL_CLAIM_COST"], "TOTAL_CLAIM_COST", max_places)
        coverage = _money(row["PAYER_COVERAGE"], "PAYER_COVERAGE", max_places)
        claim_total += claim
        coverage_total += coverage
        negative_amounts += int(claim < 0) + int(coverage < 0)
        coverage_exceeds_claim += int(coverage > claim)
        class_counts[row["ENCOUNTERCLASS"]] += 1
        category_counts[category] += 1
        year = day[:4]
        if year in category_year_counts:
            year_counts[year] += 1
            category_year_counts[year][category] += 1

    payer_rows = contents.members["payers.csv"].rows
    public_names = sorted(
        r["NAME"] for r in payer_rows if r["NAME"] in mapping.public_program_labels
    )
    return {
        "encounters": {
            "row_count": len(encounters.rows),
            "distinct_encounter_ids": len({r["Id"] for r in encounters.rows}),
            "distinct_patients": len({r["PATIENT"] for r in encounters.rows}),
            "start_date_min": min(dates),
            "start_date_max": max(dates),
            "encounter_class_counts": dict(sorted(class_counts.items())),
            "rows_by_year": {y: year_counts[y] for y in PROFILE_YEARS},
            "rows_by_payer_category": dict(sorted(category_counts.items())),
            "rows_by_year_and_payer_category": {
                y: dict(sorted(category_year_counts[y].items())) for y in PROFILE_YEARS
            },
            "total_claim_cost_sum": str(claim_total),
            "payer_coverage_sum": str(coverage_total),
            "negative_money_values": negative_amounts,
            "coverage_exceeds_claim_rows": coverage_exceeds_claim,
        },
        "payers": {
            "row_count": len(payer_rows),
            "public_program_names": public_names,
            "private_name_count": len(payer_rows) - len(public_names),
        },
    }


def describe_members(contents: ArchiveContents) -> dict[str, Any]:
    return {
        name: {
            "size_bytes": member.size_bytes,
            "sha256": member.sha256,
            "zip_timestamp": member.zip_timestamp,
            "header": member.header,
            "row_count": len(member.rows),
        }
        for name, member in sorted(contents.members.items())
    }


def build_lock(
    *,
    source_url: str,
    contents: ArchiveContents,
    observed: Mapping[str, Any],
    retrieved_on: str,
) -> dict[str, Any]:
    return {
        "lock_version": 1,
        "artifact": {
            "name": ARTIFACT_LABEL,
            "source_url": source_url,
            "sha256": contents.sha256,
            "size_bytes": contents.size_bytes,
            "retrieved_on": retrieved_on,
            "provenance_note": (
                "The upstream file is a mutable 'latest' download. This lock records only what "
                "scripts/download_and_verify.py observed on retrieved_on. No claim is made about "
                "which Synthea version, commit or configuration generated the archive."
            ),
        },
        "expected_member_names": contents.member_names,
        "used_members": describe_members(contents),
        "observed": dict(observed),
        "verification": {
            "tool": "scripts/download_and_verify.py --rebaseline",
            "verified_on": retrieved_on,
            "checks": [
                "archive SHA-256 equals the human-accepted digest",
                "member names are flat .csv files",
                "required columns present; START format; money precision",
                "every encounter payer key resolves to exactly one payer",
                "payer names map exactly to the contract (unknown names fail)",
            ],
        },
    }


def diff(expected: Any, observed: Any, path: str = "") -> list[str]:
    """Return human-readable differences between two JSON-like values."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        problems: list[str] = []
        for key in sorted(set(expected) | set(observed)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in observed:
                problems.append(f"{sub}: missing from observed")
            elif key not in expected:
                problems.append(f"{sub}: not in lock")
            else:
                problems.extend(diff(expected[key], observed[key], sub))
        return problems
    if expected != observed:
        return [f"{path}: lock={expected!r} observed={observed!r}"]
    return []


def verify_against_lock(
    contents: ArchiveContents,
    lock: Mapping[str, Any],
    contract: Mapping[str, Any],
    mapping: PayerMapping,
) -> list[str]:
    """Compare a hash-verified archive with every recorded fact. Empty list means no drift."""
    problems: list[str] = []
    if contents.sha256 != lock["artifact"]["sha256"]:
        problems.append("artifact.sha256 differs")
    if contents.size_bytes != lock["artifact"]["size_bytes"]:
        problems.append("artifact.size_bytes differs")
    if contents.member_names != lock["expected_member_names"]:
        problems.append("expected_member_names differ")
    problems.extend(diff(lock["used_members"], describe_members(contents), "used_members"))
    problems.extend(diff(lock["observed"], profile(contents, contract, mapping), "observed"))
    return problems


def stage(
    contents: ArchiveContents,
    contract: Mapping[str, Any],
    mapping: PayerMapping,
    staging_dir: Path,
) -> dict[str, Any]:
    """Write the minimized extracts that are uploaded to Databricks.

    Raw payer names and excluded encounter columns never reach the staged files.
    """
    category_by_payer = payer_categories_by_id(contents, contract, mapping)
    staging_dir.mkdir(parents=True, exist_ok=True)
    spec = contract["staging"]

    encounter_columns = spec["encounters"]["columns"]
    encounter_rows = sorted(
        contents.members["encounters.csv"].rows, key=lambda r: (r["START"], r["Id"])
    )
    encounters_path = staging_dir / spec["encounters"]["file"]
    _write_csv(
        encounters_path,
        [c["name"] for c in encounter_columns],
        ([row[c["source"]] for c in encounter_columns] for row in encounter_rows),
    )

    payers_path = staging_dir / spec["payer_categories"]["file"]
    _write_csv(
        payers_path,
        [c["name"] for c in spec["payer_categories"]["columns"]],
        ([pid, category_by_payer[pid]] for pid in sorted(category_by_payer)),
    )

    manifest = {
        "source_archive_sha256": contents.sha256,
        "files": {
            path.name: {"sha256": sha256_file(path), "rows": rows}
            for path, rows in (
                (encounters_path, len(encounter_rows)),
                (payers_path, len(category_by_payer)),
            )
        },
        "note": "Local staging only. Contains synthetic identifiers; never commit.",
    }
    write_json(staging_dir / "manifest.json", manifest)
    return manifest


def sensitive_literals(
    path: Path, mapping: PayerMapping, expected_sha256: str
) -> dict[str, list[str]]:
    """Exact values that must never appear in public outputs (artifact-tier scan only).

    The archive digest is verified against ``expected_sha256`` before the ZIP is parsed,
    and every member read goes through the same name, count and size limits.
    Values stay in memory; callers must never print or persist them.
    """
    contents = read_archive(path, expected_sha256)
    literals: dict[str, set[str]] = {
        "patient_identifier": {r["PATIENT"] for r in contents.members["encounters.csv"].rows},
        "private_payer_brand": {
            r["NAME"]
            for r in contents.members["payers.csv"].rows
            if r["NAME"] not in mapping.public_program_labels
        },
        "facility_name": set(),
        "provider_name": set(),
    }
    with _open_archive(path) as archive:
        infos = checked_members(archive)
        for member, rule in LITERAL_MEMBERS:
            if member in infos:
                data = read_member(archive, infos[member], MAX_LITERAL_MEMBER_BYTES)
                _, rows = _parse_csv(member, data)
                literals[rule] |= {r["NAME"] for r in rows if len(r.get("NAME", "")) >= 6}
    return {rule: sorted(values) for rule, values in literals.items()}


def _count_csv(path: Path) -> tuple[list[str], int]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = 0
        for record in reader:
            if len(record) != len(header):
                raise ArtifactDriftError(f"staged {path.name} has a malformed row")
            rows += 1
    return header, rows


def verify_staged_files(
    staging_dir: Path, lock: Mapping[str, Any], contract: Mapping[str, Any]
) -> dict[str, str]:
    """Recompute and check both staged extracts; return their verified SHA-256 digests.

    Fails closed when the manifest is missing or from another archive, when a staged file
    is missing, a symlink, has a different digest than the manifest, a different header
    than the contract, or a different row count than the lock.
    """
    manifest_path = staging_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ArtifactDriftError(
            f"no staging manifest in {staging_dir}; run "
            "uv run python scripts/download_and_verify.py --stage"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactDriftError("staging manifest is not valid JSON") from exc
    if manifest.get("source_archive_sha256") != lock["artifact"]["sha256"]:
        raise ArtifactDriftError("staging was produced from a different archive than the lock")
    files = manifest.get("files")
    if not isinstance(files, dict) or sorted(files) != sorted(STAGED_FILES):
        raise ArtifactDriftError(f"staging manifest must list exactly {list(STAGED_FILES)}")
    spec = contract["staging"]
    expected = {
        "encounters.csv": (
            [c["name"] for c in spec["encounters"]["columns"]],
            lock["observed"]["encounters"]["row_count"],
        ),
        "payer_categories.csv": (
            [c["name"] for c in spec["payer_categories"]["columns"]],
            lock["observed"]["payers"]["row_count"],
        ),
    }
    digests: dict[str, str] = {}
    for name in STAGED_FILES:
        path = staging_dir / name
        if path.is_symlink() or not path.is_file():
            raise ArtifactDriftError(f"staged {name} is missing or not a regular file")
        recorded = files[name].get("sha256") if isinstance(files[name], dict) else None
        if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
            raise ArtifactDriftError(f"staging manifest has no valid SHA-256 for {name}")
        observed = sha256_file(path)
        if observed != recorded:
            raise ArtifactDriftError(f"staged {name} changed since staging (SHA-256 differs)")
        header, rows = _count_csv(path)
        want_header, want_rows = expected[name]
        if header != want_header:
            raise ArtifactDriftError(f"staged {name} header differs from the contract")
        if rows != want_rows or files[name].get("rows") != want_rows:
            raise ArtifactDriftError(f"staged {name} row count differs from the lock")
        digests[name] = observed
    return digests


def _write_csv(path: Path, header: list[str], rows: Any) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
