-- 00_create_schema.sql
-- Creates the dedicated project schema and the staging volume.
--
-- Runtime validation required: offline tests check placeholders and static rules only.
-- Placeholders are rendered by scripts/render_agent_config.py (or scripts/deploy_agent.py).
-- The catalog must already exist. This project never creates or drops a catalog.
-- Use a schema that is dedicated to this project; scripts/cleanup.py drops it only if the
-- schema comment below still starts with the project marker and this deployment's run label.

CREATE SCHEMA IF NOT EXISTS {{catalog}}.{{schema}}
COMMENT 'synthea-healthcare-genie-demo run-label={{run_label}}: synthetic Synthea sample data only. Dedicated project schema; removed by sql/90_cleanup.sql.';

CREATE VOLUME IF NOT EXISTS {{catalog}}.{{schema}}.{{volume}}
COMMENT 'synthea-healthcare-genie-demo staging: minimized extracts produced locally by scripts/download_and_verify.py --stage. Contains no raw payer names.';
