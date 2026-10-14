# Try the healthcare Genie demo

This guide gets you from the repository to your first Genie question. The answer-checking workflow is optional.

## 1. Prepare your workspace

Use Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and [Databricks CLI 1.20.0](https://github.com/databricks/cli/releases/tag/v1.20.0).

You need:

- A Unity Catalog workspace and the Databricks SQL entitlement.
- An existing catalog with USE CATALOG and permission to create a dedicated schema.
- CREATE TABLE and CREATE VOLUME in that schema, plus SELECT and volume read/write access.
- CAN USE access on an existing pro or serverless SQL warehouse.
- Genie enabled in your workspace.

Sign in with your own workspace URL:

```bash
databricks auth login --host <workspace_url>
```

Note the profile name. [Authentication details](https://docs.databricks.com/aws/en/dev-tools/cli/authentication).

The scripts were tested on Linux. Native Windows is unsupported. Free Edition has not been validated for this project. SQL can auto-start the warehouse and incur charges.

## 2. Get the project and data

```bash
git clone https://github.com/mkhomutskyi/synthea-healthcare-genie-demo.git
cd synthea-healthcare-genie-demo
uv sync --locked
uv run python scripts/verify_contracts.py
uv run python scripts/download_and_verify.py --stage
```

The download is verified against the source fingerprint. Two prepared extracts are written to `.cache/staging/`. If the publisher changes the archive, the script stops rather than using different data silently.

## 3. Deploy

Choose a new dedicated schema, for example `synthea_reader_demo`. Replace the placeholders with your own values:

```bash
uv run python scripts/deploy_agent.py \
  --catalog <catalog> --schema <new_schema> \
  --warehouse-id <warehouse_id> --run-label reader-demo --profile <profile>
```

Review the printed plan. Then repeat the command with `--execute` added. It uploads the two extracts and creates the tables, metric view, and Genie Agent. No compute is created or resized.

The selected catalog, schema, warehouse, and Agent are recorded locally in `.cache/deploy-state.json`. Keep that file until cleanup finishes.

## 4. Ask questions

Open **Genie Agents** in your workspace and find **Healthcare Operations Demo [reader-demo]**. Select **Chat** mode.

Try:

1. How many encounters does the synthetic data contain for each year?
2. Which payer categories appear in this synthetic data?
3. Show simulated claim cost and payer coverage by payer category for 2025.

Read the answer and result table. Select **Show code** to inspect the SQL. Ask a new question with a different year or grouping.

[See the reference result for the third question](sample-answer.md). Results describe this small synthetic sample only.

## 5. Clean up

```bash
uv run python scripts/cleanup.py --run-label reader-demo --profile <profile>
```

Review the plan, then repeat with `--execute`. Cleanup targets the recorded Agent and the dedicated project schema; it does not drop your catalog or warehouse. Check warehouse auto-stop separately.

## Optional next step

Follow [Evaluate answers](evaluation.md) to compute local references or use the configured Benchmarks tab. Neither is required to try the demo.
