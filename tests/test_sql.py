from __future__ import annotations

import pytest

from synthea_genie_demo.checks import (
    check_metric_view,
    check_sql_static,
    contract,
    lock,
)
from synthea_genie_demo.paths import SQL_DIR
from synthea_genie_demo.render import RenderError
from synthea_genie_demo.sqltools import (
    SQL_FILES,
    find_dynamic_dates,
    has_order_by,
    render_sql,
    select_columns,
    split_statements,
    sql_parameters,
)


def test_split_respects_strings_comments_and_dollar_blocks() -> None:
    sql = (
        "-- a comment; with semicolon\n"
        "SELECT 'a;b' AS x;\n"
        "CREATE VIEW v AS $$ yaml: 'x;y' $$;\n"
        "/* block; */ SELECT `we;ird` FROM t;\n"
        "-- trailing comment only\n"
    )
    statements = split_statements(sql)
    assert len(statements) == 3
    assert statements[1].endswith("$$")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE d > CURRENT_DATE",
        "SELECT current_timestamp()",
        "SELECT now()",
        "SELECT date_sub(d, 7)",
        "SELECT d - INTERVAL 1 DAY",
        "SELECT add_months(d, -3)",
    ],
)
def test_dynamic_dates_detected(sql: str) -> None:
    assert find_dynamic_dates(sql)


def test_dynamic_dates_in_comments_ignored() -> None:
    assert find_dynamic_dates("-- never use CURRENT_DATE\nSELECT 1") == []


def test_no_sql_file_uses_dynamic_dates() -> None:
    for name in SQL_FILES:
        assert find_dynamic_dates((SQL_DIR / name).read_text(encoding="utf-8")) == [], name


def test_static_checks_pass() -> None:
    assert check_sql_static(contract(), lock()) == []


def test_metric_view_matches_contract() -> None:
    assert check_metric_view(contract(), lock()) == []


def test_every_sql_file_renders_and_splits() -> None:
    params = sql_parameters(
        lock(), contract(), catalog="demo_catalog", schema="synthea_hc_ops", run_label="demo-run"
    )
    for name in SQL_FILES:
        rendered = render_sql((SQL_DIR / name).read_text(encoding="utf-8"), params)
        assert "{{" not in rendered
        assert split_statements(rendered), name


def test_gate_values_come_from_lock() -> None:
    lk = lock()
    params = sql_parameters(lk, contract(), catalog="c", schema="s", run_label="demo-run")
    clean = render_sql((SQL_DIR / "20_clean.sql").read_text(encoding="utf-8"), params)
    assert f"COUNT(*) = {lk['observed']['encounters']['row_count']}" in clean
    assert lk["observed"]["encounters"]["total_claim_cost_sum"] in clean
    assert "DATE'2026-08-18'" in clean


def test_unknown_placeholder_rejected() -> None:
    with pytest.raises(RenderError):
        render_sql("SELECT {{nope}}", {"catalog": "c"})


@pytest.mark.parametrize(
    ("catalog", "schema"),
    [
        ("bad-name", "s"),
        ("c", "default"),
        ("c", "information_schema"),
        ("c`; DROP", "s"),
        ("", "s"),
    ],
)
def test_identifier_validation(catalog: str, schema: str) -> None:
    with pytest.raises(RenderError):
        sql_parameters(lock(), contract(), catalog=catalog, schema=schema, run_label="demo-run")


@pytest.mark.parametrize("run_label", ["", "Demo", "a", "x' OR '1", "demo run", "-demo"])
def test_run_label_validation(run_label: str) -> None:
    with pytest.raises(RenderError):
        sql_parameters(lock(), contract(), catalog="c", schema="s", run_label=run_label)


def test_schema_comment_carries_run_label() -> None:
    params = sql_parameters(lock(), contract(), catalog="c", schema="s", run_label="demo-run")
    text = render_sql((SQL_DIR / "00_create_schema.sql").read_text(encoding="utf-8"), params)
    assert "'synthea-healthcare-genie-demo run-label=demo-run: " in text


def test_select_columns_and_order_by() -> None:
    sql = (
        "SELECT\n  `Year`,\n  ROUND(MEASURE(`Total Claim Cost`), 2) AS total\n"
        "FROM v\nORDER BY `Year`"
    )
    assert select_columns(sql) == ["Year", "total"]
    assert has_order_by(sql)
    assert not has_order_by("SELECT 1 -- ORDER BY x")


def test_cleanup_sql_has_no_cascade_and_only_drops() -> None:
    text = (SQL_DIR / "90_cleanup.sql").read_text(encoding="utf-8")
    params = sql_parameters(lock(), contract(), catalog="c", schema="s", run_label="demo-run")
    for statement in split_statements(render_sql(text, params)):
        code = " ".join(
            line for line in statement.splitlines() if not line.startswith("--")
        ).strip()
        assert code.startswith("DROP ")
        assert "CASCADE" not in code.upper()
        assert "CATALOG" not in code.upper()
