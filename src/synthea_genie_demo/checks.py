from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from . import DUAL_ELIGIBLE_SENTENCE, METRIC_VIEW_NAME, SIMULATED_DATES_SENTENCE
from .artifact import OFFICIAL_SHA256, OFFICIAL_SOURCE_URL
from .holdout import (
    HOLDOUT_PROBE_CLASSES,
    HOLDOUT_PROBES_PER_CLASS,
    HOLDOUT_QUESTIONS,
    HOLDOUT_RUNS,
    HOLDOUT_SCOPE,
    HOLDOUT_SUITE,
    HOLDOUT_TOP_KEYS,
    content_sha256,
    load_holdout,
)
from .jsonio import load_json
from .overlap import MAX_SHARED_PHRASES, boundary_example_words, shared_phrases
from .paths import (
    AGENT_SETTINGS_PATH,
    AGENT_TEMPLATE_PATH,
    ARTIFACT_LOCK_PATH,
    BENCHMARKS_PATH,
    BOUNDARY_PROBES_PATH,
    CANONICAL_SQL_PATH,
    EVALUATION_DIR,
    EXAMPLE_QUERIES_PATH,
    INSTRUCTIONS_PATH,
    PAYLOAD_HOLDOUT_DIGEST_PATH,
    PAYLOAD_HOLDOUT_PATH,
    REFERENCE_DIGEST_PATH,
    REPO_ROOT,
    SCHEMA_CONTRACT_PATH,
    SQL_DIR,
)
from .reference import (
    DIGEST_KEYS,
    DIMENSIONS,
    FREEZE_COMMAND,
    HOLDOUT_FREEZE_COMMAND,
    HOLDOUT_SPECS,
    INTENT_SPECS,
    ROUNDING,
    IntentSpec,
    intents_digest,
    measure_alias,
)
from .render import (
    HEX32_RE,
    TEMPLATE_SOURCES,
    canonical_sql_text,
    instruction_text,
    intent_sql,
    parse_example_queries,
    render_space,
    semantic_sql,
    template_text,
    validate_serialized_space,
)
from .scan import Scanner
from .sqltools import (
    SQL_FILES,
    find_dynamic_dates,
    has_order_by,
    render_sql,
    select_columns,
    split_statements,
    sql_parameters,
    sql_placeholders,
    strip_comments,
    strip_strings,
)

TEST_CATALOG, TEST_SCHEMA, TEST_RUN_LABEL = "demo_catalog", "synthea_hc_ops", "demo-run"

PROBE_CATEGORIES = {
    "identifiers",
    "medical_advice",
    "diagnosis",
    "causal_claim",
    "real_population_comparison",
    "ambiguous_date",
    "best_payer",
    "worst_facility_or_real_hospital",
}

PROBE_BEHAVIORS = {"refuse", "safe_redirect", "clarify"}

RAW_SOURCE_COLUMNS = (
    "ORGANIZATION",
    "PROVIDER",
    "DESCRIPTION",
    "REASONCODE",
    "REASONDESCRIPTION",
    "NAME",
    "ADDRESS",
)

HIDDEN_IDENTIFIERS = ("patient_id", "payer_id", "encounter_id")

STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "did",
    "each",
    "every",
    "for",
    "from",
    "give",
    "how",
    "in",
    "me",
    "of",
    "show",
    "the",
    "there",
    "through",
    "to",
    "was",
    "were",
    "what",
    "which",
    "within",
}


def contract() -> dict[str, Any]:
    data: dict[str, Any] = load_json(SCHEMA_CONTRACT_PATH)
    return data


def lock() -> dict[str, Any]:
    data: dict[str, Any] = load_json(ARTIFACT_LOCK_PATH)
    return data


def benchmarks() -> dict[str, Any]:
    data: dict[str, Any] = load_json(BENCHMARKS_PATH)
    return data


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def jaccard(a: str, b: str) -> float:
    left, right = words(a), words(b)
    return len(left & right) / len(left | right) if left | right else 1.0


def normalize_question(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def check_artifact_lock(
    lock_data: Mapping[str, Any], contract_data: Mapping[str, Any]
) -> list[str]:
    problems: list[str] = []
    artifact = lock_data.get("artifact", {})
    if artifact.get("source_url") != OFFICIAL_SOURCE_URL:
        problems.append("artifact.source_url must be the official Synthea 'latest' CSV URL")
    if artifact.get("sha256") != OFFICIAL_SHA256:
        problems.append("artifact.sha256 must equal the pinned official digest")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(artifact.get("retrieved_on", ""))):
        problems.append("artifact.retrieved_on must be an ISO date")
    if "commit" in str(artifact).lower() and "no claim" not in str(artifact).lower():
        problems.append("the lock must not claim a generating commit")
    members = lock_data.get("used_members", {})
    if sorted(members) != ["encounters.csv", "payers.csv"]:
        problems.append("used_members must be exactly encounters.csv and payers.csv")
    for name, member in members.items():
        required = contract_data["sources"][name]["required_columns"]
        missing = [c for c in required if c not in member.get("header", [])]
        if missing:
            problems.append(f"{name} header lacks required columns {missing}")
        if not re.match(r"^[0-9a-f]{64}$", str(member.get("sha256", ""))):
            problems.append(f"{name} sha256 is not a hex digest")
    names = lock_data.get("expected_member_names", [])
    if names != sorted(names) or not set(members) <= set(names):
        problems.append("expected_member_names must be sorted and include the used members")
    observed = lock_data.get("observed", {})
    enc = observed.get("encounters", {})
    payers = observed.get("payers", {})
    mapping = contract_data["payer_mapping"]
    if enc.get("row_count") != members.get("encounters.csv", {}).get("row_count"):
        problems.append("observed encounter row_count disagrees with used_members")
    if payers.get("row_count") != members.get("payers.csv", {}).get("row_count"):
        problems.append("observed payer row_count disagrees with used_members")
    if sum(enc.get("encounter_class_counts", {}).values()) != enc.get("row_count"):
        problems.append("encounter_class_counts do not sum to row_count")
    by_category = enc.get("rows_by_payer_category", {})
    if sum(by_category.values()) != enc.get("row_count"):
        problems.append("rows_by_payer_category do not sum to row_count")
    if not set(by_category) <= set(contract_data["payer_categories"]):
        problems.append("rows_by_payer_category uses unknown categories")
    for year, counts in enc.get("rows_by_year_and_payer_category", {}).items():
        if sum(counts.values()) != enc.get("rows_by_year", {}).get(year):
            problems.append(f"rows_by_year_and_payer_category[{year}] does not sum to rows_by_year")
    if payers.get("public_program_names") != sorted(mapping["public_program_labels"]):
        problems.append("observed public program names differ from the contract")
    if payers.get("private_name_count") != mapping["private_name_count"]:
        problems.append("observed private payer count differs from the contract")
    if str(enc.get("start_date_max", "9999")) > "2026-08-18":
        problems.append("encounter dates must not exceed the 2026-08-18 simulation reference date")
    return problems


def check_schema_contract(contract_data: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    mapping = contract_data["payer_mapping"]
    expected_public = {
        "Medicare": "Medicare",
        "Medicaid": "Medicaid",
        "Dual Eligible": "Dual Eligible",
        "NO_INSURANCE": "No Insurance",
    }
    if dict(mapping["public_program_labels"]) != expected_public:
        problems.append("public program labels must be exactly the four approved mappings")
    if mapping["private_label"] != "Private Insurance":
        problems.append("private label must be 'Private Insurance'")
    hashes = mapping["private_name_sha256"]
    if len(hashes) != 6 or len(set(hashes)) != 6 or hashes != sorted(hashes):
        problems.append("private_name_sha256 must hold six sorted unique digests")
    if mapping["unknown_policy"] != "fail":
        problems.append("unknown payer policy must be 'fail'")
    view = contract_data["metric_view"]
    if view["name"] != METRIC_VIEW_NAME:
        problems.append("metric view name must be encounter_operations_metrics")
    dims = [d["name"] for d in view["dimensions"]]
    if dims != ["Encounter Date", "Quarter", "Year", "Class", "Payer Category"]:
        problems.append(f"unexpected metric view dimensions {dims}")
    expected_measures = {
        "Encounter Count": "COUNT(1)",
        "Unique Patient Count": "COUNT(DISTINCT source.patient_id)",
        "Total Claim Cost": "SUM(source.total_claim_cost)",
        "Total Payer Coverage": "SUM(source.payer_coverage)",
        "Claim Cost Not Covered by Payer": (
            "SUM(source.total_claim_cost) - SUM(source.payer_coverage)"
        ),
        "Coverage Rate": "SUM(source.payer_coverage) / NULLIF(SUM(source.total_claim_cost), 0)",
        "Average Claim Cost": "AVG(source.total_claim_cost)",
        "Encounters per Patient": "COUNT(1) / NULLIF(COUNT(DISTINCT source.patient_id), 0)",
    }
    measures = {m["name"]: m["expr"] for m in view["measures"]}
    if measures != expected_measures:
        problems.append("metric view measures differ from the approved definitions")
    if sorted(measures) != sorted(ROUNDING) or tuple(dims) != DIMENSIONS:
        problems.append("metric view fields differ from the benchmark reference vocabulary")
    return problems


def _test_params(lock_data: Mapping[str, Any], contract_data: Mapping[str, Any]) -> dict[str, str]:
    return sql_parameters(
        lock_data,
        contract_data,
        catalog=TEST_CATALOG,
        schema=TEST_SCHEMA,
        run_label=TEST_RUN_LABEL,
    )


def _metric_view_yaml(contract_data: Mapping[str, Any], lock_data: Mapping[str, Any]) -> Any:
    text = (SQL_DIR / "30_metric_view.sql").read_text(encoding="utf-8")
    params = _test_params(lock_data, contract_data)
    rendered = render_sql(text, params)
    match = re.search(r"\$\$(.*)\$\$", rendered, flags=re.DOTALL)
    if not match:
        raise ValueError("30_metric_view.sql has no $$ YAML body")
    return yaml.safe_load(match.group(1))


def check_metric_view(contract_data: Mapping[str, Any], lock_data: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    spec = contract_data["metric_view"]
    doc = _metric_view_yaml(contract_data, lock_data)
    if str(doc.get("version")) != "1.1":
        problems.append("metric view YAML version must be 1.1")
    if doc.get("source") != f"{TEST_CATALOG}.{TEST_SCHEMA}.{spec['source_table']}":
        problems.append("metric view source must be the clean encounters table")
    joins = doc.get("joins", [])
    if len(joins) != 1:
        problems.append("metric view must have exactly one join")
    else:
        join = joins[0]
        if join.get("source") != f"{TEST_CATALOG}.{TEST_SCHEMA}.{spec['join']['table']}":
            problems.append("join must target payer_categories")
        if join.get("on") != spec["join"]["on"] or join.get("name") != spec["join"]["name"]:
            problems.append("join name/condition differs from the contract")
        if join.get("cardinality", "many_to_one") != "many_to_one":
            problems.append("payer join must be many-to-one")
    fields = [(f["name"], f["expr"]) for f in doc.get("fields", [])]
    if fields != [(d["name"], d["expr"]) for d in spec["dimensions"]]:
        problems.append("metric view fields differ from contract dimensions")
    measures = [(m["name"], m["expr"]) for m in doc.get("measures", [])]
    if measures != [(m["name"], m["expr"]) for m in spec["measures"]]:
        problems.append("metric view measures differ from the contract")
    for item in doc.get("fields", []):
        if any(h in item["expr"] for h in spec["hidden_columns"]):
            problems.append(f"field {item['name']} exposes a hidden identifier column")
    display = {m["name"]: m.get("display_name") for m in doc.get("measures", [])}
    for m in spec["measures"]:
        if "display_name" in m and display.get(m["name"]) != m["display_name"]:
            problems.append(f"measure {m['name']} display_name differs from the contract")
    blob = yaml.safe_dump(doc).lower()
    for term in spec["banned_measure_terms"]:
        if term in blob:
            problems.append(f"metric view YAML uses banned term {term!r}")
    for item in [*doc.get("fields", []), *doc.get("measures", [])]:
        if len(item.get("synonyms", [])) > 10:
            problems.append(f"{item['name']} has more than 10 synonyms")
    return problems


def check_generated_files() -> list[str]:
    problems: list[str] = []
    if AGENT_TEMPLATE_PATH.read_text(encoding="utf-8") != template_text():
        problems.append(
            "Agent template is stale; run scripts/render_agent_config.py --write-template"
        )
    if CANONICAL_SQL_PATH.read_text(encoding="utf-8") != canonical_sql_text():
        problems.append(
            "Canonical SQL is stale; run scripts/render_agent_config.py --write-template"
        )
    return problems


def check_template() -> list[str]:
    template = load_json(AGENT_TEMPLATE_PATH)
    problems = validate_serialized_space(template["serialized_space"], rendered=False)
    try:
        render_space(template, TEST_CATALOG, TEST_SCHEMA)
    except ValueError as exc:
        problems.append(str(exc))
    space = template["serialized_space"]
    bench = benchmarks()
    if len(space["benchmarks"]["questions"]) != len(bench["questions"]):
        problems.append("template benchmark count differs from evaluation/benchmarks.json")
    if len(space["config"]["sample_questions"]) != 3:
        problems.append("exactly three landing sample questions are required")
    if len(space["instructions"]["example_question_sqls"]) < 3:
        problems.append("at least three example SQL patterns are required")
    return problems


def check_benchmarks(bench: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if not bench.get("frozen") or bench.get("mode") != "Chat":
        problems.append("benchmark suite must be frozen and Chat mode")
    intents = bench["intents"]
    ids = [i["id"] for i in intents]
    if len(intents) != 7 or len(set(ids)) != 7:
        problems.append("exactly seven unique intents are required")
    if sorted(ids) != sorted(INTENT_SPECS):
        problems.append("intent IDs differ from the offline reference specs")
    questions = bench["questions"]
    if len(questions) != 14:
        problems.append("exactly 14 benchmark questions are required")
    keys = [q["key"] for q in questions]
    if len(set(keys)) != len(keys):
        problems.append("benchmark question keys must be unique")
    normalized = [normalize_question(q["question"]) for q in questions]
    if len(set(normalized)) != len(normalized):
        problems.append("benchmark questions must be unique")
    by_intent: dict[str, list[str]] = {}
    for q in questions:
        if q["intent_id"] not in ids:
            problems.append(f"question {q['key']} points to unknown intent")
        by_intent.setdefault(q["intent_id"], []).append(q["question"])
    for intent_id in ids:
        phrasings = by_intent.get(intent_id, [])
        if len(phrasings) != 2:
            problems.append(f"intent {intent_id} needs exactly two phrasings")
        elif jaccard(*phrasings) >= 0.6:
            problems.append(f"intent {intent_id} phrasings are not materially different")
    allowed_years = {str(y) for y in bench["benchmark_years"]}
    for intent in intents:
        sql = intent_sql(intent)
        label = intent["id"]
        if find_dynamic_dates(sql):
            problems.append(f"{label} uses relative dates {find_dynamic_dates(sql)}")
        if not has_order_by(sql):
            problems.append(f"{label} lacks ORDER BY")
        if re.search(r"select\s+\*|\blimit\b", strip_comments(sql), flags=re.IGNORECASE):
            problems.append(f"{label} must not use SELECT * or LIMIT")
        years = set(re.findall(r"\b(20\d\d)\b", sql))
        if not years or not years <= allowed_years:
            problems.append(f"{label} must filter on fixed benchmark years only")
        if bench["metric_view_placeholder"] not in sql:
            problems.append(f"{label} must query the metric view placeholder")
        try:
            columns = select_columns(sql)
        except ValueError as exc:
            problems.append(f"{label} is not a single SELECT ... FROM statement: {exc}")
            continue
        if columns != intent["expected_columns"]:
            problems.append(f"{label} columns {columns} differ from expected_columns")
        spec = INTENT_SPECS.get(label)
        if spec and spec.columns != intent["expected_columns"]:
            problems.append(f"{label} expected_columns differ from the offline reference spec")
        order = re.search(
            r"order\s+by\s+(.+)$", strip_comments(sql), flags=re.IGNORECASE | re.DOTALL
        )
        if spec and order:
            ordered = [c.strip().strip("`") for c in order.group(1).split(",")]
            if ordered != list(spec.dimensions):
                problems.append(f"{label} must order by every grouping column")
        problems.extend(check_intent_semantics(intent, bench))
    return problems


SEMANTIC_KEYS = {"dimensions", "measures", "years"}

SEMANTIC_MEASURE_KEYS = {"alias", "measure", "round"}


def check_intent_semantics(
    intent: Mapping[str, Any],
    bench: Mapping[str, Any],
    *,
    specs: Mapping[str, IntentSpec] = INTENT_SPECS,
    years_key: str = "benchmark_years",
) -> list[str]:
    """One source of truth: canonical_sql must be generated from the structured semantics,
    and the semantics must equal the independent offline reference spec."""
    label = intent["id"]
    semantics = intent.get("semantics")
    if not isinstance(semantics, dict) or set(semantics) != SEMANTIC_KEYS:
        return [f"{label} needs semantics with exactly {sorted(SEMANTIC_KEYS)}"]
    problems: list[str] = []
    dims, measures, years = semantics["dimensions"], semantics["measures"], semantics["years"]
    if not dims or not all(d in DIMENSIONS for d in dims) or len(set(dims)) != len(dims):
        problems.append(f"{label} semantics dimensions must be distinct Metric View fields")
    if not measures:
        problems.append(f"{label} semantics need at least one measure")
    for m in measures:
        if not isinstance(m, dict) or set(m) != SEMANTIC_MEASURE_KEYS:
            problems.append(
                f"{label} semantic measures need exactly {sorted(SEMANTIC_MEASURE_KEYS)}"
            )
            return problems
        if m["measure"] not in ROUNDING:
            problems.append(f"{label} uses unknown measure {m['measure']!r}")
            continue
        if m["alias"] != measure_alias(m["measure"]):
            problems.append(
                f"{label} alias {m['alias']!r} does not name measure {m['measure']!r} "
                f"(expected {measure_alias(m['measure'])!r})"
            )
        if m["round"] != ROUNDING[m["measure"]] or isinstance(m["round"], bool):
            problems.append(f"{label} {m['measure']} must round to {ROUNDING[m['measure']]}")
    if (
        not years
        or not all(type(y) is int for y in years)
        or not set(years) <= set(bench[years_key])
        or years != sorted(set(years))
    ):
        problems.append(f"{label} semantics years must be sorted, fixed suite years")
    if problems:
        return problems
    if intent["canonical_sql"] != semantic_sql(semantics, bench["metric_view_placeholder"]):
        problems.append(f"{label} canonical_sql is not the SQL generated from its semantics")
    if intent["expected_columns"] != [*dims, *(m["alias"] for m in measures)]:
        problems.append(f"{label} expected_columns differ from its semantics")
    spec = specs.get(label)
    if spec is None or semantics != spec.semantics():
        problems.append(f"{label} semantics differ from the independent offline reference spec")
    return problems


def check_suite_versions(bench: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    history = bench.get("version_history", [])
    versions = [h.get("suite_version") for h in history]
    if (
        not versions
        or versions[-1] != bench["suite_version"]
        or len(set(versions)) != len(versions)
    ):
        problems.append("version_history must end with the current suite_version, once each")
    published = [h for h in history if h.get("published")]
    if published:
        problems.append(
            "a published suite version must be kept as a separate frozen file, not edited here"
        )
    rubric = (EVALUATION_DIR / "rubric.md").read_text(encoding="utf-8")
    if f"suite {bench['suite_version']}" not in rubric:
        problems.append(f"evaluation/rubric.md must name benchmark suite {bench['suite_version']}")
    template = load_json(AGENT_TEMPLATE_PATH)
    if template["_meta"]["benchmark_suite_version"] != bench["suite_version"]:
        problems.append("agent template was generated from a different suite version")
    return problems


def check_reference_digest(bench: Mapping[str, Any], lock_data: Mapping[str, Any]) -> list[str]:
    """The committed frozen expected-result digest must belong to this archive and suite."""
    digest = load_json(REFERENCE_DIGEST_PATH)
    problems: list[str] = []
    if sorted(digest) != sorted(DIGEST_KEYS):
        return [f"reference digest must have exactly the keys {sorted(DIGEST_KEYS)}"]
    if digest.get("generated_by") != FREEZE_COMMAND:
        problems.append(f"reference digest must name the command that froze it: {FREEZE_COMMAND}")
    if digest.get("artifact_sha256") != lock_data["artifact"]["sha256"]:
        problems.append("reference digest was frozen from a different archive")
    if digest.get("benchmark_intents_sha256") != intents_digest(bench["intents"]):
        problems.append(
            "reference digest was frozen for different intent semantics; refreeze with "
            "uv run python scripts/run_benchmarks.py --reference --freeze-digest"
        )
    frozen = digest.get("intents", {})
    if sorted(frozen) != sorted(INTENT_SPECS):
        problems.append("reference digest must cover exactly the benchmark intents")
    for intent, entry in frozen.items():
        if set(entry) != {"rows", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            problems.append(f"reference digest entry {intent} is malformed")
    return problems


def _uses_held_out_shape(sql: str, held_out: list[Mapping[str, Any]]) -> list[str]:
    hits = []
    for shape in held_out:
        measures = shape.get("measures", [])
        if any(f"`{m}`" in sql for m in measures):
            hits.append(shape["id"])
        together = shape.get("dimensions_together", [])
        if together and all(f"`{d}`" in sql for d in together):
            hits.append(shape["id"])
    return hits


def check_examples_and_samples(bench: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    examples = parse_example_queries(EXAMPLE_QUERIES_PATH.read_text(encoding="utf-8"))
    settings = load_json(AGENT_SETTINGS_PATH)
    samples = [s["question"] for s in settings["sample_questions"]]
    bench_questions = [q["question"] for q in bench["questions"]]
    bench_norm = {normalize_question(q) for q in bench_questions}
    held_out = bench["held_out_shapes"]
    if len(held_out) < 2:
        problems.append("at least two query shapes must be held out of the examples")
    if len(examples) < 3:
        problems.append("at least three example SQL patterns are required")
    example_years = {str(y) for y in bench["example_years"]}
    canonical = {normalize_question(intent_sql(i)) for i in bench["intents"]}
    for ex in examples:
        years = set(re.findall(r"\b(20\d\d)\b", ex.question + " " + ex.sql))
        if years != example_years:
            problems.append(f"example {ex.key} must use only {sorted(example_years)}")
        if find_dynamic_dates(ex.sql):
            problems.append(f"example {ex.key} uses relative dates")
        if not has_order_by(ex.sql):
            problems.append(f"example {ex.key} lacks ORDER BY")
        if hits := _uses_held_out_shape(ex.sql, held_out):
            problems.append(f"example {ex.key} uses held-out shapes {hits}")
        if normalize_question(ex.question) in bench_norm:
            problems.append(f"example {ex.key} repeats a benchmark question")
        if normalize_question(ex.sql) in canonical:
            problems.append(f"example {ex.key} repeats a canonical benchmark answer")
        for bq in bench_questions:
            if jaccard(ex.question, bq) >= 0.5:
                problems.append(f"example {ex.key} is too close to benchmark question {bq!r}")
    if len(samples) != 3:
        problems.append("exactly three landing sample questions are required")
    other = bench_norm | {normalize_question(e.question) for e in examples}
    for sample in samples:
        if normalize_question(sample) in other:
            problems.append(f"sample question {sample!r} overlaps a benchmark or example")
        if re.search(r"\b20(2[4-5])\b", sample):
            problems.append(f"sample question {sample!r} must not use benchmark years")
    return problems


def check_agent_text(contract_data: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    text = instruction_text(INSTRUCTIONS_PATH.read_text(encoding="utf-8"))
    for sentence in (SIMULATED_DATES_SENTENCE, DUAL_ELIGIBLE_SENTENCE):
        if sentence not in text:
            problems.append(f"instructions must include: {sentence!r}")
    template = AGENT_TEMPLATE_PATH.read_text(encoding="utf-8").lower()
    for term in contract_data["metric_view"]["banned_measure_terms"]:
        if term in template:
            problems.append(f"agent configuration uses banned term {term!r}")
    space_text = str(load_json(AGENT_TEMPLATE_PATH)["serialized_space"])
    for identifier in HIDDEN_IDENTIFIERS:
        if identifier in space_text:
            problems.append(f"agent configuration references hidden identifier {identifier}")
    if "not enforcement" not in INSTRUCTIONS_PATH.read_text(encoding="utf-8"):
        problems.append("instructions.md must describe instructions as guidance, not enforcement")
    return problems


ALLOWED_SQL_PLACEHOLDERS = {
    "catalog",
    "schema",
    "volume",
    "run_label",
    "expected_encounter_rows",
    "expected_distinct_patients",
    "expected_payer_rows",
    "expected_total_claim_cost",
    "expected_total_payer_coverage",
    "expected_max_encounter_date",
    "simulation_reference_date",
    "payer_category_list",
}


def check_sql_static(contract_data: Mapping[str, Any], lock_data: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    params = _test_params(lock_data, contract_data)
    for name in SQL_FILES:
        path = SQL_DIR / name
        text = path.read_text(encoding="utf-8")
        unknown = sql_placeholders(path) - ALLOWED_SQL_PLACEHOLDERS
        if unknown:
            problems.append(f"{name} uses unknown placeholders {sorted(unknown)}")
        if "runtime validation required" not in text.lower():
            problems.append(f"{name} must state that runtime validation is required")
        if found := find_dynamic_dates(text):
            problems.append(f"{name} uses relative dates {found}")
        body = strip_comments(text).upper()
        for forbidden in ("CASCADE", "DROP CATALOG", "CREATE CATALOG", "WAREHOUSE"):
            if forbidden in body:
                problems.append(f"{name} must not use {forbidden}")
        statements = split_statements(render_sql(text, params))
        if not statements:
            problems.append(f"{name} has no statements")
        if name == "20_clean.sql":
            code = strip_strings(text).upper()
            for column in RAW_SOURCE_COLUMNS:
                if re.search(rf"\b{column}\b", code):
                    problems.append(f"{name} references raw source column {column}")
        if name in {"10_bronze.sql", "20_clean.sql"} and "ASSERT_TRUE" not in body:
            problems.append(f"{name} must contain fail-closed assert_true gates")
        if name == "90_cleanup.sql":
            for raw in statements:
                statement = " ".join(strip_comments(raw).split())
                if not re.match(r"^DROP (VIEW|TABLE|VOLUME|SCHEMA) IF EXISTS ", statement.upper()):
                    problems.append(f"90_cleanup.sql has a non-drop statement: {statement[:60]}")
                if not statement.split()[-1].startswith(f"{TEST_CATALOG}.{TEST_SCHEMA}"):
                    problems.append("90_cleanup.sql may only drop objects in the project schema")
    return problems


def check_boundary_probes(bench: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    data = load_json(BOUNDARY_PROBES_PATH)
    probes = data["probes"]
    ids = [p["id"] for p in probes]
    if len(set(ids)) != len(ids):
        problems.append("boundary probe IDs must be unique")
    categories = {p["category"] for p in probes}
    if missing := PROBE_CATEGORIES - categories:
        problems.append(f"boundary probes missing categories {sorted(missing)}")
    bench_norm = {normalize_question(q["question"]) for q in bench["questions"]}
    for probe in probes:
        if probe["expected_behavior"] not in PROBE_BEHAVIORS:
            problems.append(f"probe {probe['id']} has an unknown expected_behavior")
        if normalize_question(probe["prompt"]) in bench_norm:
            problems.append(f"probe {probe['id']} duplicates a benchmark question")
        if not probe.get("must_not"):
            problems.append(f"probe {probe['id']} must list must_not behaviors")
        problems += sql_policy_problems(str(probe["id"]), probe)
    return problems


NO_SQL_BEHAVIORS = ("refuse", "clarify")


def sql_policy_problems(label: str, probe: Mapping[str, Any]) -> list[str]:
    """A probe's explicit sql_allowed policy: a boolean, and false for refuse or clarify."""
    allowed = probe.get("sql_allowed")
    if not isinstance(allowed, bool):
        return [f"probe {label} needs an explicit boolean sql_allowed"]
    if allowed and probe.get("expected_behavior") in NO_SQL_BEHAVIORS:
        return [f"probe {label} must refuse or clarify, so its sql_allowed must be false"]
    return []


HOLDOUT_INTENT_KEYS = {"id", "title", "expected_columns", "semantics", "canonical_sql"}

HOLDOUT_QUESTION_KEYS = {"key", "intent_id", "question"}

HOLDOUT_PROBE_KEYS = {
    "id",
    "category",
    "prompt",
    "expected_behavior",
    "sql_allowed",
    "acceptable",
    "must_not",
}

HOLDOUT_HISTORY_KEYS = {"suite_version", "frozen_on", "published", "content_sha256", "summary"}

HOLDOUT_KEY_RE = re.compile(r"ph[0-9]+")

HOLDOUT_PROBE_ID_RE = re.compile(r"phb[0-9]+-[a-z0-9-]+")


def payload_holdout() -> dict[str, Any]:
    return load_holdout(PAYLOAD_HOLDOUT_PATH)


def _sendable(text: Any) -> bool:
    return (
        isinstance(text, str)
        and bool(text)
        and text == text.strip()
        and not text.startswith("-")
        and "\x00" not in text
    )


def _holdout_version_problems(suite: Mapping[str, Any]) -> list[str]:
    history = suite["version_history"]
    if not isinstance(history, list) or not history:
        return ["payload-holdout version_history must be a non-empty list"]
    problems: list[str] = []
    if any(not isinstance(h, dict) or set(h) != HOLDOUT_HISTORY_KEYS for h in history):
        return [f"each payload-holdout version entry needs exactly {sorted(HOLDOUT_HISTORY_KEYS)}"]
    versions = [h["suite_version"] for h in history]
    if versions[-1] != suite["suite_version"] or len(set(versions)) != len(versions):
        problems.append("payload-holdout version_history must end with the current version, once")
    if any(h["published"] is not False for h in history):
        problems.append("a published payload-holdout version must be kept as a separate file")
    if history[-1]["content_sha256"] != content_sha256(suite):
        problems.append(
            "payload-holdout content differs from its recorded content_sha256; any change "
            f"needs a new suite_version with the new hash {content_sha256(suite)}"
        )
    return problems


def check_payload_holdout(suite: Mapping[str, Any], bench: Mapping[str, Any]) -> list[str]:
    """The payload-held-out suite's schema, version hash, semantics, SQL scope and probes."""
    if not isinstance(suite, dict) or set(suite) != HOLDOUT_TOP_KEYS:
        return [f"payload-holdout must have exactly the keys {sorted(HOLDOUT_TOP_KEYS)}"]
    problems: list[str] = []
    if (
        suite["suite"] != HOLDOUT_SUITE
        or suite["frozen"] is not True
        or suite["mode"] != "Chat"
        or suite["scope"] != HOLDOUT_SCOPE
        or not _is_exact_int(suite["analytical_runs"], HOLDOUT_RUNS)
        or suite["metric_view_placeholder"] != bench["metric_view_placeholder"]
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(suite["frozen_on"]))
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(suite["suite_version"]))
    ):
        problems.append(
            f"payload-holdout must be the frozen Chat-mode suite {HOLDOUT_SUITE}, asked "
            f"{HOLDOUT_RUNS} times, on the benchmark Metric View placeholder"
        )
    years = suite["years"]
    if (
        not isinstance(years, list)
        or not years
        or not all(type(y) is int for y in years)
        or years != sorted(set(years))
        or not set(years) <= set(bench["benchmark_years"])
    ):
        problems.append("payload-holdout years must be sorted, fixed benchmark calendar years")
        return problems
    problems += _holdout_version_problems(suite)

    intents, questions, probes = suite["intents"], suite["questions"], suite["boundary_probes"]
    if not all(isinstance(x, list) for x in (intents, questions, probes)):
        return [*problems, "payload-holdout intents, questions and probes must be lists"]
    if [i.get("id") if isinstance(i, dict) else None for i in intents] != list(HOLDOUT_SPECS):
        problems.append("payload-holdout intents must be exactly the HOLDOUT_SPECS, in order")
        return problems
    for intent in intents:
        label = intent["id"]
        if set(intent) != HOLDOUT_INTENT_KEYS:
            problems.append(f"{label} needs exactly {sorted(HOLDOUT_INTENT_KEYS)}")
            continue
        semantic = check_intent_semantics(intent, suite, specs=HOLDOUT_SPECS, years_key="years")
        problems += semantic
        if semantic:
            continue
        sql = intent_sql(intent)
        body = strip_comments(sql)
        if find_dynamic_dates(sql) or re.search(r"select\s+\*|\blimit\b", body, re.IGNORECASE):
            problems.append(f"{label} must use fixed years, no SELECT * and no LIMIT")
        if not has_order_by(sql) or select_columns(sql) != intent["expected_columns"]:
            problems.append(f"{label} must select its expected columns and order its result")
        if any(identifier in sql for identifier in HIDDEN_IDENTIFIERS):
            problems.append(f"{label} references a hidden identifier")

    if len(questions) != HOLDOUT_QUESTIONS:
        problems.append(f"payload-holdout needs exactly {HOLDOUT_QUESTIONS} analytical questions")
    keys = [q.get("key") if isinstance(q, dict) else None for q in questions]
    if len(set(keys)) != len(keys):
        problems.append("payload-holdout question keys must be unique")
    asked = []
    for q in questions:
        if not isinstance(q, dict) or set(q) != HOLDOUT_QUESTION_KEYS:
            problems.append(f"each payload-holdout question needs exactly {HOLDOUT_QUESTION_KEYS}")
            continue
        if not HOLDOUT_KEY_RE.fullmatch(str(q["key"])) or not _sendable(q["question"]):
            problems.append(f"payload-holdout question {q['key']!r} has a bad key or text")
        asked.append(q["intent_id"])
    if sorted(asked) != sorted(HOLDOUT_SPECS):
        problems.append("each payload-holdout intent needs exactly one question")

    if len(probes) != HOLDOUT_PROBES_PER_CLASS * len(HOLDOUT_PROBE_CLASSES):
        problems.append("payload-holdout needs exactly two paraphrases per boundary class")
    ids = [p.get("id") if isinstance(p, dict) else None for p in probes]
    if len(set(ids)) != len(ids):
        problems.append("payload-holdout probe IDs must be unique")
    by_class: dict[str, int] = {}
    for p in probes:
        if not isinstance(p, dict) or set(p) != HOLDOUT_PROBE_KEYS:
            problems.append(
                f"each payload-holdout probe needs exactly {sorted(HOLDOUT_PROBE_KEYS)}"
            )
            continue
        label = str(p["id"])
        if not HOLDOUT_PROBE_ID_RE.fullmatch(label) or not _sendable(p["prompt"]):
            problems.append(f"payload-holdout probe {label!r} has a bad ID or prompt")
        expected = HOLDOUT_PROBE_CLASSES.get(p["category"])
        if expected is None or p["expected_behavior"] != expected:
            problems.append(
                f"{label}: category must be one of {sorted(HOLDOUT_PROBE_CLASSES)} with its "
                "fixed expected behavior"
            )
        by_class[p["category"]] = by_class.get(p["category"], 0) + 1
        must_not = p["must_not"]
        if (
            not isinstance(must_not, list)
            or not must_not
            or not all(isinstance(m, str) and m for m in must_not)
            or not isinstance(p["acceptable"], str)
            or not p["acceptable"]
        ):
            problems.append(f"{label} needs acceptable text and must_not behaviors")
        problems += sql_policy_problems(label, p)
    if by_class != dict.fromkeys(HOLDOUT_PROBE_CLASSES, HOLDOUT_PROBES_PER_CLASS):
        problems.append("payload-holdout needs exactly two paraphrases per boundary class")
    regression_ids = {q["key"] for q in bench["questions"]} | {
        p["id"] for p in load_json(BOUNDARY_PROBES_PATH)["probes"]
    }
    if regression_ids & {*keys, *ids}:
        problems.append("payload-holdout item IDs must differ from every regression item ID")
    problems += holdout_phrase_overlap(suite, bench)
    return problems


def holdout_phrase_overlap(suite: Mapping[str, Any], bench: Mapping[str, Any]) -> list[str]:
    """Paraphrases that reuse a distinctive phrase from instructions, probes or benchmarks."""
    instructions = instruction_text(INSTRUCTIONS_PATH.read_text(encoding="utf-8"))
    corpus = [
        instructions,
        *(p["prompt"] for p in load_json(BOUNDARY_PROBES_PATH)["probes"]),
        *(q["question"] for q in bench["questions"]),
    ]
    examples = boundary_example_words(instructions)
    if not examples:
        return ["the instructions' Boundaries section lists no parenthetical examples to check"]
    problems: list[str] = []
    for p in suite["boundary_probes"]:
        if not isinstance(p, dict) or not isinstance(p.get("prompt"), str):
            continue
        shared = shared_phrases(p["prompt"], corpus, examples)
        if len(shared) > MAX_SHARED_PHRASES:
            problems.append(
                f"{p.get('id')} reuses distinctive wording from the instructions, regression "
                f"probes or benchmarks: {shared}"
            )
    return problems


def _is_exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def _payload_strings(value: Any) -> list[str]:
    """Every string in a serialized space, plus each list of strings joined as one text."""
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            if item and all(isinstance(x, str) for x in item):
                found.extend(("".join(item), " ".join(item)))
            for child in item:
                walk(child)
        elif isinstance(item, str):
            found.append(item)

    walk(value)
    return [_squash(s) for s in found]


def holdout_payload_leaks(
    suite: Mapping[str, Any], template: Mapping[str, Any], template_raw: str
) -> list[str]:
    """Every holdout question, paraphrase or canonical SQL found in the Agent payload."""
    problems: list[str] = []
    rendered = render_space(template, TEST_CATALOG, TEST_SCHEMA)
    template_strings = _payload_strings(template["serialized_space"])
    rendered_strings = _payload_strings(rendered)
    raw = _squash(template_raw)
    texts = [(q["key"], q["question"]) for q in suite["questions"]] + [
        (p["id"], p["prompt"]) for p in suite["boundary_probes"]
    ]
    for label, text in texts:
        needle = _squash(text)
        normalized = normalize_question(text)
        if (
            needle in raw
            or any(needle in s for s in template_strings)
            or any(normalize_question(s) == normalized for s in template_strings)
        ):
            problems.append(f"payload-holdout item {label} appears in the Agent payload")
    placeholder = suite["metric_view_placeholder"]
    concrete = f"{TEST_CATALOG}.{TEST_SCHEMA}.{METRIC_VIEW_NAME}"
    for intent in suite["intents"]:
        sql = " ".join(intent["canonical_sql"])
        as_template = _squash(sql)
        as_rendered = _squash(sql.replace(placeholder, concrete))
        if any(as_template in s for s in template_strings) or any(
            as_rendered in s for s in rendered_strings
        ):
            problems.append(f"payload-holdout SQL {intent['id']} appears in the Agent payload")
    return problems


def check_payload_holdout_exclusion(
    suite: Mapping[str, Any], bench: Mapping[str, Any]
) -> list[str]:
    """Held out of the payload, no exact duplicate, and no embedded result shape reused."""
    problems: list[str] = []
    if PAYLOAD_HOLDOUT_PATH in TEMPLATE_SOURCES:
        problems.append("evaluation/payload-holdout.json must not be an Agent template source")
    template = load_json(AGENT_TEMPLATE_PATH)
    sources = template["_meta"].get("sources_sha256", {})
    if any("payload-holdout" in name for name in sources):
        problems.append("the Agent template was generated from the payload-holdout suite")
    problems += holdout_payload_leaks(
        suite, template, AGENT_TEMPLATE_PATH.read_text(encoding="utf-8")
    )

    examples = parse_example_queries(EXAMPLE_QUERIES_PATH.read_text(encoding="utf-8"))
    settings = load_json(AGENT_SETTINGS_PATH)
    regression_probes = load_json(BOUNDARY_PROBES_PATH)["probes"]
    embedded = {
        normalize_question(text)
        for text in (
            *(q["question"] for q in bench["questions"]),
            *(s["question"] for s in settings["sample_questions"]),
            *(e.question for e in examples),
            *(p["prompt"] for p in regression_probes),
        )
    }
    held = [q["question"] for q in suite["questions"]] + [
        p["prompt"] for p in suite["boundary_probes"]
    ]
    normalized = [normalize_question(t) for t in held]
    if len(set(normalized)) != len(normalized):
        problems.append("payload-holdout texts must be distinct from each other")
    for text, norm in zip(held, normalized, strict=True):
        if norm in embedded:
            problems.append(f"payload-holdout text duplicates an embedded question: {text!r}")

    embedded_sql = {_squash(" ".join(i["canonical_sql"])) for i in bench["intents"]} | {
        _squash(e.sql) for e in examples
    }
    embedded_columns = [list(i["expected_columns"]) for i in bench["intents"]]
    for e in examples:
        try:
            embedded_columns.append(select_columns(e.sql))
        except ValueError:
            problems.append(f"example {e.key} is not a single SELECT statement")
    shapes = {_shape(i["semantics"]) for i in bench["intents"]}
    for intent in suite["intents"]:
        label = intent["id"]
        if _squash(" ".join(intent["canonical_sql"])) in embedded_sql:
            problems.append(f"{label} reuses an embedded canonical or example SQL")
        if list(intent["expected_columns"]) in embedded_columns:
            problems.append(f"{label} has the result columns of an embedded benchmark or example")
        if _shape(intent["semantics"]) in shapes:
            problems.append(f"{label} repeats an embedded dimension, measure and year combination")
    return problems


def _shape(semantics: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(semantics["dimensions"]),
        frozenset(m["measure"] for m in semantics["measures"]),
        tuple(semantics["years"]),
    )


def check_holdout_reference_digest(
    suite: Mapping[str, Any], lock_data: Mapping[str, Any]
) -> list[str]:
    """The committed payload-holdout reference digest belongs to this archive and suite."""
    digest = load_json(PAYLOAD_HOLDOUT_DIGEST_PATH)
    problems: list[str] = []
    if sorted(digest) != sorted(DIGEST_KEYS):
        return [f"payload-holdout digest must have exactly the keys {sorted(DIGEST_KEYS)}"]
    if digest.get("generated_by") != HOLDOUT_FREEZE_COMMAND:
        problems.append("payload-holdout digest must name the command that froze it")
    if digest.get("artifact_sha256") != lock_data["artifact"]["sha256"]:
        problems.append("payload-holdout digest was frozen from a different archive")
    if digest.get("benchmark_intents_sha256") != intents_digest(suite["intents"]):
        problems.append(
            f"payload-holdout digest was frozen for different semantics; refreeze with "
            f"{HOLDOUT_FREEZE_COMMAND}"
        )
    frozen = digest.get("intents", {})
    if list(frozen) != sorted(HOLDOUT_SPECS):
        problems.append("payload-holdout digest must cover exactly the holdout intents")
    for intent, entry in frozen.items():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"rows", "sha256"}
            or type(entry["rows"]) is not int
            or entry["rows"] < 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
        ):
            problems.append(f"payload-holdout digest entry {intent} is malformed or empty")
    return problems


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(markdown: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if match:
            slug = _slug(match.group(1).replace("`", ""))
            n = counts.get(slug, 0)
            anchors.add(slug if n == 0 else f"{slug}-{n}")
            counts[slug] = n + 1
    return anchors


def check_doc_links(root: Path = REPO_ROOT) -> list[str]:
    problems: list[str] = []
    link = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
    for path in sorted(root.rglob("*.md")):
        if any(part in {".venv", ".cache", ".git", ".pytest_cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        no_code = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in link.findall(no_code):
            if re.match(r"^[a-z]+:", target):
                continue
            file_part, _, anchor = target.partition("#")
            dest = path if not file_part else (path.parent / file_part).resolve()
            label = f"{path.relative_to(root)} -> {target}"
            if not dest.exists():
                problems.append(f"broken link {label}")
            elif (
                anchor and dest.suffix == ".md" and anchor not in _anchors(dest.read_text("utf-8"))
            ):
                problems.append(f"broken anchor {label}")
    return problems


def check_hex_ids_in_template() -> list[str]:
    space = load_json(AGENT_TEMPLATE_PATH)["serialized_space"]
    ids: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "id" in value:
                ids.append(value["id"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(space)
    bad = [i for i in ids if not HEX32_RE.match(str(i))]
    return [f"invalid id {i!r}" for i in bad] + (
        ["ids must be unique"] if len(set(ids)) != len(ids) else []
    )


def check_public_outputs(root: Path = REPO_ROOT, scanner: Scanner | None = None) -> list[str]:
    scanner = scanner or Scanner.from_contract(contract())
    return [str(finding) for finding in scanner.scan_tree(root)]


def run_offline_checks() -> dict[str, list[str]]:
    c, lk, b, h = contract(), lock(), benchmarks(), payload_holdout()
    checks: dict[str, Callable[[], list[str]]] = {
        "artifact lock": lambda: check_artifact_lock(lk, c),
        "schema contract": lambda: check_schema_contract(c),
        "metric definitions": lambda: check_metric_view(c, lk),
        "generated files": check_generated_files,
        "Agent configuration": check_template,
        "configuration IDs": check_hex_ids_in_template,
        "benchmark questions": lambda: check_benchmarks(b),
        "suite versions": lambda: check_suite_versions(b),
        "reference digest": lambda: check_reference_digest(b, lk),
        "examples": lambda: check_examples_and_samples(b),
        "Agent instructions": lambda: check_agent_text(c),
        "SQL": lambda: check_sql_static(c, lk),
        "boundary questions": lambda: check_boundary_probes(b),
        "additional questions": lambda: check_payload_holdout(h, b),
        "additional question isolation": lambda: check_payload_holdout_exclusion(h, b),
        "additional reference digest": lambda: check_holdout_reference_digest(h, lk),
        "documentation links": check_doc_links,
        "public files": check_public_outputs,
    }
    results = {}
    for label, check in checks.items():
        try:
            results[label] = check()
        except Exception as exc:
            results[label] = [f"{type(exc).__name__}: {exc}"]
    return results
