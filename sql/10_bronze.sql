-- 10_bronze.sql
-- Loads the two minimized staging extracts from the project volume as string columns.
--
-- Runtime validation required. Inputs are the files written by
--   uv run python scripts/download_and_verify.py --stage
-- and uploaded to /Volumes/{{catalog}}/{{schema}}/{{volume}}/.
-- The staging extracts already exclude raw payer names, facility and provider keys and
-- clinical terminology columns; nothing here reads the original archive.
-- Every gate uses assert_true and stops the run on drift from data/artifact.lock.json.

CREATE OR REPLACE TABLE {{catalog}}.{{schema}}.bronze_encounters
COMMENT 'synthea-healthcare-genie-demo bronze: staged synthetic encounters as strings.'
AS SELECT
  encounter_id,
  start_utc,
  patient_id,
  payer_id,
  encounter_class,
  total_claim_cost,
  payer_coverage
FROM read_files(
  '/Volumes/{{catalog}}/{{schema}}/{{volume}}/encounters.csv',
  format => 'csv',
  header => true,
  mode => 'FAILFAST',
  schema => 'encounter_id STRING, start_utc STRING, patient_id STRING, payer_id STRING, encounter_class STRING, total_claim_cost STRING, payer_coverage STRING'
);

CREATE OR REPLACE TABLE {{catalog}}.{{schema}}.bronze_payer_categories
COMMENT 'synthea-healthcare-genie-demo bronze: payer key to payer category, mapped locally. No payer names.'
AS SELECT
  payer_id,
  payer_category
FROM read_files(
  '/Volumes/{{catalog}}/{{schema}}/{{volume}}/payer_categories.csv',
  format => 'csv',
  header => true,
  mode => 'FAILFAST',
  schema => 'payer_id STRING, payer_category STRING'
);

-- Gate: row counts match the verified artifact lock.
SELECT assert_true(
  COUNT(*) = {{expected_encounter_rows}},
  'Gate failed: bronze_encounters row count differs from data/artifact.lock.json'
) AS gate_bronze_encounter_rows
FROM {{catalog}}.{{schema}}.bronze_encounters;

SELECT assert_true(
  COUNT(*) = {{expected_payer_rows}},
  'Gate failed: bronze_payer_categories row count differs from data/artifact.lock.json'
) AS gate_bronze_payer_rows
FROM {{catalog}}.{{schema}}.bronze_payer_categories;

-- Gate: encounter keys are present and unique.
SELECT assert_true(
  COUNT(DISTINCT encounter_id) = COUNT(*) AND COUNT_IF(encounter_id IS NULL) = 0,
  'Gate failed: encounter_id is missing or duplicated'
) AS gate_bronze_encounter_keys
FROM {{catalog}}.{{schema}}.bronze_encounters;
