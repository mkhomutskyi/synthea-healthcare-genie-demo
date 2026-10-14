"""Offline SQL helpers: splitting, static checks and template rendering."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .render import (
    PLACEHOLDER_RE,
    RenderError,
    render_placeholders,
    validate_identifier,
    validate_run_label,
    validate_schema_name,
)

SQL_FILES = (
    "00_create_schema.sql",
    "10_bronze.sql",
    "20_clean.sql",
    "30_metric_view.sql",
    "40_canonical_answers.sql",
    "90_cleanup.sql",
)
DEPLOY_SQL_FILES = SQL_FILES[:4]
DEFAULT_VOLUME = "raw_staging"
SIMULATION_REFERENCE_DATE = "2026-08-18"

DYNAMIC_DATE_PATTERNS = {
    "CURRENT_DATE": r"\bcurrent_date\b",
    "CURRENT_TIMESTAMP": r"\bcurrent_timestamp\b",
    "NOW()": r"\bnow\s*\(",
    "GETDATE()": r"\bgetdate\s*\(",
    "CURDATE()": r"\bcurdate\s*\(",
    "LOCALTIMESTAMP": r"\blocaltimestamp\b",
    "INTERVAL": r"\binterval\b",
    "DATE_ADD()": r"\bdate_add\s*\(",
    "DATE_SUB()": r"\bdate_sub\s*\(",
    "DATEADD()": r"\bdateadd\s*\(",
    "DATEDIFF()": r"\bdatediff\s*\(",
    "ADD_MONTHS()": r"\badd_months\s*\(",
    "TODAY": r"\btoday\b",
    "UNIX_TIMESTAMP()": r"\bunix_timestamp\s*\(\s*\)",
}


def _scan(sql: str) -> list[tuple[int, int, str]]:
    """Tokenize into (start, end, kind) spans for comments, strings, identifiers and $$ bodies."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if two == "--":
            end = sql.find("\n", i)
            end = n if end < 0 else end
            spans.append((i, end, "comment"))
            i = end
        elif two == "/*":
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            spans.append((i, end, "comment"))
            i = end
        elif two == "$$":
            end = sql.find("$$", i + 2)
            if end < 0:
                raise ValueError("Unterminated $$ block")
            spans.append((i, end + 2, "dollar"))
            i = end + 2
        elif sql[i] in "'\"`":
            quote = sql[i]
            j = i + 1
            while j < n:
                if sql[j] == "\\" and quote != "`":
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValueError(f"Unterminated {quote} quote")
            spans.append((i, j + 1, "identifier" if quote == "`" else "string"))
            i = j + 1
        else:
            i += 1
    return spans


def _mask(sql: str, kinds: set[str], keep_delimiters: bool = False) -> str:
    chars = list(sql)
    for start, end, kind in _scan(sql):
        if kind in kinds:
            lo, hi = (start + 1, end - 1) if keep_delimiters else (start, end)
            for k in range(lo, hi):
                if chars[k] != "\n":
                    chars[k] = " "
    return "".join(chars)


def strip_comments(sql: str) -> str:
    return _mask(sql, {"comment"})


def strip_strings(sql: str) -> str:
    """Blank comments, string literals and $$ bodies, keeping identifiers and code."""
    return _mask(sql, {"comment", "string", "dollar"}, keep_delimiters=True)


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons; drop comment-only fragments."""
    masked = _mask(sql, {"comment", "string", "identifier", "dollar"}, keep_delimiters=True)
    statements: list[str] = []
    start = 0
    for index, char in enumerate(masked):
        if char == ";":
            statements.append(sql[start:index])
            start = index + 1
    statements.append(sql[start:])
    return [s.strip() for s in statements if strip_comments(s).strip()]


def find_dynamic_dates(sql: str) -> list[str]:
    text = strip_comments(sql).lower()
    return [label for label, pattern in DYNAMIC_DATE_PATTERNS.items() if re.search(pattern, text)]


def _top_level_split(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    masked = _mask(text, {"string", "identifier"}, keep_delimiters=True)
    for raw, char in zip(text, masked, strict=True):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(raw)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def select_columns(sql: str) -> list[str]:
    """Output column names of a single top-level SELECT (alias or backticked field)."""
    text = strip_comments(sql)
    masked = _mask(text, {"string", "identifier"}, keep_delimiters=True)
    select = re.search(r"\bselect\b", masked, flags=re.IGNORECASE)
    from_ = re.search(r"\bfrom\b", masked, flags=re.IGNORECASE)
    if not select or not from_ or from_.start() < select.end():
        raise ValueError("Expected a SELECT ... FROM statement")
    names: list[str] = []
    for item in _top_level_split(text[select.end() : from_.start()]):
        alias = re.search(r"\bas\s+(`[^`]+`|\w+)\s*$", item, flags=re.IGNORECASE)
        token = alias.group(1) if alias else item
        names.append(token.strip().strip("`"))
    return names


def has_order_by(sql: str) -> bool:
    masked = _mask(strip_comments(sql), {"string", "identifier"}, keep_delimiters=True)
    return re.search(r"\border\s+by\b", masked, flags=re.IGNORECASE) is not None


def sql_parameters(
    lock: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    catalog: str,
    schema: str,
    run_label: str,
    volume: str = DEFAULT_VOLUME,
) -> dict[str, str]:
    validate_identifier(catalog, "catalog")
    validate_schema_name(schema)
    validate_identifier(volume, "volume")
    validate_run_label(run_label)
    observed = lock["observed"]
    categories = ", ".join(f"'{c}'" for c in contract["payer_categories"])
    return {
        "catalog": catalog,
        "schema": schema,
        "volume": volume,
        "run_label": run_label,
        "expected_encounter_rows": str(observed["encounters"]["row_count"]),
        "expected_distinct_patients": str(observed["encounters"]["distinct_patients"]),
        "expected_payer_rows": str(observed["payers"]["row_count"]),
        "expected_total_claim_cost": observed["encounters"]["total_claim_cost_sum"],
        "expected_total_payer_coverage": observed["encounters"]["payer_coverage_sum"],
        "expected_max_encounter_date": observed["encounters"]["start_date_max"],
        "simulation_reference_date": SIMULATION_REFERENCE_DATE,
        "payer_category_list": categories,
    }


def render_sql(text: str, params: Mapping[str, str]) -> str:
    rendered = render_placeholders(text, params)
    assert isinstance(rendered, str)
    if PLACEHOLDER_RE.search(rendered):
        raise RenderError("Unrendered placeholders remain in SQL")
    return rendered


def sql_placeholders(path: Path) -> set[str]:
    return set(PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8")))
