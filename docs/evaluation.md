# Evaluate answers

Trying the demo does not require an evaluation run. Use these optional checks when you want to inspect accuracy more closely.

## Compute independent answers

After preparing the data:

```bash
uv run python scripts/run_benchmarks.py --reference
```

Python recomputes the seven expected aggregate results from the verified CSV extracts, then checks them against the committed reference digests. The local output is `.cache/reference/reference-results.json`.

To check the four additional requests:

```bash
uv run python scripts/run_benchmarks.py --reference --payload-holdout
```

## Check canonical SQL

```bash
uv run python scripts/run_benchmarks.py --canonical \
  --catalog <catalog> --schema <schema> \
  --warehouse-id <warehouse_id> --profile <profile>
```

Review the plan, then add `--execute`. The queries are read-only and use the existing warehouse. Compare the SQL results with the local reference:

```bash
uv run python scripts/run_benchmarks.py --compare
```

Canonical SQL checks use exact columns and row order. Genie answer checks permit documented label aliases and reordered rows/columns, but validate column meaning and values. Duplicate or unknown labels fail. Rounding is fixed: integer counts, two decimals for money and per-patient values, and four decimals for Coverage Rate.

## Try the configured benchmarks

The Agent includes 14 questions under **Benchmarks**. Run them in **Chat** mode and review the misses. Native scoring compares generated results with the provided SQL answer. It is a separate evaluator from this project's local checks; do not treat its scores as interchangeable with the historical experiment.

[Databricks testing guidance](https://docs.databricks.com/aws/en/genie-agents/monitor) recommends realistic questions, alternate phrasings, and review of failures. Repeat questions to observe variability. The benchmark section is for evaluation; it is not answer-generation context.

## Inspect boundaries

[boundary-probes.json](../evaluation/boundary-probes.json) contains requests that should receive a refusal, safe redirect, or clarification. Review the answer text and generated SQL. A good table does not prove a good boundary response.

The project reports synthetic aggregate operations metrics. It should not list IDs, provide diagnosis or treatment advice, infer causes, or recommend a payer or facility. Instructions guide behavior; they are not guarantees.

Keep extra test questions separate from examples used to tune the Agent. Record failures as well as successes, and keep results tied to the data and configuration you actually tested.
