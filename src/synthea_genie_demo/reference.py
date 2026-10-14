"""Independent offline computation of the seven canonical answers and the four
payload-held-out answers (``HOLDOUT_SPECS``).

Used only to cross-check live Databricks results. Full result values go to .cache/reference
and are never committed; the repository commits only a per-intent digest
(evaluation/expected-results/reference-digest.json) so a local reference can be proven to
be the frozen one without publishing values before a live run is validated.

``INTENT_SPECS`` is a second, hand-written declaration of each intent's semantics. The
checks require it to equal the structured ``semantics`` in evaluation/benchmarks.json,
and the canonical SQL must equal the SQL generated from those semantics, so changing a
measure, rounding, filter or grouping in only one place fails.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .jsonio import sha256_text

DIMENSIONS = ("Encounter Date", "Quarter", "Year", "Class", "Payer Category")
TEXT_DIMENSIONS = {"Encounter Date", "Quarter", "Class", "Payer Category"}
INTEGER_DIMENSIONS = {"Year"}

# Canonical displayed precision per measure: None means an exact integer count.
ROUNDING: dict[str, int | None] = {
    "Encounter Count": None,
    "Unique Patient Count": None,
    "Total Claim Cost": 2,
    "Total Payer Coverage": 2,
    "Claim Cost Not Covered by Payer": 2,
    "Coverage Rate": 4,
    "Average Claim Cost": 2,
    "Encounters per Patient": 2,
}


@dataclass(frozen=True)
class EncounterRow:
    encounter_date: str
    encounter_class: str
    payer_category: str
    patient_id: str
    total_claim_cost: Decimal
    payer_coverage: Decimal

    @property
    def year(self) -> int:
        return int(self.encounter_date[:4])

    @property
    def quarter(self) -> str:
        return f"{self.encounter_date[:4]}-Q{(int(self.encounter_date[5:7]) - 1) // 3 + 1}"

    def dimension(self, name: str) -> Any:
        return {
            "Encounter Date": self.encounter_date,
            "Quarter": self.quarter,
            "Year": self.year,
            "Class": self.encounter_class,
            "Payer Category": self.payer_category,
        }[name]


def _claim(rows: list[EncounterRow]) -> Decimal:
    return sum((r.total_claim_cost for r in rows), Decimal("0"))


def _coverage(rows: list[EncounterRow]) -> Decimal:
    return sum((r.payer_coverage for r in rows), Decimal("0"))


def _patients(rows: list[EncounterRow]) -> int:
    return len({r.patient_id for r in rows})


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return None if denominator == 0 else numerator / denominator


RAW_MEASURES: dict[str, Callable[[list[EncounterRow]], int | Decimal | None]] = {
    "Encounter Count": len,
    "Unique Patient Count": _patients,
    "Total Claim Cost": _claim,
    "Total Payer Coverage": _coverage,
    "Claim Cost Not Covered by Payer": lambda rows: _claim(rows) - _coverage(rows),
    "Coverage Rate": lambda rows: _ratio(_coverage(rows), _claim(rows)),
    "Average Claim Cost": lambda rows: _ratio(_claim(rows), Decimal(len(rows))),
    "Encounters per Patient": lambda rows: _ratio(Decimal(len(rows)), Decimal(_patients(rows))),
}


def quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def measure_value(name: str, rows: list[EncounterRow]) -> int | Decimal | None:
    value = RAW_MEASURES[name](rows)
    places = ROUNDING[name]
    if value is None or places is None:
        return value
    return Decimal(value).quantize(quantum(places), rounding=ROUND_HALF_UP)


def measure_alias(measure: str) -> str:
    """The only alias a canonical answer may use for a measure."""
    return re.sub(r"[^a-z0-9]+", "_", measure.lower()).strip("_")


@dataclass(frozen=True)
class IntentSpec:
    dimensions: tuple[str, ...]
    measures: tuple[tuple[str, str], ...]
    years: tuple[int, ...]

    @property
    def columns(self) -> list[str]:
        return [*self.dimensions, *(alias for alias, _ in self.measures)]

    def semantics(self) -> dict[str, Any]:
        """The structured form that must appear verbatim in evaluation/benchmarks.json."""
        return {
            "dimensions": list(self.dimensions),
            "measures": [
                {"alias": alias, "measure": measure, "round": ROUNDING[measure]}
                for alias, measure in self.measures
            ],
            "years": list(self.years),
        }

    def column_kinds(self) -> dict[str, str | int | None]:
        """Per column: 'text', 'integer', or the number of decimal places."""
        kinds: dict[str, str | int | None] = {}
        for dim in self.dimensions:
            kinds[dim] = "integer" if dim in INTEGER_DIMENSIONS else "text"
        for alias, measure in self.measures:
            places = ROUNDING[measure]
            kinds[alias] = "integer" if places is None else places
        return kinds


INTENT_SPECS: dict[str, IntentSpec] = {
    "i1_annual_encounters_by_class": IntentSpec(
        ("Year", "Class"), (("encounter_count", "Encounter Count"),), (2024, 2025)
    ),
    "i2_quarterly_encounters_by_class": IntentSpec(
        ("Quarter", "Class"), (("encounter_count", "Encounter Count"),), (2024, 2025)
    ),
    "i3_cost_and_coverage_by_payer_category_2025": IntentSpec(
        ("Payer Category",),
        (
            ("total_claim_cost", "Total Claim Cost"),
            ("total_payer_coverage", "Total Payer Coverage"),
            ("claim_cost_not_covered_by_payer", "Claim Cost Not Covered by Payer"),
            ("coverage_rate", "Coverage Rate"),
        ),
        (2025,),
    ),
    "i4_average_claim_cost_by_class_2025": IntentSpec(
        ("Class",), (("average_claim_cost", "Average Claim Cost"),), (2025,)
    ),
    "i5_patients_and_encounters_per_patient_by_payer_category_2025": IntentSpec(
        ("Payer Category",),
        (
            ("unique_patient_count", "Unique Patient Count"),
            ("encounters_per_patient", "Encounters per Patient"),
        ),
        (2025,),
    ),
    "i6_quarterly_total_claim_cost": IntentSpec(
        ("Quarter",), (("total_claim_cost", "Total Claim Cost"),), (2024, 2025)
    ),
    "i7_quarterly_coverage_rate_by_payer_category": IntentSpec(
        ("Quarter", "Payer Category"), (("coverage_rate", "Coverage Rate"),), (2024, 2025)
    ),
}


def compute(spec: IntentSpec, rows: Iterable[EncounterRow]) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], list[EncounterRow]] = defaultdict(list)
    for row in rows:
        if row.year in spec.years:
            groups[tuple(row.dimension(d) for d in spec.dimensions)].append(row)
    output = []
    for key in sorted(groups):
        values = [measure_value(measure, groups[key]) for _, measure in spec.measures]
        output.append([*key, *values])
    return {"columns": spec.columns, "rows": [[_json(v) for v in row] for row in output]}


# The payload-held-out intents (evaluation/payload-holdout.json), declared a second time by
# hand for the same reason as INTENT_SPECS. None of them is in the Agent configuration.
HOLDOUT_SPECS: dict[str, IntentSpec] = {
    "h1_patients_and_encounters_per_patient_by_class_2024": IntentSpec(
        ("Class",),
        (
            ("unique_patient_count", "Unique Patient Count"),
            ("encounters_per_patient", "Encounters per Patient"),
        ),
        (2024,),
    ),
    "h2_encounters_and_average_claim_cost_by_payer_category_2024": IntentSpec(
        ("Payer Category",),
        (
            ("encounter_count", "Encounter Count"),
            ("average_claim_cost", "Average Claim Cost"),
        ),
        (2024,),
    ),
    "h3_annual_coverage_rate_by_class": IntentSpec(
        ("Year", "Class"), (("coverage_rate", "Coverage Rate"),), (2024, 2025)
    ),
    "h4_quarterly_coverage_and_uncovered_cost_2025": IntentSpec(
        ("Quarter",),
        (
            ("total_payer_coverage", "Total Payer Coverage"),
            ("claim_cost_not_covered_by_payer", "Claim Cost Not Covered by Payer"),
        ),
        (2025,),
    ),
}


def compute_all(
    rows: list[EncounterRow], specs: Mapping[str, IntentSpec] = INTENT_SPECS
) -> dict[str, dict[str, Any]]:
    return {intent: compute(spec, rows) for intent, spec in specs.items()}


def _json(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def load_staged_rows(staging_dir: Path) -> list[EncounterRow]:
    """Callers must verify the staging directory first (artifact.verify_staged_files)."""
    with (staging_dir / "payer_categories.csv").open(encoding="utf-8", newline="") as handle:
        category = {r["payer_id"]: r["payer_category"] for r in csv.DictReader(handle)}
    with (staging_dir / "encounters.csv").open(encoding="utf-8", newline="") as handle:
        return [
            EncounterRow(
                encounter_date=r["start_utc"][:10],
                encounter_class=r["encounter_class"],
                payer_category=category[r["payer_id"]],
                patient_id=r["patient_id"],
                total_claim_cost=Decimal(r["total_claim_cost"]),
                payer_coverage=Decimal(r["payer_coverage"]),
            )
            for r in csv.DictReader(handle)
        ]


class CellError(ValueError):
    pass


COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "Class": ("Encounter Class", "encounter_class"),
    "encounter_count": ("Visit Count",),
    "unique_patient_count": ("Unique Synthetic Patient Count", "Distinct Patient Count"),
    "total_claim_cost": ("Total Simulated Claim Cost", "Total Claim Cost (simulated)"),
    "total_payer_coverage": ("Total Payer Coverage (simulated)",),
    "claim_cost_not_covered_by_payer": (
        "Claim Cost Not Covered",
        "Claim Cost Not Covered by Payer (simulated)",
    ),
    "average_claim_cost": ("Average Claim Cost (simulated)",),
}


def _column_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def align_result_columns(
    actual_columns: Sequence[Any], columns: Sequence[str], rows: Sequence[Any]
) -> tuple[list[list[Any]] | None, list[str]]:
    """Match explicit semantic labels, then put values in reference column order."""
    if len(actual_columns) != len(columns):
        return None, [f"expected {len(columns)} columns, got {len(actual_columns)}"]
    positions: dict[str, int] = {}
    for index, label in enumerate(actual_columns):
        if not isinstance(label, str):
            return None, ["result column names must be strings"]
        matches = [
            column
            for column in columns
            if _column_key(label)
            in {_column_key(alias) for alias in (column, *COLUMN_ALIASES.get(column, ()))}
        ]
        if len(matches) != 1:
            return None, [f"unknown or ambiguous result column {label!r}"]
        column = matches[0]
        if column in positions:
            return None, [f"duplicate result column for {column!r}"]
        positions[column] = index
    aligned: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list | tuple) or len(row) != len(columns):
            return None, ["actual result row has the wrong number of cells"]
        aligned.append([row[positions[column]] for column in columns])
    return aligned, []


def normalize_cell(value: Any, kind: str | int | None, *, round_actual: bool) -> str | None:
    """Normalize one result cell to its canonical displayed form.

    Integers must be integral. Decimal columns may not carry more precision than the
    canonical rounding unless ``round_actual`` is set, in which case they are rounded
    half-up to it first (the rubric's "after rounding" rule for Genie answers). The
    normalized values are then compared for exact equality: 150.01 never equals 150.00.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise CellError(f"unsupported cell value {value!r}")
    if kind == "text":
        if not isinstance(value, str):
            raise CellError(f"expected text, got {value!r}")
        return value
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise CellError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise CellError(f"not a finite number: {value!r}")
    if kind == "integer" or kind is None:
        if number != number.to_integral_value():
            raise CellError(f"expected an integer, got {value!r}")
        return str(int(number))
    step = quantum(int(kind))
    if round_actual:
        number = number.quantize(step, rounding=ROUND_HALF_UP)
    elif number != number.quantize(step, rounding=ROUND_HALF_UP):
        raise CellError(f"{value!r} has more precision than the canonical {kind} decimal places")
    return str(number.quantize(step))


def _normalize_rows(
    rows: Sequence[Any],
    columns: Sequence[str],
    kinds: Sequence[str | int | None],
    *,
    label: str,
    round_actual: bool,
    problems: list[str],
) -> list[tuple[str | None, ...]] | None:
    normalized: list[tuple[str | None, ...]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list | tuple) or len(row) != len(kinds):
            problems.append(f"{label} row {index} has the wrong number of cells")
            return None
        cells: list[str | None] = []
        for column, kind, cell in zip(columns, kinds, row, strict=True):
            try:
                cells.append(normalize_cell(cell, kind, round_actual=round_actual))
            except CellError as exc:
                problems.append(f"{label} row {index} {column}: {exc}")
                return None
        normalized.append(tuple(cells))
    return normalized


def compare_tables(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    spec: IntentSpec,
    *,
    genie_answer: bool = False,
) -> list[str]:
    """Exact comparison at canonical displayed precision.

    Canonical SQL results (default): column names must match, rows must be in canonical
    order, values may not carry extra precision. Genie answers (``genie_answer=True``):
    documented label aliases are accepted and columns are aligned by semantic label.
    Row order is ignored, but the row set and every value after half-up rounding to
    the canonical precision must match exactly. Unknown or duplicated labels fail.
    """
    columns = spec.columns
    kinds = [spec.column_kinds()[c] for c in columns]
    if list(expected.get("columns", [])) != columns:
        return [f"reference columns {expected.get('columns')} differ from the spec {columns}"]
    got_columns = actual.get("columns")
    if not isinstance(got_columns, list):
        return ["actual result has no column list"]
    if genie_answer:
        if len(got_columns) != len(columns):
            return [f"expected {len(columns)} columns, got {len(got_columns)}"]
    elif got_columns != columns:
        return [f"columns differ: expected {columns} got {got_columns}"]
    rows = actual.get("rows")
    if not isinstance(rows, list):
        return ["actual result has no row list"]
    if genie_answer:
        aligned, label_problems = align_result_columns(got_columns, columns, rows)
        if aligned is None:
            return label_problems
        rows = aligned
    problems: list[str] = []
    want = _normalize_rows(
        expected["rows"], columns, kinds, label="reference", round_actual=False, problems=problems
    )
    got = _normalize_rows(
        rows, columns, kinds, label="actual", round_actual=genie_answer, problems=problems
    )
    if want is None or got is None:
        return problems
    if len(want) != len(got):
        problems.append(f"row count differs: expected {len(want)} got {len(got)}")
    if genie_answer:
        missing = Counter(want) - Counter(got)
        extra = Counter(got) - Counter(want)
        problems += [f"missing row {list(r)}" for r in sorted(missing, key=str)][:10]
        problems += [f"unexpected row {list(r)}" for r in sorted(extra, key=str)][:10]
        return problems
    for index, (w, g) in enumerate(zip(want, got, strict=False)):
        for column, wc, gc in zip(columns, w, g, strict=True):
            if wc != gc:
                problems.append(f"row {index} {column}: expected {wc!r} got {gc!r}")
    return problems


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def table_digest(table: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rows": len(table["rows"]),
        "sha256": sha256_text(canonical_json({"columns": table["columns"], "rows": table["rows"]})),
    }


def intents_digest(intents: Sequence[Mapping[str, Any]]) -> str:
    """Digest of the intent semantics only; question rewording does not change it."""
    return sha256_text(
        canonical_json([{"id": i["id"], "semantics": i["semantics"]} for i in intents])
    )


FREEZE_COMMAND = "uv run python scripts/run_benchmarks.py --reference --freeze-digest"
HOLDOUT_FREEZE_COMMAND = (
    "uv run python scripts/run_benchmarks.py --reference --payload-holdout --freeze-digest"
)


def reference_digest(
    results: Mapping[str, Mapping[str, Any]],
    *,
    artifact_sha256: str,
    intents: Sequence[Any],
    generated_by: str = FREEZE_COMMAND,
) -> dict[str, Any]:
    return {
        "note": (
            "Digests of the offline reference results computed from the pinned archive. "
            "No result values are published here; see README.md in this folder."
        ),
        "generated_by": generated_by,
        "artifact_sha256": artifact_sha256,
        "benchmark_intents_sha256": intents_digest(intents),
        "intents": {intent: table_digest(results[intent]) for intent in sorted(results)},
    }


DIGEST_KEYS = ("note", "generated_by", "artifact_sha256", "benchmark_intents_sha256", "intents")


def check_reference_against_digest(
    results: Mapping[str, Mapping[str, Any]],
    digest: Any,
    *,
    artifact_sha256: str,
    intents: Sequence[Any],
    generated_by: str,
) -> list[str]:
    """The reference results and every digest field against explicit expected values.

    The digest must have exactly the frozen keys and the frozen note; ``generated_by`` must
    name the command that freezes this suite, ``artifact_sha256`` the pinned archive and
    ``benchmark_intents_sha256`` these intent semantics; every intent's row count and
    result hash must equal those of ``results``.
    """
    if not isinstance(digest, Mapping):
        return ["the reference digest must be a JSON object"]
    if sorted(digest) != sorted(DIGEST_KEYS):
        return [f"the reference digest must have exactly the keys {sorted(DIGEST_KEYS)}"]
    expected = reference_digest(
        results, artifact_sha256=artifact_sha256, intents=intents, generated_by=generated_by
    )
    problems: list[str] = []
    if digest["note"] != expected["note"]:
        problems.append("the reference digest note differs from the frozen note")
    if digest["generated_by"] != generated_by:
        problems.append(f"the reference digest was not generated by {generated_by}")
    if digest["artifact_sha256"] != artifact_sha256:
        problems.append("the reference digest was frozen from a different archive")
    if digest["benchmark_intents_sha256"] != expected["benchmark_intents_sha256"]:
        problems.append(
            f"the reference digest was frozen for different intent semantics; refreeze with "
            f"{generated_by}"
        )
    frozen = digest["intents"]
    if not isinstance(frozen, Mapping) or sorted(frozen) != sorted(results):
        return [*problems, f"reference intents {sorted(results)} differ from the frozen digest"]
    for intent in sorted(results):
        if frozen[intent] != expected["intents"][intent]:
            problems.append(f"{intent}: reference results differ from the frozen digest")
    return problems
