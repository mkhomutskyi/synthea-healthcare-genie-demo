-- Generic example SQL patterns rendered into the Agent's example_question_sqls.
--
-- Rules enforced by tests:
--   * Examples use 2023 only. 2024-2025 is reserved for the frozen benchmarks.
--   * No example uses a held-out shape listed in evaluation/benchmarks.json
--     (per-patient measures, Quarter with Payer Category, Coverage Rate,
--     Average Claim Cost).
--   * No example question repeats or closely paraphrases a benchmark question.
--   * No relative dates (CURRENT_DATE, NOW(), INTERVAL arithmetic).
--
-- Block format: "-- example: <key>" then "-- question: <text>" then one SQL
-- statement ending with a semicolon.

-- example: ex-2023-encounter-count-by-payer-category
-- question: How many encounters were recorded in 2023 for each payer category?
SELECT
  `Payer Category`,
  MEASURE(`Encounter Count`) AS encounter_count
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2023
GROUP BY `Payer Category`
ORDER BY `Payer Category`;

-- example: ex-2023-total-claim-cost-by-class
-- question: What was the simulated total claim cost for each encounter class during 2023?
SELECT
  `Class`,
  ROUND(MEASURE(`Total Claim Cost`), 2) AS total_claim_cost
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2023
GROUP BY `Class`
ORDER BY `Class`;

-- example: ex-2023-payer-coverage-by-quarter
-- question: What was the simulated total payer coverage in each 2023 quarter?
SELECT
  `Quarter`,
  ROUND(MEASURE(`Total Payer Coverage`), 2) AS total_payer_coverage
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2023
GROUP BY `Quarter`
ORDER BY `Quarter`;
