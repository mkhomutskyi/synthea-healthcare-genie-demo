"""Generate the version-2 Genie Agent configuration and canonical SQL from frozen sources.

``evaluation/benchmarks.json`` is the only source for benchmark questions and answers.
The committed template and ``sql/40_canonical_answers.sql`` are generated outputs; tests
regenerate them and fail on any difference.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import METRIC_VIEW_NAME, PROJECT_SLUG
from .jsonio import dumps_pretty, load_json, sha256_text
from .paths import (
    AGENT_SETTINGS_PATH,
    BENCHMARKS_PATH,
    EXAMPLE_QUERIES_PATH,
    INSTRUCTIONS_PATH,
    repo_relative,
)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
WAREHOUSE_ID_RE = re.compile(r"^[0-9A-Za-z]{8,64}$")
RUN_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
RESERVED_SCHEMAS = {"default", "information_schema"}
TEMPLATE_PLACEHOLDERS = ("catalog", "schema")
BEGIN_MARKER = "<!-- BEGIN AGENT INSTRUCTIONS -->"
END_MARKER = "<!-- END AGENT INSTRUCTIONS -->"
MAX_STRING = 25_000
MAX_ARRAY = 10_000
TEMPLATE_SOURCES = (BENCHMARKS_PATH, AGENT_SETTINGS_PATH, INSTRUCTIONS_PATH, EXAMPLE_QUERIES_PATH)


class RenderError(ValueError):
    pass


def stable_id(kind: str, key: str) -> str:
    """Deterministic 32-character lowercase hex ID for a serialized_space element."""
    return sha256_text(f"{PROJECT_SLUG}:{kind}:{key}")[:32]


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise RenderError(
            f"{label} {value!r} must be a simple identifier (letters, digits, underscore)."
        )
    return value


def validate_schema_name(value: str) -> str:
    validate_identifier(value, "schema")
    if value.lower() in RESERVED_SCHEMAS:
        raise RenderError(f"Use a dedicated schema for this project, not {value!r}.")
    return value


def render_placeholders(value: Any, params: Mapping[str, str]) -> Any:
    """Replace ``{{name}}`` placeholders recursively; unknown names are an error."""
    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in params:
                raise RenderError(f"Unknown placeholder {{{{{name}}}}}")
            return params[name]

        return PLACEHOLDER_RE.sub(substitute, value)
    if isinstance(value, list):
        return [render_placeholders(v, params) for v in value]
    if isinstance(value, dict):
        return {k: render_placeholders(v, params) for k, v in value.items()}
    return value


def placeholders_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(PLACEHOLDER_RE.findall(value))
    if isinstance(value, list):
        return set().union(*(placeholders_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return placeholders_in(list(value.values()))
    return set()


@dataclass(frozen=True)
class Example:
    key: str
    question: str
    sql: str


def parse_example_queries(text: str) -> list[Example]:
    examples: list[Example] = []
    blocks = re.split(r"^-- example:\s*", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        key, _, rest = block.partition("\n")
        match = re.match(r"-- question:\s*(.+)\n", rest)
        if not match:
            raise RenderError(f"Example {key!r} is missing a '-- question:' line")
        sql = rest[match.end() :].strip()
        if not sql.endswith(";") or sql.count(";") != 1:
            raise RenderError(f"Example {key!r} must contain exactly one statement ending in ';'")
        examples.append(Example(key=key.strip(), question=match.group(1).strip(), sql=sql[:-1]))
    if not examples:
        raise RenderError("No examples found")
    return examples


def instruction_text(markdown: str) -> str:
    start = markdown.find(BEGIN_MARKER)
    end = markdown.find(END_MARKER)
    if start < 0 or end < start:
        raise RenderError("instructions.md must contain the BEGIN/END AGENT INSTRUCTIONS markers")
    return markdown[start + len(BEGIN_MARKER) : end].strip()


def text_lines(text: str) -> list[str]:
    lines = text.strip().split("\n")
    return [line + "\n" for line in lines[:-1]] + [lines[-1]]


def intent_sql(intent: Mapping[str, Any]) -> str:
    return "\n".join(intent["canonical_sql"])


def semantic_sql(semantics: Mapping[str, Any], source: str) -> list[str]:
    """The only canonical SQL shape an intent's structured semantics may produce."""
    dims = [f"`{d}`" for d in semantics["dimensions"]]
    items = list(dims)
    for measure in semantics["measures"]:
        expr = f"MEASURE(`{measure['measure']}`)"
        if measure["round"] is not None:
            expr = f"ROUND({expr}, {int(measure['round'])})"
        items.append(f"{expr} AS {measure['alias']}")
    years = [int(y) for y in semantics["years"]]
    where = (
        f"WHERE `Year` = {years[0]}"
        if len(years) == 1
        else f"WHERE `Year` IN ({', '.join(str(y) for y in years)})"
    )
    grouping = ", ".join(dims)
    return [
        "SELECT",
        *(f"  {item}," for item in items[:-1]),
        f"  {items[-1]}",
        f"FROM {source}",
        where,
        f"GROUP BY {grouping}",
        f"ORDER BY {grouping}",
    ]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_serialized_space(
    benchmarks: Mapping[str, Any],
    settings: Mapping[str, Any],
    instructions_md: str,
    examples_sql: str,
) -> dict[str, Any]:
    intents = {intent["id"]: intent for intent in benchmarks["intents"]}
    sample_questions = [
        {"id": stable_id("sample_question", s["key"]), "question": [s["question"]]}
        for s in settings["sample_questions"]
    ]
    examples = [
        {
            "id": stable_id("example_question_sql", ex.key),
            "question": [ex.question],
            "sql": text_lines(ex.sql),
        }
        for ex in parse_example_queries(examples_sql)
    ]
    benchmark_questions = [
        {
            "id": stable_id("benchmark", q["key"]),
            "question": [q["question"]],
            "answer": [
                {"format": "SQL", "content": text_lines(intent_sql(intents[q["intent_id"]]))}
            ],
        }
        for q in benchmarks["questions"]
    ]
    return {
        "version": 2,
        "config": {"sample_questions": sorted(sample_questions, key=lambda x: x["id"])},
        "data_sources": {
            "metric_views": [
                {
                    "identifier": benchmarks["metric_view_placeholder"],
                    "description": [settings["metric_view_description"]],
                }
            ]
        },
        "instructions": {
            "text_instructions": [
                {
                    "id": stable_id("text_instruction", "general"),
                    "content": text_lines(instruction_text(instructions_md)),
                }
            ],
            "example_question_sqls": sorted(examples, key=lambda x: x["id"]),
        },
        "benchmarks": {"questions": sorted(benchmark_questions, key=lambda x: x["id"])},
    }


def build_template_document() -> dict[str, Any]:
    benchmarks = load_json(BENCHMARKS_PATH)
    settings = load_json(AGENT_SETTINGS_PATH)
    space = build_serialized_space(
        benchmarks, settings, _read(INSTRUCTIONS_PATH), _read(EXAMPLE_QUERIES_PATH)
    )
    return {
        "_meta": {
            "generated_by": "scripts/render_agent_config.py --write-template",
            "do_not_edit": "Generated file. Edit the sources listed below and regenerate.",
            "placeholders": list(TEMPLATE_PLACEHOLDERS),
            "benchmark_suite_version": benchmarks["suite_version"],
            "sources_sha256": {repo_relative(p): sha256_text(_read(p)) for p in TEMPLATE_SOURCES},
        },
        "title_prefix": settings["title_prefix"],
        "description": settings["description"],
        "serialized_space": space,
    }


def template_text() -> str:
    return dumps_pretty(build_template_document())


def canonical_sql_text() -> str:
    benchmarks = load_json(BENCHMARKS_PATH)
    by_intent: dict[str, list[str]] = {}
    for q in benchmarks["questions"]:
        by_intent.setdefault(q["intent_id"], []).append(q["key"])
    parts = [
        "-- GENERATED from evaluation/benchmarks.json by scripts/render_agent_config.py.",
        "-- Do not edit by hand. Regenerate with:",
        "--   uv run python scripts/render_agent_config.py --write-template",
        f"-- Benchmark suite {benchmarks['suite_version']} (frozen {benchmarks['frozen_on']}).",
        "-- Runtime validation required: run against the deployed Metric View before publishing.",
        "-- Placeholders: {{catalog}}, {{schema}}.",
        "",
    ]
    for intent in benchmarks["intents"]:
        parts.extend(
            [
                f"-- intent: {intent['id']}",
                f"-- title: {intent['title']}",
                f"-- benchmark questions: {', '.join(by_intent[intent['id']])}",
                f"-- expected columns: {', '.join(intent['expected_columns'])}",
                intent_sql(intent) + ";",
                "",
            ]
        )
    return "\n".join(parts)


def render_space(template: Mapping[str, Any], catalog: str, schema: str) -> dict[str, Any]:
    validate_identifier(catalog, "catalog")
    validate_schema_name(schema)
    rendered: dict[str, Any] = render_placeholders(
        template["serialized_space"], {"catalog": catalog, "schema": schema}
    )
    errors = validate_serialized_space(rendered, rendered=True)
    if errors:
        raise RenderError("Rendered serialized_space is invalid:\n- " + "\n- ".join(errors))
    return rendered


def validate_run_label(value: str) -> str:
    if not RUN_LABEL_RE.match(value):
        raise RenderError("run label must match ^[a-z0-9][a-z0-9-]{2,40}$")
    return value


def validate_warehouse_id(value: str) -> str:
    if not WAREHOUSE_ID_RE.match(value):
        raise RenderError("warehouse id must be 8-64 alphanumeric characters")
    return value


def agent_title(prefix: str, run_label: str) -> str:
    return f"{prefix} [{validate_run_label(run_label)}]"


def build_create_request(
    template: Mapping[str, Any],
    *,
    catalog: str,
    schema: str,
    warehouse_id: str,
    run_label: str,
    parent_path: str | None = None,
) -> dict[str, Any]:
    validate_warehouse_id(warehouse_id)
    space = render_space(template, catalog, schema)
    body: dict[str, Any] = {
        "title": agent_title(template["title_prefix"], run_label),
        "description": template["description"],
        "warehouse_id": warehouse_id,
        "serialized_space": json.dumps(space, ensure_ascii=False, separators=(",", ":")),
    }
    if parent_path:
        body["parent_path"] = parent_path
    return body


def _check_sorted(items: list[Any], key: str, label: str, errors: list[str]) -> None:
    values = [item.get(key) for item in items]
    if values != sorted(values):
        errors.append(f"{label} must be sorted by {key}")


def _check_ids(items: list[Any], label: str, errors: list[str]) -> list[str]:
    ids: list[str] = []
    for item in items:
        value = item.get("id")
        if not isinstance(value, str) or not HEX32_RE.match(value):
            errors.append(f"{label} id {value!r} is not 32 lowercase hex characters")
        else:
            ids.append(value)
    _check_sorted(items, "id", label, errors)
    return ids


def _check_limits(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, str) and len(value) > MAX_STRING:
        errors.append(f"{path} exceeds {MAX_STRING} characters")
    elif isinstance(value, list):
        if len(value) > MAX_ARRAY:
            errors.append(f"{path} exceeds {MAX_ARRAY} items")
        for i, item in enumerate(value):
            _check_limits(item, f"{path}[{i}]", errors)
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_limits(v, f"{path}.{k}", errors)


def _non_empty_strings(value: Any) -> bool:
    return (
        isinstance(value, list) and len(value) > 0 and all(isinstance(v, str) and v for v in value)
    )


def validate_serialized_space(space: Mapping[str, Any], *, rendered: bool) -> list[str]:
    """Check the documented version-2 rules plus this project's own constraints."""
    errors: list[str] = []
    if space.get("version") != 2:
        errors.append("version must be 2")
    unexpected = set(space) - {"version", "config", "data_sources", "instructions", "benchmarks"}
    if unexpected:
        errors.append(f"unexpected top-level keys {sorted(unexpected)}")

    samples = space.get("config", {}).get("sample_questions", [])
    sample_ids = _check_ids(samples, "config.sample_questions", errors)
    for item in samples:
        if not _non_empty_strings(item.get("question")) or len(item["question"]) != 1:
            errors.append("each sample question needs exactly one question string")

    data_sources = space.get("data_sources", {})
    if set(data_sources) != {"metric_views"}:
        errors.append("data_sources must contain only metric_views (no tables)")
    views = data_sources.get("metric_views", [])
    if len(views) != 1:
        errors.append("exactly one metric view must be attached")
    _check_sorted(views, "identifier", "data_sources.metric_views", errors)
    for view in views:
        parts = str(view.get("identifier", "")).split(".")
        if len(parts) != 3 or parts[2] != METRIC_VIEW_NAME:
            errors.append(f"metric view identifier must be catalog.schema.{METRIC_VIEW_NAME}")
        elif rendered and not all(IDENTIFIER_RE.match(p) for p in parts):
            errors.append("rendered metric view identifier has invalid parts")
        configs = view.get("column_configs", [])
        _check_sorted(configs, "column_name", "metric view column_configs", errors)

    instructions = space.get("instructions", {})
    allowed_instruction_keys = {"text_instructions", "example_question_sqls"}
    if set(instructions) - allowed_instruction_keys:
        errors.append("instructions may only contain text_instructions and example_question_sqls")
    texts = instructions.get("text_instructions", [])
    if len(texts) != 1:
        errors.append("exactly one text instruction is required (the documented maximum is 1)")
    text_ids = _check_ids(texts, "instructions.text_instructions", errors)
    for item in texts:
        if not _non_empty_strings(item.get("content")):
            errors.append("text instruction content must be a non-empty list of strings")
    examples = instructions.get("example_question_sqls", [])
    example_ids = _check_ids(examples, "instructions.example_question_sqls", errors)
    for item in examples:
        if not _non_empty_strings(item.get("question")) or not _non_empty_strings(item.get("sql")):
            errors.append("each example needs question and sql string lists")

    questions = space.get("benchmarks", {}).get("questions", [])
    benchmark_ids = _check_ids(questions, "benchmarks.questions", errors)
    for item in questions:
        answers = item.get("answer", [])
        if len(answers) != 1 or answers[0].get("format") != "SQL":
            errors.append("each benchmark needs exactly one answer with format SQL")
        elif not _non_empty_strings(answers[0].get("content")):
            errors.append("benchmark answer content must be a non-empty list of strings")
        if not _non_empty_strings(item.get("question")) or len(item["question"]) != 1:
            errors.append("each benchmark needs exactly one question string")

    all_question_ids = sample_ids + benchmark_ids
    if len(set(all_question_ids)) != len(all_question_ids):
        errors.append("sample question and benchmark IDs must be unique across both collections")
    instruction_ids = text_ids + example_ids
    if len(set(instruction_ids)) != len(instruction_ids):
        errors.append("instruction IDs must be unique across instruction collections")

    _check_limits(space, "serialized_space", errors)
    found = placeholders_in(dict(space))
    if rendered and found:
        errors.append(f"unrendered placeholders remain: {sorted(found)}")
    if not rendered and found - set(TEMPLATE_PLACEHOLDERS):
        errors.append(
            f"unknown template placeholders: {sorted(found - set(TEMPLATE_PLACEHOLDERS))}"
        )
    return errors
