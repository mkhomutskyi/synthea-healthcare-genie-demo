-- 20_clean.sql
-- Typed clean encounter model and payer category model, followed by fail-closed gates.
--
-- Runtime validation required.
-- Design rules:
--   * No raw names and no identifier is meant for display.
--   * patient_id is kept only for COUNT(DISTINCT ...) inside the Metric View.
--   * payer_id is kept only as the many-to-one join key to payer_categories.
--   * encounter_date is the UTC calendar date taken from the first 10 characters of
--     the source START timestamp, so it does not depend on the session time zone.
--   * Fixed literals only; no relative dates.

CREATE OR REPLACE TABLE {{catalog}}.{{schema}}.payer_categories (
  payer_id STRING NOT NULL COMMENT 'Synthetic payer key. Join key only; never display.',
  payer_category STRING NOT NULL COMMENT 'One of: Dual Eligible, Medicaid, Medicare, No Insurance, Private Insurance.'
)
COMMENT 'synthea-healthcare-genie-demo clean: one row per synthetic payer key with its payer category. No payer names.';

INSERT INTO {{catalog}}.{{schema}}.payer_categories
SELECT payer_id, payer_category
FROM {{catalog}}.{{schema}}.bronze_payer_categories;

CREATE OR REPLACE TABLE {{catalog}}.{{schema}}.clean_encounters (
  encounter_date DATE NOT NULL COMMENT 'Simulated encounter date (UTC calendar date of the simulated start timestamp).',
  encounter_class STRING NOT NULL COMMENT 'Synthea encounter class, for example ambulatory, emergency or wellness.',
  payer_id STRING NOT NULL COMMENT 'Synthetic payer key. Join key to payer_categories only; never display.',
  patient_id STRING NOT NULL COMMENT 'Synthetic patient key. Used only for distinct counts; never display.',
  total_claim_cost DECIMAL(18,2) NOT NULL COMMENT 'Simulated total claim cost.',
  payer_coverage DECIMAL(18,2) NOT NULL COMMENT 'Simulated amount covered by the payer.'
)
COMMENT 'synthea-healthcare-genie-demo clean: one row per synthetic encounter. All dates and amounts are simulated.';

INSERT INTO {{catalog}}.{{schema}}.clean_encounters
SELECT
  to_date(substr(start_utc, 1, 10), 'yyyy-MM-dd') AS encounter_date,
  encounter_class,
  payer_id,
  patient_id,
  CAST(total_claim_cost AS DECIMAL(18,2)) AS total_claim_cost,
  CAST(payer_coverage AS DECIMAL(18,2)) AS payer_coverage
FROM {{catalog}}.{{schema}}.bronze_encounters;

-- Gate: payer keys are unique, so the Metric View join is truly many-to-one.
SELECT assert_true(
  COUNT(*) = COUNT(DISTINCT payer_id) AND COUNT(*) = {{expected_payer_rows}},
  'Gate failed: payer_categories must have exactly one row per payer key'
) AS gate_payer_key_unique
FROM {{catalog}}.{{schema}}.payer_categories;

-- Gate: only the five approved category labels exist.
SELECT assert_true(
  COUNT_IF(payer_category NOT IN ({{payer_category_list}})) = 0,
  'Gate failed: unexpected payer_category label'
) AS gate_payer_category_labels
FROM {{catalog}}.{{schema}}.payer_categories;

-- Gate: every encounter resolves to a payer category.
SELECT assert_true(
  COUNT_IF(p.payer_id IS NULL) = 0,
  'Gate failed: an encounter payer key has no payer category'
) AS gate_encounter_payer_join
FROM {{catalog}}.{{schema}}.clean_encounters AS e
LEFT JOIN {{catalog}}.{{schema}}.payer_categories AS p
  ON e.payer_id = p.payer_id;

-- Gate: counts, sums and date bounds match the verified artifact lock.
SELECT assert_true(
  COUNT(*) = {{expected_encounter_rows}}
    AND COUNT(DISTINCT patient_id) = {{expected_distinct_patients}}
    AND SUM(total_claim_cost) = CAST('{{expected_total_claim_cost}}' AS DECIMAL(28,2))
    AND SUM(payer_coverage) = CAST('{{expected_total_payer_coverage}}' AS DECIMAL(28,2))
    AND MAX(encounter_date) = DATE'{{expected_max_encounter_date}}'
    AND MAX(encounter_date) <= DATE'{{simulation_reference_date}}',
  'Gate failed: clean_encounters differs from data/artifact.lock.json'
) AS gate_clean_encounters_lock
FROM {{catalog}}.{{schema}}.clean_encounters;

-- Gate: simulated payer coverage never exceeds simulated claim cost, and amounts are non-negative.
SELECT assert_true(
  COUNT_IF(payer_coverage > total_claim_cost OR payer_coverage < 0 OR total_claim_cost < 0) = 0,
  'Gate failed: invalid simulated money values'
) AS gate_clean_money
FROM {{catalog}}.{{schema}}.clean_encounters;
