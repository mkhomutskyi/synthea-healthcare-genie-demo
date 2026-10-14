"""Compute independent reference answers, run canonical SQL, and compare results.

Use --reference for local checks. --canonical contacts Databricks only with --execute.
Use --payload-holdout to check the four additional analytical questions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from synthea_genie_demo.artifact import ArtifactError, verify_staged_files
from synthea_genie_demo.checks import (
    benchmarks,
    check_benchmarks,
    check_examples_and_samples,
    check_payload_holdout,
    check_payload_holdout_exclusion,
    payload_holdout,
)
from synthea_genie_demo.databricks_cli import CliError, CliRunner, execute_statement, result_table
from synthea_genie_demo.holdout import HOLDOUT_CANONICAL_RESULTS_FILE, holdout_reference
from synthea_genie_demo.jsonio import load_json, write_json
from synthea_genie_demo.paths import (
    ARTIFACT_LOCK_PATH,
    DEFAULT_STAGING_DIR,
    PAYLOAD_HOLDOUT_DIGEST_PATH,
    REFERENCE_DIGEST_PATH,
    REFERENCE_DIR,
    RENDERED_DIR,
    RESULTS_DIR,
    SCHEMA_CONTRACT_PATH,
    ensure_local_output_dir,
    repo_relative,
)
from synthea_genie_demo.reference import (
    FREEZE_COMMAND,
    HOLDOUT_FREEZE_COMMAND,
    HOLDOUT_SPECS,
    INTENT_SPECS,
    check_reference_against_digest,
    compare_tables,
    compute_all,
    load_staged_rows,
    reference_digest,
)
from synthea_genie_demo.render import (
    RenderError,
    intent_sql,
    render_placeholders,
    validate_identifier,
    validate_schema_name,
    validate_warehouse_id,
)

RESULTS_FILE = "canonical-results.json"
REFERENCE_FILE = "reference-results.json"
HOLDOUT_RESULTS_FILE = HOLDOUT_CANONICAL_RESULTS_FILE


def validate() -> int:
    bench = benchmarks()
    problems = check_benchmarks(bench) + check_examples_and_samples(bench)
    for intent in bench["intents"]:
        keys = [q["key"] for q in bench["questions"] if q["intent_id"] == intent["id"]]
        print(f"{intent['id']}: {', '.join(keys)}")
    holdout = payload_holdout()
    problems += check_payload_holdout(holdout, bench)
    if not problems:
        problems += check_payload_holdout_exclusion(holdout, bench)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    print(
        f"{len(bench['questions'])} frozen questions, {len(bench['intents'])} intents; "
        f"payload-holdout {holdout['suite_version']}: {len(holdout['questions'])} questions, "
        f"{len(holdout['boundary_probes'])} boundary paraphrases; "
        f"{'no problems' if not problems else f'{len(problems)} problem(s)'}"
    )
    return 1 if problems else 0


def holdout_reference_mode(args: argparse.Namespace) -> int:
    lock: dict[str, Any] = load_json(args.lock)
    contract: dict[str, Any] = load_json(args.contract)
    suite = payload_holdout()
    digest_path = args.digest or PAYLOAD_HOLDOUT_DIGEST_PATH
    if args.freeze_digest:
        try:
            verify_staged_files(args.staging_dir, lock, contract)
        except ArtifactError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 2
        results = compute_all(load_staged_rows(args.staging_dir), HOLDOUT_SPECS)
        empty = [intent for intent, table in results.items() if not table["rows"]]
        if empty:
            print(f"BLOCKED: holdout intents with no rows: {empty}", file=sys.stderr)
            return 2
        digest = reference_digest(
            results,
            artifact_sha256=lock["artifact"]["sha256"],
            intents=suite["intents"],
            generated_by=HOLDOUT_FREEZE_COMMAND,
        )
        write_json(digest_path, digest)
        print(f"Froze {repo_relative(digest_path)}; review the diff before committing it.")
        return 0
    checked, problems = holdout_reference(args.staging_dir, lock, contract, suite, digest_path)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if checked is None:
        return 1
    for intent, table in checked.items():
        print(f"{intent}: {len(table['rows'])} rows (computed in memory, not written)")
    print(f"Payload-holdout reference matches the frozen digest {repo_relative(digest_path)}.")
    return 0


def reference(args: argparse.Namespace, out_dir: Path) -> int:
    lock: dict[str, Any] = load_json(args.lock)
    contract: dict[str, Any] = load_json(args.contract)
    digest_path = args.digest or REFERENCE_DIGEST_PATH
    try:
        verify_staged_files(args.staging_dir, lock, contract)
    except ArtifactError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    results = compute_all(load_staged_rows(args.staging_dir))
    write_json(out_dir / REFERENCE_FILE, results)
    for intent, table in results.items():
        print(f"{intent}: {len(table['rows'])} rows")
    print(f"Wrote {repo_relative(out_dir / REFERENCE_FILE)} (local only)")
    digest = reference_digest(
        results, artifact_sha256=lock["artifact"]["sha256"], intents=benchmarks()["intents"]
    )
    if args.freeze_digest:
        write_json(digest_path, digest)
        print(f"Froze {repo_relative(digest_path)}; review the diff before committing it.")
        return 0
    problems = check_reference_against_digest(
        results,
        load_json(digest_path),
        artifact_sha256=lock["artifact"]["sha256"],
        intents=benchmarks()["intents"],
        generated_by=FREEZE_COMMAND,
    )
    if not problems and load_json(digest_path) != digest:
        problems = ["reference digest differs from the one this reference would freeze"]
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print(f"Reference matches the frozen digest {repo_relative(digest_path)}.")
    return 1 if problems else 0


def canonical(args: argparse.Namespace, out_dir: Path) -> int:
    if not (args.catalog and args.schema and args.warehouse_id):
        print("--canonical needs --catalog, --schema and --warehouse-id", file=sys.stderr)
        return 2
    try:
        validate_identifier(args.catalog, "catalog")
        validate_schema_name(args.schema)
        validate_warehouse_id(args.warehouse_id)
    except RenderError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    runner = CliRunner(execute=args.execute, profile=args.profile, cli=args.cli)
    params = {"catalog": args.catalog, "schema": args.schema}
    results = {}
    intents = payload_holdout()["intents"] if args.payload_holdout else benchmarks()["intents"]
    try:
        for intent in intents:
            sql = render_placeholders(intent_sql(intent), params)
            response = execute_statement(
                runner,
                warehouse_id=args.warehouse_id,
                statement=sql,
                payload_path=ensure_local_output_dir(args.work_dir) / f"{intent['id']}.json",
                description=f"canonical SQL {intent['id']}",
                mutating=False,
            )
            if response is not None:
                results[intent["id"]] = result_table(response)
    except CliError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 5
    if not args.execute:
        print(f"Dry run complete; add --execute to run the {len(intents)} read-only queries.")
        return 0
    name = HOLDOUT_RESULTS_FILE if args.payload_holdout else RESULTS_FILE
    write_json(out_dir / name, results)
    print(f"Wrote {repo_relative(out_dir / name)} (local only)")
    return 0


def compare(results_dir: Path, reference_dir: Path, digest_path: Path, lock: dict[str, Any]) -> int:
    actual_path, expected_path = results_dir / RESULTS_FILE, reference_dir / REFERENCE_FILE
    missing = [repo_relative(p) for p in (actual_path, expected_path) if not p.exists()]
    if missing:
        print(f"Missing {', '.join(missing)}", file=sys.stderr)
        return 2
    actual, expected = load_json(actual_path), load_json(expected_path)
    stale = check_reference_against_digest(
        expected,
        load_json(digest_path),
        artifact_sha256=lock["artifact"]["sha256"],
        intents=benchmarks()["intents"],
        generated_by=FREEZE_COMMAND,
    )
    for problem in stale:
        print(f"FAIL: {problem}; rerun --reference", file=sys.stderr)
    if stale:
        return 1
    failures = 0
    for intent, spec in INTENT_SPECS.items():
        problems = compare_tables(
            expected[intent], actual.get(intent, {"columns": [], "rows": []}), spec
        )
        print(f"[{'ok' if not problems else 'FAIL'}] {intent}")
        for problem in problems[:10]:
            print(f"        - {problem}")
        failures += bool(problems)
    return 1 if failures else 0


def compare_holdout(args: argparse.Namespace, results_dir: Path) -> int:
    actual_path = results_dir / HOLDOUT_RESULTS_FILE
    if not actual_path.exists():
        print(f"Missing {repo_relative(actual_path)}", file=sys.stderr)
        return 2
    expected, problems = holdout_reference(
        args.staging_dir,
        load_json(args.lock),
        load_json(args.contract),
        payload_holdout(),
        args.digest or PAYLOAD_HOLDOUT_DIGEST_PATH,
    )
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if expected is None:
        return 1
    actual = load_json(actual_path)
    failures = 0
    for intent, spec in HOLDOUT_SPECS.items():
        mismatches = compare_tables(
            expected[intent], actual.get(intent, {"columns": [], "rows": []}), spec
        )
        print(f"[{'ok' if not mismatches else 'FAIL'}] {intent}")
        for problem in mismatches[:10]:
            print(f"        - {problem}")
        failures += bool(mismatches)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reference", action="store_true")
    mode.add_argument("--canonical", action="store_true")
    mode.add_argument("--compare", action="store_true")
    parser.add_argument("--freeze-digest", action="store_true")
    parser.add_argument(
        "--payload-holdout",
        action="store_true",
        help="with --reference, --canonical or --compare: use the payload-holdout suite",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--catalog")
    parser.add_argument("--schema")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--profile")
    parser.add_argument("--cli", default="databricks")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--lock", type=Path, default=ARTIFACT_LOCK_PATH)
    parser.add_argument("--contract", type=Path, default=SCHEMA_CONTRACT_PATH)
    parser.add_argument("--digest", type=Path, help="default: the suite's committed digest")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--reference-dir", type=Path, default=REFERENCE_DIR)
    parser.add_argument("--work-dir", type=Path, default=RENDERED_DIR / "benchmarks")
    args = parser.parse_args(argv)
    if args.freeze_digest and not args.reference:
        print("--freeze-digest is only valid with --reference", file=sys.stderr)
        return 2
    if args.payload_holdout and not (args.reference or args.canonical or args.compare):
        print("--payload-holdout needs --reference, --canonical or --compare", file=sys.stderr)
        return 2
    try:
        results_dir = ensure_local_output_dir(args.results_dir)
        reference_dir = ensure_local_output_dir(args.reference_dir)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    if args.reference and args.payload_holdout:
        return holdout_reference_mode(args)
    if args.reference:
        return reference(args, reference_dir)
    if args.canonical:
        return canonical(args, results_dir)
    if args.compare and args.payload_holdout:
        return compare_holdout(args, results_dir)
    if args.compare:
        return compare(
            results_dir, reference_dir, args.digest or REFERENCE_DIGEST_PATH, load_json(args.lock)
        )
    return validate()


if __name__ == "__main__":
    raise SystemExit(main())
