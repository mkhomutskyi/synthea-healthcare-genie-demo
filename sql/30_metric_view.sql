-- 30_metric_view.sql
-- Unity Catalog Metric View attached to the Genie Agent (the only attached data source).
--
-- Metric view syntax: YAML specification 1.1, CREATE VIEW ... WITH METRICS LANGUAGE YAML.
-- Runtime validation required in your workspace before using the demo results.
-- See docs/QUICKSTART.md for deployment and docs/results.md for the tested scope.
-- Agent metadata (display_name, synonyms, format)
-- requires Databricks Runtime 17.3 or above; SQL warehouses run a current Databricks SQL
-- version. The join uses the documented default many-to-one cardinality, so no
-- `cardinality` or `rely` keys (Runtime 18.1 features) are used. Uniqueness of the join key
-- is enforced by the gate in sql/20_clean.sql.
-- YAML specification 1.1 removes '#' comments on save, so the YAML body carries none.
--
-- Non-additivity: Unique Patient Count, Coverage Rate, Average Claim Cost and
-- Encounters per Patient must be recomputed at each grain, never summed or averaged
-- across payer categories, quarters or classes.

CREATE OR REPLACE VIEW {{catalog}}.{{schema}}.encounter_operations_metrics
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
comment: "Aggregate operational metrics over synthetic Synthea encounters. All dates and monetary values are simulated. Unique Patient Count, Coverage Rate, Average Claim Cost and Encounters per Patient are not additive across groups."
source: {{catalog}}.{{schema}}.clean_encounters

joins:
  - name: payer
    source: {{catalog}}.{{schema}}.payer_categories
    'on': source.payer_id = payer.payer_id

fields:
  - name: Encounter Date
    expr: source.encounter_date
    display_name: Encounter Date
    comment: "Simulated encounter date (UTC calendar date). Simulated relative to 2026-08-18."
    format:
      type: date
      date_format: year_month_day
      leading_zeros: true
    synonyms: ['visit date', 'date of encounter']

  - name: Quarter
    expr: CONCAT(CAST(YEAR(source.encounter_date) AS STRING), '-Q', CAST(QUARTER(source.encounter_date) AS STRING))
    display_name: Quarter
    comment: "Simulated calendar quarter label such as 2024-Q1. Quarters without encounters do not appear."
    synonyms: ['calendar quarter', 'encounter quarter']

  - name: Year
    expr: YEAR(source.encounter_date)
    display_name: Year
    comment: "Simulated calendar year. 2025 and every other year are simulated, not real activity."
    synonyms: ['calendar year', 'encounter year']

  - name: Class
    expr: source.encounter_class
    display_name: Encounter Class
    comment: "Synthea encounter class, for example ambulatory, emergency, inpatient or wellness."
    synonyms: ['encounter class', 'encounter type', 'visit type']

  - name: Payer Category
    expr: payer.payer_category
    display_name: Payer Category
    comment: "Medicare, Medicaid, Dual Eligible, No Insurance or Private Insurance. Small synthetic sample; Dual Eligible values rest on very few synthetic encounters and are not interpretable."
    synonyms: ['coverage category', 'insurance category', 'payer type']

measures:
  - name: Encounter Count
    expr: COUNT(1)
    display_name: Encounter Count
    comment: "Number of synthetic encounters. Additive across groups."
    format:
      type: number
      decimal_places:
        type: exact
        places: 0
    synonyms: ['number of encounters', 'visit count', 'encounters']

  - name: Unique Patient Count
    expr: COUNT(DISTINCT source.patient_id)
    display_name: Unique Patient Count
    comment: "Distinct synthetic patients. Not additive across payer categories, quarters or classes."
    format:
      type: number
      decimal_places:
        type: exact
        places: 0
    synonyms: ['distinct patients', 'unique patients', 'patient count']

  - name: Total Claim Cost
    expr: SUM(source.total_claim_cost)
    display_name: Total Claim Cost (simulated)
    comment: "Sum of simulated total claim cost."
    format:
      type: number
      decimal_places:
        type: exact
        places: 2
    synonyms: ['claim cost', 'total claims', 'claim charges']

  - name: Total Payer Coverage
    expr: SUM(source.payer_coverage)
    display_name: Total Payer Coverage (simulated)
    comment: "Sum of simulated amounts covered by payers."
    format:
      type: number
      decimal_places:
        type: exact
        places: 2
    synonyms: ['payer coverage', 'covered amount', 'amount payers paid']

  - name: Claim Cost Not Covered by Payer
    expr: SUM(source.total_claim_cost) - SUM(source.payer_coverage)
    display_name: Claim Cost Not Covered by Payer (simulated)
    comment: "Total Claim Cost minus Total Payer Coverage, both simulated. A difference of two sums; it does not describe what any person paid or owes."
    format:
      type: number
      decimal_places:
        type: exact
        places: 2
    synonyms: ['claim cost not covered by payer', 'cost not covered by payer']

  - name: Coverage Rate
    expr: SUM(source.payer_coverage) / NULLIF(SUM(source.total_claim_cost), 0)
    display_name: Coverage Rate
    comment: "Total Payer Coverage divided by Total Claim Cost, as a ratio of sums. Not additive; do not average across groups."
    format:
      type: percentage
      decimal_places:
        type: exact
        places: 2
    synonyms: ['payer coverage rate', 'share covered by payer']

  - name: Average Claim Cost
    expr: AVG(source.total_claim_cost)
    display_name: Average Claim Cost (simulated)
    comment: "Average simulated total claim cost per encounter. Not additive across groups."
    format:
      type: number
      decimal_places:
        type: exact
        places: 2
    synonyms: ['mean claim cost', 'average cost per encounter']

  - name: Encounters per Patient
    expr: COUNT(1) / NULLIF(COUNT(DISTINCT source.patient_id), 0)
    display_name: Encounters per Patient
    comment: "Encounter Count divided by Unique Patient Count. Not additive across groups."
    format:
      type: number
      decimal_places:
        type: exact
        places: 2
    synonyms: ['visits per patient', 'encounters per synthetic patient']
$$;
