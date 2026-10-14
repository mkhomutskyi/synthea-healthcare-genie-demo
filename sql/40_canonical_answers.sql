-- GENERATED from evaluation/benchmarks.json by scripts/render_agent_config.py.
-- Do not edit by hand. Regenerate with:
--   uv run python scripts/render_agent_config.py --write-template
-- Benchmark suite 1.1.0 (frozen 2026-10-13).
-- Runtime validation required: run against the deployed Metric View before publishing.
-- Placeholders: {{catalog}}, {{schema}}.

-- intent: i1_annual_encounters_by_class
-- title: Annual encounter counts by class, 2024-2025
-- benchmark questions: i1-a, i1-b
-- expected columns: Year, Class, encounter_count
SELECT
  `Year`,
  `Class`,
  MEASURE(`Encounter Count`) AS encounter_count
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` IN (2024, 2025)
GROUP BY `Year`, `Class`
ORDER BY `Year`, `Class`;

-- intent: i2_quarterly_encounters_by_class
-- title: Quarterly encounter counts by class, 2024-2025
-- benchmark questions: i2-a, i2-b
-- expected columns: Quarter, Class, encounter_count
SELECT
  `Quarter`,
  `Class`,
  MEASURE(`Encounter Count`) AS encounter_count
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` IN (2024, 2025)
GROUP BY `Quarter`, `Class`
ORDER BY `Quarter`, `Class`;

-- intent: i3_cost_and_coverage_by_payer_category_2025
-- title: 2025 simulated claim cost, payer coverage, cost not covered and coverage rate by payer category
-- benchmark questions: i3-a, i3-b
-- expected columns: Payer Category, total_claim_cost, total_payer_coverage, claim_cost_not_covered_by_payer, coverage_rate
SELECT
  `Payer Category`,
  ROUND(MEASURE(`Total Claim Cost`), 2) AS total_claim_cost,
  ROUND(MEASURE(`Total Payer Coverage`), 2) AS total_payer_coverage,
  ROUND(MEASURE(`Claim Cost Not Covered by Payer`), 2) AS claim_cost_not_covered_by_payer,
  ROUND(MEASURE(`Coverage Rate`), 4) AS coverage_rate
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2025
GROUP BY `Payer Category`
ORDER BY `Payer Category`;

-- intent: i4_average_claim_cost_by_class_2025
-- title: 2025 average simulated claim cost by class
-- benchmark questions: i4-a, i4-b
-- expected columns: Class, average_claim_cost
SELECT
  `Class`,
  ROUND(MEASURE(`Average Claim Cost`), 2) AS average_claim_cost
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2025
GROUP BY `Class`
ORDER BY `Class`;

-- intent: i5_patients_and_encounters_per_patient_by_payer_category_2025
-- title: 2025 unique synthetic patients and encounters per patient by payer category
-- benchmark questions: i5-a, i5-b
-- expected columns: Payer Category, unique_patient_count, encounters_per_patient
SELECT
  `Payer Category`,
  MEASURE(`Unique Patient Count`) AS unique_patient_count,
  ROUND(MEASURE(`Encounters per Patient`), 2) AS encounters_per_patient
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` = 2025
GROUP BY `Payer Category`
ORDER BY `Payer Category`;

-- intent: i6_quarterly_total_claim_cost
-- title: Quarterly simulated total claim cost, 2024-2025
-- benchmark questions: i6-a, i6-b
-- expected columns: Quarter, total_claim_cost
SELECT
  `Quarter`,
  ROUND(MEASURE(`Total Claim Cost`), 2) AS total_claim_cost
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` IN (2024, 2025)
GROUP BY `Quarter`
ORDER BY `Quarter`;

-- intent: i7_quarterly_coverage_rate_by_payer_category
-- title: Quarterly coverage rate by payer category, 2024-2025
-- benchmark questions: i7-a, i7-b
-- expected columns: Quarter, Payer Category, coverage_rate
SELECT
  `Quarter`,
  `Payer Category`,
  ROUND(MEASURE(`Coverage Rate`), 4) AS coverage_rate
FROM {{catalog}}.{{schema}}.encounter_operations_metrics
WHERE `Year` IN (2024, 2025)
GROUP BY `Quarter`, `Payer Category`
ORDER BY `Quarter`, `Payer Category`;
