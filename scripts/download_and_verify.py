"""Download the official Synthea CSV archive into the ignored cache and verify it.

Fails closed: the archive is used only when its SHA-256 equals data/artifact.lock.json and
every recorded filename, header, count and payer fact still matches. Nothing under data/
is ever changed by this script. --rebaseline writes a *proposed* lock under
.cache/rebaseline/ and requires the new digest to be typed explicitly via --accept-sha256.

Examples:
  uv run python scripts/download_and_verify.py
  uv run python scripts/download_and_verify.py --stage
  uv run python scripts/download_and_verify.py --rebaseline --accept-sha256 <64-hex>
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from synthea_genie_demo.artifact import (
    OFFICIAL_SOURCE_URL,
    ArtifactError,
    build_lock,
    diff,
    download_verified,
    profile,
    read_archive,
    stage,
    verify_against_lock,
    verify_file_hash,
)
from synthea_genie_demo.jsonio import dumps_pretty, load_json, sha256_file, write_json
from synthea_genie_demo.paths import (
    ARCHIVE_FILENAME,
    ARTIFACT_LOCK_PATH,
    DEFAULT_ARCHIVE_DIR,
    DEFAULT_STAGING_DIR,
    REBASELINE_DIR,
    SCHEMA_CONTRACT_PATH,
    ensure_local_output_dir,
    repo_relative,
)
from synthea_genie_demo.payers import PayerMapping, PayerMappingError


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help="archive directory (default .cache/synthea; inside the repo only .cache/ is allowed)",
    )
    parser.add_argument("--archive", type=Path, help="verify an existing archive file instead")
    parser.add_argument("--offline", action="store_true", help="never download")
    parser.add_argument("--url", help="override the source URL (https:// or file:// only)")
    parser.add_argument("--stage", action="store_true", help="write minimized staging extracts")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument(
        "--rebaseline", action="store_true", help="propose a new lock; changes nothing"
    )
    parser.add_argument("--accept-sha256", help="digest a human accepted for --rebaseline")
    parser.add_argument("--lock", type=Path, default=ARTIFACT_LOCK_PATH)
    parser.add_argument("--contract", type=Path, default=SCHEMA_CONTRACT_PATH)
    parser.add_argument("--rebaseline-dir", type=Path, default=REBASELINE_DIR)
    parser.add_argument("--retrieved-on", default=date.today().isoformat())
    return parser.parse_args(argv)


def resolve_archive(args: argparse.Namespace, expected: str, url: str) -> Path:
    if args.archive:
        verify_file_hash(args.archive, expected)
        return Path(args.archive)
    dest = ensure_local_output_dir(args.dest) / ARCHIVE_FILENAME
    if dest.exists() and sha256_file(dest) == expected:
        print(f"Using cached archive {repo_relative(dest)} (hash verified).")
        return dest
    if args.offline:
        raise ArtifactError(f"--offline set and no verified archive at {repo_relative(dest)}")
    print(f"Downloading {url}")
    return download_verified(url, expected, dest)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    contract = load_json(args.contract)
    mapping = PayerMapping.from_contract(contract)
    lock = load_json(args.lock) if args.lock.exists() else None

    if args.rebaseline:
        if not args.accept_sha256 or not re.fullmatch(r"[0-9a-f]{64}", args.accept_sha256):
            print("--rebaseline requires --accept-sha256 <64 lowercase hex>", file=sys.stderr)
            return 2
        expected = args.accept_sha256
        url = args.url or OFFICIAL_SOURCE_URL
    else:
        if lock is None:
            print(
                f"No lock at {args.lock}; see docs/data-and-metrics.md#updating-the-source",
                file=sys.stderr,
            )
            return 2
        expected = lock["artifact"]["sha256"]
        url = args.url or lock["artifact"]["source_url"]

    try:
        archive = resolve_archive(args, expected, url)
        contents = read_archive(archive, expected)
        if args.rebaseline:
            observed = profile(contents, contract, mapping)
            proposed = build_lock(
                source_url=url,
                contents=contents,
                observed=observed,
                retrieved_on=args.retrieved_on,
            )
            out_dir = ensure_local_output_dir(args.rebaseline_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            write_json(out_dir / "artifact.lock.proposed.json", proposed)
            changes = diff(lock, proposed) if lock else ["no existing lock"]
            (out_dir / "artifact.lock.diff.txt").write_text(
                "\n".join(changes) + "\n", encoding="utf-8"
            )
            print(f"Proposed lock written to {repo_relative(out_dir)}/artifact.lock.proposed.json")
            print(f"{len(changes)} difference(s) vs the current lock. Nothing under data/ changed.")
            print("Review, then copy it to data/artifact.lock.json by hand and commit.")
            return 0
        assert lock is not None
        problems = verify_against_lock(contents, lock, contract, mapping)
        if problems:
            print("Artifact drift detected; failing closed:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 3
    except (ArtifactError, PayerMappingError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 3

    observed = lock["observed"]
    print(f"Verified archive sha256={contents.sha256}")
    print(f"encounters.csv rows={observed['encounters']['row_count']}")
    print(f"payers.csv rows={observed['payers']['row_count']}; payer mapping exact")
    if args.stage:
        try:
            staging = ensure_local_output_dir(args.staging_dir)
        except ValueError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 3
        manifest = stage(contents, contract, mapping, staging)
        print(f"Staged minimized extracts in {repo_relative(staging)}:")
        print(dumps_pretty(manifest["files"]), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
