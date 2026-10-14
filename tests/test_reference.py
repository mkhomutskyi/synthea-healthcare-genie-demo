from decimal import Decimal
from pathlib import Path
from typing import Any

from synthea_genie_demo.artifact import read_archive, stage
from synthea_genie_demo.payers import PayerMapping
from synthea_genie_demo.reference import (
    INTENT_SPECS,
    EncounterRow,
    compare_tables,
    compute,
    compute_all,
    load_staged_rows,
)


def row(date: str, cls: str, cat: str, patient: str, claim: str, cov: str) -> EncounterRow:
    return EncounterRow(date, cls, cat, patient, Decimal(claim), Decimal(cov))


ROWS = [
    row("2024-01-15", "ambulatory", "Medicare", "p1", "100.00", "80.00"),
    row("2024-02-01", "ambulatory", "Medicaid", "p1", "50.00", "50.00"),
    row("2024-05-20", "emergency", "Medicare", "p2", "200.01", "0.00"),
    row("2025-11-30", "wellness", "No Insurance", "p3", "10.00", "0.00"),
    row("2025-12-31", "wellness", "No Insurance", "p3", "0.00", "0.00"),
    row("2023-06-01", "wellness", "Medicare", "p4", "999.00", "1.00"),
]


def test_quarter_labels() -> None:
    assert ROWS[0].quarter == "2024-Q1" and ROWS[2].quarter == "2024-Q2"
    assert ROWS[3].quarter == "2025-Q4"


def test_fixed_years_and_empty_groups_omitted() -> None:
    table = compute(INTENT_SPECS["i2_quarterly_encounters_by_class"], ROWS)
    assert table["columns"] == ["Quarter", "Class", "encounter_count"]
    assert table["rows"] == [
        ["2024-Q1", "ambulatory", 2],
        ["2024-Q2", "emergency", 1],
        ["2025-Q4", "wellness", 2],
    ]


def test_cost_and_rate_rounding() -> None:
    table = compute(INTENT_SPECS["i7_quarterly_coverage_rate_by_payer_category"], ROWS)
    rates = {(r[0], r[1]): r[2] for r in table["rows"]}
    assert rates[("2024-Q1", "Medicare")] == "0.8000"
    assert rates[("2024-Q2", "Medicare")] == "0.0000"
    i3 = compute(INTENT_SPECS["i3_cost_and_coverage_by_payer_category_2025"], ROWS)
    assert i3["rows"] == [["No Insurance", "10.00", "0.00", "10.00", "0.0000"]]


def test_zero_claim_rate_is_null() -> None:
    zero = [row("2025-01-01", "wellness", "Medicaid", "p9", "0.00", "0.00")]
    table = compute(INTENT_SPECS["i3_cost_and_coverage_by_payer_category_2025"], zero)
    assert table["rows"][0][-1] is None


def test_per_patient_measures_are_not_summed() -> None:
    table = compute(
        INTENT_SPECS["i5_patients_and_encounters_per_patient_by_payer_category_2025"], ROWS
    )
    assert table["rows"] == [["No Insurance", 1, "2.00"]]


I6 = INTENT_SPECS["i6_quarterly_total_claim_cost"]
I3 = INTENT_SPECS["i3_cost_and_coverage_by_payer_category_2025"]
I1 = INTENT_SPECS["i1_annual_encounters_by_class"]
COST = {"columns": ["Quarter", "total_claim_cost"], "rows": [["2024-Q1", "150.00"]]}


def table(columns: list[str], *rows: list[Any]) -> dict[str, Any]:
    return {"columns": columns, "rows": [list(r) for r in rows]}


def test_compare_tables_is_exact_at_displayed_precision() -> None:
    cols = ["Quarter", "total_claim_cost"]
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.00"]), I6) == []
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.0"]), I6) == []
    assert compare_tables(COST, table(cols, ["2024-Q1", 150]), I6) == []
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.01"]), I6)
    assert compare_tables(COST, table(cols, ["2024-Q1", "149.99"]), I6)
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.004"]), I6)
    assert compare_tables(COST, table(["Quarter", "x"]), I6)
    assert compare_tables(COST, table(cols), I6)
    assert compare_tables(COST, table(cols, ["2024-Q1"]), I6)
    assert compare_tables(COST, table(cols, ["2024-Q1", None]), I6)
    assert compare_tables(COST, table(cols, ["2024-Q1", True]), I6)


def test_compare_tables_genie_answers_round_then_match_exactly() -> None:
    cols = ["quarter", "Total Claim Cost"]
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.004"]), I6, genie_answer=True) == []
    assert compare_tables(COST, table(cols, ["2024-Q1", 149.995]), I6, genie_answer=True) == []
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.01"]), I6, genie_answer=True)
    assert compare_tables(COST, table(cols, ["2024-Q1", "150.005"]), I6, genie_answer=True)
    assert compare_tables(COST, table(["only"], ["2024-Q1"]), I6, genie_answer=True)
    two = table(["Year", "Class", "encounter_count"], [2024, "a", 3], [2025, "b", 4])
    swapped = table(["year", "Encounter Class", "Encounter Count"], [2025, "b", 4], [2024, "a", 3])
    assert compare_tables(two, swapped, I1, genie_answer=True) == []
    assert compare_tables(two, swapped, I1)
    assert compare_tables(two, table(["y", "c", "n"], [2024, "a", "3.5"]), I1, genie_answer=True)


def test_semantic_labels_detect_wrong_measure_names_and_accept_reordering() -> None:
    expected = table(I3.columns, ["Medicare", "100.00", "60.00", "40.00", "0.6000"])
    labels = [
        "Payer Category",
        "total_payer_coverage",
        "total_claim_cost",
        "claim_cost_not_covered_by_payer",
        "coverage_rate",
    ]
    assert compare_tables(expected, table(labels, *expected["rows"]), I3, genie_answer=True)
    reordered = table(labels, ["Medicare", "60.00", "100.00", "40.00", "0.6000"])
    assert compare_tables(expected, reordered, I3, genie_answer=True) == []
    duplicate = table(
        [
            "Payer Category",
            "total_claim_cost",
            "total_claim_cost",
            "claim_cost_not_covered_by_payer",
            "coverage_rate",
        ],
        *expected["rows"],
    )
    assert compare_tables(expected, duplicate, I3, genie_answer=True)


def test_rates_and_counts_are_exact_types() -> None:
    cols = I3.columns
    expected = table(cols, ["No Insurance", "10.00", "0.00", "10.00", "0.4567"])
    assert compare_tables(expected, table(cols, ["No Insurance", 10, 0, 10, "0.4567"]), I3) == []
    assert compare_tables(expected, table(cols, ["No Insurance", 10, 0, 10, "0.45671"]), I3)
    assert compare_tables(expected, table(cols, ["No Insurance", 10, 0, 10, "0.4568"]), I3)
    counts = table(I1.columns, [2024, "a", 3])
    assert compare_tables(counts, table(I1.columns, [2024, "a", "3"]), I1) == []
    assert compare_tables(counts, table(I1.columns, [2024, "a", "2.5"]), I1)
    assert compare_tables(counts, table(I1.columns, ["2024.5", "a", 3]), I1)


def test_reference_from_fixture_staging(
    tmp_path: Path, archive: Path, contract: dict[str, Any]
) -> None:
    stage(read_archive(archive), contract, PayerMapping.from_contract(contract), tmp_path)
    results = compute_all(load_staged_rows(tmp_path))
    assert set(results) == set(INTENT_SPECS)
    annual = results["i1_annual_encounters_by_class"]["rows"]
    assert sum(r[2] for r in annual) == sum(
        1 for r in load_staged_rows(tmp_path) if r.year in (2024, 2025)
    )
