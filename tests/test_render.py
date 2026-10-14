from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from synthea_genie_demo.checks import (
    check_agent_text,
    check_generated_files,
    check_hex_ids_in_template,
    check_template,
    contract,
)
from synthea_genie_demo.jsonio import load_json, sha256_text
from synthea_genie_demo.paths import AGENT_TEMPLATE_PATH, REPO_ROOT
from synthea_genie_demo.render import (
    HEX32_RE,
    RenderError,
    build_create_request,
    canonical_sql_text,
    render_space,
    stable_id,
    template_text,
    validate_serialized_space,
)


@pytest.fixture
def template() -> dict[str, Any]:
    data: dict[str, Any] = load_json(AGENT_TEMPLATE_PATH)
    return data


def test_generated_files_are_current() -> None:
    assert check_generated_files() == []


def test_generation_is_deterministic() -> None:
    assert template_text() == template_text()
    assert canonical_sql_text() == canonical_sql_text()


def test_meta_hashes_match_sources(template: dict[str, Any]) -> None:
    for rel, digest in template["_meta"]["sources_sha256"].items():
        assert sha256_text((REPO_ROOT / rel).read_text(encoding="utf-8")) == digest


def test_template_valid(template: dict[str, Any]) -> None:
    assert check_template() == []
    assert check_hex_ids_in_template() == []
    assert check_agent_text(contract()) == []
    assert validate_serialized_space(template["serialized_space"], rendered=False) == []


def test_stable_ids() -> None:
    first = stable_id("benchmark", "i1-a")
    assert first == stable_id("benchmark", "i1-a")
    assert HEX32_RE.match(first)
    assert first != stable_id("benchmark", "i1-b")
    assert first != stable_id("sample_question", "i1-a")


def test_space_shape(template: dict[str, Any]) -> None:
    space = template["serialized_space"]
    assert space["version"] == 2
    assert list(space["data_sources"]) == ["metric_views"]
    (view,) = space["data_sources"]["metric_views"]
    assert view["identifier"] == "{{catalog}}.{{schema}}.encounter_operations_metrics"
    assert len(space["benchmarks"]["questions"]) == 14
    assert len(space["config"]["sample_questions"]) == 3
    assert len(space["instructions"]["text_instructions"]) == 1
    for collection in (
        space["config"]["sample_questions"],
        space["instructions"]["example_question_sqls"],
        space["benchmarks"]["questions"],
    ):
        ids = [item["id"] for item in collection]
        assert ids == sorted(ids)
    for question in space["benchmarks"]["questions"]:
        assert len(question["answer"]) == 1
        assert question["answer"][0]["format"] == "SQL"


def test_render_and_request(template: dict[str, Any]) -> None:
    rendered = render_space(template, "demo_catalog", "synthea_hc_ops")
    assert "{{" not in json.dumps(rendered)
    request = build_create_request(
        template,
        catalog="demo_catalog",
        schema="synthea_hc_ops",
        warehouse_id="wh0fixture0demo1",
        run_label="demo-run",
    )
    assert request["title"].endswith("[demo-run]")
    assert json.loads(request["serialized_space"]) == rendered


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(warehouse_id="bad id!"),
        {"run_label": "Bad Label"},
        {"catalog": "c.d"},
        {"schema": "default"},
    ],
)
def test_request_input_validation(template: dict[str, Any], kwargs: dict[str, str]) -> None:
    args = dict(
        catalog="demo_catalog",
        schema="synthea_hc_ops",
        warehouse_id="wh0fixture0demo1",
        run_label="demo-run",
    )
    args.update(kwargs)
    with pytest.raises(RenderError):
        build_create_request(template, **args)


def _broken(template: dict[str, Any], change: Any) -> list[str]:
    space = copy.deepcopy(template["serialized_space"])
    change(space)
    return validate_serialized_space(space, rendered=False)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(version=1),
        lambda s: s["benchmarks"]["questions"].reverse(),
        lambda s: s["benchmarks"]["questions"][0].update(id="ABC"),
        lambda s: s["benchmarks"]["questions"][1].update(id=s["benchmarks"]["questions"][0]["id"]),
        lambda s: s["benchmarks"]["questions"][0]["answer"].append(
            {"format": "SQL", "content": ["SELECT 1"]}
        ),
        lambda s: s["instructions"]["text_instructions"].append({"id": "f" * 32, "content": ["x"]}),
        lambda s: s["data_sources"].update(tables=[{"identifier": "a.b.c"}]),
        lambda s: s["data_sources"]["metric_views"][0].update(identifier="a.b.other_view"),
        lambda s: s["config"]["sample_questions"][0].update(
            id=s["benchmarks"]["questions"][0]["id"]
        ),
        lambda s: s["instructions"]["text_instructions"][0].update(content=["x" * 25_001]),
        lambda s: s["instructions"]["text_instructions"][0].update(content=["{{unknown}}"]),
    ],
)
def test_validation_catches_rule_breaks(template: dict[str, Any], change: Any) -> None:
    assert _broken(template, change)


def test_canonical_sql_file_header() -> None:
    text = canonical_sql_text()
    assert "GENERATED" in text and "Runtime validation required" in text
    assert text.count("-- intent:") == 7
