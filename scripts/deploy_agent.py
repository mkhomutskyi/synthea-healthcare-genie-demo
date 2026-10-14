"""Deploy the schema, tables, Metric View and Genie Agent. Dry run unless --execute.

Without --execute the full plan is printed and no Databricks command runs (not even a
read). With --execute every step runs through the Databricks CLI with the profile you
already authenticated yourself. This script never authenticates, never creates a catalog,
and never creates, starts on purpose, resizes or edits a SQL warehouse. Running SQL on a
stopped warehouse can auto-start it, which consumes compute.

Command shapes are pinned to Databricks CLI 1.20.0 help output. They were exercised live
once, on the run recorded in docs/results.md; other CLI versions, workspaces,
regions and clouds are unverified. The Agent is created with
``databricks api post /api/2.0/genie/spaces`` (documented REST body), not
``genie create-space``.

With --execute, local state (.cache/deploy-state.json) is written before the first
mutating step and updated with the space ID after the Agent is created, so a partial
deployment can still be cleaned up. A state file for a different run blocks the deploy.
When the Agent is created, the state also records the SHA-256 of the exact create-space
request file that was validated and sent (a local receipt, not a remote attestation).
The request bytes are written to a private, read-only file; the CLI reads that file, and
it must be the same file with the same bytes before and after the call. If it changed,
the new space is still recorded for cleanup but without a receipt, and the deploy fails.
The payload-held-out evaluation requires that receipt. --update-existing clears it,
because the Agent then no longer matches the recorded create request. State files are
replaced atomically and are never read or written through a symlink.

--parent-path is checked once (validate_parent_path) before the request is built or any
state is written. With --execute, the Databricks CLI must be exactly the pinned version;
any other version blocks before the first command that reads or changes the workspace.

Example:
  uv run python scripts/deploy_agent.py --catalog main --schema synthea_hc_ops \\
      --warehouse-id <warehouse-id> --run-label demo-2026-10 --profile <profile>
  ...review the plan, then add --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from synthea_genie_demo.artifact import ArtifactError
from synthea_genie_demo.databricks_cli import (
    CLI_HELP_BASIS,
    CLI_LIVE_STATUS,
    TESTED_CLI_VERSION,
    CliError,
    CliRunner,
)
from synthea_genie_demo.deploy import (
    SQL_BY_STEP,
    STEPS,
    DeployError,
    Target,
    check_staging,
    create_agent,
    load_state,
    new_state,
    preflight,
    render_sql_files,
    run_sql,
    same_identity,
    title_for,
    upload_staging,
    validate_create_request,
    validate_parent_path,
    validate_state,
    write_state,
)
from synthea_genie_demo.jsonio import load_json
from synthea_genie_demo.paths import (
    AGENT_TEMPLATE_PATH,
    ARTIFACT_LOCK_PATH,
    DEFAULT_STAGING_DIR,
    DEPLOY_STATE_PATH,
    RENDERED_DIR,
    SCHEMA_CONTRACT_PATH,
    SQL_DIR,
    ensure_local_output_dir,
)
from synthea_genie_demo.render import RenderError, build_create_request
from synthea_genie_demo.sqltools import DEFAULT_VOLUME, DEPLOY_SQL_FILES


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--volume", default=DEFAULT_VOLUME)
    parser.add_argument("--warehouse-id", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--parent-path")
    parser.add_argument("--profile")
    parser.add_argument("--cli", default="databricks")
    parser.add_argument("--steps", nargs="+", choices=STEPS, default=list(STEPS))
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--work-dir", type=Path, default=RENDERED_DIR)
    parser.add_argument("--state-path", type=Path, default=DEPLOY_STATE_PATH)
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="update the recorded Agent instead of creating a new one",
    )
    parser.add_argument("--execute", action="store_true", help="actually run the plan")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        target = Target(args.catalog, args.schema, args.warehouse_id, args.run_label, args.volume)
        work_dir = ensure_local_output_dir(args.work_dir)
        state_path = ensure_local_output_dir(args.state_path.parent) / args.state_path.name
        lock = load_json(ARTIFACT_LOCK_PATH)
        contract = load_json(SCHEMA_CONTRACT_PATH)
        template = load_json(AGENT_TEMPLATE_PATH)
        title = title_for(template, args.run_label)
        parent_path = validate_parent_path(args.parent_path)
        request = build_create_request(
            template,
            catalog=target.catalog,
            schema=target.schema,
            warehouse_id=target.warehouse_id,
            run_label=target.run_label,
            parent_path=parent_path,
        )
        request_problems = validate_create_request(
            request, warehouse_id=target.warehouse_id, title=title
        )
        if request_problems:
            raise RenderError("; ".join(request_problems))
        rendered = render_sql_files(
            target, lock, contract, SQL_DIR, work_dir / "sql", DEPLOY_SQL_FILES
        )
    except (DeployError, RenderError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    mode = "EXECUTE" if args.execute else "DRY RUN (nothing is sent to Databricks)"
    print(f"Mode: {mode}")
    print(f"Target: {target.full_schema} volume {target.volume}; Agent title {title!r}")
    print(f"{CLI_HELP_BASIS} {CLI_LIVE_STATUS}")

    level = "BLOCKED" if args.execute else "WARNING"
    staging_problems = (
        check_staging(args.staging_dir, lock, contract) if "upload" in args.steps else []
    )
    for problem in staging_problems:
        print(f"{level}: {problem}", file=sys.stderr)
    if staging_problems and args.execute:
        return 3

    state = load_state(state_path)
    state_problems = (
        validate_state(state, title_prefix=str(template["title_prefix"]))
        if state is not None
        else []
    )
    if not state_problems and state is not None and not same_identity(state, target, title):
        state_problems = [
            f"{state_path.name} records a different deployment ({state['run_label']} in "
            f"{state['catalog']}.{state['schema']}); clean it up first"
        ]
    if not state_problems and "agent" in args.steps:
        recorded = state.get("space_id") if state else None
        if args.update_existing and not recorded:
            state_problems = ["--update-existing needs a recorded space for this exact title"]
        elif recorded and not args.update_existing:
            state_problems = [
                f"an Agent ({recorded}) is already recorded for this run; "
                "use --update-existing or clean up first"
            ]
    for problem in state_problems:
        print(f"{level}: {problem}", file=sys.stderr)
    if state_problems and args.execute:
        return 3
    valid_state = state if state is not None and not state_problems else None

    runner = CliRunner(execute=args.execute, profile=args.profile, cli=args.cli)
    try:
        if args.execute:
            version = runner.version()
            if version != TESTED_CLI_VERSION:
                print(
                    f"BLOCKED: Databricks CLI {version or 'version unknown'} is not the pinned "
                    f"{TESTED_CLI_VERSION}; install exactly {TESTED_CLI_VERSION} to deploy",
                    file=sys.stderr,
                )
                return 4
        else:
            print(f"Note: --execute requires exactly Databricks CLI {TESTED_CLI_VERSION}.")
        if "preflight" in args.steps:
            problems = preflight(runner, target, title)
            if args.update_existing:
                problems = [p for p in problems if "already exists" not in p]
            if problems:
                for problem in problems:
                    print(f"PREFLIGHT FAILED: {problem}", file=sys.stderr)
                return 4
        recorded_space = valid_state.get("space_id") if valid_state else None
        kept = valid_state if valid_state and recorded_space else {}
        state_parent = kept.get("parent_path") if recorded_space else parent_path
        if args.execute and any(step in args.steps for step in STEPS[1:]):
            write_state(
                state_path,
                new_state(
                    target,
                    title,
                    recorded_space,
                    parent_path=state_parent,
                    agent_request_sha256=kept.get("agent_request_sha256"),
                ),
            )
            print(f"Recorded deployment target in {state_path.name} (local, git-ignored).")
        for step in STEPS[1:]:
            if step not in args.steps:
                continue
            print(f"== step: {step}")
            if step == "upload":
                upload_staging(
                    runner,
                    target,
                    args.staging_dir,
                    lock,
                    contract,
                    work_dir / "upload",
                    root=work_dir,
                )
            elif step == "agent":
                created = create_agent(
                    runner, request, work_dir, valid_state, update_existing=args.update_existing
                )
                if created.space_id and args.execute:
                    write_state(
                        state_path,
                        new_state(
                            target,
                            title,
                            created.space_id,
                            parent_path=state_parent if args.update_existing else parent_path,
                            agent_request_sha256=created.agent_request_sha256,
                        ),
                    )
                    print(f"Recorded Agent in {state_path.name} (local, git-ignored).")
                if created.drift:
                    raise DeployError(
                        "the create-space request file changed while the CLI read it ("
                        + "; ".join(created.drift)
                        + "); the Agent is recorded for cleanup without a request receipt"
                    )
            else:
                for name in SQL_BY_STEP[step]:
                    run_sql(runner, target, name, rendered[name], work_dir / "statements")
    except (ArtifactError, CliError, DeployError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 5
    if not args.execute:
        print("Dry run complete. Re-run with --execute to apply this plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
