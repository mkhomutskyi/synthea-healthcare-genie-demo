"""Run the offline contract checks; with --artifact also verify the cached archive.

Tier 1 (default, CI): offline checks only, no network, no archive needed.
Tier 2 (--artifact): verify the locally cached archive's SHA-256 against
data/artifact.lock.json before opening it, re-check every recorded fact, and scan public
outputs for exact local values (synthetic patient keys, private payer, facility and
provider names). Values are held in memory and never printed. When staging exists, the
staged files are verified and the offline reference is checked against the committed
digest in evaluation/expected-results/reference-digest.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from synthea_genie_demo.artifact import (
    ArtifactError,
    read_archive,
    sensitive_literals,
    verify_against_lock,
    verify_staged_files,
)
from synthea_genie_demo.checks import benchmarks, contract, lock, run_offline_checks
from synthea_genie_demo.jsonio import load_json
from synthea_genie_demo.paths import (
    ARCHIVE_FILENAME,
    DEFAULT_ARCHIVE_DIR,
    DEFAULT_STAGING_DIR,
    REFERENCE_DIGEST_PATH,
    REPO_ROOT,
)
from synthea_genie_demo.payers import PayerMapping, PayerMappingError
from synthea_genie_demo.reference import (
    FREEZE_COMMAND,
    check_reference_against_digest,
    compute_all,
    load_staged_rows,
)
from synthea_genie_demo.scan import Scanner, image_files


def staging_checks(staging_dir: Path) -> dict[str, list[str]]:
    if not (staging_dir / "manifest.json").exists():
        print(f"[note] no staging in {staging_dir}; reference digest check skipped")
        return {}
    try:
        verify_staged_files(staging_dir, lock(), contract())
    except ArtifactError as exc:
        return {"staged files": [str(exc)]}
    results = compute_all(load_staged_rows(staging_dir))
    return {
        "staged files": [],
        "reference matches frozen digest": check_reference_against_digest(
            results,
            load_json(REFERENCE_DIGEST_PATH),
            artifact_sha256=lock()["artifact"]["sha256"],
            intents=benchmarks()["intents"],
            generated_by=FREEZE_COMMAND,
        ),
    }


def artifact_checks(archive: Path, staging_dir: Path) -> dict[str, list[str]]:
    c = contract()
    lk = lock()
    mapping = PayerMapping.from_contract(c)
    if not archive.exists():
        return {"artifact": [f"no cached archive at {archive}; run scripts/download_and_verify.py"]}
    try:
        contents = read_archive(archive, lk["artifact"]["sha256"])
        drift = verify_against_lock(contents, lk, c, mapping)
        literals = sensitive_literals(archive, mapping, lk["artifact"]["sha256"])
        scanner = Scanner.from_contract(c, exact_literals=literals)
    except (ArtifactError, PayerMappingError) as exc:
        return {"artifact": [str(exc)]}
    return {
        "artifact matches lock": drift,
        "public output scan (exact local values)": [str(f) for f in scanner.scan_tree(REPO_ROOT)],
        **staging_checks(staging_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact", action="store_true", help="also run the artifact tier")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE_DIR / ARCHIVE_FILENAME)
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    args = parser.parse_args(argv)

    results = run_offline_checks()
    if args.artifact:
        results.update(artifact_checks(args.archive, args.staging_dir))
    failed = 0
    for name, problems in results.items():
        status = "ok" if not problems else "FAIL"
        print(f"[{status:>4}] {name}")
        for problem in problems:
            print(f"        - {problem}")
        failed += bool(problems)
    images = image_files(REPO_ROOT)
    if images:
        print(f"[note] {len(images)} image(s) need manual redaction review: {', '.join(images)}")
    print(f"{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
