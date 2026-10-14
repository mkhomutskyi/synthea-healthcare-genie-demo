"""Remove only this project's Databricks resources. Dry run unless --execute.

Identity comes from the local deploy state (.cache/deploy-state.json by default). The state
must carry this project's slug, an exact run label, catalog, schema, volume, title and
space ID shape, and --run-label must equal the recorded run label. --catalog, --schema and
--space-id are optional confirmations that must equal the state; they never replace it.

Order: verify the recorded Agent (space ID plus exact unique title), trash it, then drop
the project's view, tables, volume and schema. The rendered drops must equal an exact
allowlist for the validated identifiers, and run only when the schema comment still starts
with the project marker for the recorded run label. Never touches a SQL warehouse (it only
runs the statements), never drops a catalog, never uses CASCADE.

The state file is removed only when it is the default state path, every cleanup step ran
and succeeded, and the file did not change meanwhile. Otherwise it is kept, so a partial
cleanup can be resumed. --purge-local-cache refuses to delete .cache/ while any file
beneath it is, or might be, a deployment state (see deploy.cache_purge_blockers).

Example:
  uv run python scripts/cleanup.py --run-label demo-2026-10 --profile <profile>
  ...review, then add --execute
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from synthea_genie_demo.databricks_cli import CliError, CliRunner
from synthea_genie_demo.deploy import (
    DeployError,
    Target,
    cache_purge_blockers,
    load_state,
    render_sql_files,
    run_statements,
    schema_marker_ok,
    validate_cleanup_statements,
    validate_state,
    verify_space_for_cleanup,
    write_state,
)
from synthea_genie_demo.jsonio import load_json
from synthea_genie_demo.paths import (
    AGENT_TEMPLATE_PATH,
    ARTIFACT_LOCK_PATH,
    CACHE_DIR,
    DEPLOY_STATE_PATH,
    RENDERED_DIR,
    SCHEMA_CONTRACT_PATH,
    SQL_DIR,
    ensure_local_output_dir,
)
from synthea_genie_demo.render import RenderError


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-label", help="must equal the run label recorded in state")
    parser.add_argument("--state-path", type=Path, default=DEPLOY_STATE_PATH)
    parser.add_argument("--work-dir", type=Path, default=RENDERED_DIR / "cleanup")
    parser.add_argument("--catalog", help="confirmation; must equal the recorded catalog")
    parser.add_argument("--schema", help="confirmation; must equal the recorded schema")
    parser.add_argument("--space-id", help="confirmation; must equal the recorded space ID")
    parser.add_argument("--warehouse-id", help="warehouse that runs the drops (default: state)")
    parser.add_argument("--profile")
    parser.add_argument("--cli", default="databricks")
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-sql", action="store_true")
    parser.add_argument(
        "--purge-local-cache",
        action="store_true",
        help="also delete .cache/ (archive, staging, rendered files); blocked while state exists",
    )
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def identity_problems(args: argparse.Namespace, state: Any, title_prefix: str) -> list[str]:
    if state is None:
        return [f"no deploy state at {args.state_path}; nothing recorded to clean up"]
    problems = validate_state(state, title_prefix=title_prefix)
    if problems:
        return problems
    if not args.run_label:
        problems.append("--run-label is required and must equal the recorded run label")
    for flag, key in (
        ("run_label", "run_label"),
        ("catalog", "catalog"),
        ("schema", "schema"),
        ("space_id", "space_id"),
    ):
        given = getattr(args, flag)
        if given is not None and given != state[key]:
            problems.append(f"--{flag.replace('_', '-')} {given!r} differs from the recorded value")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    remote = not (args.skip_agent and args.skip_sql)
    default_state = DEPLOY_STATE_PATH.resolve()
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN (nothing is sent to Databricks)'}")

    state: Any = None
    target: Target | None = None
    if remote:
        try:
            state_path = ensure_local_output_dir(args.state_path.parent) / args.state_path.name
            template = load_json(AGENT_TEMPLATE_PATH)
            state = load_state(state_path)
            problems = identity_problems(args, state, str(template["title_prefix"]))
            if not problems:
                target = Target(
                    state["catalog"],
                    state["schema"],
                    args.warehouse_id or state["warehouse_id"],
                    state["run_label"],
                    state["volume"],
                )
        except (RenderError, ValueError) as exc:
            problems = [str(exc)]
        if problems:
            for problem in problems:
                print(f"BLOCKED: {problem}", file=sys.stderr)
            return 2
    else:
        state_path = args.state_path.expanduser().resolve()
    assert target is not None or not remote

    runner = CliRunner(execute=args.execute, profile=args.profile, cli=args.cli)
    completed = {"agent": args.skip_agent, "sql": args.skip_sql}
    try:
        if not args.skip_agent and target is not None:
            space_id = state["space_id"]
            if space_id is None:
                print("== Agent: none recorded for this run (deploy stopped before create)")
                completed["agent"] = True
            else:
                print(f"== Agent: trash space {space_id} only if its title is {state['title']!r}")
                problems = verify_space_for_cleanup(runner, space_id, state["title"])
                if problems:
                    for problem in problems:
                        print(f"BLOCKED: {problem}", file=sys.stderr)
                    return 3
                runner.call(
                    "trash the project Agent", "genie", "trash-space", space_id, mutating=True
                )
                if args.execute:
                    state = {**state, "space_id": None}
                    write_state(state_path, state)
                completed["agent"] = True
        if not args.skip_sql and target is not None:
            print(
                f"== SQL: drop project objects in {target.full_schema} "
                f"(marker for run label {target.run_label} required)"
            )
            work = ensure_local_output_dir(args.work_dir)
            rendered = render_sql_files(
                target,
                load_json(ARTIFACT_LOCK_PATH),
                load_json(SCHEMA_CONTRACT_PATH),
                SQL_DIR,
                work / "sql",
                ("90_cleanup.sql",),
            )
            statements = validate_cleanup_statements(rendered["90_cleanup.sql"], target)
            ok, info = runner.try_call("schema marker check", "schemas", "get", target.full_schema)
            if args.execute and not ok:
                print(f"Schema {target.full_schema} not found; nothing to drop.")
                completed["sql"] = True
            elif args.execute and not schema_marker_ok(info, target.run_label):
                print(
                    f"BLOCKED: {target.full_schema} lacks the project marker for run label "
                    f"{target.run_label}; not dropping",
                    file=sys.stderr,
                )
                return 3
            else:
                run_statements(runner, target, "90_cleanup.sql", statements, work / "statements")
                completed["sql"] = True
    except (CliError, DeployError, RenderError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        print(f"Kept {state_path.name} so the cleanup can be resumed.", file=sys.stderr)
        return 5

    if args.execute and remote:
        full = all(completed.values()) and not (args.skip_agent or args.skip_sql)
        unchanged = state_path.exists() and load_state(state_path) == state
        if full and unchanged and state_path == default_state:
            state_path.unlink()
            print("Removed local deploy state.")
        else:
            print(f"Kept {state_path} (partial cleanup, non-default path, or state changed).")
    if args.purge_local_cache:
        blockers = cache_purge_blockers(
            CACHE_DIR, title_prefix=str(load_json(AGENT_TEMPLATE_PATH)["title_prefix"])
        )
        if blockers:
            level = "BLOCKED" if args.execute else "WARNING"
            for blocker in blockers:
                print(f"{level}: {blocker}", file=sys.stderr)
            print(
                f"{level}: not purging .cache/; clean up each recorded deployment "
                "(cleanup.py --state-path <file>) and remove its state file first",
                file=sys.stderr,
            )
            if args.execute:
                return 6
        elif args.execute:
            if CACHE_DIR.exists():
                shutil.rmtree(CACHE_DIR)
            print("Deleted .cache/.")
        else:
            print("Would delete .cache/ (archive, staging, rendered files).")
    if not args.execute:
        print("Dry run complete. Re-run with --execute to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
