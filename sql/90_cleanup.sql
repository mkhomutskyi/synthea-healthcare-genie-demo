-- 90_cleanup.sql
-- Removes only the objects this project creates. Never drops a catalog, never touches a
-- SQL warehouse, never uses CASCADE.
--
-- Runtime validation required.
-- Prefer: uv run python scripts/cleanup.py (dry run first, then --execute).
-- scripts/cleanup.py runs the final DROP SCHEMA only after confirming the schema comment
-- still carries the synthea-healthcare-genie-demo marker. If you run this file by hand,
-- run the last statement only if the schema was created for this project.
-- DROP SCHEMA without CASCADE fails if the schema still holds anything else.

DROP VIEW IF EXISTS {{catalog}}.{{schema}}.encounter_operations_metrics;

DROP TABLE IF EXISTS {{catalog}}.{{schema}}.clean_encounters;

DROP TABLE IF EXISTS {{catalog}}.{{schema}}.payer_categories;

DROP TABLE IF EXISTS {{catalog}}.{{schema}}.bronze_encounters;

DROP TABLE IF EXISTS {{catalog}}.{{schema}}.bronze_payer_categories;

DROP VOLUME IF EXISTS {{catalog}}.{{schema}}.{{volume}};

DROP SCHEMA IF EXISTS {{catalog}}.{{schema}};
