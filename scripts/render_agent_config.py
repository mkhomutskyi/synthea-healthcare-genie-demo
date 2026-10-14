"""Refresh or check the generated Agent template and canonical SQL. Runs locally."""

from __future__ import annotations

import argparse

from synthea_genie_demo.checks import check_generated_files
from synthea_genie_demo.paths import AGENT_TEMPLATE_PATH, CANONICAL_SQL_PATH
from synthea_genie_demo.render import canonical_sql_text, template_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write-template", action="store_true")
    args = parser.parse_args()
    if args.write_template:
        AGENT_TEMPLATE_PATH.write_text(template_text(), encoding="utf-8", newline="\n")
        CANONICAL_SQL_PATH.write_text(canonical_sql_text(), encoding="utf-8", newline="\n")
    problems = check_generated_files()
    for problem in problems:
        print(problem)
    if not problems:
        print("Agent configuration and canonical SQL are current.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
