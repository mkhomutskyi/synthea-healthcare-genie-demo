"""Thin, dry-run-first wrapper around the Databricks CLI.

No function here connects to a workspace unless the runner was created with
``execute=True``. In dry-run mode commands are printed and nothing is invoked, not even
``databricks --version``.

Pinned expectation: commands and flags match the ``--help`` output of Databricks CLI
1.20.0 inspected offline (tests/test_cli_shapes.py records those shapes). Genie Agent
create and update go through ``databricks api`` against the documented REST paths,
because ``genie create-space`` in that CLI takes WAREHOUSE_ID and SERIALIZED_SPACE as
positional arguments. These shapes were exercised live once, with CLI 1.20.0, on the run
recorded in docs/results.md. That is one validated run, not a guarantee for other
CLI versions, workspaces, regions or clouds.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonio import write_json

TESTED_CLI_VERSION = "1.20.0"
CLI_HELP_BASIS = (
    f"Command shapes match Databricks CLI {TESTED_CLI_VERSION} help output (offline check)."
)
CLI_EXPECTATION = CLI_HELP_BASIS
CLI_LIVE_STATUS = (
    f"Validated live once with CLI {TESTED_CLI_VERSION} on the recorded run in "
    "docs/results.md; other CLI versions, workspaces, regions and clouds are unverified."
)
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}
STATEMENT_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}")
WORKSPACE_HOSTS = r"(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)"
REDACTIONS = (
    (re.compile(rf"https?://[A-Za-z0-9.-]+\.{WORKSPACE_HOSTS}\S*"), "<workspace-url>"),
    (re.compile(r"\bdapi[0-9a-f]{32}\b"), "<token>"),
    (re.compile(r"\bBearer\s+\S+"), "Bearer <token>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class CliError(RuntimeError):
    pass


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class CliRunner:
    execute: bool
    profile: str | None = None
    cli: str = "databricks"
    run: Run | None = None
    log: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        run = self.run or subprocess.run
        return run(command, capture_output=True, text=True, check=False)

    def argv(self, *args: str) -> list[str]:
        command = [self.cli, *args, "--output", "json"]
        if self.profile:
            command += ["--profile", self.profile]
        return command

    def call(self, description: str, *args: str, mutating: bool) -> Any:
        command = self.argv(*args)
        kind = "WRITE" if mutating else "READ "
        line = f"[{kind}] {description}: {shlex.join(command)}"
        self.log.append(line)
        self.commands.append(command)
        if not self.execute:
            print(f"[dry-run] {line}")
            return None
        print(line)
        result = self._run(command)
        if result.returncode != 0:
            raise CliError(
                f"{description} failed: {redact(result.stderr or result.stdout).strip()}"
            )
        output = result.stdout.strip()
        if not output:
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"text": redact(output)}

    def try_call(self, description: str, *args: str) -> tuple[bool, Any]:
        """Read-only call whose failure (for example 'not found') is an answer, not an error."""
        try:
            return True, self.call(description, *args, mutating=False)
        except CliError as exc:
            return False, str(exc)

    def version(self) -> str | None:
        if not self.execute:
            return None
        result = self._run([self.cli, "--version"])
        match = re.search(r"v?(\d+\.\d+\.\d+)", result.stdout or "")
        return match.group(1) if match else None


def statement_payload(warehouse_id: str, statement: str) -> dict[str, Any]:
    return {
        "warehouse_id": warehouse_id,
        "statement": statement,
        "wait_timeout": "50s",
        "on_wait_timeout": "CONTINUE",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    }


def execute_statement(
    runner: CliRunner,
    *,
    warehouse_id: str,
    statement: str,
    payload_path: Path,
    description: str,
    mutating: bool = True,
    poll_seconds: float = 2.0,
    max_wait_seconds: float = 600.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Run one SQL statement through the Statement Execution API via ``databricks api``."""
    write_json(payload_path, statement_payload(warehouse_id, statement))
    response = runner.call(
        description,
        "api",
        "post",
        "/api/2.0/sql/statements",
        "--json",
        f"@{payload_path}",
        mutating=mutating,
    )
    if response is None:
        return None
    waited, delay = 0.0, poll_seconds
    while response.get("status", {}).get("state") not in TERMINAL_STATES:
        if waited >= max_wait_seconds:
            raise CliError(f"{description}: statement did not finish within {max_wait_seconds}s")
        sleep(delay)
        waited += delay
        delay = min(delay * 2, 60.0)
        if not STATEMENT_ID_RE.fullmatch(str(response.get("statement_id", ""))):
            raise CliError(f"{description}: response has no valid statement_id")
        response = runner.call(
            f"{description} (poll)",
            "api",
            "get",
            f"/api/2.0/sql/statements/{response['statement_id']}",
            mutating=False,
        )
    state = response["status"]["state"]
    if state != "SUCCEEDED":
        message = response["status"].get("error", {}).get("message", state)
        raise CliError(f"{description}: {state}: {redact(str(message))}")
    if response.get("result", {}).get("next_chunk_internal_link"):
        raise CliError(f"{description}: result is chunked; expected a small aggregate result")
    return dict(response)


def result_table(response: dict[str, Any]) -> dict[str, Any]:
    columns = [c["name"] for c in response["manifest"]["schema"]["columns"]]
    rows = response.get("result", {}).get("data_array", []) or []
    return {"columns": columns, "rows": rows}
