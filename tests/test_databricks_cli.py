from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from synthea_genie_demo.databricks_cli import CliError, CliRunner, execute_statement, redact
from synthea_genie_demo.deploy import (
    DeployError,
    Target,
    create_agent,
    list_spaces,
    preflight,
    schema_marker_ok,
    verify_space_for_cleanup,
)
from synthea_genie_demo.jsonio import sha256_file

ST1, ST2, NEW_SPACE = "st1", "s", "new-space"
WAREHOUSE = "wh0fixture0demo1"
TARGET = Target("demo_catalog", "synthea_hc_ops", WAREHOUSE, "demo-run")


class FakeCli:
    """Records commands and returns canned JSON keyed by the first matching command words."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        joined = " ".join(command)
        for key, value in self.responses.items():
            if key in joined:
                if isinstance(value, list):
                    value = value.pop(0)
                if isinstance(value, Exception):
                    return subprocess.CompletedProcess(command, 1, "", str(value))
                return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        return subprocess.CompletedProcess(command, 0, "{}", "")


def test_dry_run_never_spawns(no_subprocess: list[Any], tmp_path: Path) -> None:
    runner = CliRunner(execute=False, profile="p")
    assert runner.call("x", "genie", "trash-space", "abc", mutating=True) is None
    assert runner.version() is None
    assert preflight(runner, TARGET, "title") == []
    assert (
        execute_statement(
            runner,
            warehouse_id="w",
            statement="SELECT 1",
            payload_path=tmp_path / "s.json",
            description="d",
        )
        is None
    )
    assert no_subprocess == []
    assert all("--profile p" in line for line in runner.log)


def test_execute_parses_json_and_raises_redacted_errors() -> None:
    secret_host = "https://team-x.cloud.databricks" + ".com/path"
    fake = FakeCli({"warehouses get": RuntimeError(f"denied at {secret_host}")})
    runner = CliRunner(execute=True, run=fake)
    with pytest.raises(CliError) as info:
        runner.call("wh", "warehouses", "get", "w", mutating=False)
    assert secret_host not in str(info.value)
    assert "<workspace-url>" in redact(secret_host)


def test_statement_polling_and_failure(tmp_path: Path) -> None:
    sleeps: list[float] = []
    fake = FakeCli(
        {
            "api post": {"statement_id": ST1, "status": {"state": "PENDING"}},
            "api get": [
                {"statement_id": ST1, "status": {"state": "RUNNING"}},
                {
                    "statement_id": ST1,
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {"schema": {"columns": [{"name": "n"}]}},
                    "result": {"data_array": [["1"]]},
                },
            ],
        }
    )
    runner = CliRunner(execute=True, run=fake)
    response = execute_statement(
        runner,
        warehouse_id="w",
        statement="SELECT 1",
        payload_path=tmp_path / "p.json",
        description="q",
        sleep=sleeps.append,
    )
    assert response and response["status"]["state"] == "SUCCEEDED"
    assert len(sleeps) == 2
    payload = json.loads((tmp_path / "p.json").read_text())
    assert payload["disposition"] == "INLINE" and payload["wait_timeout"] == "50s"

    failing = FakeCli(
        {
            "api post": {
                "statement_id": ST2,
                "status": {
                    "state": "FAILED",
                    "error": {"message": "Gate failed: clean_encounters differs"},
                },
            }
        }
    )
    with pytest.raises(CliError, match="Gate failed"):
        execute_statement(
            CliRunner(execute=True, run=failing),
            warehouse_id="w",
            statement="x",
            payload_path=tmp_path / "f.json",
            description="gate",
        )


def test_statement_timeout(tmp_path: Path) -> None:
    fake = FakeCli({"api": {"statement_id": ST2, "status": {"state": "RUNNING"}}})
    with pytest.raises(CliError, match="did not finish"):
        execute_statement(
            CliRunner(execute=True, run=fake),
            warehouse_id="w",
            statement="x",
            payload_path=tmp_path / "t.json",
            description="slow",
            max_wait_seconds=3,
            sleep=lambda s: None,
        )


def _preflight(responses: dict[str, Any]) -> list[str]:
    return preflight(CliRunner(execute=True, run=FakeCli(responses)), TARGET, "My Title [demo-run]")


GOOD = {
    "current-user me": {"userName": "someone"},
    "warehouses get": {"warehouse_type": "PRO"},
    "catalogs get": {"name": "demo_catalog"},
    "schemas get": RuntimeError("SCHEMA_NOT_FOUND"),
    "list-spaces": {"spaces": [{"title": "Other"}]},
}


def test_preflight_passes_on_good_state() -> None:
    assert _preflight(dict(GOOD)) == []


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"warehouses get": {"warehouse_type": "CLASSIC"}}, "pro or serverless"),
        ({"catalogs get": RuntimeError("not found")}, "never created"),
        ({"schemas get": {"comment": "someone else's schema"}}, "marker"),
        ({"list-spaces": {"spaces": [{"title": "My Title [demo-run]"}]}}, "already exists"),
        ({"current-user me": RuntimeError("unauthenticated")}, "not authenticated"),
    ],
)
def test_preflight_blocks(override: dict[str, Any], fragment: str) -> None:
    problems = _preflight({**GOOD, **override})
    assert any(fragment in p for p in problems), problems


def test_preflight_accepts_serverless_and_marked_schema() -> None:
    problems = _preflight(
        {
            **GOOD,
            "warehouses get": {"warehouse_type": "CLASSIC", "enable_serverless_compute": True},
            "schemas get": {"comment": "synthea-healthcare-genie-demo run-label=demo-run: x"},
        }
    )
    assert problems == []


@pytest.mark.parametrize(
    "comment",
    [
        "synthea-healthcare-genie-demo: x",
        "synthea-healthcare-genie-demo run-label=other-run: x",
        "copied from synthea-healthcare-genie-demo run-label=demo-run: x",
    ],
)
def test_preflight_requires_marker_for_this_run_label(comment: str) -> None:
    problems = _preflight({**GOOD, "schemas get": {"comment": comment}})
    assert any("marker" in p for p in problems), problems
    assert not schema_marker_ok({"comment": comment}, "demo-run")
    assert not schema_marker_ok("not a mapping", "demo-run")


def test_list_spaces_paginates() -> None:
    fake = FakeCli(
        {
            "list-spaces": [
                {"spaces": [{"title": "a"}], "next_page_token": "t2"},
                {"spaces": [{"title": "b"}]},
            ]
        }
    )
    spaces = list_spaces(CliRunner(execute=True, run=fake))
    assert [s["title"] for s in spaces or []] == ["a", "b"]
    assert "--page-token" in fake.commands[1]


REQUEST = {
    "title": "T",
    "description": "d",
    "warehouse_id": WAREHOUSE,
    "serialized_space": "{}",
    "parent_path": "/" + "Workspace/Users/someone/folder",
}


def test_create_and_update_agent(tmp_path: Path) -> None:
    fake = FakeCli({"api post": {"space_id": NEW_SPACE}})
    runner = CliRunner(execute=True, run=fake)
    created = create_agent(runner, REQUEST, tmp_path, None, update_existing=False)
    path = tmp_path / "create-space.request.json"
    assert created.space_id == NEW_SPACE and created.drift == ()
    assert created.agent_request_sha256 == sha256_file(path)
    assert fake.commands[0][:6] == [
        "databricks",
        "api",
        "post",
        "/api/2.0/genie/spaces",
        "--json",
        f"@{tmp_path / 'create-space.request.json'}",
    ]
    assert json.loads((tmp_path / "create-space.request.json").read_text()) == REQUEST

    patch = FakeCli({"api patch": {"space_id": NEW_SPACE}})
    state = {"title": "T", "space_id": NEW_SPACE}
    updated = create_agent(
        CliRunner(execute=True, run=patch), REQUEST, tmp_path, state, update_existing=True
    )
    assert updated.space_id == NEW_SPACE and updated.agent_request_sha256 is None
    assert patch.commands[0][1:5] == [
        "api",
        "patch",
        f"/api/2.0/genie/spaces/{NEW_SPACE}",
        "--json",
    ]
    update = json.loads((tmp_path / "update-space.request.json").read_text())
    assert "parent_path" not in update and update["serialized_space"] == "{}"

    with pytest.raises(DeployError):
        create_agent(runner, REQUEST, tmp_path, {"title": "Other"}, update_existing=True)
    with pytest.raises(DeployError):
        create_agent(
            runner, REQUEST, tmp_path, dict(title="T", space_id="../x"), update_existing=True
        )
    for bad in ({}, dict(space_id="a/../../b"), dict(space_id=7)):
        with pytest.raises(DeployError):
            create_agent(
                CliRunner(execute=True, run=FakeCli({"api post": bad})),
                REQUEST,
                tmp_path,
                None,
                update_existing=False,
            )


def test_cleanup_requires_exact_title() -> None:
    fake = FakeCli({"get-space": {"title": "Synthea Healthcare Operations Genie Agent [x]"}})
    runner = CliRunner(execute=True, run=fake)
    assert (
        verify_space_for_cleanup(runner, "s", "Synthea Healthcare Operations Genie Agent [x]") == []
    )
    assert verify_space_for_cleanup(runner, "s", "Synthea Healthcare Operations Genie Agent [y]")
    other = FakeCli({"get-space": dict(title="T", space_id="different")})
    assert verify_space_for_cleanup(CliRunner(execute=True, run=other), "s", "T")
    with pytest.raises(DeployError):
        verify_space_for_cleanup(runner, "../../api/2.0/x", "T")


def test_statement_id_is_validated_before_polling(tmp_path: Path) -> None:
    fake = FakeCli({"api post": {**dict(statement_id="../../x"), "status": {"state": "PENDING"}}})
    with pytest.raises(CliError, match="statement_id"):
        execute_statement(
            CliRunner(execute=True, run=fake),
            warehouse_id="w",
            statement="x",
            payload_path=tmp_path / "x.json",
            description="d",
            sleep=lambda s: None,
        )
